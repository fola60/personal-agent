import os
from contextlib import asynccontextmanager
from typing import Annotated, AsyncGenerator

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.agent import run_agent_async, SYSTEM_PROMPT
from app.crud import delete_session as db_delete_session
from app.crud import get_or_create_session, load_history, save_turn
from app.database import get_session, init_db
from app.scheduler import start_scheduler

load_dotenv()

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
        default="claude-opus-4-5",
        description="Claude model identifier.",
    )
    allowed_tools: list[str] | None = Field(
        default=None,
        description="Override the default tool list. E.g. ['Read', 'Bash', 'WebSearch'].",
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
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Add it to your .env file or docker-compose environment."
        )
    if not os.getenv("TWILIO_AUTH_TOKEN"):
        raise RuntimeError(
            "TWILIO_AUTH_TOKEN environment variable is not set. "
            "Add it to your .env file or docker-compose environment."
        )
    if not os.getenv("TWILIO_ACCOUNT_SID"):
        raise RuntimeError(
            "TWILIO_ACCOUNT_SID environment variable is not set. "
            "Add it to your .env file or docker-compose environment."
        )
    if not os.getenv("TWILIO_FROM_NUMBER"):
        raise RuntimeError(
            "TWILIO_FROM_NUMBER environment variable is not set. "
            "Add it to your .env file or docker-compose environment."
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
    description="A personal AI agent powered by Claude.",
    version="0.1.0",
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
        reply, _ = await run_agent_async(
            user_message=body.message,
            history=history,
            model=body.model,
            **(({"allowed_tools": body.allowed_tools}) if body.allowed_tools else {}),
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

    try:
        reply, _ = await run_agent_async(
            user_message=Body,
            history=history,
            system_prompt=user_system_prompt,
        )
        await save_turn(db, session, Body, reply)
    except Exception:  # noqa: BLE001
        reply = "Sorry, something went wrong. Please try again."

    # Respond with TwiML so Twilio sends the reply back via WhatsApp
    twiml = MessagingResponse()
    twiml.message(reply)
    return Response(content=str(twiml), media_type="text/xml")
