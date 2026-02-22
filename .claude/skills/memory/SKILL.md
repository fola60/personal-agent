---
name: memory-management
description: Persistent long-term memory for facts, preferences, and notes about the user that survive conversation resets.
---

## Purpose
Store, retrieve and delete persistent memories about the user. Organised by category: `fact`, `preference`, `note`.

## Instructions
When the user shares personal information or preferences:
1. Proactively call `remember` — don't ask for permission.
2. Use the user's phone number from context.
3. Choose the appropriate category: `fact` for personal info, `preference` for likes/settings, `note` for anything else.
4. At the start of a new conversation, call `recall` to load user context.
5. Memories persist across session resets.

## MCP Server
Server name: `memory` — tools are prefixed `mcp__memory__`

## Available Tools
- remember
- recall
- forget
