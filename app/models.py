import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Date, Enum, ForeignKey, Numeric, String, Text, UniqueConstraint, func
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
    phone_number: Mapped[str] = mapped_column(String(64), index=True)  # e.g. "telegram:123456"
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
# Category (extensible spending categories)
# ---------------------------------------------------------------------------

class Category(Base):
    """A spending category used for transaction classification."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    budget: Mapped["Budget | None"] = relationship("Budget", back_populates="category", uselist=False)


# ---------------------------------------------------------------------------
# Budget (1-to-1 with Category)
# ---------------------------------------------------------------------------

class Budget(Base):
    """A monthly spending budget linked to a single category."""

    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), unique=True)
    phone_number: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # monthly limit
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    category: Mapped["Category"] = relationship("Category", back_populates="budget")


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


# ---------------------------------------------------------------------------
# TransactionTip (user-provided tips for categorisation)
# ---------------------------------------------------------------------------

class TransactionTip(Base):
    """A user-provided tip for categorising transactions by pattern."""

    __tablename__ = "transaction_tips"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(String(64), index=True)
    pattern: Mapped[str] = mapped_column(String(255))  # e.g. merchant, keyword
    category: Mapped[str] = mapped_column(String(64))  # must match a valid category
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# AIBUser (stores AIB OAuth details per user)
# ---------------------------------------------------------------------------

class AIBUser(Base):
    """Stores AIB OAuth details for a user (via TrueLayer)."""

    __tablename__ = "aib_users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(String(64), index=True)
    telegram_id: Mapped[str] = mapped_column(String(64), index=True, nullable=True)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    truelayer_user_id: Mapped[str] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# RecurringTransaction (auto-detected recurring payments/subscriptions)
# ---------------------------------------------------------------------------

class RecurringTransaction(Base):
    """Auto-detected recurring transaction pattern (e.g. subscriptions, bills)."""

    __tablename__ = "recurring_transactions"
    __table_args__ = (
        UniqueConstraint("phone_number", "description_pattern", name="uq_recurring_phone_pattern"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(String(64), index=True)
    description_pattern: Mapped[str] = mapped_column(String(255))  # normalized merchant/description
    detected_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # typical amount
    frequency: Mapped[str] = mapped_column(String(32), default="monthly")  # monthly, weekly
    category: Mapped[str] = mapped_column(String(64), default="subscriptions")
    is_active: Mapped[bool] = mapped_column(default=True)
    last_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# BudgetAlert (tracks one-time budget alerts per category per month)
# ---------------------------------------------------------------------------

class BudgetAlert(Base):
    """Tracks budget overage alerts to ensure only one alert per category per month."""

    __tablename__ = "budget_alerts"
    __table_args__ = (
        UniqueConstraint("phone_number", "category", "month", name="uq_budget_alert_phone_cat_month"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(64))
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM format
    alerted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# GmailUser (stores Gmail OAuth credentials per user)
# ---------------------------------------------------------------------------

class GmailUser(Base):
    """Stores Gmail OAuth credentials for a user."""

    __tablename__ = "gmail_users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(String(64), index=True, unique=True)
    email: Mapped[str] = mapped_column(String(255))
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
