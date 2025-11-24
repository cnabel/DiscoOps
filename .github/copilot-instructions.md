# Copilot Instructions for DiscoOps

## Repository Overview

DiscoOps is a Discord Red Bot cog that provides operational features for Discord server management. The cog offers tools to:
- Track recently joined members
- Analyze role membership
- Manage Discord scheduled events
- Create and sync event roles with scheduled events

## Project Structure

```
DiscoOps/
├── discoops/
│   ├── __init__.py       # Cog initialization and setup
│   ├── discoops.py       # Main cog implementation (~700 lines)
│   └── info.json         # Cog metadata
└── README.md             # Documentation
```

## Key Technologies

- **Python 3.8+**: Primary language
- **Red-DiscordBot v3.5.0+**: Bot framework
- **discord.py**: Discord API wrapper (via Red)
- **Red Config API**: For persistent storage

## Code Style and Conventions

### General Guidelines
1. **Output Format**: All user-facing outputs must be plain Markdown messages (not embeds)
2. **Pagination**: Automatically split messages under Discord's 2,000-character limit
3. **Timestamps**: Display as both Discord timestamps (`<t:...>`) and Unix epoch
4. **Error Handling**: Graceful error messages with clear user guidance
5. **Permissions**: Check permissions before operations (Manage Guild for commands, Manage Roles for role operations)

### Python Conventions
- Use type hints for function parameters and return values
- Follow PEP 8 style guidelines
- Use async/await patterns for Discord operations
- Prefer f-strings for string formatting
- Include docstrings for commands and complex functions

### Discord Red Bot Patterns
- Use `@commands.group()` for command grouping
- Use `@commands.guild_only()` decorator for guild-specific commands
- Use `@commands.admin_or_permissions()` for permission checks
- Use `ctx.send()` for sending messages
- Use Red's Config API for persistent data storage
- Handle both sync and async setup() for compatibility

### Command Structure
- Base command: `[p]do` or `[p]discoops`
- Subcommands organized by feature: `members`, `event`, `logs`, `debug`
- Support flexible input (quoted names, partials, diacritics)
- Provide clear help text with examples

## Testing Approach

This repository currently has no automated test infrastructure. When making changes:
1. Manually verify commands work in a Discord test server
2. Test edge cases (empty results, long lists, special characters)
3. Verify pagination works correctly for large outputs
4. Test with different permission levels
5. Ensure error messages are clear and actionable

## Dependencies

- No external dependencies beyond Red-DiscordBot
- Relies on Red's built-in libraries (discord.py, Config API)
- Requires "Server Members Intent" enabled in Discord Developer Portal

## Common Tasks

### Adding a New Command
1. Define command using appropriate decorator (`@discoops.command()` or as subcommand)
2. Add permission check if needed
3. Implement pagination for potentially long outputs
4. Use plain Markdown formatting (headers with `#`, blockquotes with `>`)
5. Update help text in docstrings

### Modifying Output Format
- Keep outputs as plain text messages
- Use consistent header hierarchy (main header #, section headers ##, subsection ###)
- Use Discord timestamp format: `<t:{unix_timestamp}:f>`
- Include Unix epoch for scripting: `(unix: {timestamp})`
- Apply pagination helper for messages >1900 chars

### Working with Events
- Use `guild.scheduled_events` to access events
- Handle event matching (case-insensitive, partial, diacritics)
- Check for event existence and provide clear error messages
- Sort events by start time when listing

### Role Management
- Store event-role mappings in Config: `await self.config.guild(guild).event_roles()`
- Check bot's role hierarchy before role operations
- Verify "Manage Roles" permission
- Provide clear success/error messages

## Permissions Model

**Bot Requirements:**
- View Channels
- Send Messages
- Manage Roles (for event role commands)
- Server Members Intent (in Developer Portal)

**User Requirements:**
- Manage Server (a.k.a. Manage Guild) permission for all DiscoOps commands
- Owner-only for debug commands (`logs`, `clearlogs`, `debug`)

## Important Notes

1. **Backward Compatibility**: Maintain compatibility with Red 3.5+ while supporting older versions where possible
2. **No Embeds**: Never use Discord embeds; all output must be plain Markdown text
3. **Character Limits**: Always paginate outputs; Discord has a 2,000-character message limit
4. **Member Data**: Do not store personal user data; only store event-role mappings
5. **Unicode Support**: Handle diacritics and special characters in event names correctly
6. **Logging**: Use in-memory logging with owner-only access for debugging

## When Contributing

1. Keep changes minimal and focused
2. Test manually in a Discord server before submitting
3. Update README.md if adding new commands or changing behavior
4. Ensure info.json metadata is accurate
5. Follow existing code patterns and formatting
6. Maintain the plain-text, no-embed output philosophy
