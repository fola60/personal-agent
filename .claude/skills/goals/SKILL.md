---
name: goals-management
description: CRUD tools for managing daily, weekly, monthly, and yearly personal goals with completion tracking.
---

## Purpose
Add, delete, edit and mark goals as completed. 

## Instructions
When completing a goal is referenced or you are told to mark a goal as completed:
1. Call the list_goals tool to identify the id of the goal being referenced
2. Call the complete_goal tool, with the id and a status.

## MCP Server
Server name: `goals` — tools are prefixed `mcp__goals__`

## Available Tools
- set_goal
- list_goals
- comlete_goal
- delete_goal
- edit_goal
- generate_daily

