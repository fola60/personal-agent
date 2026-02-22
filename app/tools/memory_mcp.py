#!/usr/bin/env python3
"""
Memory MCP server — persistent long-term memory for the agent.

Stores key-value entries organised by category (facts, preferences, notes, etc.)
that survive conversation resets. Each entry is scoped to a phone number.

Tools exposed:
  remember      – save or update a memory entry
  recall        – retrieve all memories for a user (optionally filtered by category)
  forget        – delete a specific memory entry
"""
import asyncio
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
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

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("memory")


# ── Tool manifest ────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="remember",
            description=(
                "Save or update a persistent memory entry. "
                "If a memory with the same key and category already exists for this user, "
                "its value is replaced. Use this proactively when the user shares personal "
                "information, preferences, or anything worth remembering long-term."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "User's WhatsApp number in Twilio format, e.g. 'whatsapp:+353857313407'.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key. E.g. 'name', 'job', 'location', 'wake_up_time', 'favourite_food'.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Memory value. E.g. 'Afolabi', 'Software Engineer', 'Dublin'.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["fact", "preference", "note"],
                        "description": (
                            "Category of memory. "
                            "'fact' for personal info (name, job, location). "
                            "'preference' for likes/dislikes/settings (tone, language, wake time). "
                            "'note' for anything else worth remembering."
                        ),
                        "default": "fact",
                    },
                },
                "required": ["phone_number", "key", "value"],
            },
        ),
        types.Tool(
            name="recall",
            description="Retrieve all stored memories for a user, optionally filtered by category.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "User's WhatsApp number in Twilio format.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["fact", "preference", "note"],
                        "description": "Filter by category. Omit to retrieve all memories.",
                    },
                },
                "required": ["phone_number"],
            },
        ),
        types.Tool(
            name="forget",
            description="Delete a specific memory entry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "User's WhatsApp number in Twilio format.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key to delete.",
                    },
                },
                "required": ["phone_number", "key"],
            },
        ),
    ]


# ── Tool execution ───────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    from app.models import Memory  # imported here to avoid circular issues at spawn time

    async with _Session() as db:
        if name == "remember":
            phone = arguments["phone_number"]
            key = arguments["key"]
            value = arguments["value"]
            category = arguments.get("category", "fact")

            # Upsert: check if key already exists for this user
            result = await db.execute(
                select(Memory).where(
                    Memory.phone_number == phone,
                    Memory.key == key,
                )
            )
            entry = result.scalar_one_or_none()

            if entry:
                old_value = entry.value
                entry.value = value
                entry.category = category
                await db.commit()
                text = f"✓ Updated [{category}] {key} = {value} (was: {old_value})"
            else:
                entry = Memory(
                    phone_number=phone, key=key, value=value, category=category,
                )
                db.add(entry)
                await db.commit()
                text = f"✓ Remembered [{category}] {key} = {value}"

        elif name == "recall":
            phone = arguments["phone_number"]
            stmt = (
                select(Memory)
                .where(Memory.phone_number == phone)
                .order_by(Memory.category, Memory.key)
            )
            if cat := arguments.get("category"):
                stmt = stmt.where(Memory.category == cat)

            entries = (await db.execute(stmt)).scalars().all()

            if not entries:
                text = "No memories stored yet for this user."
            else:
                current_cat = None
                lines = []
                for e in entries:
                    if e.category != current_cat:
                        current_cat = e.category
                        lines.append(f"\n## {current_cat.title()}s")
                    lines.append(f"  • {e.key}: {e.value}")
                text = "User memories:" + "\n".join(lines)

        elif name == "forget":
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
