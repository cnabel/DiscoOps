# AGENTS.md — DiscoOps (Red-DiscordBot Cog)

This repository is a **Red-DiscordBot v3** cog named **DiscoOps**. It provides operational commands for Discord server management, with outputs intentionally formatted as **plain Markdown messages** (not embeds) and **auto-paginated** to stay under Discord’s 2,000 character limit.

## Project Structure

- `discoops/discoops.py` — Main cog implementation (commands, helpers, disk logging).
- `discoops/info.json` — Red cog metadata (schema, permissions, install message, data statement).
- `discoops/__init__.py` — Cog loader/export.
- `README.md` — End-user docs and command usage.

## Key Coding Patterns (Please Follow)

### 1) Plain Markdown output + pagination
- Output is sent via `ctx.send(...)` as plain text.
- Long responses are split using `DiscoOps._send_paginated(...)`.
- Global safe limit is `MAX_MSG = 1900` (intentionally below 2,000).
- Prefer structured Markdown:
  - `#` for top-level “page” header
  - `##` for item sections
  - `>` blockquotes for multi-line fields like descriptions

When adding new commands that may produce multi-line output, use `_send_paginated` rather than sending one large message.

### 2) Scheduled Events matching
- Event selection uses normalized comparison via `_norm_text(...)`.
- Matching behavior: exact match first, then partial match.
- Normalization uses NFKC + trims quotes + `casefold()`.

When adding new event-related commands, reuse `_get_scheduled_events(...)` and `_event_match(...)`.

### 3) Persistent config (Red `Config`)
- Guild-scoped config stores `event_roles` (maps `event_id -> role_id`).
- Global config stores `log_writes` for disk log maintenance.

Avoid storing personal user data unless absolutely necessary; if you add new stored data, update `discoops/info.json` (`end_user_data_statement`).

### 4) Disk logging
- Disk log lives under the cog data directory: `cog_data_path(self) / "discoops.log"`.
- Logging is designed to be non-fatal (I/O errors must not break bot behavior).
- Rotation/retention:
  - size cap: `MAX_LOG_BYTES`
  - time prune: `MAX_LOG_DAYS`
  - periodic cleanup: `CLEANUP_EVERY_WRITES`

If you change logging behavior, keep it:
- safe (never raise into command flow),
- bounded (respect size/time limits),
- low I/O (avoid frequent writes beyond the existing scheme).

### 5) Red loader compatibility
- `setup(bot)` is written to support both async and sync loader patterns.

Preserve this compatibility unless you have a strong reason to remove it.

## Command Style and Permissions

- Root command group: `[p]do` (alias `[p]discoops`).
- Commands are `@commands.guild_only()` and require `@commands.has_permissions(manage_guild=True)` for most operations.
- Owner-only debug/log commands use `@commands.is_owner()`.

If adding commands, keep:
- permission checks consistent with existing behavior,
- error messages user-friendly,
- outputs copy/paste-friendly.

## Quick Dev Notes

- This repo does not currently define a formal Python toolchain (`pyproject.toml`, CI workflows, etc.).
- Development typically happens inside a Red bot environment; direct execution of the cog file is not expected.

If you add linting/tests/CI later, keep changes minimal and aligned with Red cog conventions.
