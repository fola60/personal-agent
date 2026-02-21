import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import Response
from pydantic import BaseModel, Field
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.agent import run_agent_async

load_dotenv()

# ---------------------------------------------------------------------------
# In-memory session store  (replace with Redis / DB for persistence)
# ---------------------------------------------------------------------------
_sessions: dict[str, list[dict]] = {}


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
    yield


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
async def chat(body: ChatRequest) -> ChatResponse:
    """
    Send a message to the personal agent.

    - If **session_id** is omitted a new conversation is created and its ID
      is returned so you can continue the thread on subsequent calls.
    - The agent uses `claude_agent_sdk` internally, running a full agentic
      loop (with tools) before returning the final reply.
    """
    session_id = body.session_id or str(uuid.uuid4())
    history = _sessions.get(session_id, [])

    try:
        reply, updated_history = await run_agent_async(
            user_message=body.message,
            history=history,
            model=body.model,
            **(({"allowed_tools": body.allowed_tools}) if body.allowed_tools else {}),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _sessions[session_id] = updated_history
    return ChatResponse(reply=reply, session_id=session_id)


@app.delete("/sessions/{session_id}", tags=["agent"])
def delete_session(session_id: str) -> dict:
    """Clear the conversation history for a session."""
    _sessions.pop(session_id, None)
    return {"deleted": session_id}


# ---------------------------------------------------------------------------
# WhatsApp webhook  (Twilio)
# ---------------------------------------------------------------------------

@app.post("/whatsapp", tags=["whatsapp"])
async def whatsapp_webhook(
    request: Request,
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
    session_id = From  # e.g. "whatsapp:+447700900000"
    history = _sessions.get(session_id, [])

    try:
        reply, updated_history = await run_agent_async(
            user_message=Body,
            history=history,
        )
    except Exception as exc:  # noqa: BLE001
        reply = "Sorry, something went wrong. Please try again."
        updated_history = history

    _sessions[session_id] = updated_history

    # Respond with TwiML so Twilio sends the reply back via WhatsApp
    twiml = MessagingResponse()
    twiml.message(reply)
    return Response(content=str(twiml), media_type="text/xml")
