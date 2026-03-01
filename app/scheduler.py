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


async def _send_telegram(chat_id: int, body: str) -> None:
    """Send a Telegram message via Bot API."""
    import httpx

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set — cannot send Telegram message")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": body})
        resp.raise_for_status()
    logger.info("Sent Telegram message to chat %d (%d chars)", chat_id, len(body))


async def _send_reminder_message(to: str, body: str) -> None:
    """Route outbound reminder to WhatsApp or Telegram based on the session key."""
    if to.startswith("telegram:"):
        chat_id = int(to.removeprefix("telegram:"))
        await _send_telegram(chat_id, body)
    else:
        await _send_whatsapp(to, body)


async def _fire_reminder(reminder: Reminder, db: AsyncSession) -> None:
    """Send the pre-generated reminder message."""
    try:
        await _send_reminder_message(reminder.phone_number, reminder.message)
    except Exception:
        logger.exception("Send failed for reminder %d", reminder.id)
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
# Budget alert checker
# ---------------------------------------------------------------------------

async def check_budget_alerts() -> None:
    """
    Check all users' budgets against current month spending.
    Send one-time alert if spending exceeds budget for a category.
    Runs every 6 hours.
    """
    from decimal import Decimal
    from app.models import Budget, BudgetAlert, Transaction, AIBUser
    
    now = datetime.now(timezone.utc)
    month_str = now.strftime("%Y-%m")
    month_start = now.replace(day=1).date()
    
    try:
        async with _Session() as db:
            # Get all users with budgets
            budget_result = await db.execute(
                select(Budget.phone_number, Budget.category_id, Budget.amount)
            )
            budgets = budget_result.all()
            
            if not budgets:
                return
            
            # Get category names
            from app.models import Category
            cat_result = await db.execute(select(Category))
            categories = {c.id: c.name for c in cat_result.scalars().all()}
            
            # Group budgets by user
            user_budgets: dict[str, list] = {}
            for phone, cat_id, amount in budgets:
                if phone not in user_budgets:
                    user_budgets[phone] = []
                cat_name = categories.get(cat_id, "other")
                user_budgets[phone].append({"category": cat_name, "amount": amount})
            
            # Check each user's spending
            for phone_number, budget_list in user_budgets.items():
                # Get this month's transactions
                txns_result = await db.execute(
                    select(Transaction)
                    .where(Transaction.phone_number == phone_number)
                    .where(Transaction.date >= month_start)
                    .where(Transaction.transaction_type == "debit")
                )
                transactions = txns_result.scalars().all()
                
                # Sum spending per category
                spending: dict[str, Decimal] = {}
                for txn in transactions:
                    cat = txn.category or "other"
                    spending[cat] = spending.get(cat, Decimal(0)) + abs(txn.amount)
                
                # Check each budget
                for budget in budget_list:
                    cat_name = budget["category"]
                    budget_amount = budget["amount"]
                    spent = spending.get(cat_name, Decimal(0))
                    
                    if spent > budget_amount:
                        # Check if alert already sent this month
                        alert_result = await db.execute(
                            select(BudgetAlert)
                            .where(BudgetAlert.phone_number == phone_number)
                            .where(BudgetAlert.category == cat_name)
                            .where(BudgetAlert.month == month_str)
                        )
                        existing_alert = alert_result.scalar_one_or_none()
                        
                        if not existing_alert:
                            # Send alert
                            overspent = spent - budget_amount
                            message = (
                                f"⚠️ Budget Alert: You've exceeded your {cat_name} budget!\n"
                                f"Budget: €{budget_amount:.2f}\n"
                                f"Spent: €{spent:.2f}\n"
                                f"Over by: €{overspent:.2f}"
                            )
                            
                            try:
                                await _send_reminder_message(phone_number, message)
                                logger.info(f"Sent budget alert to {phone_number} for {cat_name}")
                            except Exception as e:
                                logger.error(f"Failed to send budget alert to {phone_number}: {e}")
                                continue
                            
                            # Record that we sent the alert
                            db.add(BudgetAlert(
                                phone_number=phone_number,
                                category=cat_name,
                                month=month_str,
                            ))
                            await db.commit()
    
    except Exception:
        logger.exception("Failed to check budget alerts")


