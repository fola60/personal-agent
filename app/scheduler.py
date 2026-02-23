"""
Reminder scheduler — polls the database for due reminders, generates agent
messages, and sends them proactively via Twilio WhatsApp.

Uses APScheduler (AsyncIOScheduler) with a single interval job that runs
every 60 seconds.  Cron evaluation is handled by `croniter`.
"""
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from twilio.rest import Client as TwilioClient

from app.models import Reminder

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB engine (standalone — scheduler runs outside request context)
# ---------------------------------------------------------------------------

_db_url = os.getenv("DATABASE_URL", "")
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_engine = create_async_engine(_db_url, pool_pre_ping=True)
_Session = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)

# ---------------------------------------------------------------------------
# Twilio client
# ---------------------------------------------------------------------------

_twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
_twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "")
_twilio_from = os.getenv("TWILIO_FROM_NUMBER", "")

_twilio: TwilioClient | None = None


def _get_twilio() -> TwilioClient:
    global _twilio
    if _twilio is None:
        _twilio = TwilioClient(_twilio_sid, _twilio_token)
    return _twilio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_due(reminder: Reminder, now_utc: datetime) -> bool:
    """Return True if the reminder should fire right now."""
    if not reminder.enabled:
        return False

    if reminder.is_recurring and reminder.cron_expression:
        tz = ZoneInfo(reminder.timezone)
        # Seed croniter from the last run (or creation time)
        base = (reminder.last_run_at or reminder.created_at).astimezone(tz)
        cron = croniter(reminder.cron_expression, base)
        next_fire = cron.get_next(datetime).astimezone(timezone.utc)
        return now_utc >= next_fire

    if not reminder.is_recurring and reminder.run_at:
        # One-off: fire once if run_at has passed and it hasn't fired yet
        return reminder.last_run_at is None and now_utc >= reminder.run_at

    return False


async def _send_whatsapp(to: str, body: str) -> None:
    """Send a WhatsApp message via Twilio REST API."""
    import asyncio

    client = _get_twilio()
    await asyncio.to_thread(
        client.messages.create,
        body=body,
        from_=_twilio_from,
        to=to,
    )
    logger.info("Sent WhatsApp message to %s (%d chars)", to, len(body))


async def _fire_reminder(reminder: Reminder, db: AsyncSession) -> None:
    """Generate an agent message for the reminder and send it."""
    from app.agent import run_agent_async

    try:
        reply, _, _usage = await run_agent_async(
            user_message=reminder.prompt,
            history=[],
        )
    except Exception:
        logger.exception("Agent failed for reminder %d", reminder.id)
        reply = f"⏰ Reminder: {reminder.title}"

    try:
        await _send_whatsapp(reminder.phone_number, reply)
    except Exception:
        logger.exception("Twilio send failed for reminder %d", reminder.id)
        return

    # Update last_run_at; disable one-off reminders after firing
    reminder.last_run_at = datetime.now(timezone.utc)
    if not reminder.is_recurring:
        reminder.enabled = False
    await db.commit()
    logger.info("Fired reminder %d (%s)", reminder.id, reminder.title)


# ---------------------------------------------------------------------------
# Poller job
# ---------------------------------------------------------------------------

async def check_reminders() -> None:
    """Check all enabled reminders and fire any that are due."""
    now = datetime.now(timezone.utc)
    async with _Session() as db:
        result = await db.execute(
            select(Reminder).where(Reminder.enabled == True)  # noqa: E712
        )
        reminders = result.scalars().all()

        for r in reminders:
            if _is_due(r, now):
                await _fire_reminder(r, db)


# ---------------------------------------------------------------------------
# Daily session reset
# ---------------------------------------------------------------------------

async def reset_sessions() -> None:
    """Delete all conversation history. Runs daily at midnight UTC."""
    from app.models import Message, Session as DBSession

    try:
        async with _Session() as db:
            result_msg = await db.execute(delete(Message))
            result_ses = await db.execute(delete(DBSession))
            await db.commit()
            logger.info(
                "Daily session reset: deleted %d messages and %d sessions.",
                result_msg.rowcount,
                result_ses.rowcount,
            )
    except Exception:
        logger.exception("Failed to reset sessions")


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

def start_scheduler() -> AsyncIOScheduler:
    """Create, configure, and start the APScheduler instance."""
    scheduler = AsyncIOScheduler(job_defaults={"misfire_grace_time": 120})
    scheduler.add_job(
        check_reminders,
        trigger="interval",
        seconds=60,
        id="check_reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reset_sessions,
        trigger=CronTrigger(hour=0, minute=0),  # midnight UTC
        id="daily_session_reset",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Scheduler started: reminder poller (60s) + daily session reset (midnight UTC)")
    return scheduler