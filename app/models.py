import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Date, Enum, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class GoalPeriod(str, enum.Enum):
    daily   = "daily"
    weekly  = "weekly"
    monthly = "monthly"
    yearly  = "yearly"


class CompletionStatus(str, enum.Enum):
    no       = "No"
    somewhat = "Somewhat"
    yes      = "Yes"


# ---------------------------------------------------------------------------
# Session / Message
# ---------------------------------------------------------------------------

class Session(Base):
    """Represents a conversation session (one per user / session_id)."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="session",
        order_by="Message.created_at",
        cascade="all, delete-orphan",
    )


class Message(Base):
    """A single message turn within a session."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))   # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped["Session"] = relationship("Session", back_populates="messages")


# ---------------------------------------------------------------------------
# Goal
# ---------------------------------------------------------------------------

class Goal(Base):
    """A personal goal with a time period and completion status."""

    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    period: Mapped[GoalPeriod] = mapped_column(Enum(GoalPeriod, name="goalperiod"))
    completed: Mapped[CompletionStatus] = mapped_column(
        Enum(CompletionStatus, name="completionstatus"),
        default=CompletionStatus.no,
        server_default="no",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# Reminder
# ---------------------------------------------------------------------------

class Reminder(Base):
    """A scheduled reminder that fires either once or on a cron schedule."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)  # pre-generated message to send when firing
    phone_number: Mapped[str] = mapped_column(String(64))  # e.g. "whatsapp:+1234567890"

    # Scheduling — one of these must be set
    cron_expression: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class Memory(Base):
    """A persistent key-value memory entry for a user (facts, preferences, etc.)."""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(64), default="fact")  # e.g. "fact", "preference", "note"
    tier: Mapped[int] = mapped_column(default=1, server_default="1")  # 1=auto-loaded, 2=on-demand
    key: Mapped[str] = mapped_column(String(255))       # e.g. "name", "job", "location"
    value: Mapped[str] = mapped_column(Text)             # e.g. "Afolabi", "Software Engineer"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------

class Transaction(Base):
    """A financial transaction — imported from CSV or logged manually."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[datetime] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # negative = expense, positive = income
    balance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    category: Mapped[str] = mapped_column(String(64), default="other")
    transaction_type: Mapped[str] = mapped_column(String(16))  # "debit" | "credit"
    source: Mapped[str] = mapped_column(String(32), default="csv")  # "csv" | "manual"
    raw_description: Mapped[str] = mapped_column(Text, default="")  # original bank description
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)  # dedup key
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
