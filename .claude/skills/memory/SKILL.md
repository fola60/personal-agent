---
name: memory-management
description: Two-tiered persistent memory system — core profile (auto-loaded) and vault (on-demand).
---

## Purpose
Store, retrieve and delete persistent memories about the user using a two-tier system:
- **Tier 1 (Core)**: Profile info, personal facts, preferences. Automatically summarised and loaded into every new session.
- **Tier 2 (Vault)**: Things the user explicitly asks you to remember. Only recalled when the user asks for them.

## Instructions
### Tier 1 — Core (auto-loaded)
1. Proactively call `remember` with `tier: 1` when the user shares personal info or preferences — don't ask permission.
2. Categories: `fact` (name, job, location), `preference` (tone, wake time, language), `note` (anything else).
3. Tier 1 memories are loaded at session start via `recall(tier=1)`.

### Tier 2 — Vault (on-demand)
1. Only call `remember` with `tier: 2` when the user explicitly says "remember this" or asks you to store something.
2. Only call `recall(tier=2)` when the user explicitly asks to retrieve stored information.
3. Never auto-load tier 2 memories at session start.

### General
- Use the user's phone number from context.
- Memories persist across session resets (sessions reset daily, memories do not).
- Use `forget` to delete any memory regardless of tier.

## MCP Server
Server name: `memory` — tools are prefixed `mcp__memory__`

## Available Tools
- **remember** — save or update a memory entry (specify tier 1 or 2)
- **recall** — retrieve memories (filter by tier and/or category)
- **forget** — delete a specific memory by key
