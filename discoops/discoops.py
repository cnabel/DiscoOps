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

        default_guild = {
            "event_roles": {}  # Maps event_id to role_id
        }
        self.config.register_guild(**default_guild)

        self.logs: deque[str] = deque(maxlen=1000)

    # --------- tiny internal logger ----------
    def log_info(self, message: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")
        self.logs.append(f"[{ts}] {message}")

    # --------- helpers ----------
    @staticmethod
    def _norm_text(s: str) -> str:
        """Normalize text for comparisons (NFKC + strip quotes + casefold)."""
        if s is None:
            return ""
        s = unicodedata.normalize("NFKC", s).strip()
        s = s.strip(' "\'“”‘’')
        return s.casefold()

    @staticmethod
    def _event_match(events: List[discord.ScheduledEvent], query: str) -> Optional[discord.ScheduledEvent]:
        """Find event by normalized exact name, then partial match."""
        nq = DiscoOps._norm_text(query)
        for e in events:
            if DiscoOps._norm_text(e.name) == nq:
                return e
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

        if period_l in ['days', 'day']:
            delta = timedelta(days=amount)
        elif period_l in ['weeks', 'week']:
            delta = timedelta(weeks=amount)
        else:
            delta = timedelta(days=amount * 30)

        cutoff_date = datetime.now(timezone.utc) - delta

        try:
            members = list(ctx.guild.members)
            if not members:
                try:
                    await ctx.guild.chunk()
                    members = list(ctx.guild.members)
                except Exception:
                    pass
        except Exception as e:
            members = []
            self.log_info(f"Error accessing guild members: {e}")

        if not members:
            await ctx.send(
                "I couldn't access the member list. Ensure **Server Members Intent** is enabled and the bot has cached members."
            )
            self.log_info("members list empty or inaccessible; likely missing Server Members Intent")
            return

        try:
            recent: List[tuple[discord.Member, datetime]] = []
            for m in members:
                ja = getattr(m, "joined_at", None)
                if not ja:
                    continue
                if ja.tzinfo is None:
                    ja = ja.replace(tzinfo=timezone.utc)
                if ja > cutoff_date:
                    recent.append((m, ja))
        except Exception as e:
            self.log_info(f"Error filtering recent members: {e}")
            await ctx.send("An error occurred while reading member join dates.")
            return

        recent.sort(key=lambda tup: tup[1], reverse=True)

        if not recent:
            await ctx.send(f"No members joined in the last {amount} {period_l}.")
            self.log_info("No recent members found")
            return

        # README-like embed (headers + blockquote, no emojis/bullets)
        title = f"# New Members\n**Range:** last **{amount} {period_l}**  •  **Found:** {len(recent)}"
        embed = discord.Embed(
            title="",
            description=title,
            color=discord.Color.blue(),
        )

        for i, (member, ja) in enumerate(recent[:25], start=1):
            epoch = int(ja.timestamp())
            block = (
                f"> **Member**: {member.mention} ({member.display_name})\n"
                f"> **ID**: `{member.id}`\n"
                f"> **Joined**: <t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
            )
            # Put the member name as a copy-friendly header line
            embed.add_field(
                name=f"## `{member.display_name}`",
                value=block,
                inline=False
            )

        if len(recent) > 25:
            embed.set_footer(text=f"Showing first 25 of {len(recent)} members")

        await ctx.send(embed=embed)
        self.log_info(f"Sent recent members list ({len(recent)} found)")

    @members_group.command(name="role")
    async def members_role(self, ctx, *, role: discord.Role):
        """List all members with a specific role and show count."""
        self.log_info(f"{ctx.author} invoked 'members role' for role {role.id} in guild {ctx.guild.id}")
        members_with_role = role.members

        header = f"# Members with role\n**Role:** `{role.name}`  •  **Total:** {len(members_with_role)}"
        embed = discord.Embed(description=header, color=role.color if role.color.value else discord.Color.blue())

        if not members_with_role:
            embed.add_field(name="## Members", value="> None", inline=False)
        else:
            # Group into chunks of 15
            chunk_size = 15
            for i in range(0, min(len(members_with_role), 60), chunk_size):
                chunk = members_with_role[i:i + chunk_size]
                lines = [f"{i+j+1}. {m.mention} ({m.display_name})" for j, m in enumerate(chunk)]
                embed.add_field(
                    name=f"## Members {i+1}-{i+len(chunk)}",
                    value="\n".join(lines),
                    inline=False
                )
            if len(members_with_role) > 60:
                embed.set_footer(text=f"Showing first 60 of {len(members_with_role)} members")

        role_info = (
            f"> **Created**: {role.created_at.strftime('%Y-%m-%d')}\n"
            f"> **Position**: {role.position}\n"
            f"> **Mentionable**: {'Yes' if role.mentionable else 'No'}\n"
            f"> **Color**: {str(role.color)}"
        )
        embed.add_field(name="## Role Info", value=role_info, inline=False)

        await ctx.send(embed=embed)
        self.log_info(f"Sent members-with-role list ({len(members_with_role)} members)")

    # ========== Event Commands (Using Discord's Scheduled Events) ==========

    @discoops.group(name="event", aliases=["events"], invoke_without_command=True)
    async def event_group(self, ctx, *, event_name: Optional[str] = None):
        """
        Event management commands.

        - `[p]do event list` — list scheduled events
        - `[p]do event "Name"` — show summary + interested members
        (Alias: `events` kept for backward compatibility.)
        """
        if event_name:
            await self._event_info_with_members(ctx, event_name)
        else:
            await ctx.send_help(ctx.command)

    @event_group.command(name="list", aliases=["ls"])
    async def event_list(self, ctx):
        """List scheduled events (README style)."""
        events: List[discord.ScheduledEvent] = await ctx.guild.fetch_scheduled_events(with_counts=True)
        if not events:
            await ctx.send("No scheduled events found in this server.")
            return

        far_future = datetime.max.replace(tzinfo=timezone.utc)
        events.sort(key=lambda e: e.start_time or far_future)

        header = f"# Scheduled Events\n**Total:** {len(events)}"
        embed = discord.Embed(description=header, color=discord.Color.green())

        for event in events:
            status = event.status.name.title()
            user_count = event.user_count or 0

            if event.start_time:
                epoch = int(event.start_time.replace(tzinfo=event.start_time.tzinfo or timezone.utc).timestamp())
                start_line = f"<t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
            else:
                start_line = "N/A"

            summary = [
                f"> **Status**: {status}",
                f"> **Start**: {start_line}",
                f"> **Interested**: {user_count}",
            ]
            if event.description:
                desc = event.description[:200] + "..." if len(event.description) > 200 else event.description
                summary.append(f"> **Description**: {desc}")
            if event.location:
                summary.append(f"> **Location**: {event.location}")
            elif event.channel:
                summary.append(f"> **Channel**: {event.channel.mention}")

            # Title line is just the copy-friendly header with backticked name
            embed.add_field(
                name=f"## `{event.name}`",
                value="\n".join(summary) + f"\n\n**Quick:** `[p]do event \"{event.name}\"`",
                inline=False
            )

        await ctx.send(embed=embed)
        self.log_info(f"{ctx.author} listed events in guild {ctx.guild.id}")

    @event_group.command(name="members")  # deprecated path, kept for compatibility
    async def event_members_legacy(self, ctx, *, event_name: str):
        """[Deprecated] Use: `[p]do event "Name"` instead."""
        await ctx.send("`members` is deprecated. Use: `[p]do event \"Event Name\"`.\nShowing the info below:")
        await self._event_info_with_members(ctx, event_name)

    async def _event_info_with_members(self, ctx, event_name: str):
        """Show one event summary (README style) + interested members."""
        events = await ctx.guild.fetch_scheduled_events(with_counts=True)
        event = self._event_match(events, event_name)
        if not event:
            await ctx.send(f"Event '{event_name}' not found. Use `[p]do event list` to see all events.")
            self.log_info(f"event info: not found for query={event_name!r}")
            return

        # Collect users
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

        status = event.status.name.title()
        if event.start_time:
            epoch = int(event.start_time.replace(tzinfo=event.start_time.tzinfo or timezone.utc).timestamp())
            start_line = f"<t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
        else:
            start_line = "N/A"

        user_count = event.user_count or 0

        # Build README-like embed: title header, blockquote summary, then members
        title_header = f"# `{event.name}`"
        summary_block = (
            f"> **Status**: {status}\n"
            f"> **Start**: {start_line}\n"
            f"> **Interested**: {user_count}\n"
            f"{(f'> **Description**: {event.description[:1024]}\n') if event.description else ''}"
            f"{(f'> **Location**: {event.location}\n') if event.location else (f'> **Channel**: {event.channel.mention}\n' if event.channel else '')}"
            f"> **Role**: `[p]do event role create \"{event.name}\"` • "
            f"`[p]do event role sync \"{event.name}\"` • "
            f"`[p]do event role delete \"{event.name}\"`"
        )

        embed = discord.Embed(
            description=f"{title_header}\n\n{summary_block}",
            color=discord.Color.blue()
        )

        # Interested members
        if interested_users:
            lines = [f"{i}. {m.mention} ({m.display_name})" for i, m in enumerate(interested_users[:50], start=1)]
            # Split into chunks to avoid field limits
            chunk_size = 15
            for i in range(0, len(lines), chunk_size):
                chunk = lines[i:i + chunk_size]
                embed.add_field(
                    name=f"## Interested Members {i+1}-{i+len(chunk)}",
                    value="\n".join(chunk),
                    inline=False
                )
            if len(interested_users) > 50:
                embed.set_footer(text=f"Showing first 50 of {len(interested_users)} interested members")
        else:
            embed.add_field(
                name="## Interested Members",
                value="> None",
                inline=False
            )

        await ctx.send(embed=embed)
        self.log_info(f"{ctx.author} viewed event info for {event.id} in guild {ctx.guild.id}")

    @event_group.command(name="role")
    async def event_role(self, ctx, action: str, *, event_name: str):
        """
        Create, sync, or delete a role for event attendees.
        Usage: [p]do event role <create|sync|delete> <event_name>
        """
        action = (action or "").lower()
        if action not in ['create', 'sync', 'delete']:
            await ctx.send("Action must be 'create', 'sync', or 'delete'")
            return

        events = await ctx.guild.fetch_scheduled_events(with_counts=True)
        event = self._event_match(events, event_name)
        if not event:
            await ctx.send(f"Event '{event_name}' not found. Use `[p]do event list` to see all events.")
            self.log_info(f"event role: not found for query={event_name!r}")
            return

        # Interested users
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
                await ctx.send(f"Created role {role.mention} and added to {added} interested members")
                self.log_info(f"Created role {role.id} for event {event.id} in guild {ctx.guild.id}")
            except discord.Forbidden:
                await ctx.send("I don't have permission to create roles.")
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

            await ctx.send(f"Sync complete for {role.mention} — Added: {added} • Removed: {removed}")
            self.log_info(f"Synced role {role.id} for event {event.id} in guild {ctx.guild.id}: +{added}/-{removed}")

        elif action == "delete":
            if event_id_str not in event_roles:
                await ctx.send(f"No role exists for event **{event.name}**")
                return
            role = ctx.guild.get_role(event_roles[event_id_str])
            if role:
                try:
                    await role.delete(reason=f"Event role deleted by {ctx.author}")
                    await ctx.send(f"Deleted role for event **{event.name}**")
                except discord.Forbidden:
                    await ctx.send("I don't have permission to delete this role.")
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
            title="DiscoOps Logs",
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
        embed = discord.Embed(title="DiscoOps Debug", color=discord.Color.orange())
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
        await ctx.send("Logs cleared.")
        self.log_info(f"Logs cleared by {ctx.author}")

    @discoops.command(name="help")
    async def discoops_help(self, ctx):
        """Show detailed help for DiscoOps commands."""
        embed = discord.Embed(
            title="DiscoOps Help",
            description=(
                "# Commands\n"
                "## Members\n"
                "`[p]do members new <amount> <days|weeks|months>` — List recent joins\n"
                "`[p]do members role <@role>` — List members with a role\n\n"
                "## Events\n"
                "`[p]do event list` — List scheduled events\n"
                "`[p]do event \"Event Name\"` — Show one event (+ members)\n"
                "`[p]do event role <create|sync|delete> \"Event Name\"` — Manage event role"
            ),
            color=0x3498db
        )
        embed.set_footer(text="Replace [p] with your bot's prefix | Group aliases: do event / do events")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(DiscoOps(bot))