# ---------------------------------------------------------------------------
# Goal scheduler jobs
# ---------------------------------------------------------------------------

DUBLIN_TZ = ZoneInfo("Europe/Dublin")


async def _get_openai_analysis(prompt: str, max_tokens: int = 500) -> str:
    """Generate AI analysis for goal recaps."""
    import httpx
    
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return "(AI analysis unavailable - no API key)"
    
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def send_morning_goals() -> None:
    """
    Send daily goals to all users at 9am Irish time.
    Zero AI tokens - just DB query and formatted message.
    """
    from app.models import Goal, GoalPeriod
    
    try:
        async with _Session() as db:
            # Get all users with daily goals
            result = await db.execute(
                select(Goal)
                .where(Goal.period == GoalPeriod.daily)
                .order_by(Goal.phone_number, Goal.created_at)
            )
            goals = result.scalars().all()
            
            if not goals:
                logger.info("No daily goals to send")
                return
            
            # Group by user
            user_goals: dict[str, list] = {}
            for g in goals:
                if g.phone_number not in user_goals:
                    user_goals[g.phone_number] = []
                user_goals[g.phone_number].append(g)
            
            # Send to each user
            for phone_number, user_goal_list in user_goals.items():
                lines = ["☀️ Good morning! Here are your goals for today:\n"]
                for i, g in enumerate(user_goal_list, 1):
                    status_icon = "⬜" if g.completed.value == "No" else ("🟨" if g.completed.value == "Somewhat" else "✅")
                    lines.append(f"{i}. {status_icon} {g.name}")
                    if g.description:
                        lines.append(f"   └─ {g.description}")
                
                lines.append("\nHave a productive day! 💪")
                message = "\n".join(lines)
                
                try:
                    await _send_reminder_message(phone_number, message)
                    logger.info(f"Sent morning goals to {phone_number}")
                except Exception as e:
                    logger.error(f"Failed to send morning goals to {phone_number}: {e}")
    
    except Exception:
        logger.exception("Failed to send morning goals")


async def send_evening_checkin() -> None:
    """
    Send evening check-in at 10pm Irish time.
    Lists daily goals and asks user to report what they completed.
    User replies naturally and the agent interprets via normal flow.
    """
    from app.models import Goal, GoalPeriod, CompletionStatus
    
    try:
        async with _Session() as db:
            # Get users with daily goals that aren't all completed
            result = await db.execute(
                select(Goal)
                .where(Goal.period == GoalPeriod.daily)
                .where(Goal.completed != CompletionStatus.yes)
                .order_by(Goal.phone_number, Goal.created_at)
            )
            goals = result.scalars().all()
            
            if not goals:
                logger.info("No incomplete daily goals for evening check-in")
                return
            
            # Group by user
            user_goals: dict[str, list] = {}
            for g in goals:
                if g.phone_number not in user_goals:
                    user_goals[g.phone_number] = []
                user_goals[g.phone_number].append(g)
            
            # Send check-in to each user
            for phone_number, user_goal_list in user_goals.items():
                lines = ["🌙 Evening check-in! How did today go?\n", "Your goals for today were:"]
                for i, g in enumerate(user_goal_list, 1):
                    status_icon = "⬜" if g.completed.value == "No" else "🟨"
                    lines.append(f"{i}. {status_icon} [id={g.id}] {g.name}")
                
                lines.append("\nWhich goals did you complete? Just tell me naturally (e.g., 'I finished the first two' or 'completed all except the report').")
                message = "\n".join(lines)
                
                try:
                    await _send_reminder_message(phone_number, message)
                    logger.info(f"Sent evening check-in to {phone_number}")
                except Exception as e:
                    logger.error(f"Failed to send evening check-in to {phone_number}: {e}")
    
    except Exception:
        logger.exception("Failed to send evening check-in")


