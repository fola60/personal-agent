#!/usr/bin/env python3
"""
Reminders tool handlers — reminder management for the personal agent.

Tools:
  set_reminder      – create a new one-off or recurring reminder
    set_daily_schedule – create multiple one-off reminders for a day's tasks
  list_reminders    – list reminders, optionally filtered by phone number
  edit_reminder     – update any fields on an existing reminder
  delete_reminder   – delete a reminder by id
"""
import os
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Standalone DB engine  (same pattern as goals_mcp.py)
# ---------------------------------------------------------------------------

_db_url = os.getenv("DATABASE_URL", "")
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_engine = create_async_engine(_db_url, pool_pre_ping=True)
_Session = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)


def _parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_time_hhmm(value: str) -> time:
    txt = value.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(txt, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid time '{value}'. Use HH:MM (24-hour).")


def _minutes_since_midnight(val: time) -> int:
    return val.hour * 60 + val.minute


def _generate_schedule_message(task_title: str, local_when: datetime) -> str:
    return (
        f"⏰ {task_title}\n"
        f"It's time for this task now ({local_when.strftime('%H:%M')})."
    )


def _plan_missing_task_minutes(
    *,
    missing_count: int,
    used_minutes: set[int],
    start_minutes: int,
    end_minutes: int,
) -> list[int]:
    if missing_count <= 0:
        return []

    if end_minutes <= start_minutes:
        end_minutes = start_minutes + (12 * 60)

    interval = max(30, (end_minutes - start_minutes) // (missing_count + 1))
    planned: list[int] = []

    for idx in range(missing_count):
        candidate = start_minutes + interval * (idx + 1)
        while candidate in used_minutes and candidate <= end_minutes:
            candidate += 30

        if candidate > end_minutes:
            candidate = start_minutes
            while candidate in used_minutes and candidate <= end_minutes:
                candidate += 30

        if candidate > end_minutes:
            candidate = start_minutes + (idx * 30)
            while candidate in used_minutes:
                candidate += 30

        planned.append(candidate)
        used_minutes.add(candidate)

    return planned


# ── Tool execution ───────────────────────────────────────────────────────────

async def call_tool(name: str, arguments: dict) -> str:
    from app.models import Reminder  # imported here to avoid circular issues at spawn time

    async with _Session() as db:
        if name == "reminders_set_reminder":
            cron_expr = arguments.get("cron_expression")
            run_at_str = arguments.get("run_at")
            tz = arguments.get("timezone", "UTC")

            if not cron_expr and not run_at_str:
                return "✗ You must provide either 'cron_expression' (recurring) or 'run_at' (one-off)."

            run_at_dt = None
            if run_at_str:
                try:
                    run_at_dt = datetime.fromisoformat(run_at_str)
                    if run_at_dt.tzinfo is None:
                        run_at_dt = run_at_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    return f"✗ Invalid datetime format: {run_at_str}. Use ISO 8601."

            # Validate cron expression
            if cron_expr:
                try:
                    from croniter import croniter
                    croniter(cron_expr)
                except (ValueError, KeyError) as e:
                    return f"✗ Invalid cron expression '{cron_expr}': {e}"

            is_recurring = bool(cron_expr)

            reminder = Reminder(
                title=arguments["title"],
                message=arguments["message"],
                phone_number=arguments["phone_number"],
                cron_expression=cron_expr,
                run_at=run_at_dt,
                is_recurring=is_recurring,
                timezone=tz,
                enabled=True,
            )
            db.add(reminder)
            await db.commit()
            await db.refresh(reminder)

            schedule_info = (
                f"Cron: {cron_expr} ({tz})" if is_recurring
                else f"One-off at: {run_at_dt}"
            )
            text = (
                f"✓ Reminder created (id={reminder.id})\n"
                f"  Title    : {reminder.title}\n"
                f"  Message  : {reminder.message}\n"
                f"  Phone    : {reminder.phone_number}\n"
                f"  Schedule : {schedule_info}\n"
                f"  Recurring: {'Yes' if is_recurring else 'No'}"
            )

        elif name == "reminders_set_daily_schedule":
            phone_number = arguments["phone_number"]
            timezone_name = arguments.get("timezone", "UTC")
            tasks = arguments.get("tasks", [])

            if not tasks:
                return "✗ No tasks provided. Add at least one task to build today's schedule."

            try:
                tz = ZoneInfo(timezone_name)
            except Exception:
                return f"✗ Invalid timezone '{timezone_name}'. Use an IANA timezone like 'Europe/Dublin'."

            schedule_date_str = arguments.get("schedule_date")
            if schedule_date_str:
                try:
                    schedule_day = _parse_iso_date(schedule_date_str)
                except ValueError:
                    return f"✗ Invalid schedule_date '{schedule_date_str}'. Use YYYY-MM-DD."
            else:
                schedule_day = datetime.now(tz).date()

            day_start_str = arguments.get("day_start", "09:00")
            day_end_str = arguments.get("day_end", "21:00")
            try:
                day_start_time = _parse_time_hhmm(day_start_str)
                day_end_time = _parse_time_hhmm(day_end_str)
            except ValueError as e:
                return f"✗ {e}"

            start_minutes = _minutes_since_midnight(day_start_time)
            end_minutes = _minutes_since_midnight(day_end_time)

            used_minutes: set[int] = set()
            prepared_tasks: list[dict] = []
            missing_indexes: list[int] = []

            for idx, task in enumerate(tasks):
                task_title = str(task.get("title", "")).strip()
                if not task_title:
                    return f"✗ Task at index {idx} is missing 'title'."

                raw_time = task.get("time")
                if raw_time:
                    try:
                        parsed_time = _parse_time_hhmm(str(raw_time))
                    except ValueError as e:
                        return f"✗ Task '{task_title}' has invalid time. {e}"
                    minute_of_day = _minutes_since_midnight(parsed_time)
                    used_minutes.add(minute_of_day)
                else:
                    minute_of_day = None
                    missing_indexes.append(idx)

                prepared_tasks.append(
                    {
                        "title": task_title,
                        "description": str(task.get("description", "")).strip(),
                        "minute_of_day": minute_of_day,
                    }
                )

            planned_minutes = _plan_missing_task_minutes(
                missing_count=len(missing_indexes),
                used_minutes=used_minutes,
                start_minutes=start_minutes,
                end_minutes=end_minutes,
            )

            for idx, minute_of_day in zip(missing_indexes, planned_minutes):
                prepared_tasks[idx]["minute_of_day"] = minute_of_day

            created_reminders: list[Reminder] = []
            prepared_tasks.sort(key=lambda item: int(item["minute_of_day"]))

            for task in prepared_tasks:
                minute_of_day = int(task["minute_of_day"])
                task_time = time(hour=minute_of_day // 60, minute=minute_of_day % 60)
                local_run_at = datetime.combine(schedule_day, task_time, tzinfo=tz)

                title = task["title"]
                if task["description"]:
                    title = f"{title} — {task['description']}"

                reminder = Reminder(
                    title=title,
                    message=_generate_schedule_message(task["title"], local_run_at),
                    phone_number=phone_number,
                    cron_expression=None,
                    run_at=local_run_at,
                    is_recurring=False,
                    timezone=timezone_name,
                    enabled=True,
                )
                db.add(reminder)
                created_reminders.append(reminder)

            await db.commit()
            for reminder in created_reminders:
                await db.refresh(reminder)

            rows = []
            for reminder in created_reminders:
                local_display = reminder.run_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
                rows.append(
                    f"[id={reminder.id}] {local_display} — {reminder.title}\n"
                    f"  Message: {reminder.message}"
                )

            text = (
                f"✓ Created {len(created_reminders)} reminders for {schedule_day.isoformat()} ({timezone_name}).\n"
                + "\n\n".join(rows)
            )

        elif name == "reminders_list_reminders":
            stmt = select(Reminder).order_by(Reminder.created_at)
            if phone := arguments.get("phone_number"):
                stmt = stmt.where(Reminder.phone_number == phone)
            if arguments.get("enabled_only", True):
                stmt = stmt.where(Reminder.enabled == True)  # noqa: E712
            reminders = (await db.execute(stmt)).scalars().all()

            if not reminders:
                text = "No reminders found."
            else:
                rows = []
                for r in reminders:
                    schedule = (
                        f"Cron: {r.cron_expression} ({r.timezone})"
                        if r.is_recurring
                        else f"One-off at: {r.run_at}"
                    )
                    rows.append(
                        f"[id={r.id}] {r.title}\n"
                        f"  Message   : {r.message}\n"
                        f"  Phone     : {r.phone_number}\n"
                        f"  Schedule  : {schedule}\n"
                        f"  Enabled   : {'Yes' if r.enabled else 'No'}\n"
                        f"  Last run  : {r.last_run_at or 'Never'}\n"
                        f"  Created   : {r.created_at.strftime('%Y-%m-%d %H:%M')}"
                    )
                text = "\n\n".join(rows)

        elif name == "reminders_edit_reminder":
            reminder = (
                await db.execute(select(Reminder).where(Reminder.id == arguments["id"]))
            ).scalar_one_or_none()
            if not reminder:
                text = f"✗ No reminder with id={arguments['id']}"
            else:
                changes = []
                if "title" in arguments:
                    reminder.title = arguments["title"]
                    changes.append(f"  Title          → {reminder.title}")
                if "message" in arguments:
                    reminder.message = arguments["message"]
                    changes.append(f"  Message        → {reminder.message}")
                if "cron_expression" in arguments:
                    cron_val = arguments["cron_expression"]
                    try:
                        from croniter import croniter
                        croniter(cron_val)
                    except (ValueError, KeyError) as e:
                        return f"✗ Invalid cron expression '{cron_val}': {e}"
                    reminder.cron_expression = cron_val
                    reminder.is_recurring = True
                    reminder.run_at = None
                    changes.append(f"  Cron           → {cron_val}")
                if "run_at" in arguments:
                    try:
                        run_at_dt = datetime.fromisoformat(arguments["run_at"])
                        if run_at_dt.tzinfo is None:
                            run_at_dt = run_at_dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        return f"✗ Invalid datetime: {arguments['run_at']}"
                    reminder.run_at = run_at_dt
                    reminder.is_recurring = False
                    reminder.cron_expression = None
                    changes.append(f"  Run at         → {run_at_dt}")
                if "timezone" in arguments:
                    reminder.timezone = arguments["timezone"]
                    changes.append(f"  Timezone       → {reminder.timezone}")
                if "enabled" in arguments:
                    reminder.enabled = arguments["enabled"]
                    changes.append(f"  Enabled        → {'Yes' if reminder.enabled else 'No'}")

                if not changes:
                    text = "No fields provided to update."
                else:
                    await db.commit()
                    text = f"✓ Reminder {reminder.id} updated:\n" + "\n".join(changes)

        elif name == "reminders_delete_reminder":
            reminder = (
                await db.execute(select(Reminder).where(Reminder.id == arguments["id"]))
            ).scalar_one_or_none()
            if not reminder:
                text = f"✗ No reminder with id={arguments['id']}"
            else:
                await db.delete(reminder)
                await db.commit()
                text = f"✓ Deleted reminder {arguments['id']}: {reminder.title}"

        else:
            text = f"Unknown tool: {name}"

    return text
