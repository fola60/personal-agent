import asyncio
from typing import Any

from claude_code_sdk import query, ClaudeCodeOptions as ClaudeAgentOptions
from claude_code_sdk.types import AssistantMessage, ResultMessage

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a helpful personal assistant. "
    "Be concise and direct."
)

# Tools the agent may call.
# Common built-ins: "Read", "Edit", "Bash", "WebSearch", "TodoRead", "TodoWrite"
DEFAULT_TOOLS: list[str] = ["Read", "Bash", "WebSearch", "Edit"]


# ---------------------------------------------------------------------------
# Core agent function  (mirrors the standalone `main` style)
# ---------------------------------------------------------------------------

async def run_agent_async(
    user_message: str,
    history: list[dict] | None = None,
    allowed_tools: list[str] = DEFAULT_TOOLS,
    system_prompt: str = SYSTEM_PROMPT,
    model: str = "claude-opus-4-5",
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
            permission_mode="acceptEdits",  # auto-approve file edits
            model=model,
            system_prompt=system_prompt,
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
        prompt="Explain agent workflows.",
        options=ClaudeAgentOptions(
            allowed_tools=DEFAULT_TOOLS,
            permission_mode="acceptEdits",
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