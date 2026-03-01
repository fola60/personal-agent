#!/usr/bin/env python3
"""
Memory tool handlers — two-tiered persistent long-term memory.

Tier 1 (core): Facts, preferences, profile info. Automatically summarised
               and loaded into every session. Saved proactively by the agent.
Tier 2 (vault): Information the user explicitly asks to remember. Only
                retrieved when the user explicitly asks to recall it.

Tools:
  remember      – save or update a memory entry (tier 1 or 2)
  recall        – retrieve memories (tier 1 auto-loaded; tier 2 on-demand)
  forget        – delete a specific memory entry
"""
import os

from sqlalchemy import delete, select
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
    from app.models import Memory  # imported here to avoid circular issues at spawn time

    async with _Session() as db:
        if name == "memory_remember":
            phone = arguments["phone_number"]
            key = arguments["key"]
            value = arguments["value"]
            category = arguments.get("category", "fact")
            tier = arguments.get("tier", 1)

            # Upsert: check if key already exists for this user
            result = await db.execute(
                select(Memory).where(
                    Memory.phone_number == phone,
                    Memory.key == key,
                )
            )
            entry = result.scalar_one_or_none()

            tier_label = "core" if tier == 1 else "vault"
            if entry:
                old_value = entry.value
                entry.value = value
                entry.category = category
                entry.tier = tier
                await db.commit()
                text = f"✓ Updated [{tier_label}/{category}] {key} = {value} (was: {old_value})"
            else:
                entry = Memory(
                    phone_number=phone, key=key, value=value,
                    category=category, tier=tier,
                )
                db.add(entry)
                await db.commit()
                text = f"✓ Remembered [{tier_label}/{category}] {key} = {value}"

        elif name == "memory_recall":
            phone = arguments["phone_number"]
            stmt = (
                select(Memory)
                .where(Memory.phone_number == phone)
                .order_by(Memory.tier, Memory.category, Memory.key)
            )
            if tier := arguments.get("tier"):
                stmt = stmt.where(Memory.tier == tier)
            if cat := arguments.get("category"):
                stmt = stmt.where(Memory.category == cat)

            entries = (await db.execute(stmt)).scalars().all()

            if not entries:
                text = "No memories found."
            else:
                current_tier = None
                current_cat = None
                lines = []
                for e in entries:
                    tier_label = "Core" if e.tier == 1 else "Vault"
                    if e.tier != current_tier:
                        current_tier = e.tier
                        current_cat = None
                        lines.append(f"\n{'='*20} {tier_label} (Tier {e.tier}) {'='*20}")
                    if e.category != current_cat:
                        current_cat = e.category
                        lines.append(f"\n## {current_cat.title()}s")
                    lines.append(f"  • {e.key}: {e.value}")
                text = "User memories:" + "\n".join(lines)

        elif name == "memory_forget":
            phone = arguments["phone_number"]
            key = arguments["key"]
            result = await db.execute(
                delete(Memory).where(
                    Memory.phone_number == phone,
                    Memory.key == key,
                )
            )
            await db.commit()
            if result.rowcount == 0:
                text = f"✗ No memory found with key '{key}'"
            else:
                text = f"✓ Forgot: {key}"

        else:
            text = f"Unknown tool: {name}"

    return text
