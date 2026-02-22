#!/usr/bin/env python3
"""
Goals MCP server — exposes goal management as native MCP tools that
the claude_code_sdk can call directly (no Bash required).

Tools exposed:
  set_goal          – create a new goal for a given period
  list_goals        – list goals, optionally filtered by period
  complete_goal     – update a goal's completion status
  delete_goal       – delete a goal by id
  generate_daily    – list weekly goals so the agent can derive daily tasks
"""
import asyncio
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Standalone DB engine
# ---------------------------------------------------------------------------

_db_url = os.getenv("DATABASE_URL", "")
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

_engine = create_async_engine(_db_url, pool_pre_ping=True)
_Session = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("goals")


# ── Tool manifest ────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="set_goal",
            description="Create a new personal goal.",
            inputSchema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly", "yearly"],
                        "description": "The time period for this goal.",
                    },
                    "name": {"type": "string", "description": "Short goal name."},
                    "description": {
                        "type": "string",
                        "description": "Optional longer description.",
                        "default": "",
                    },
                },
                "required": ["period", "name"],
            },
        ),
        types.Tool(
            name="list_goals",
            description="List personal goals, optionally filtered by period.",
            inputSchema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly", "yearly"],
                        "description": "Filter by period. Omit to list all goals.",
                    }
                },
                "required": [],
            },
        ),
        types.Tool(
            name="complete_goal",
            description="Update the completion status of a goal.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Goal id."},
                    "status": {
                        "type": "string",
                        "enum": ["No", "Somewhat", "Yes"],
                        "description": "New completion status.",
                    },
                },
                "required": ["id", "status"],
            },
        ),
        types.Tool(
            name="delete_goal",
            description="Delete a goal by id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Goal id to delete."}
                },
                "required": ["id"],
            },
        ),
        types.Tool(
            name="edit_goal",
            description="Edit any attributes of an existing goal. Pass only the fields you want to change.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Goal id to edit."},
                    "name": {"type": "string", "description": "New goal name."},
                    "description": {"type": "string", "description": "New description."},
                    "period": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly", "yearly"],
                        "description": "New time period.",
                    },
                    "completed": {
                        "type": "string",
                        "enum": ["No", "Somewhat", "Yes"],
                        "description": "New completion status.",
                    },
                },
                "required": ["id"],
            },
        ),
        types.Tool(
            name="generate_daily",
            description=(
                "List all weekly goals so you can derive concrete daily tasks from them. "
                "Call this only when the user explicitly asks to generate daily goals "
                "from their weekly goals. After calling this, use set_goal with "
                "period='daily' to create each task."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


# ── Tool execution ───────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    from app.models import CompletionStatus, Goal, GoalPeriod  # imported here to avoid circular issues at spawn time

    async with _Session() as db:
        if name == "set_goal":
            goal = Goal(
                name=arguments["name"],
                description=arguments.get("description", ""),
                period=GoalPeriod(arguments["period"]),
                completed=CompletionStatus.no,
            )
            db.add(goal)
            await db.commit()
            await db.refresh(goal)
            text = (
                f"✓ Goal created (id={goal.id})\n"
                f"  Period : {goal.period.value}\n"
                f"  Name   : {goal.name}\n"
                f"  Desc   : {goal.description or '—'}"
            )

        elif name == "list_goals":
            stmt = select(Goal).order_by(Goal.period, Goal.created_at)
            if period := arguments.get("period"):
                stmt = stmt.where(Goal.period == GoalPeriod(period))
            goals = (await db.execute(stmt)).scalars().all()
            if not goals:
                text = "No goals found."
            else:
                rows = []
                for g in goals:
                    rows.append(
                        f"[id={g.id}] ({g.period.value}) {g.name}\n"
                        f"  Description : {g.description or '—'}\n"
                        f"  Completed   : {g.completed.value}\n"
                        f"  Created     : {g.created_at.strftime('%Y-%m-%d')}"
                    )
                text = "\n\n".join(rows)

        elif name == "complete_goal":
            goal = (
                await db.execute(select(Goal).where(Goal.id == arguments["id"]))
            ).scalar_one_or_none()
            if not goal:
                text = f"✗ No goal with id={arguments['id']}"
            else:
                goal.completed = CompletionStatus(arguments["status"])
                await db.commit()
                text = f"✓ Goal {goal.id} marked '{goal.completed.value}': {goal.name}"

        elif name == "delete_goal":
            goal = (
                await db.execute(select(Goal).where(Goal.id == arguments["id"]))
            ).scalar_one_or_none()
            if not goal:
                text = f"✗ No goal with id={arguments['id']}"
            else:
                await db.delete(goal)
                await db.commit()
                text = f"✓ Deleted goal {arguments['id']}: {goal.name}"

        elif name == "edit_goal":
            goal = (
                await db.execute(select(Goal).where(Goal.id == arguments["id"]))
            ).scalar_one_or_none()
            if not goal:
                text = f"✗ No goal with id={arguments['id']}"
            else:
                changes = []
                if "name" in arguments:
                    goal.name = arguments["name"]
                    changes.append(f"  Name        → {goal.name}")
                if "description" in arguments:
                    goal.description = arguments["description"]
                    changes.append(f"  Description → {goal.description}")
                if "period" in arguments:
                    goal.period = GoalPeriod(arguments["period"])
                    changes.append(f"  Period      → {goal.period.value}")
                if "completed" in arguments:
                    goal.completed = CompletionStatus(arguments["completed"])
                    changes.append(f"  Completed   → {goal.completed.value}")
                if not changes:
                    text = "No fields provided to update."
                else:
                    await db.commit()
                    text = f"✓ Goal {goal.id} updated:\n" + "\n".join(changes)

        elif name == "generate_daily":
            weekly = (
                await db.execute(
                    select(Goal)
                    .where(Goal.period == GoalPeriod.weekly)
                    .order_by(Goal.created_at)
                )
            ).scalars().all()
            if not weekly:
                text = "No weekly goals found. Add weekly goals first."
            else:
                rows = []
                for g in weekly:
                    rows.append(
                        f"[id={g.id}] {g.name}\n"
                        f"  Description : {g.description or '—'}\n"
                        f"  Completed   : {g.completed.value}"
                    )
                text = (
                    "=== Weekly goals ===\n\n"
                    + "\n\n".join(rows)
                    + "\n\nFor each weekly goal above, break it into concrete daily tasks "
                    "and call set_goal with period='daily'."
                )
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
