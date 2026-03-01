"""
Core agent module — OpenAI chat completions with function calling.

Maintains a tool-calling loop: sends messages → checks for tool_calls →
executes tools → feeds results back → repeats until final text reply.
"""
import json
import logging
from datetime import date
from typing import Any

from openai import AsyncOpenAI

from app.tools.registry import TOOLS, dispatch_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI client (uses OPENAI_API_KEY env var automatically)
# ---------------------------------------------------------------------------

client = AsyncOpenAI()

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = """\
You are a helpful personal assistant. Be concise and direct.
Today's date is: {today}

You have tools for managing goals, reminders, and long-term memory.

Key behaviours:
- Proactively save personal facts the user mentions (name, job, location, interests, etc.) as tier 1 memory.
- When the user explicitly says "remember this" or asks you to store something specific, save it as tier 2 memory.
- Only recall tier 2 memories when the user explicitly asks (e.g. "what did I ask you to remember?").
- Tier 1 memories are automatically loaded into every conversation — do NOT call memory_recall for tier 1.
- When creating reminders, ALWAYS use the user's session key (phone_number) from context — never ask the user for it.
- The session key looks like 'whatsapp:+1234567890' or 'telegram:123456789' depending on the channel.
- When setting a reminder, generate a complete, friendly message that will be sent when it fires (e.g. "⏰ Time to feed your cat!").
- Ask the user's timezone if not known; default to UTC.
- Always confirm ids and details after creating or updating goals/reminders.
"""

# Limit conversation history to keep token usage low
MAX_HISTORY_TURNS = 20


# ---------------------------------------------------------------------------
# Core agent function
# ---------------------------------------------------------------------------

async def run_agent_async(
    user_message: str,
    history: list[dict] | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    model: str = DEFAULT_MODEL,
    **_: Any,
) -> tuple[str, list[dict], dict]:
    """
    Send a message to the agent and return (reply, updated_history, usage).

    Resolves the {today} placeholder in the system prompt at call time.
    usage dict has keys: input_tokens, output_tokens.
    """
    system_prompt = system_prompt.format(today=date.today().isoformat())
    history = history or []

    # Build message list for OpenAI
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    # Append trimmed history (last N messages)
    if history:
        messages.extend(history[-MAX_HISTORY_TURNS:])

    messages.append({"role": "user", "content": user_message})

    total_input = 0
    total_output = 0

    # Agentic tool-calling loop
    max_iterations = 15  # safety limit
    for _ in range(max_iterations):
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        # Accumulate token usage
        if response.usage:
            total_input += response.usage.prompt_tokens
            total_output += response.usage.completion_tokens

        choice = response.choices[0]
        assistant_msg = choice.message

        # Append assistant message to conversation
        messages.append(assistant_msg.model_dump(exclude_none=True))

        # If no tool calls, we have the final reply
        if not assistant_msg.tool_calls:
            break

        # Execute each tool call and feed results back
        for tool_call in assistant_msg.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            logger.info("Tool call: %s(%s)", fn_name, fn_args)
            result = await dispatch_tool(fn_name, fn_args)
            logger.info("Tool call result: %s", result)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    reply = assistant_msg.content or ""
    usage = {"input_tokens": total_input, "output_tokens": total_output}

    updated_history = history + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": reply},
    ]
    return reply, updated_history, usage