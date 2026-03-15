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
        # ── TrueLayer Transaction & Balance Tools ─────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "finance_getall_transactions",
                "description": "Fetch all transactions for a user using TrueLayer. Refreshes token if expired.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_transactions_recent",
                "description": "Fetch transactions from the last 6 hours for a user using TrueLayer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_get_balance",
                "description": "Fetch current account balance for a user using TrueLayer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_get_category",
                "description": "Fetch transactions by category for a user using TrueLayer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                        "category": {"type": "string", "description": "Category name."},
                    },
                    "required": ["phone_number", "category"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_get_merchant",
                "description": "Fetch transactions by merchant or description for a user using TrueLayer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                        "merchant": {"type": "string", "description": "Merchant or description keyword."},
                    },
                    "required": ["phone_number", "merchant"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_getby_daterange",
                "description": "Fetch transactions for a custom date range for a user using TrueLayer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                        "start_date": {"type": "string", "description": "Start date (YYYY-MM-DD)."},
                        "end_date": {"type": "string", "description": "End date (YYYY-MM-DD)."},
                    },
                    "required": ["phone_number", "start_date", "end_date"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_get_scheduledpayments",
                "description": "Fetch upcoming scheduled payments (direct debits, standing orders) for a user using TrueLayer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_get_summary",
                "description": "Fetch monthly or weekly spending summaries for a user using TrueLayer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                        "period": {"type": "string", "enum": ["weekly", "monthly"], "description": "Summary period."},
                    },
                    "required": ["phone_number", "period"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_get_status",
                "description": "Fetch budget status (remaining, overspent, etc.) for a user using TrueLayer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_get_income",
                "description": "Fetch income (credit) transactions for a user using TrueLayer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_list_recurring",
                "description": "List all recurring expenses (subscriptions, bills) the user has set up.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_recurring_status",
                "description": "Check payment status of recurring expenses for the current month. Shows which bills/subscriptions have been paid vs unpaid.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_remove_recurring",
                "description": "Remove a recurring expense that the user no longer wants to track.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                        "pattern": {"type": "string", "description": "The name/pattern of the recurring expense to remove."},
                    },
                    "required": ["phone_number", "pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_add_recurring",
                "description": "Add a recurring expense (subscription, bill, rent, etc.) that the user wants to track. Ask the user for name, amount, and frequency. If category is not provided, ask the user to specify one.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                        "name": {"type": "string", "description": "Name/description of the recurring expense (e.g. 'Netflix', 'Rent', 'Gym membership')."},
                        "amount": {"type": "number", "description": "Expected amount of the recurring expense."},
                        "frequency": {"type": "string", "enum": ["weekly", "monthly"], "description": "How often this expense occurs."},
                        "category": {"type": "string", "description": "Category for this recurring expense. If not inferrable from context, ask the user."},
                    },
                    "required": ["phone_number", "name", "amount", "frequency", "category"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_suggest_recurring",
                "description": "Analyze 90 days of transaction history and suggest potential recurring expenses the user might want to track. Returns recommendations based on repeated transaction patterns.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finance_sync_transactions",
                "description": "Manually sync/import latest transactions from TrueLayer into local database.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
    # ── Email ────────────────────────────────────────────────────────────
        {
            "type": "function",
            "function": {
                "name": "email_list_recent",
                "description": "List recent emails from the user's Gmail inbox. Returns subject, sender, date, and snippet for each email.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                        "max_results": {"type": "integer", "description": "Maximum number of emails to return (default 10, max 20)."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "email_search",
                "description": "Search emails by query. Supports Gmail search syntax (from:, subject:, has:attachment, etc.).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                        "query": {"type": "string", "description": "Search query (e.g. 'from:amazon subject:order', 'is:unread', 'has:attachment')."},
                        "max_results": {"type": "integer", "description": "Maximum number of results (default 10, max 20)."},
                    },
                    "required": ["phone_number", "query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "email_read",
                "description": "Read the full content of a specific email by its ID. Use email_list_recent or email_search first to get the ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                        "message_id": {"type": "string", "description": "The email message ID to read."},
                    },
                    "required": ["phone_number", "message_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "email_unread_count",
                "description": "Get the count of unread emails in the user's Gmail inbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "User's session key."},
                    },
                    "required": ["phone_number"],
                },
            },
        },
    # ── Goals ────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "goals_set_goal",
            "description": "Create a new personal goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context (e.g. 'telegram:123456789').",
                    },
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
                "required": ["phone_number", "period", "name"],
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
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "period": {
                        "type": "string",
                        "enum": ["daily", "weekly", "monthly", "yearly"],
                        "description": "Filter by period. Omit to list all goals.",
                    }
                },
                "required": ["phone_number"],
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
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "id": {"type": "integer", "description": "Goal id."},
                    "status": {
                        "type": "string",
                        "enum": ["No", "Somewhat", "Yes"],
                        "description": "New completion status.",
                    },
                },
                "required": ["phone_number", "id", "status"],
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
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
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
                "required": ["phone_number", "id"],
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
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "id": {"type": "integer", "description": "Goal id to delete."}
                },
                "required": ["phone_number", "id"],
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
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    }
                },
                "required": ["phone_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goals_set_daily_goal_for_week",
            "description": (
                "Create a separate daily goal record for each remaining day in the current week. "
                "Use when the user asks to set a daily goal for this week."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Base daily goal name to apply to each remaining day.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional shared description applied to each generated daily goal.",
                        "default": "",
                    },
                },
                "required": ["phone_number", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goals_set_daily_goal_for_month",
            "description": (
                "Create a separate daily goal record for each remaining day in the current month. "
                "Use when the user asks to set a daily goal for this month."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Base daily goal name to apply to each remaining day.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional shared description applied to each generated daily goal.",
                        "default": "",
                    },
                },
                "required": ["phone_number", "name"],
            },
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
    {
        "type": "function",
        "function": {
            "name": "finance_add_category",
            "description": "Add a new spending category that can be used for transaction classification.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Category name (lowercase, e.g. 'pets', 'charity').",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finance_list_categories",
            "description": "List all available spending categories (default and custom).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finance_remove_category",
            "description": "Remove a custom spending category. Default categories cannot be removed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Category name to remove.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finance_add_tip",
            "description": "Add a tip for categorising transactions: when a description contains a pattern, assign a category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Pattern to match in transaction descriptions (e.g. merchant, keyword).",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category to assign when pattern matches.",
                    },
                },
                "required": ["phone_number", "pattern", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finance_list_tips",
            "description": "List all tips for categorising transactions for a user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                },
                "required": ["phone_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finance_remove_tip",
            "description": "Remove a tip for categorising transactions by pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Pattern to remove.",
                    },
                },
                "required": ["phone_number", "pattern"],
            },
        },
    },
    # ── Budgets ──────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "finance_set_budget",
            "description": "Set or update a monthly spending budget for a category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Category name (must exist — use finance_list_categories to check).",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Monthly budget amount in euros.",
                    },
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                },
                "required": ["category", "amount", "phone_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finance_list_budgets",
            "description": "List all configured monthly budgets.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finance_remove_budget",
            "description": "Remove a monthly budget for a category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Category name whose budget to remove.",
                    },
                },
                "required": ["category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finance_check_budgets",
            "description": (
                "Check current month spending against all budgets. "
                "Shows how much has been spent, the limit, and how much is left or over."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone_number": {
                        "type": "string",
                        "description": "The user's session key from context.",
                    },
                },
                "required": ["phone_number"],
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
        elif prefix == "email":
            from app.tools.email_mcp import call_tool
        else:
            raise ValueError(f"Unknown tool prefix: {prefix}")
        _HANDLERS[prefix] = call_tool
    return _HANDLERS[prefix]


async def dispatch_tool(name: str, arguments: dict) -> str:
    """
    Route an OpenAI tool call to the correct handler.

    Tool names are formatted as '<prefix>_<tool_name>'.
    The call_tool functions in each MCP module accept (name, arguments)
    and return a string result.
    """
    # Split on first underscore to get prefix
    prefix, _, tool_name = name.partition("_")

    if not tool_name:
        return f"Unknown tool: {name}"

    try:
        handler = _get_handler(prefix)
        # Pass the full tool name to the handler
        return await handler(name, arguments)
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return f"Tool error: {exc}"
