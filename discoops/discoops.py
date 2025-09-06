# File: discoops/discoops.py

import discord
from redbot.core import commands, Config
from datetime import datetime, timedelta, timezone
import asyncio
from typing import Optional, Union, List
from collections import deque
import unicodedata


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

        # Simple in-memory log buffer (owner-only commands below expose this)
        self.logs: deque[str] = deque(maxlen=1000)

    # --------- tiny internal logger ----------
    def log_info(self, message: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")
        self.logs.append(f"[{ts}] {message}")

    # --------- helpers ----------
    @staticmethod
    def _norm_text(s: str) -> str:
        """
        Normalize text for comparisons:
        - Unicode NFKC
        - strip ASCII + smart quotes and surrounding whitespace
        - casefold for better Unicode-insensitive match
        """
        if s is None:
            return ""
        s = unicodedata.normalize("NFKC", s).strip()
        s = s.strip(' "\'“”‘’')  # remove surrounding quotes if present
        return s.casefold()

    @staticmethod
    def _event_match(events: List[discord.ScheduledEvent], query: str) -> Optional[discord.ScheduledEvent]:
        """Find event by normalized exact name, then partial match."""
        nq = DiscoOps._norm_text(query)
        # exact match
        for e in events:
            if DiscoOps._norm_text(e.name) == nq:
                return e
        # partial match
        for e in events:
            if nq in DiscoOps._norm_text(e.name):
                return e
        return None

    # ============= Command Group =============

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
        self.log_info(f"{ctx.author} invoked 'members new' with amount={amount}, period={period} in guild {ctx.guild.id}")

        period_l = (period or "").lower()
        if period_l not in ['days', 'day', 'weeks', 'week', 'months', 'month']:
            await ctx.send("Period must be 'days', 'weeks', or 'months'")
            self.log_info("Invalid period provided to 'members new'")
            return

        # Calculate time delta
        if period_l in ['days', 'day']:
            delta = timedelta(days=amount)
        elif period_l in ['weeks', 'week']:
            delta = timedelta(weeks=amount)
        else:  # months (approximate)
            delta = timedelta(days=amount * 30)

        # Make cutoff tz-aware (UTC) to avoid naive/aware comparison errors
        cutoff_date = datetime.now(timezone.utc) - delta

        # Try to access members; this needs the Server Members Intent
        try:
            members = list(ctx.guild.members)
            if not members:
                try:
                    await ctx.guild.chunk()
                    members = list(ctx.guild.members)
                except Exception as e:
                    pass
        except Exception as e:
            members = []
            self.log_info(f"Error accessing guild members: {e}")

        if not members:
            await ctx.send(
                "I couldn't access the member list. Make sure **Server Members Intent** is enabled for the bot "
                "and the bot has recently been online to cache members."
            )
            self.log_info("members list empty or inaccessible; likely missing Server Members Intent")
            return

        # Filter recent members (handle tz-awareness defensively)
        try:
            recent_members = []
            for m in members:
                ja = getattr(m, "joined_at", None)
                if not ja:
                    continue
                # Ensure tz-aware UTC for joined_at
                if ja.tzinfo is None:
                    ja = ja.replace(tzinfo=timezone.utc)
                if ja > cutoff_date:
                    recent_members.append((m, ja))
        except Exception as e:
            self.log_info(f"Error filtering recent members: {e}")
            await ctx.send("An error occurred while reading member join dates.")
            return

        # Sort by join date (most recent first)
        recent_members.sort(key=lambda tup: tup[1], reverse=True)

        if not recent_members:
            await ctx.send(f"No members joined in the last {amount} {period_l}.")
            self.log_info("No recent members found")
            return

        # Create embed with results
        embed = discord.Embed(
            title=f"New Members - Last {amount} {period_l}",
            color=discord.Color.blue(),
            description=f"Found {len(recent_members)} member(s)"
        )

        # Add members to embed (limit to 25 fields)
        for i, (member, ja) in enumerate(recent_members[:25]):
            join_date = ja.strftime("%Y-%m-%d %H:%M UTC")
            days_ago = (datetime.now(timezone.utc) - ja).days
            embed.add_field(
                name=f"{i+1}. {member.display_name}",
                value=f"ID: {member.id}\nJoined: {join_date}\n({days_ago} days ago)",
                inline=True
            )

        if len(recent_members) > 25:
            embed.set_footer(text=f"Showing first 25 of {len(recent_members)} members")

        await ctx.send(embed=embed)
        self.log_info(f"Sent recent members list ({len(recent_members)} found)")

    @members_group.command(name="role")
    async def members_role(self, ctx, *, role: discord.Role):
        """
        List all members with a specific role and show count.

        Usage: [p]do members role <@role>
        Example: [p]do members role @Moderator
        """
        self.log_info(f"{ctx.author} invoked 'members role' for role {role.id} in guild {ctx.guild.id}")
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
        self.log_info(f"Sent members-with-role list ({len(members_with_role)} members)")

    # ========== Events Commands (Using Discord's Scheduled Events) ==========

    @discoops.group(name="events")
    async def events_group(self, ctx):
        """Event management commands for Discord Scheduled Events."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @events_group.command(name="list")
    async def events_list(self, ctx):
        """List all scheduled events with name and description."""
        events: List[discord.ScheduledEvent] = await ctx.guild.fetch_scheduled_events(with_counts=True)

        if not events:
            await ctx.send("No scheduled events found in this server.")
            return

        # sort by start time (earliest first); put None at the end
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        events.sort(key=lambda e: e.start_time or far_future)

        embed = discord.Embed(
            title="📅 Scheduled Events",
            color=discord.Color.green(),
            description=f"Total: {len(events)} event(s)"
        )

        for event in events:
            # Status badge
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

            user_count = event.user_count or 0

            # Time formatting (Discord timestamp + raw epoch)
            if event.start_time:
                epoch = int(event.start_time.replace(tzinfo=event.start_time.tzinfo or timezone.utc).timestamp())
                discord_time = f"<t:{epoch}:F> • <t:{epoch}:R>"
                unix_time = f"`{epoch}`"
                start_line = f"{discord_time} (unix: {unix_time})"
            else:
                start_line = "N/A"

            # Field content (copy-friendly name + quick command)
            field_value = (
                f"**Name:** `{event.name}`\n"
                f"**Status:** {status_emoji} {status}\n"
                f"**Start:** {start_line}\n"
                f"**Interested:** {user_count} users\n"
                f"**Quick command:** `[p]do events members \"{event.name}\"`\n"
            )

            if event.description:
                desc = event.description[:200] + "..." if len(event.description) > 200 else event.description
                field_value += f"**Description:** {desc}\n"

            if event.location:
                field_value += f"**Location:** {event.location}\n"
            elif event.channel:
                field_value += f"**Channel:** {event.channel.mention}\n"

            embed.add_field(
                name=f"{status_emoji} {event.name}",
                value=field_value,
                inline=False
            )

        await ctx.send(embed=embed)
        self.log_info(f"{ctx.author} listed events in guild {ctx.guild.id}")

    @events_group.command(name="members")
    async def events_members(self, ctx, *, event_name: str):
        """
        List members who are interested in a specific event.

        Usage: [p]do events members <event_name>
        """
        events = await ctx.guild.fetch_scheduled_events(with_counts=True)

        # Normalize and match (handles quotes/diacritics)
        event = self._event_match(events, event_name)

        if not event:
            await ctx.send(f"Event '{event_name}' not found. Use `[p]do events list` to see all events.")
            self.log_info(f"events members: not found for query={event_name!r}")
            return

        # Fetch users interested in the event
        try:
            interested_users = []
            async for user in event.users():
                member = ctx.guild.get_member(user.id)
                if member:
                    interested_users.append(member)
        except Exception as e:
            await ctx.send(f"Error fetching interested users: {e}")
            self.log_info(f"Error fetching users for event {event.id}: {e}")
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

        if event.start_time:
            epoch = int(event.start_time.timestamp())
            start_line = f"<t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
        else:
            start_line = "N/A"

        embed.add_field(
            name="Event Details",
            value=(
                f"**Name:** `{event.name}`\n"
                f"**Start:** {start_line}\n"
                f"**Status:** {event.status.name}\n"
                f"**Quick command:** `[p]do events members \"{event.name}\"`"
            ),
            inline=False
        )

        # List interested members
        member_list = []
        for i, member in enumerate(interested_users[:50], 1):
            member_list.append(f"{i}. {member.mention} ({member.display_name})")

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
        self.log_info(f"{ctx.author} listed interested members for event {event.id} in guild {ctx.guild.id}")

    @events_group.command(name="role")
    async def events_role(self, ctx, action: str, *, event_name: str):
        """
        Create, sync, or delete a role for event attendees.

        Usage: [p]do events role <create|sync|delete> <event_name>
        """
        action = (action or "").lower()
        if action not in ['create', 'sync', 'delete']:
            await ctx.send("Action must be 'create', 'sync', or 'delete'")
            return

        events = await ctx.guild.fetch_scheduled_events(with_counts=True)
        event = self._event_match(events, event_name)

        if not event:
            await ctx.send(f"Event '{event_name}' not found. Use `[p]do events list` to see all events.")
            self.log_info(f"events role: not found for query={event_name!r}")
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
            self.log_info(f"Error fetching users for event {event.id}: {e}")
            return

        event_roles = await self.config.guild(ctx.guild).event_roles()
        event_id_str = str(event.id)

        if action == "create":
            if event_id_str in event_roles:
                role = ctx.guild.get_role(event_roles[event_id_str])
                if role:
                    await ctx.send(f"Role already exists: {role.mention}")
                    return
            try:
                role = await ctx.guild.create_role(
                    name=f"Event: {event.name}",
                    color=discord.Color.random(),
                    mentionable=True,
                    reason=f"Event role created by {ctx.author}"
                )
                async with self.config.guild(ctx.guild).event_roles() as roles:
                    roles[event_id_str] = role.id
                added = 0
                for member in interested_users:
                    try:
                        await member.add_roles(role)
                        added += 1
                    except discord.Forbidden:
                        pass
                await ctx.send(f"✅ Created role {role.mention} and added to {added} interested members")
                self.log_info(f"Created role {role.id} for event {event.id} in guild {ctx.guild.id}")
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

            current_members = set(m.id for m in role.members)
            interested_member_ids = set(m.id for m in interested_users)

            to_add = interested_member_ids - current_members
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
            self.log_info(f"Synced role {role.id} for event {event.id} in guild {ctx.guild.id}: +{added}/-{removed}")

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
            async with self.config.guild(ctx.guild).event_roles() as roles:
                del roles[event_id_str]
            self.log_info(f"Deleted role for event {event.id} in guild {ctx.guild.id}")

    # ========== Debug / Logs (Owner Only) ==========

    @discoops.command(name="logs")
    @commands.is_owner()
    async def discoops_logs(self, ctx, count: Optional[int] = 10):
        """View recent in-memory logs. Default: 10"""
        count = max(1, min(int(count or 10), 50))
        if not self.logs:
            await ctx.send("No logs recorded yet.")
            return

        lines = list(self.logs)[-count:]
        content = "\n".join(lines)
        embed = discord.Embed(
            title=f"🧾 DiscoOps Logs (last {len(lines)})",
            description=f"```\n{content}\n```",
            color=discord.Color.dark_grey()
        )
        await ctx.send(embed=embed)

    @discoops.command(name="debug")
    @commands.is_owner()
    async def discoops_debug(self, ctx):
        """Show basic debug information (owner only)."""
        g = ctx.guild
        me = g.me
        perms = g.me.guild_permissions
        embed = discord.Embed(title="🐞 DiscoOps Debug", color=discord.Color.orange())
        embed.add_field(
            name="Guild",
            value=f"Name: {g.name}\nID: {g.id}\nMembers: {g.member_count}",
            inline=False
        )
        embed.add_field(
            name="Bot",
            value=f"Name: {me}\nID: {me.id}",
            inline=False
        )
        embed.add_field(
            name="Key Permissions",
            value=(
                f"Manage Roles: {perms.manage_roles}\n"
                f"Manage Guild: {perms.manage_guild}\n"
                f"View Audit Log: {perms.view_audit_log}\n"
                f"Send Messages: {perms.send_messages}\n"
                f"Embed Links: {perms.embed_links}"
            ),
            inline=False
        )
        await ctx.send(embed=embed)

    @discoops.command(name="clearlogs")
    @commands.is_owner()
    async def discoops_clearlogs(self, ctx):
        """Clear all stored logs."""
        self.logs.clear()
        await ctx.send("✅ Logs cleared.")
        self.log_info(f"Logs cleared by {ctx.author}")

    @discoops.command(name="help")
    async def discoops_help(self, ctx):
        """Show detailed help for DiscoOps commands."""
        embed = discord.Embed(
            title="🛠️ DiscoOps Help",
            description="Operational features for Discord server management",
            color=0x3498db  # Blue color in hex
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
            name="🔧 Debug Commands (Owner Only)",
            value="`[p]do logs [count]`\n"
                  "→ View recent logs (default: 10)\n\n"
                  "`[p]do debug`\n"
                  "→ Show debug information\n\n"
                  "`[p]do clearlogs`\n"
                  "→ Clear all stored logs",
            inline=False
        )

        embed.add_field(
            name="ℹ️ Notes",
            value="• Events are Discord's built-in Scheduled Events\n"
                  "• Create events through Discord's UI (not the bot)\n"
                  "• The bot tracks who's interested in events\n"
                  "• Event roles auto-update when synced\n"
                  "• Use `[p]do logs` if you encounter errors",
            inline=False
        )

        embed.set_footer(text="Replace [p] with your bot's prefix | Aliases: [p]do or [p]discoops")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(DiscoOps(bot))
