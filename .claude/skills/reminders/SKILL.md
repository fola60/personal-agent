---
name: reminders-management
description: Schedule one-off or recurring reminders via cron expressions, delivered as agent-generated WhatsApp messages.
---

## Purpose
Create, edit, list and delete scheduled reminders. Supports one-off (run_at) and recurring (cron_expression) reminders delivered via WhatsApp.

## Instructions
When creating a reminder:
1. ALWAYS use the user's phone number from context.
2. Use `cron_expression` for recurring (e.g. `0 9 * * *` = daily at 9am) or `run_at` (ISO 8601) for one-off.
3. The `prompt` field is what the agent will be asked when the reminder fires — make it descriptive.
4. Ask the user's timezone if not already known; default to UTC.
5. Always confirm reminder details (id, schedule, prompt) after creating or updating.

Cron format: minute hour day-of-month month day-of-week (5 fields).

One-off reminders auto-disable after firing.

## MCP Server
Server name: `reminders` — tools are prefixed `mcp__reminders__`

## Available Tools
- set_reminder
- list_reminders
- edit_reminder
- delete_reminder
