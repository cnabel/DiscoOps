# DiscoOps - Discord Red Bot Cog

Operational commands for Discord server management.

- Plain Markdown messages (no embeds)
- Auto-paginated output under Discord's 2,000 character limit

## Installation

1. Add the repository:
```text
[p]repo add DiscoOps https://github.com/cnabel/DiscoOps
```

2. Install the cog:
```text
[p]cog install DiscoOps discoops
```

3. Load the cog:
```text
[p]load discoops
```

## QuickStart

Replace `[p]` with your bot's prefix.

Base command: `[p]do` (alias: `[p]discoops`).

Members:
```text
[p]do members new 7 days
[p]do members role @Moderator
```

Scheduled Events:
```text
[p]do event list
[p]do event "Game Night"
```

Optional: event attendee roles
```text
[p]do event role create "Game Night"
[p]do event role sync "Game Night"
[p]do event role delete "Game Night"
```

Ping the event role (as implemented: must be a trailing literal ` --ping` suffix):
```text
[p]do event role sync "Game Night" --ping
```

Detailed event wizard:
```text
[p]do event create
```

This opens an interactive wizard to create rich event posts with:
- Calendar event linking (import details from Discord Scheduled Events)
- Custom participation roles (Tank, Healer, DPS, etc.) with capacity limits
- Live preview that updates as you configure
- Optional calendar sync (edits flow back to the scheduled event)
- Automatic discussion thread creation

## Permissions

User (running commands):
- Manage Server (Manage Guild). All DiscoOps commands require this and are guild-only.

Bot:
- Server Members Intent: required for member join listings
- Manage Roles: only required for event role commands

## Full Reference

See `docs/commands.md`.
