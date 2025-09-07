# DiscoOps - Discord Red Bot Cog

A clean, README-style operational toolkit for Discord server management.  
Outputs are plain Markdown messages (not embeds) with automatic pagination under Discord’s 2,000-character limit.

## Features

### Member Management
- List members who joined recently (days/weeks/months)
- Inspect role membership with readable, paginated lists
- Lightweight, copy-friendly formatting

### Event Management (Discord Scheduled Events)
- List all scheduled events (sorted by start time)
- Show a single event summary **plus** interested members
- Create / sync / delete per-event roles (optional workflow)
- Robust name matching (handles quotes/diacritics/partials)

## Installation

1. **Add the repository to your Red Bot:**  
```[p]repo add DiscoOps https://github.com/yourusername/DiscoOps```

2. **Install the cog:**  
```[p]cog install DiscoOps discoops```

3. **Load the cog:**  
```[p]load discoops```

## Commands

Replace [p] with your bot’s prefix.  
Use either [p]do or [p]discoops as the base command.  
For events, **event is primary**; events remains as an alias.

### Member Commands

#### View Recent Members
List members who joined within a time window:
```[p]do members new 7 days```  
```[p]do members new 2 weeks```  
```[p]do members new 1 month```

- Output shows each member with ID and join time as Discord timestamp (<t:...>) and unix epoch.

#### Analyze Roles
Show members with a specific role:  
```[p]do members role @Moderator```  
```[p]do members role @Verified```  
```[p]do members role "Game Night"```

### Event Commands

#### List All Events
Plain-message list, sorted by start time (soonest first):  
```[p]do event list```

- Event titles are headers one level below the main “Scheduled Events” header.
- Start time appears as Discord timestamp and unix epoch.
- Multi-line descriptions stay inside a blockquote for consistent formatting.
- Auto-paginated if needed.

#### Show One Event (Summary + Interested Members)
Combined summary and attendance in one command:  
```[p]do event "Game Night"```  
```[p]do event Rådsmötet```

- Displays: status, start time, interested count, (multi-line) description, location/channel.
- Interested Members N header shows the total; additional pages are labeled “(continued)”.

#### Manage Event Roles
(Optional workflow—no hints are auto-printed in outputs.)  
```[p]do event role create "Game Night"```  
```[p]do event role sync "Game Night"```  
```[p]do event role delete "Game Night"```

### Debug / Logs (Owner Only)  
```[p]do logs [count]```   # default 10; paginated if long  
```[p]do debug```          # basic environment and permission info (plain text)  
```[p]do clearlogs```      # clear in-memory logs

### Help
```[p]do help```  

## Practical Usage Examples

### Weekly New Member Report
```[p]do members new 7 days```  

### Event Management Workflow
1) Create a Scheduled Event in Discord.  
2) Get exact names:
```[p]do event list```  
3) Create a role for an event (optional):  
```[p]do event role create "Friday Game Night"```  
4) Before the event, sync attendees:  
```[p]do event role sync "Friday Game Night"```  
5) After the event, clean up:  
```[p]do event role delete "Friday Game Night"```

### Role Audit  
```[p]do members role @Moderator```  
```[p]do members role @VIP```  
```[p]do members role @Subscriber```

## Behavior & Formatting

- Plain messages, not embeds. Everything is sent as normal messages with headers (#, ##) and blockquotes (>).
- Pagination: All long outputs auto-split below 2,000 characters.
- Timestamps: Times are shown as Discord timestamps (rendered in the viewer’s local timezone) and a unix epoch for copy/paste.
- Event Names: Robust matching (case-insensitive, handles partials, quotes, diacritics). For absolute accuracy, copy the name from [p]do event list.

## Permissions Required

Bot:
- View Channels
- Send Messages
- Manage Roles (only if you use event role commands)
- Server Members Intent (in the bot developer portal) to access join dates/member lists

User (to run DiscoOps commands):
- Manage Server (a.k.a. Manage Guild)

> Note: Since outputs are plain messages, Embed Links permission is not required.

## Tips & Best Practices

1. Exact Names: Use [p]do event list and copy the exact header text for names that include special characters.
2. Large Servers: Outputs paginate cleanly; you can rerun with narrower windows (e.g., 3 days) or filter by role.
3. Time Display: Discord timestamps (<t:...>) auto-render in each user’s local timezone; unix epoch is included for tooling/scripting.
4. Role Workflow: Roles are optional—use only if you need to @mention attendees.

## Troubleshooting

### “No members joined in the last X …”
- Ensure the bot has Server Members Intent enabled and has had time to cache members.

### “Event not found”
Run:  
```[p]do event list```  
Copy the event name exactly (quotes/diacritics are supported). Partial names also work in most cases.

### Role creation/sync/delete problems
- Confirm the bot has Manage Roles and its role is above the roles it needs to assign/remove.

### Commands not working
- Verify the cog is loaded: [p]cog list
- Check the channel’s bot permissions (Send Messages, View Channels)
- Ensure you have Manage Server

## Support

Issues and feature requests: GitHub repository

## License

Provided as-is for use with Red-DiscordBot.
