#!/usr/bin/env python3
"""
Reminders tool handlers — reminder management for the personal agent.

Tools:
  set_reminder      – create a new one-off or recurring reminder
  list_reminders    – list reminders, optionally filtered by phone number
  edit_reminder     – update any fields on an existing reminder
  delete_reminder   – delete a reminder by id
"""
import os
from datetime import datetime, timezone

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
