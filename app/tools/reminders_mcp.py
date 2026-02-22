#!/usr/bin/env python3
"""
Reminders MCP server — exposes reminder management as native MCP tools
that the claude_code_sdk can call directly.

Tools exposed:
  set_reminder      – create a new one-off or recurring reminder
  list_reminders    – list reminders, optionally filtered by phone number
  edit_reminder     – update any fields on an existing reminder
  delete_reminder   – delete a reminder by id
"""
import asyncio
import os
from datetime import datetime, timezone

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
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

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("reminders")


# ── Tool manifest ────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="set_reminder",
            description=(
                "Create a new reminder. Provide either 'cron_expression' for recurring "
                "reminders (e.g. '0 9 * * *' for daily at 9am) or 'run_at' for a one-off "
                "reminder (ISO 8601 datetime). The 'prompt' is what the agent will be asked "
                "to generate and send when the reminder fires."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short human-readable title for the reminder.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The instruction/question the agent will be given when "
                            "the reminder fires. E.g. 'Give me a motivational message "
                            "and remind me to review my weekly goals.'"
                        ),
                    },
                    "phone_number": {
                        "type": "string",
                        "description": (
                            "WhatsApp number to send the reminder to, in Twilio format "
                            "e.g. 'whatsapp:+1234567890'."
                        ),
                    },
                    "cron_expression": {
                        "type": "string",
                        "description": (
                            "Cron expression for recurring reminders (5 fields: "
                            "minute hour day-of-month month day-of-week). "
                            "E.g. '0 9 * * *' = every day at 9:00 AM. "
                            "Omit for one-off reminders."
                        ),
                    },
                    "run_at": {
                        "type": "string",
                        "description": (
                            "ISO 8601 datetime for one-off reminders. "
                            "E.g. '2026-02-23T14:30:00+00:00'. Omit for recurring."
                        ),
                    },
                    "timezone": {
                        "type": "string",
                        "description": (
                            "IANA timezone for the reminder. Defaults to 'UTC'. "
                            "E.g. 'America/New_York', 'Europe/London'."
                        ),
                        "default": "UTC",
                    },
                },
                "required": ["title", "prompt", "phone_number"],
            },
        ),
        types.Tool(
            name="list_reminders",
            description="List reminders, optionally filtered by phone number.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "Filter to reminders for this phone number.",
                    },
                    "enabled_only": {
                        "type": "boolean",
                        "description": "If true, only show enabled reminders. Default true.",
                        "default": True,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="edit_reminder",
            description="Edit any attributes of an existing reminder. Pass only the fields you want to change.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Reminder id to edit."},
                    "title": {"type": "string", "description": "New title."},
                    "prompt": {"type": "string", "description": "New prompt."},
                    "cron_expression": {
                        "type": "string",
                        "description": "New cron expression (makes it recurring).",
                    },
                    "run_at": {
                        "type": "string",
                        "description": "New one-off datetime (ISO 8601).",
                    },
                    "timezone": {"type": "string", "description": "New timezone."},
                    "enabled": {
                        "type": "boolean",
                        "description": "Enable or disable the reminder.",
                    },
                },
                "required": ["id"],
            },
        ),
        types.Tool(
            name="delete_reminder",
            description="Delete a reminder by id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Reminder id to delete."},
                },
                "required": ["id"],
            },
        ),
    ]


# ── Tool execution ───────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    from app.models import Reminder  # imported here to avoid circular issues at spawn time

    async with _Session() as db:
        if name == "set_reminder":
            cron_expr = arguments.get("cron_expression")
            run_at_str = arguments.get("run_at")
            tz = arguments.get("timezone", "UTC")

            if not cron_expr and not run_at_str:
                return [types.TextContent(
                    type="text",
                    text="✗ You must provide either 'cron_expression' (recurring) or 'run_at' (one-off).",
                )]

            run_at_dt = None
            if run_at_str:
                try:
                    run_at_dt = datetime.fromisoformat(run_at_str)
                    if run_at_dt.tzinfo is None:
                        run_at_dt = run_at_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    return [types.TextContent(
                        type="text",
                        text=f"✗ Invalid datetime format: {run_at_str}. Use ISO 8601.",
                    )]

            # Validate cron expression
            if cron_expr:
                try:
                    from croniter import croniter
                    croniter(cron_expr)
                except (ValueError, KeyError) as e:
                    return [types.TextContent(
                        type="text",
                        text=f"✗ Invalid cron expression '{cron_expr}': {e}",
                    )]

            is_recurring = bool(cron_expr)

            reminder = Reminder(
                title=arguments["title"],
                prompt=arguments["prompt"],
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
                f"  Prompt   : {reminder.prompt}\n"
                f"  Phone    : {reminder.phone_number}\n"
                f"  Schedule : {schedule_info}\n"
                f"  Recurring: {'Yes' if is_recurring else 'No'}"
            )

        elif name == "list_reminders":
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
                        f"  Prompt    : {r.prompt}\n"
                        f"  Phone     : {r.phone_number}\n"
                        f"  Schedule  : {schedule}\n"
                        f"  Enabled   : {'Yes' if r.enabled else 'No'}\n"
                        f"  Last run  : {r.last_run_at or 'Never'}\n"
                        f"  Created   : {r.created_at.strftime('%Y-%m-%d %H:%M')}"
                    )
                text = "\n\n".join(rows)

        elif name == "edit_reminder":
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
                if "prompt" in arguments:
                    reminder.prompt = arguments["prompt"]
                    changes.append(f"  Prompt         → {reminder.prompt}")
                if "cron_expression" in arguments:
                    cron_val = arguments["cron_expression"]
                    try:
                        from croniter import croniter
                        croniter(cron_val)
                    except (ValueError, KeyError) as e:
                        return [types.TextContent(
                            type="text",
                            text=f"✗ Invalid cron expression '{cron_val}': {e}",
                        )]
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
                        return [types.TextContent(
                            type="text",
                            text=f"✗ Invalid datetime: {arguments['run_at']}",
                        )]
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

        elif name == "delete_reminder":
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

    return [types.TextContent(type="text", text=text)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
