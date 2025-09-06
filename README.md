# File: README.md

# DiscoOps - Discord Red Bot Cog

A comprehensive operational toolkit for Discord server management, designed to make running a Discord server easier and more efficient.

## Features

### 📊 Member Management
- Track new members who joined recently
- Analyze role membership and statistics
- View member counts and role distributions

### 📅 Event Management
- Integrate with Discord's built-in Scheduled Events
- Track event attendees and interested members
- Create and manage event-specific roles
- Automatically sync roles with event attendance

## Installation

1. **Add the repository to your Red Bot:**
```
[p]repo add DiscoOps https://github.com/yourusername/DiscoOps
```

2. **Install the cog:**
```
[p]cog install DiscoOps discoops
```

3. **Load the cog:**
```
[p]load discoops
```

## Commands

Replace `[p]` with your bot's prefix. You can use either `[p]do` or `[p]discoops` as the base command.

### Member Commands

#### View Recent Members
See who joined your server recently:
```
[p]do members new 7 days
[p]do members new 2 weeks  
[p]do members new 1 month
```

#### Analyze Roles
Check who has a specific role:
```
[p]do members role @Moderator
[p]do members role @Verified
[p]do members role "Game Night"
```

### Event Commands

#### List All Events
View all scheduled events sorted by date (soonest first):
```
[p]do events list
```

#### Check Event Attendance
See who's interested in an event:
```
[p]do events members "Game Night"
[p]do events members "Movie Watch Party"
```

#### Manage Event Roles

**Create a role for an event:**
```
[p]do events role create "Game Night"
```
This creates a role and assigns it to everyone interested in the event.

**Sync the role with current attendees:**
```
[p]do events role sync "Game Night"
```
This updates the role - adds it to newly interested members and removes it from those no longer interested.

**Delete an event role:**
```
[p]do events role delete "Game Night"
```

### Get Help
View all available commands:
```
[p]do help
```

## Practical Usage Examples

### Weekly New Member Report
Run this every Monday to see who joined in the past week:
```
[p]do members new 7 days
```

### Event Management Workflow

1. **Create an event in Discord** (using Discord's event feature)

2. **List events to get the exact name:**
```
[p]do events list
```

3. **Create a role for the event:**
```
[p]do events role create "Friday Game Night"
```

4. **Before the event, sync the role to catch latecomers:**
```
[p]do events role sync "Friday Game Night"
```

5. **Use the role to ping attendees:**
```
@Event: Friday Game Night The event is starting in 10 minutes!
```

6. **After the event, clean up:**
```
[p]do events role delete "Friday Game Night"
```

### Role Audit
Check how many people have specific roles:
```
[p]do members role @Moderator
[p]do members role @VIP
[p]do members role @Subscriber
```

### Bulk Event Management
If you have multiple events, you can quickly manage them:
```
# List all events
[p]do events list

# Check attendance for each
[p]do events members "Event 1"
[p]do events members "Event 2"

# Create roles for popular events
[p]do events role create "Popular Event"
```

## Permissions Required

The bot needs the following Discord permissions:
- **View Server Members** - To list and analyze members
- **Manage Roles** - To create and assign event roles
- **Send Messages** - To respond to commands
- **Embed Links** - To send formatted responses
- **View Channels** - To read commands

Users need the **Manage Server** permission to use DiscoOps commands.

## Tips & Best Practices

1. **Event Names**: When using event commands, you can use partial names. The bot will try to match your input to existing events.

2. **Role Colors**: Event roles are created with random colors. You can manually edit them in Discord's role settings if needed.

3. **Large Servers**: For servers with many members, the bot limits displays to the first 25-50 entries to avoid hitting Discord's limits.

4. **Timezone**: All times are displayed in UTC for consistency.

5. **Regular Maintenance**: 
   - Sync event roles before events start
   - Clean up old event roles after events end
   - Run weekly member reports to track growth

## Troubleshooting

### "No members joined in the last X days"
- Make sure the bot has permission to view server members
- Check if members might have joined just outside your time range

### "Event not found"
- Use `[p]do events list` to see the exact event names
- Event names are case-insensitive but must exist in Discord

### Role creation fails
- Ensure the bot has "Manage Roles" permission
- Check that the bot's role is high enough in the hierarchy

### Commands not working
- Verify the cog is loaded: `[p]cog list`
- Check bot permissions in the channel
- Ensure you have "Manage Server" permission

## Support

For issues, feature requests, or contributions, please visit the [GitHub repository](https://github.com/yourusername/DiscoOps).

## License

This cog is provided as-is for use with Red-DiscordBot.

---

*DiscoOps - Making Discord server operations simple and efficient.*
