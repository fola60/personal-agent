import asyncio
from typing import Any

from claude_code_sdk import query, ClaudeCodeOptions as ClaudeAgentOptions
from claude_code_sdk.types import AssistantMessage, ResultMessage

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a helpful personal assistant. Be concise and direct.

You have access to MCP tools for managing goals, reminders, and user profile.
Refer to your skill files in .claude/skills/ for detailed tool documentation.

Key behaviours:
- Retrieve the user's profile at the start of a conversation to personalise responses.
- Proactively save personal facts the user mentions (name, job, location, interests, etc.).
- When creating reminders, ALWAYS use the user's phone number from context.
- Ask the user's timezone if not known; default to UTC.
- Always confirm ids and details after creating or updating goals/reminders.
"""

# Built-in claude tools
DEFAULT_TOOLS: list[str] = [
    "WebSearch",
    # MCP goal tools (server name 'goals' → prefix mcp__goals__)
    "mcp__goals__set_goal",
    "mcp__goals__list_goals",
    "mcp__goals__complete_goal",
    "mcp__goals__edit_goal",
    "mcp__goals__delete_goal",
    "mcp__goals__generate_daily",
    # MCP reminder tools (server name 'reminders' → prefix mcp__reminders__)
    "mcp__reminders__set_reminder",
    "mcp__reminders__list_reminders",
    "mcp__reminders__edit_reminder",
    "mcp__reminders__delete_reminder",
    # MCP memory tools (server name 'memory' → prefix mcp__memory__)
    "mcp__memory__remember",
    "mcp__memory__recall",
    "mcp__memory__forget",
]

# MCP server config — claude_code_sdk spawns this as a subprocess
MCP_SERVERS: dict[str, dict] = {
    "goals": {
        "command": "python",
        "args": ["-m", "app.tools.goals_mcp"],
        "cwd": "/app",
    },
    "reminders": {
        "command": "python",
        "args": ["-m", "app.tools.reminders_mcp"],
        "cwd": "/app",
    },
    "memory": {
        "command": "python",
        "args": ["-m", "app.tools.memory_mcp"],
        "cwd": "/app",
    },
}


# ---------------------------------------------------------------------------
# Core agent function  (mirrors the standalone `main` style)
# ---------------------------------------------------------------------------

async def run_agent_async(
    user_message: str,
    history: list[dict] | None = None,
    allowed_tools: list[str] = DEFAULT_TOOLS,
    system_prompt: str = SYSTEM_PROMPT,
    model: str = "claude-sonnet-4-20250514",
    **_: Any,
) -> tuple[str, list[dict]]:
    """
    Query the agent for a single turn and return (reply, updated_history).

    Internally this is structured exactly like the standalone `main()` below:
    it opens an async-for loop over `query()`, collects text from
    AssistantMessage blocks, and captures the final ResultMessage.
    """
    history = history or []

    # Build a prompt that includes prior conversation context
    prompt = _build_prompt(history, user_message)

    reply_chunks: list[str] = []
    final_reply: str = ""

    # Agentic loop: streams messages as Claude works
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            allowed_tools=allowed_tools,
            permission_mode="acceptEdits",
            model=model,
            system_prompt=system_prompt,
            mcp_servers=MCP_SERVERS,
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text"):
                    reply_chunks.append(block.text)
        elif isinstance(message, ResultMessage):
            # SDK's consolidated final answer
            if message.result:
                final_reply = str(message.result)

    reply = final_reply or "\n".join(reply_chunks).strip()

    updated_history = history + [
        {"role": "user",      "content": user_message},
        {"role": "assistant", "content": reply},
    ]
    return reply, updated_history


# ---------------------------------------------------------------------------
# Sync convenience wrapper
# ---------------------------------------------------------------------------

def run_agent(
    user_message: str,
    history: list[dict] | None = None,
    **kwargs: Any,
) -> tuple[str, list[dict]]:
    """Synchronous wrapper around :func:`run_agent_async`."""
    return asyncio.run(run_agent_async(user_message, history or [], **kwargs))


# ---------------------------------------------------------------------------
# Standalone demo  (python -m app.agent  or  python app/agent.py)
# ---------------------------------------------------------------------------

async def main() -> None:
    """Interactive demo — runs a single hardcoded prompt and streams output."""
    async for message in query(
        prompt="What goals do I have this week?",
        options=ClaudeAgentOptions(
            allowed_tools=DEFAULT_TOOLS,
            permission_mode="acceptEdits",
            mcp_servers=MCP_SERVERS,
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text"):
                    print(block.text)
                elif hasattr(block, "name"):
                    print(f"Tool: {block.name}")
        elif isinstance(message, ResultMessage):
            print(f"Done: {message.subtype}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_prompt(history: list[dict], new_message: str) -> str:
    """Flatten conversation history into a single prompt string."""
    if not history:
        return new_message

    lines: list[str] = ["<conversation_history>"]
    for turn in history:
        role = turn.get("role", "unknown").upper()
        content = turn.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in content
            )
        lines.append(f"{role}: {content}")
    lines.append("</conversation_history>")
    lines.append(f"\nUser: {new_message}")
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())