async def send_weekly_recap() -> None:
    """
    Send weekly goal recap on Sunday at 8pm Irish time.
    Includes AI pattern analysis (~300 tokens).
    """
    from app.models import Goal, GoalPeriod
    
    try:
        async with _Session() as db:
            # Get users with weekly goals
            result = await db.execute(
                select(Goal)
                .where(Goal.period == GoalPeriod.weekly)
                .order_by(Goal.phone_number, Goal.created_at)
            )
            goals = result.scalars().all()
            
            if not goals:
                logger.info("No weekly goals for recap")
                return
            
            # Group by user
            user_goals: dict[str, list] = {}
            for g in goals:
                if g.phone_number not in user_goals:
                    user_goals[g.phone_number] = []
                user_goals[g.phone_number].append(g)
            
            for phone_number, user_goal_list in user_goals.items():
                # Calculate stats
                total = len(user_goal_list)
                completed = sum(1 for g in user_goal_list if g.completed.value == "Yes")
                partial = sum(1 for g in user_goal_list if g.completed.value == "Somewhat")
                rate = (completed + partial * 0.5) / total * 100 if total > 0 else 0
                
                # Build goal list for AI
                goal_summary = "\n".join([
                    f"- {g.name}: {g.completed.value}"
                    for g in user_goal_list
                ])
                
                # Get AI analysis
                ai_prompt = f"""Analyze this week's goal completion and provide brief insights (2-3 sentences max):

Goals this week:
{goal_summary}

Completion rate: {rate:.0f}%

Focus on: patterns you notice, what went well, one actionable suggestion for next week. Be encouraging but honest."""
                
                try:
                    analysis = await _get_openai_analysis(ai_prompt, max_tokens=200)
                except Exception as e:
                    logger.error(f"AI analysis failed: {e}")
                    analysis = "Unable to generate analysis."
                
                # Build message
                lines = [
                    "📊 Weekly Goal Recap\n",
                    f"Completion rate: {rate:.0f}%",
                    f"✅ Completed: {completed}",
                    f"🟨 Partial: {partial}",
                    f"⬜ Not done: {total - completed - partial}",
                    "",
                    "📝 Analysis:",
                    analysis,
                ]
                message = "\n".join(lines)
                
                try:
                    await _send_reminder_message(phone_number, message)
                    logger.info(f"Sent weekly recap to {phone_number}")
                except Exception as e:
                    logger.error(f"Failed to send weekly recap to {phone_number}: {e}")
    
    except Exception:
        logger.exception("Failed to send weekly recap")


async def send_monthly_recap() -> None:
    """
    Send monthly goal recap on the last day of the month at 8pm Irish time.
    Includes deep AI reflection (~600 tokens).
    """
    from app.models import Goal, GoalPeriod
    
    try:
        async with _Session() as db:
            # Get users with monthly goals
            result = await db.execute(
                select(Goal)
                .where(Goal.period == GoalPeriod.monthly)
                .order_by(Goal.phone_number, Goal.created_at)
            )
            goals = result.scalars().all()
            
            if not goals:
                logger.info("No monthly goals for recap")
                return
            
            user_goals: dict[str, list] = {}
            for g in goals:
                if g.phone_number not in user_goals:
                    user_goals[g.phone_number] = []
                user_goals[g.phone_number].append(g)
            
            for phone_number, user_goal_list in user_goals.items():
                total = len(user_goal_list)
                completed = sum(1 for g in user_goal_list if g.completed.value == "Yes")
                partial = sum(1 for g in user_goal_list if g.completed.value == "Somewhat")
                rate = (completed + partial * 0.5) / total * 100 if total > 0 else 0
                
                goal_summary = "\n".join([
                    f"- {g.name}: {g.completed.value} (created: {g.created_at.strftime('%Y-%m-%d')})"
                    for g in user_goal_list
                ])
                
                ai_prompt = f"""Provide a thoughtful monthly reflection on these goals (4-6 sentences):

Monthly goals:
{goal_summary}

Overall completion rate: {rate:.0f}%

Include: what patterns emerged this month, areas of strength, areas needing attention, and 2-3 specific suggestions for improvement next month. Be supportive and constructive."""
                
                try:
                    analysis = await _get_openai_analysis(ai_prompt, max_tokens=400)
                except Exception as e:
                    logger.error(f"AI analysis failed: {e}")
                    analysis = "Unable to generate reflection."
                
                lines = [
                    "📅 Monthly Goal Reflection\n",
                    f"Overall completion: {rate:.0f}%",
                    f"✅ Completed: {completed} | 🟨 Partial: {partial} | ⬜ Not done: {total - completed - partial}",
                    "",
                    "🔍 Deep Reflection:",
                    analysis,
                ]
                message = "\n".join(lines)
                
                try:
                    await _send_reminder_message(phone_number, message)
                    logger.info(f"Sent monthly recap to {phone_number}")
                except Exception as e:
                    logger.error(f"Failed to send monthly recap to {phone_number}: {e}")
    
    except Exception:
        logger.exception("Failed to send monthly recap")


