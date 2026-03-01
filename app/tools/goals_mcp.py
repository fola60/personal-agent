#!/usr/bin/env python3
"""
Goals tool handlers — goal management for the personal agent.

Tools:
  set_goal          – create a new goal for a given period
  list_goals        – list goals, optionally filtered by period
  complete_goal     – update a goal's completion status
  delete_goal       – delete a goal by id
  edit_goal         – edit any attributes of a goal
  generate_daily    – list weekly goals so the agent can derive daily tasks
"""
import os

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


# ── Tool execution ───────────────────────────────────────────────────────────

async def call_tool(name: str, arguments: dict) -> str:
    from app.models import CompletionStatus, Goal, GoalPeriod  # imported here to avoid circular issues at spawn time

    phone = arguments.get("phone_number", "")

    async with _Session() as db:
        if name == "goals_set_goal":
            goal = Goal(
                phone_number=phone,
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

        elif name == "goals_list_goals":
            stmt = select(Goal).where(Goal.phone_number == phone).order_by(Goal.period, Goal.created_at)
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

        elif name == "goals_complete_goal":
            goal = (
                await db.execute(select(Goal).where(Goal.id == arguments["id"], Goal.phone_number == phone))
            ).scalar_one_or_none()
            if not goal:
                text = f"✗ No goal with id={arguments['id']}"
            else:
                goal.completed = CompletionStatus(arguments["status"])
                await db.commit()
                text = f"✓ Goal {goal.id} marked '{goal.completed.value}': {goal.name}"

        elif name == "goals_delete_goal":
            goal = (
                await db.execute(select(Goal).where(Goal.id == arguments["id"], Goal.phone_number == phone))
            ).scalar_one_or_none()
            if not goal:
                text = f"✗ No goal with id={arguments['id']}"
            else:
                await db.delete(goal)
                await db.commit()
                text = f"✓ Deleted goal {arguments['id']}: {goal.name}"

        elif name == "goals_edit_goal":
            goal = (
                await db.execute(select(Goal).where(Goal.id == arguments["id"], Goal.phone_number == phone))
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

        elif name == "goals_generate_daily":
            weekly = (
                await db.execute(
                    select(Goal)
                    .where(Goal.phone_number == phone)
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

    return text
