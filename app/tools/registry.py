"""
Tool registry — OpenAI function-calling definitions and dispatcher.

Converts the existing MCP tool logic into OpenAI-compatible function schemas
and routes tool calls to the underlying handlers (which still use their own
standalone DB sessions).
"""
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI function-calling tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    # ── Goals ────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "goals_set_goal",
            "description": "Create a new personal goal.",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goals_list_goals",
            "description": "List personal goals, optionally filtered by period.",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goals_complete_goal",
            "description": "Update the completion status of a goal.",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goals_edit_goal",
            "description": "Edit any attributes of an existing goal. Pass only the fields you want to change.",
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goals_delete_goal",
            "description": "Delete a goal by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Goal id to delete."}
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goals_generate_daily",
            "description": (
                "List all weekly goals so you can derive concrete daily tasks from them. "
                "Call this only when the user explicitly asks to generate daily goals "
                "from their weekly goals. After calling this, use goals_set_goal with "
                "period='daily' to create each task."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ── Reminders ────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "reminders_set_reminder",
            "description": (
                "Create a new reminder. Provide either 'cron_expression' for recurring "
                "reminders (e.g. '0 9 * * *' for daily at 9am) or 'run_at' for a one-off "
                "reminder (ISO 8601 datetime). Generate a complete, friendly message that "
                "will be sent to the user when the reminder fires."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short human-readable title for the reminder.",
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The complete message to send when the reminder fires. "
                            "Make it friendly and specific (e.g. '⏰ Time to feed your cat!')."
                        ),
                    },
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key. Use the value from context (e.g. 'whatsapp:+1234567890' or 'telegram:123456789').",
                    },
                    "cron_expression": {
                        "type": "string",
                        "description": (
                            "Cron expression for recurring reminders (5 fields). "
                            "E.g. '0 9 * * *' = every day at 9:00 AM. Omit for one-off."
                        ),
                    },
                    "run_at": {
                        "type": "string",
                        "description": "ISO 8601 datetime for one-off reminders. Omit for recurring.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone. Defaults to 'UTC'.",
                        "default": "UTC",
                    },
                },
                "required": ["title", "message", "phone_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminders_list_reminders",
            "description": "List reminders, optionally filtered by session key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "enabled_only": {
                        "type": "boolean",
                        "description": "If true, only show enabled reminders. Default true.",
                        "default": True,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminders_edit_reminder",
            "description": "Edit any attributes of an existing reminder. Pass only the fields you want to change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Reminder id to edit."},
                    "title": {"type": "string", "description": "New title."},
                    "message": {"type": "string", "description": "New message to send when it fires."},
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reminders_delete_reminder",
            "description": "Delete a reminder by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Reminder id to delete."},
                },
                "required": ["id"],
            },
        },
    },
    # ── Memory ───────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "memory_remember",
            "description": (
                "Save or update a persistent memory entry. "
                "Tier 1 (core): auto-loaded into every session — use for personal facts "
                "and preferences the agent should always know. "
                "Tier 2 (vault): only recalled when explicitly asked — use when the user "
                "says 'remember this' or asks you to store something specific."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key. E.g. 'name', 'job', 'location'.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Memory value.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["fact", "preference", "note"],
                        "description": "'fact' for personal info, 'preference' for likes/settings, 'note' for anything else.",
                        "default": "fact",
                    },
                    "tier": {
                        "type": "integer",
                        "enum": [1, 2],
                        "description": "1 = core (auto-loaded), 2 = vault (on-demand). Default: 1.",
                        "default": 1,
                    },
                },
                "required": ["phone_number", "key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": (
                "Retrieve stored memories for a user. "
                "Filter by tier and/or category."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "tier": {
                        "type": "integer",
                        "enum": [1, 2],
                        "description": "1 = core, 2 = vault. Omit for all.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["fact", "preference", "note"],
                        "description": "Filter by category. Omit for all.",
                    },
                },
                "required": ["phone_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_forget",
            "description": "Delete a specific memory entry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key to delete.",
                    },
                },
                "required": ["phone_number", "key"],
            },
        },
    },
    # ── Finance ──────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "finance_import_csv",
            "description": (
                "Import transactions from an AIB bank CSV export. "
                "Pass the raw CSV text content. Transactions are deduplicated "
                "so re-importing the same CSV is safe. Auto-categorises spending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "csv_text": {
                        "type": "string",
                        "description": "The full raw CSV text content from an AIB export.",
                    },
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                },
                "required": ["csv_text", "phone_number"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatcher — routes OpenAI tool calls to existing handlers
# ---------------------------------------------------------------------------

# Prefix → (module path, call_tool function)
_HANDLERS: dict[str, Any] = {}


def _get_handler(prefix: str):
    """Lazy-import the MCP module's call_tool function."""
    if prefix not in _HANDLERS:
        if prefix == "goals":
            from app.tools.goals_mcp import call_tool
        elif prefix == "reminders":
            from app.tools.reminders_mcp import call_tool
        elif prefix == "memory":
            from app.tools.memory_mcp import call_tool
        elif prefix == "finance":
            from app.tools.finance_mcp import call_tool
        else:
            raise ValueError(f"Unknown tool prefix: {prefix}")
        _HANDLERS[prefix] = call_tool
    return _HANDLERS[prefix]


async def dispatch_tool(name: str, arguments: dict) -> str:
    """
    Route an OpenAI tool call to the correct handler.

    Tool names are formatted as '<prefix>_<tool_name>'.
    The call_tool functions in each MCP module accept (tool_name, arguments)
    and return list[TextContent].
    """
    # Split on first underscore to get prefix
    prefix, _, tool_name = name.partition("_")

    if not tool_name:
        return f"Unknown tool: {name}"

    try:
        handler = _get_handler(prefix)
        return await handler(tool_name, arguments)
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return f"Tool error: {exc}"
