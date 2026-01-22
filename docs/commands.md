# DiscoOps - Full Command Reference

Replace `[p]` with your bot's prefix.

Notes:
- All commands are guild-only.
- All commands require the invoking user to have Manage Server (Manage Guild).
- Output is plain Markdown (no embeds) and auto-paginated.
- Mentions are suppressed by default to avoid mass pings.

## Command Overview

Base group:
- `[p]do` (alias: `[p]discoops`)

Help:
- `[p]do help`

Members:
- `[p]do members new <amount> <days|weeks|months>`
- `[p]do members role <role>`

Events (Discord Scheduled Events):
- `[p]do event ...` (alias: `[p]do events ...`)
- `[p]do event list` (aliases: `ls`)
- `[p]do event "Event Name"` (event summary + interested members)
- `[p]do event members "Event Name"` (deprecated; use `[p]do event "Event Name"`)
- `[p]do event create` (detailed event wizard)

Event attendee roles:
- `[p]do event role <create|sync|delete> "Event Name" [--ping]`

Owner-only:
- `[p]do logs [count]`
- `[p]do debug`
- `[p]do clearlogs`

## Members

### List Recent Joins

```text
[p]do members new <amount> <days|weeks|months>
```

Examples:
```text
[p]do members new 7 days
[p]do members new 2 weeks
[p]do members new 1 months
```

Behavior:
- Requires Server Members Intent and a sufficiently cached member list.
- "months" is treated as 30 days per month (amount * 30 days).
- Output includes Discord timestamps (`<t:...>`) and unix epoch.

### List Members With A Role

```text
[p]do members role <role>
```

Examples:
```text
[p]do members role @Moderator
[p]do members role "Game Night"
```

Behavior:
- Lists members in chunks and includes basic role metadata (created date, position, mentionable, color).

## Events (Scheduled Events)

### List Events

```text
[p]do event list
[p]do event ls
```

Behavior:
- Sorted by start time (soonest first).
- Shows status, start time, interested count, shortened description, and location/channel when available.

### Show One Event (Summary + Interested Members)

```text
[p]do event "Event Name"
```

Examples:
```text
[p]do event "Game Night"
[p]do event Rådsmötet
```

Matching behavior:
- Normalizes text (unicode normalization + trims quotes + case-insensitive).
- Tries exact match first, then partial match.
- If multiple events partially match, the first match returned by Discord is used.

Interested members caveat:
- Interested users are fetched from the event, but only users resolvable as cached guild members are listed.
- The displayed interested member count reflects the members found this way.

### Deprecated Legacy Command

```text
[p]do event members "Event Name"
```

Behavior:
- Prints a deprecation note and then shows the same output as `[p]do event "Event Name"`.

## Event Attendee Roles

```text
[p]do event role <create|sync|delete> "Event Name" [--ping]
```

Create:
- Creates a mentionable role named `Event: <event name>` and assigns it to currently interested members.
- Stores the mapping `event_id -> role_id` in guild config.

Sync:
- Adds/removes the role so membership matches the current interested list.

Delete:
- Deletes the role and removes the mapping.

Ping option (documented as implemented):
- `--ping` is not a parsed flag; it is detected only if the event name argument ends with the exact suffix `" --ping"`.

Example:
```text
[p]do event role sync "Game Night" --ping
```

Role hierarchy requirement:
- The bot can only manage roles below its highest role. If role hierarchy prevents management, create will fail with guidance.

## Detailed Event Wizard

```text
[p]do event create
```

Behavior:
- Opens an interactive wizard to create detailed event posts with rich information and custom participation roles.
- Creates a live preview message with interactive buttons (Edit Description, Roles Builder, Options, Publish, Cancel).

Workflow:
1. **Link Calendar Event** — Optionally select or paste a Discord Scheduled Event to import details (title, dates, location, description, image).
2. **Edit Description** — Opens a modal to edit the event description with markdown support.
3. **Roles Builder** — Add custom participation roles (e.g., Tank, Healer, DPS) with optional emoji, capacity limits, and descriptions.
4. **Options** — Configure calendar sync behavior (whether edits should sync back to the linked scheduled event).
5. **Publish** — Creates the final event message with an embed and optional discussion thread.
6. **Cancel** — Removes the draft and preview message.

Features:
- Live preview embed updates as you configure the event.
- Import event details from Discord Scheduled Events.
- Custom role options with emoji, labels, capacity limits, and descriptions.
- Option to sync edits back to the linked calendar event.
- Automatic discussion thread creation on publish.

Notes:
- Only the organizer (user who started the wizard) can interact with the draft controls.
- Drafts are stored in memory and will be lost if the bot restarts.
- The published event uses embeds (unlike other DiscoOps commands which use plain text).

## Logs / Debug (Owner Only)

### View Recent Logs

```text
[p]do logs
[p]do logs 50
```

Behavior:
- Tails the on-disk log file and paginates if needed.
- `count` is clamped to 1..200.

### Debug Info

```text
[p]do debug
```

Behavior:
- Prints basic guild/bot info and key permission booleans.

### Clear Logs

```text
[p]do clearlogs
```

Behavior:
- Deletes the on-disk log file and then immediately writes a fresh log entry.

## Troubleshooting

### "I couldn't access the member list"

- Enable Server Members Intent for the bot and ensure the bot has time to cache members.

### "Event Not Found"

Run:
```text
[p]do event list
```

Then copy the event name exactly (partial matches usually work, but can be ambiguous).

### Event role create/sync/delete problems

- Confirm the bot has Manage Roles and its top role is above the roles it needs to manage.

### Commands not working

- These commands are guild-only and require Manage Server.
- Confirm the cog is loaded:
```text
[p]cog list
```

## Data / Privacy

- Stores only per-guild mappings of scheduled event IDs to role IDs (for the event role feature).
- Does not store personal user data persistently.