async def send_yearly_recap() -> None:
    """
    Send yearly goal recap on December 31st at 8pm Irish time.
    Comprehensive AI reflection (~800 tokens).
    """
    from app.models import Goal, GoalPeriod
    
    try:
        async with _Session() as db:
            result = await db.execute(
                select(Goal)
                .where(Goal.period == GoalPeriod.yearly)
                .order_by(Goal.phone_number, Goal.created_at)
            )
            goals = result.scalars().all()
            
            if not goals:
                logger.info("No yearly goals for recap")
                return
            
            user_goals: dict[str, list] = {}
            for g in goals:
                if g.phone_number not in user_goals:
                    user_goals[g.phone_number] = []
                user_goals[g.phone_number].append(g)
            
            for phone_number, user_goal_list in user_goals.items():
                total = len(user_goal_list)
                completed = sum(1 for g in user_goal_list if g.completed.value == "Yes")
                partial = sum(1 for g in user_goal_list if g.completed.value == "Somewhat")
                rate = (completed + partial * 0.5) / total * 100 if total > 0 else 0
                
                goal_summary = "\n".join([
                    f"- {g.name}: {g.completed.value}"
                    for g in user_goal_list
                ])
                
                ai_prompt = f"""Provide a comprehensive year-end reflection on these yearly goals (6-8 sentences):

Yearly goals:
{goal_summary}

Completion rate: {rate:.0f}%

Include: major accomplishments, areas of growth, lessons learned, what to carry forward, and vision/suggestions for the coming year. Be celebratory of wins while being honest about gaps."""
                
                try:
                    analysis = await _get_openai_analysis(ai_prompt, max_tokens=600)
                except Exception as e:
                    logger.error(f"AI analysis failed: {e}")
                    analysis = "Unable to generate reflection."
                
                lines = [
                    "🎆 Year in Review\n",
                    f"Yearly goal completion: {rate:.0f}%",
                    f"✅ Achieved: {completed} | 🟨 Partial: {partial} | ⬜ Not completed: {total - completed - partial}",
                    "",
                    "🌟 Year-End Reflection:",
                    analysis,
                    "",
                    "Here's to an amazing new year! 🥳",
                ]
                message = "\n".join(lines)
                
                try:
                    await _send_reminder_message(phone_number, message)
                    logger.info(f"Sent yearly recap to {phone_number}")
                except Exception as e:
                    logger.error(f"Failed to send yearly recap to {phone_number}: {e}")
    
    except Exception:
        logger.exception("Failed to send yearly recap")


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
    scheduler.add_job(
        check_budget_alerts,
        trigger="interval",
        hours=6,  # every 6 hours
        id="check_budget_alerts",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    
    # ── Goal scheduler jobs (Irish time) ─────────────────────────────────
    scheduler.add_job(
        send_morning_goals,
        trigger=CronTrigger(hour=9, minute=0, timezone=DUBLIN_TZ),
        id="morning_goals",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        send_evening_checkin,
        trigger=CronTrigger(hour=22, minute=0, timezone=DUBLIN_TZ),
        id="evening_checkin",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        send_weekly_recap,
        trigger=CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=DUBLIN_TZ),
        id="weekly_recap",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        send_monthly_recap,
        trigger=CronTrigger(day="last", hour=20, minute=0, timezone=DUBLIN_TZ),
        id="monthly_recap",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        send_yearly_recap,
        trigger=CronTrigger(month=12, day=31, hour=20, minute=0, timezone=DUBLIN_TZ),
        id="yearly_recap",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    
    scheduler.start()
    logger.info(
        "Scheduler started: reminders (60s), session reset (midnight UTC), "
        "budget alerts (6h), goals (9am/10pm Dublin), recaps (Sun/month-end/Dec 31)"
    )
    return scheduler