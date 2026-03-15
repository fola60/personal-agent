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
    set_daily_week    – create one daily goal per remaining day of current week
    set_daily_month   – create one daily goal per remaining day of current month
"""
import os
from calendar import monthrange
from datetime import date, timedelta

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


def _build_goal_name(base_name: str, target_day: date) -> str:
    return f"{base_name} ({target_day.isoformat()})"


def _build_goal_description(base_description: str, target_day: date) -> str:
    day_text = f"Target day: {target_day.isoformat()}"
    if base_description:
        return f"{base_description}\n{day_text}"
    return day_text


def _remaining_days_of_week(today: date) -> list[date]:
    days_until_sunday = 6 - today.weekday()
    return [today + timedelta(days=offset) for offset in range(days_until_sunday + 1)]


def _remaining_days_of_month(today: date) -> list[date]:
    last_day = monthrange(today.year, today.month)[1]
    return [date(today.year, today.month, day) for day in range(today.day, last_day + 1)]


async def _create_daily_goals_for_days(
    *,
    db: AsyncSession,
    phone_number: str,
    name: str,
    description: str,
    days: list[date],
) -> tuple[list["Goal"], list[str]]:
    from app.models import CompletionStatus, Goal, GoalPeriod

    target_names = [_build_goal_name(name, day) for day in days]
    existing = (
        await db.execute(
            select(Goal)
            .where(Goal.phone_number == phone_number)
            .where(Goal.period == GoalPeriod.daily)
            .where(Goal.name.in_(target_names))
        )
    ).scalars().all()
    existing_names = {goal.name for goal in existing}

    created_goals: list[Goal] = []
    skipped_names: list[str] = []
    for day in days:
        goal_name = _build_goal_name(name, day)
        if goal_name in existing_names:
            skipped_names.append(goal_name)
            continue

        goal = Goal(
            phone_number=phone_number,
            name=goal_name,
            description=_build_goal_description(description, day),
            period=GoalPeriod.daily,
            completed=CompletionStatus.no,
        )
        db.add(goal)
        created_goals.append(goal)

    if created_goals:
        await db.commit()
        for goal in created_goals:
            await db.refresh(goal)

    return created_goals, skipped_names


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

        elif name == "goals_set_daily_goal_for_week":
            base_name = arguments["name"]
            base_description = arguments.get("description", "")
            days = _remaining_days_of_week(date.today())
            created_goals, skipped_names = await _create_daily_goals_for_days(
                db=db,
                phone_number=phone,
                name=base_name,
                description=base_description,
                days=days,
            )

            if not created_goals:
                text = (
                    "No new daily goals created for this week; matching goals already exist for all remaining days."
                )
            else:
                rows = [f"[id={goal.id}] {goal.name}" for goal in created_goals]
                text = (
                    f"✓ Created {len(created_goals)} daily goals for the remaining days of this week.\n"
                    + "\n".join(rows)
                )
                if skipped_names:
                    text += (
                        f"\n\nSkipped {len(skipped_names)} existing goal(s):\n"
                        + "\n".join(skipped_names)
                    )

        elif name == "goals_set_daily_goal_for_month":
            base_name = arguments["name"]
            base_description = arguments.get("description", "")
            days = _remaining_days_of_month(date.today())
            created_goals, skipped_names = await _create_daily_goals_for_days(
                db=db,
                phone_number=phone,
                name=base_name,
                description=base_description,
                days=days,
            )

            if not created_goals:
                text = (
                    "No new daily goals created for this month; matching goals already exist for all remaining days."
                )
            else:
                rows = [f"[id={goal.id}] {goal.name}" for goal in created_goals]
                text = (
                    f"✓ Created {len(created_goals)} daily goals for the remaining days of this month.\n"
                    + "\n".join(rows)
                )
                if skipped_names:
                    text += (
                        f"\n\nSkipped {len(skipped_names)} existing goal(s):\n"
                        + "\n".join(skipped_names)
                    )
        else:
            text = f"Unknown tool: {name}"

    return text
