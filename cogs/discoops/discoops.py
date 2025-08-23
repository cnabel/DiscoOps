# File: cogs/discoops/discoops.py

import discord
from redbot.core import commands, Config
from datetime import datetime, timedelta
import asyncio
from typing import Optional, Union

class DiscoOps(commands.Cog):
    """Operational features to make Discord server management easier."""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=260288776360820736)
        
        # Initialize default guild settings for event role tracking
        default_guild = {
            "event_roles": {}  # Maps event_id to role_id
        }
        self.config.register_guild(**default_guild)
    
    @commands.group(name="do", aliases=["discoops"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def discoops(self, ctx):
        """DiscoOps main command group."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)
    
    # ========== Members Commands ==========
    
    @discoops.group(name="members")
    async def members_group(self, ctx):
        """Member management commands."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)
    
    @members_group.command(name="new")
    async def members_new(self, ctx, amount: int, period: str):
        """
        List members who joined in the last X days/weeks/months.
        
        Usage: [p]do members new <amount> <period>
        Example: [p]do members new 7 days
        """
        period = period.lower()
        if period not in ['days', 'day', 'weeks', 'week', 'months', 'month']:
            await ctx.send("Period must be 'days', 'weeks', or 'months'")
            return
        
        # Calculate time delta
        if period in ['days', 'day']:
            delta = timedelta(days=amount)
        elif period in ['weeks', 'week']:
            delta = timedelta(weeks=amount)
        else:  # months
            delta = timedelta(days=amount * 30)  # Approximate
        
        cutoff_date = datetime.utcnow() - delta
        
        # Get members who joined after cutoff date
        recent_members = []
        for member in ctx.guild.members:
            if member.joined_at and member.joined_at > cutoff_date:
                recent_members.append(member)
        
        # Sort by join date (most recent first)
        recent_members.sort(key=lambda m: m.joined_at, reverse=True)
        
        if not recent_members:
            await ctx.send(f"No members joined in the last {amount} {period}.")
            return
        
        # Create embed with results
        embed = discord.Embed(
            title=f"New Members - Last {amount} {period}",
            color=discord.Color.blue(),
            description=f"Found {len(recent_members)} member(s)"
        )
        
        # Add members to embed (limit to 25 fields)
        for i, member in enumerate(recent_members[:25]):
            join_date = member.joined_at.strftime("%Y-%m-%d %H:%M UTC")
            days_ago = (datetime.utcnow() - member.joined_at).days
            embed.add_field(
                name=f"{i+1}. {member.display_name}",
                value=f"ID: {member.id}\nJoined: {join_date}\n({days_ago} days ago)",
                inline=True
            )
        
        if len(recent_members) > 25:
            embed.set_footer(text=f"Showing first 25 of {len(recent_members)} members")
        
        await ctx.send(embed=embed)
    
    @members_group.command(name="role")
    async def members_role(self, ctx, *, role: discord.Role):
        """
        List all members with a specific role and show count.
        
        Usage: [p]do members role <@role>
        Example: [p]do members role @Moderator
        """
        members_with_role = role.members
        
        embed = discord.Embed(
            title=f"Members with role: {role.name}",
            color=role.color if role.color != discord.Color.default() else discord.Color.blue(),
            description=f"**Total: {len(members_with_role)} member(s)**"
        )
        
        if not members_with_role:
            embed.add_field(
                name="No Members",
                value="No members currently have this role.",
                inline=False
            )
        else:
            # Create member list
            member_list = []
            for i, member in enumerate(members_with_role[:50], 1):
                member_list.append(f"{i}. {member.mention} ({member.display_name})")
            
            # Split into chunks if needed
            chunk_size = 10
            for i in range(0, len(member_list), chunk_size):
                chunk = member_list[i:i + chunk_size]
                embed.add_field(
                    name=f"Members {i+1}-{min(i+chunk_size, len(member_list))}",
                    value="\n".join(chunk),
                    inline=False
                )
            
            if len(members_with_role) > 50:
                embed.set_footer(text=f"Showing first 50 of {len(members_with_role)} members")
        
        # Add role information
        embed.add_field(
            name="📊 Role Statistics",
            value=f"Created: {role.created_at.strftime('%Y-%m-%d')}\n"
                  f"Position: {role.position}\n"
                  f"Mentionable: {'Yes' if role.mentionable else 'No'}\n"
                  f"Color: {str(role.color)}",
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    # ========== Events Commands (Using Discord's Scheduled Events) ==========
    
    @discoops.group(name="events")
    async def events_group(self, ctx):
        """Event management commands for Discord Scheduled Events."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)
    
    @events_group.command(name="list")
    async def events_list(self, ctx):
        """List all scheduled events with name and description."""
        # Get all scheduled events from the guild
        events = await ctx.guild.fetch_scheduled_events()
        
        if not events:
            await ctx.send("No scheduled events found in this server.")
            return
        
        embed = discord.Embed(
            title="📅 Scheduled Events",
            color=discord.Color.green(),
            description=f"Total: {len(events)} event(s)"
        )
        
        for event in events:
            # Get event status
            if event.status == discord.EventStatus.scheduled:
                status_emoji = "🔔"
                status = "Scheduled"
            elif event.status == discord.EventStatus.active:
                status_emoji = "🟢"
                status = "Active"
            elif event.status == discord.EventStatus.completed:
                status_emoji = "✅"
                status = "Completed"
            else:  # cancelled
                status_emoji = "❌"
                status = "Cancelled"
            
            # Get interested users count
            user_count = event.user_count if event.user_count else 0
            
            # Format event time
            start_time = event.start_time.strftime("%Y-%m-%d %H:%M UTC") if event.start_time else "N/A"
            
            # Build field value
            field_value = f"**Status:** {status_emoji} {status}\n"
            field_value += f"**Start:** {start_time}\n"
            field_value += f"**Interested:** {user_count} users\n"
            
            if event.description:
                # Truncate description if too long
                desc = event.description[:200] + "..." if len(event.description) > 200 else event.description
                field_value += f"**Description:** {desc}\n"
            
            if event.location:
                field_value += f"**Location:** {event.location}\n"
            
            embed.add_field(
                name=f"{status_emoji} {event.name}",
                value=field_value,
                inline=False
            )
        
        await ctx.send(embed=embed)
    
    @events_group.command(name="members")
    async def events_members(self, ctx, *, event_name: str):
        """
        List members who are interested in a specific event.
        
        Usage: [p]do events members <event_name>
        """
        # Find the event by name
        events = await ctx.guild.fetch_scheduled_events()
        event = None
        
        for e in events:
            if e.name.lower() == event_name.lower():
                event = e
                break
        
        if not event:
            # Try partial match
            for e in events:
                if event_name.lower() in e.name.lower():
                    event = e
                    break
        
        if not event:
            await ctx.send(f"Event '{event_name}' not found. Use `[p]do events list` to see all events.")
            return
        
        # Fetch users interested in the event
        try:
            interested_users = []
            async for user in event.users():
                member = ctx.guild.get_member(user.id)
                if member:  # Only include if they're still in the server
                    interested_users.append(member)
        except Exception as e:
            await ctx.send(f"Error fetching interested users: {e}")
            return
        
        if not interested_users:
            await ctx.send(f"No members are marked as interested in **{event.name}**")
            return
        
        embed = discord.Embed(
            title=f"📋 Interested in: {event.name}",
            color=discord.Color.blue(),
            description=f"**Total: {len(interested_users)} member(s)**"
        )
        
        if event.description:
            embed.add_field(
                name="Event Description",
                value=event.description[:1024],
                inline=False
            )
        
        # Add event details
        start_time = event.start_time.strftime("%Y-%m-%d %H:%M UTC") if event.start_time else "N/A"
        embed.add_field(
            name="Event Details",
            value=f"**Start:** {start_time}\n**Status:** {event.status.name}",
            inline=False
        )
        
        # List interested members
        member_list = []
        for i, member in enumerate(interested_users[:50], 1):
            member_list.append(f"{i}. {member.mention} ({member.display_name})")
        
        # Split into chunks
        chunk_size = 15
        for i in range(0, len(member_list), chunk_size):
            chunk = member_list[i:i + chunk_size]
            embed.add_field(
                name=f"Interested Members ({i+1}-{min(i+chunk_size, len(member_list))})",
                value="\n".join(chunk),
                inline=False
            )
        
        if len(interested_users) > 50:
            embed.set_footer(text=f"Showing first 50 of {len(interested_users)} members")
        
        await ctx.send(embed=embed)
    
    @events_group.command(name="role")
    async def events_role(self, ctx, action: str, *, event_name: str):
        """
        Create, sync, or delete a role for event attendees.
        
        Usage: [p]do events role <create|sync|delete> <event_name>
        Actions:
        - create: Creates a new role for the event and adds all interested members
        - sync: Updates the role to match current interested members
        - delete: Removes the role associated with the event
        """
        action = action.lower()
        if action not in ['create', 'sync', 'delete']:
            await ctx.send("Action must be 'create', 'sync', or 'delete'")
            return
        
        # Find the event
        events = await ctx.guild.fetch_scheduled_events()
        event = None
        
        for e in events:
            if e.name.lower() == event_name.lower():
                event = e
                break
        
        if not event:
            for e in events:
                if event_name.lower() in e.name.lower():
                    event = e
                    break
        
        if not event:
            await ctx.send(f"Event '{event_name}' not found. Use `[p]do events list` to see all events.")
            return
        
        # Get interested users
        interested_users = []
        try:
            async for user in event.users():
                member = ctx.guild.get_member(user.id)
                if member:
                    interested_users.append(member)
        except Exception as e:
            await ctx.send(f"Error fetching interested users: {e}")
            return
        
        event_roles = await self.config.guild(ctx.guild).event_roles()
        event_id_str = str(event.id)
        
        if action == "create":
            # Check if role already exists
            if event_id_str in event_roles:
                role = ctx.guild.get_role(event_roles[event_id_str])
                if role:
                    await ctx.send(f"Role already exists: {role.mention}")
                    return
            
            # Create new role
            try:
                role = await ctx.guild.create_role(
                    name=f"Event: {event.name}",
                    color=discord.Color.random(),
                    mentionable=True,
                    reason=f"Event role created by {ctx.author}"
                )
                
                # Save role ID
                async with self.config.guild(ctx.guild).event_roles() as roles:
                    roles[event_id_str] = role.id
                
                # Add role to all interested members
                added = 0
                for member in interested_users:
                    try:
                        await member.add_roles(role)
                        added += 1
                    except discord.Forbidden:
                        pass
                
                await ctx.send(f"✅ Created role {role.mention} and added to {added} interested members")
            
            except discord.Forbidden:
                await ctx.send("❌ I don't have permission to create roles!")
                return
        
        elif action == "sync":
            if event_id_str not in event_roles:
                await ctx.send(f"No role exists for event **{event.name}**. Use `create` first.")
                return
            
            role = ctx.guild.get_role(event_roles[event_id_str])
            if not role:
                await ctx.send(f"Role no longer exists for event **{event.name}**")
                async with self.config.guild(ctx.guild).event_roles() as roles:
                    del roles[event_id_str]
                return
            
            # Get current members with role
            current_members = set(m.id for m in role.members)
            interested_member_ids = set(m.id for m in interested_users)
            
            # Members to add role to
            to_add = interested_member_ids - current_members
            # Members to remove role from
            to_remove = current_members - interested_member_ids
            
            added = removed = 0
            
            for member_id in to_add:
                member = ctx.guild.get_member(member_id)
                if member:
                    try:
                        await member.add_roles(role)
                        added += 1
                    except discord.Forbidden:
                        pass
            
            for member_id in to_remove:
                member = ctx.guild.get_member(member_id)
                if member:
                    try:
                        await member.remove_roles(role)
                        removed += 1
                    except discord.Forbidden:
                        pass
            
            await ctx.send(f"✅ Sync complete for {role.mention}!\nAdded: {added} members\nRemoved: {removed} members")
        
        elif action == "delete":
            if event_id_str not in event_roles:
                await ctx.send(f"No role exists for event **{event.name}**")
                return
            
            role = ctx.guild.get_role(event_roles[event_id_str])
            if role:
                try:
                    await role.delete(reason=f"Event role deleted by {ctx.author}")
                    await ctx.send(f"✅ Deleted role for event **{event.name}**")
                except discord.Forbidden:
                    await ctx.send("❌ I don't have permission to delete this role!")
                    return
            
            # Remove from config
            async with self.config.guild(ctx.guild).event_roles() as roles:
                del roles[event_id_str]
    
    @discoops.command(name="help")
    async def discoops_help(self, ctx):
        """Show detailed help for DiscoOps commands."""
        embed = discord.Embed(
            title="🛠️ DiscoOps Help",
            description="Operational features for Discord server management",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="📊 Member Commands",
            value="`[p]do members new <amount> <days/weeks/months>`\n"
                  "→ List members who joined recently\n"
                  "Example: `[p]do members new 7 days`\n\n"
                  "`[p]do members role <@role>`\n"
                  "→ List members with a role and show count\n"
                  "Example: `[p]do members role @Moderator`",
            inline=False
        )
        
        embed.add_field(
            name="📅 Event Commands",
            value="`[p]do events list`\n"
                  "→ List all Discord scheduled events\n\n"
                  "`[p]do events members <event_name>`\n"
                  "→ List members interested in an event\n"
                  "Example: `[p]do events members Game Night`\n\n"
                  "`[p]do events role <action> <event_name>`\n"
                  "→ Manage roles for event attendees\n"
                  "**Actions:**\n"
                  "• `create` - Create a new role for the event\n"
                  "• `sync` - Update role to match current members\n"
                  "• `delete` - Remove the event's role\n"
                  "Examples:\n"
                  "`[p]do events role create Game Night`\n"
                  "`[p]do events role sync Game Night`\n"
                  "`[p]do events role delete Game Night`",
            inline=False
        )
        
        embed.add_field(
            name="ℹ️ Notes",
            value="• Events are Discord's built-in Scheduled Events\n"
                  "• Create events through Discord's UI (not the bot)\n"
                  "• The bot tracks who's interested in events\n"
                  "• Event roles auto-update when synced",
            inline=False
        )
        
        embed.set_footer(text="Replace [p] with your bot's prefix | Aliases: [p]do or [p]discoops")
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(DiscoOps(bot))
