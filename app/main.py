import asyncio
import logging
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Annotated, AsyncGenerator

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from sqlalchemy import select

from app.agent import run_agent_async, SYSTEM_PROMPT, DEFAULT_MODEL
from app.crud import delete_session as db_delete_session
from app.crud import get_or_create_session, load_history, save_turn
from app.database import get_session, init_db, AsyncSessionLocal
from app.models import Memory
from app.scheduler import start_scheduler

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB session dependency shorthand
# ---------------------------------------------------------------------------
DB = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., description="The user's message to the agent.")
    session_id: str | None = Field(
        default=None,
        description="Pass a session_id to continue an existing conversation. "
                    "Omit (or pass null) to start a new one.",
    )
    model: str = Field(
        default="gpt-4o-mini",
        description="OpenAI model identifier.",
    )


class ChatResponse(BaseModel):
    reply: str
    session_id: str


class HealthResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY environment variable is not set. "
            "Add it to your .env file or docker-compose environment."
        )
    if not os.getenv("TWILIO_AUTH_TOKEN"):
        logger.warning(
            "TWILIO_AUTH_TOKEN not set — WhatsApp endpoint will be disabled."
        )
    if not os.getenv("TWILIO_ACCOUNT_SID"):
        logger.warning(
            "TWILIO_ACCOUNT_SID not set — WhatsApp endpoint will be disabled."
        )
    if not os.getenv("TWILIO_FROM_NUMBER"):
        logger.warning(
            "TWILIO_FROM_NUMBER not set — WhatsApp endpoint will be disabled."
        )
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set — Telegram endpoint will not send replies."
        )
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Add it to your .env file or docker-compose environment."
        )
    await init_db()
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Personal Agent",
    description="A personal AI agent powered by OpenAI.",
    version="0.2.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Liveness check."""
    return HealthResponse(status="ok")


@app.post("/chat", response_model=ChatResponse, tags=["agent"])
async def chat(body: ChatRequest, db: DB) -> ChatResponse:
    """
    Send a message to the personal agent.

    - If **session_id** is omitted a new conversation is created and its ID
      is returned so you can continue the thread on subsequent calls.
    - Conversation history is persisted in Postgres.
    """
    session = await get_or_create_session(db, body.session_id)
    history = await load_history(db, session)

    try:
        reply, _, _usage = await run_agent_async(
            user_message=body.message,
            history=history,
            model=body.model,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    await save_turn(db, session, body.message, reply)
    return ChatResponse(reply=reply, session_id=session.id)


@app.delete("/sessions/{session_id}", tags=["agent"])
async def delete_session(session_id: str, db: DB) -> dict:
    """Delete a session and all its message history from the database."""
    deleted = await db_delete_session(db, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": session_id}


# ---------------------------------------------------------------------------
# WhatsApp webhook  (Twilio)
# ---------------------------------------------------------------------------

@app.post("/whatsapp", tags=["whatsapp"])
async def whatsapp_webhook(
    request: Request,
    db: DB,
    Body: str = Form(...),
    From: str = Form(...),
) -> Response:
    """
    Twilio WhatsApp webhook.
    Twilio POSTs form-encoded data here whenever you receive a WhatsApp message.
    Each sender's number is used as their session_id so conversation history
    is maintained per-user.
    """
    # Validate the request is genuinely from Twilio
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    validator = RequestValidator(auth_token)
    form_data = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")

    # Reconstruct the public-facing URL (ngrok forwards as https but
    # internally the request arrives as http — use forwarded headers)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    url = f"{proto}://{host}{request.url.path}"

    if not validator.validate(url, dict(form_data), signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    # Use the sender's WhatsApp number as a stable session key
    session = await get_or_create_session(db, From)  # e.g. "whatsapp:+447700900000"
    history = await load_history(db, session)

    # Inject the user's phone number into the system prompt so the agent
    # can populate phone_number automatically when creating reminders.
    user_system_prompt = (
        SYSTEM_PROMPT
        + f"\n\nThe current user's WhatsApp phone number is: {From}\n"
        "Always use this phone number when creating reminders for this user."
    )

    # Auto-load tier 1 (core) memories into the system prompt
    tier1_result = await db.execute(
        select(Memory)
        .where(Memory.phone_number == From, Memory.tier == 1)
        .order_by(Memory.category, Memory.key)
    )
    tier1_entries = tier1_result.scalars().all()
    if tier1_entries:
        mem_lines = []
        for e in tier1_entries:
            mem_lines.append(f"  - [{e.category}] {e.key}: {e.value}")
        user_system_prompt += (
            "\n\n<user_profile>\n"
            "Known facts about this user (tier 1 core memories):\n"
            + "\n".join(mem_lines)
            + "\n</user_profile>"
        )

    try:
        reply, _, usage = await run_agent_async(
            user_message=Body,
            history=history,
            system_prompt=user_system_prompt,
        )
        await save_turn(db, session, Body, reply)

        # Append token usage summary
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        if in_tok or out_tok:
            reply += f"\n\n_Tokens: {in_tok:,} in / {out_tok:,} out ({in_tok + out_tok:,} total)_"
    except Exception:  # noqa: BLE001
        reply = "Sorry, something went wrong. Please try again."

    # Respond with TwiML so Twilio sends the reply back via WhatsApp
    twiml = MessagingResponse()
    twiml.message(reply)
    return Response(content=str(twiml), media_type="text/xml")


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

_raw_allowed = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
TELEGRAM_ALLOWED_CHAT_IDS: set[str] = {
    cid.strip() for cid in _raw_allowed.split(",") if cid.strip()
}


class _DeduplicateSet:
    """Keeps the last N update_ids to prevent duplicate processing."""

    def __init__(self, maxlen: int = 1000) -> None:
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._maxlen = maxlen

    def add_if_new(self, update_id: int) -> bool:
        """Return True if this is a new update_id, False if already seen."""
        if update_id in self._seen:
            return False
        self._seen[update_id] = None
        if len(self._seen) > self._maxlen:
            self._seen.popitem(last=False)
        return True


_seen_updates = _DeduplicateSet()


async def _send_telegram(chat_id: int, text: str) -> None:
    """Send a message via the Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": text})
        resp.raise_for_status()


async def _download_telegram_file(file_id: str) -> str:
    """Download a file from Telegram and return its text content."""
    async with httpx.AsyncClient(timeout=30) as client:
        # Get file path from Telegram
        resp = await client.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id},
        )
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]

        # Download the file
        dl_resp = await client.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        )
        dl_resp.raise_for_status()
        return dl_resp.text


@app.post("/telegram", tags=["telegram"])
async def telegram_webhook(request: Request) -> dict:
    """
    Telegram Bot API webhook.
    Returns 200 immediately and processes the message in the background
    to avoid Telegram retry-induced duplicate replies.
    """
    update = await request.json()

    message = update.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    update_id = update.get("update_id")

    # Access control — only whitelisted chat IDs may use the bot
    if TELEGRAM_ALLOWED_CHAT_IDS and str(chat_id) not in TELEGRAM_ALLOWED_CHAT_IDS:
        logger.warning("Blocked Telegram message from chat_id=%s", chat_id)
        return {"ok": True}

    # Deduplicate — skip if Telegram retried this update
    if update_id is not None and not _seen_updates.add_if_new(update_id):
        logger.info("Skipping duplicate Telegram update_id=%s", update_id)
        return {"ok": True}

    # Handle document uploads (CSV files)
    document = message.get("document")
    if document:
        file_name = document.get("file_name", "")
        if file_name.lower().endswith(".csv"):
            caption = message.get("caption", "Import this CSV")
            asyncio.create_task(_handle_telegram_csv(chat_id, document["file_id"], caption))
            return {"ok": True}
        else:
            await _send_telegram(chat_id, "I can only process CSV files. Please upload a .csv file.")
            return {"ok": True}

    # Only handle text messages
    text = message.get("text")
    if not text:
        return {"ok": True}

    # /start command — greet and return
    if text.strip().lower() == "/start":
        await _send_telegram(chat_id, "Hey! I'm your personal agent. Send me a message to get started.")
        return {"ok": True}

    # Process in the background so we return 200 immediately
    asyncio.create_task(_handle_telegram_message(chat_id, text))
    return {"ok": True}


async def _handle_telegram_message(chat_id: int, text: str) -> None:
    """Process a Telegram message in the background."""
    session_key = f"telegram:{chat_id}"

    async with AsyncSessionLocal() as db:
        session = await get_or_create_session(db, session_key)
        history = await load_history(db, session)

        user_system_prompt = (
            SYSTEM_PROMPT
            + f"\n\nThe current user's session key (phone_number) is: {session_key}\n"
            "Always use this value for the phone_number parameter when creating reminders."
        )

        # Auto-load tier 1 (core) memories into the system prompt
        tier1_result = await db.execute(
            select(Memory)
            .where(Memory.phone_number == session_key, Memory.tier == 1)
            .order_by(Memory.category, Memory.key)
        )
        tier1_entries = tier1_result.scalars().all()
        if tier1_entries:
            mem_lines = []
            for e in tier1_entries:
                mem_lines.append(f"  - [{e.category}] {e.key}: {e.value}")
            user_system_prompt += (
                "\n\n<user_profile>\n"
                "Known facts about this user (tier 1 core memories):\n"
                + "\n".join(mem_lines)
                + "\n</user_profile>"
            )

        try:
            reply, _, usage = await run_agent_async(
                user_message=text,
                history=history,
                system_prompt=user_system_prompt,
            )
            await save_turn(db, session, text, reply)

            # Append token usage summary
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            if in_tok or out_tok:
                reply += f"\n\nTokens: {in_tok:,} in / {out_tok:,} out ({in_tok + out_tok:,} total)"
        except Exception:
            logger.exception("Error processing Telegram message from chat_id=%s", chat_id)
            reply = "Sorry, something went wrong. Please try again."

    await _send_telegram(chat_id, reply)


async def _handle_telegram_csv(chat_id: int, file_id: str, caption: str) -> None:
    """Download a CSV file from Telegram and import transactions."""
    from app.tools.finance_mcp import call_tool as finance_call_tool

    session_key = f"telegram:{chat_id}"

    try:
        await _send_telegram(chat_id, "📊 Downloading and processing your CSV...")
        csv_text = await _download_telegram_file(file_id)
        result = await finance_call_tool("import_csv", {
            "csv_text": csv_text,
            "phone_number": session_key,
        })
        await _send_telegram(chat_id, result)
    except Exception:
        logger.exception("CSV import failed for chat_id=%s", chat_id)
        await _send_telegram(chat_id, "Sorry, something went wrong importing the CSV. Please try again.")
