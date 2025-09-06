# File: discoops/discoops.py

import discord
from redbot.core import commands, Config
from datetime import datetime, timedelta, timezone
import asyncio
from typing import Optional
from collections import deque
import unicodedata

class DiscoOps(commands.Cog):
    """Operational features to make Discord server management easier."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=260288776360820736)
        default_guild = {"event_roles": {}}  # Maps event_id to role_id
        self.config.register_guild(**default_guild)

        # In-memory log buffer
        self.logs = deque(maxlen=1000)

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
    async def _get_scheduled_events(guild, with_counts: bool = True):
        """Safely fetch scheduled events across discord.py versions."""
        try:
            # discord.py 2.x
            return await guild.fetch_scheduled_events(with_counts=with_counts)
        except TypeError:
            # Some builds don’t support with_counts kwarg
            try:
                return await guild.fetch_scheduled_events()
            except Exception:
                return []
        except AttributeError:
            # Very old discord.py without scheduled events
            return []

    @classmethod
    def _event_match(cls, events, query: str):
        """Find event by normalized exact name, then partial match."""
        nq = cls._norm_text(query)
        for e in events:
            if cls._norm_text(getattr(e, "name", "")) == nq:
                return e
        for e in events:
            if nq in cls._norm_text(getattr(e, "name", "")):
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
        if period_l not in ("days", "day", "weeks", "week", "months", "month"):
            await ctx.send("Period must be 'days', 'weeks', or 'months'")
            self.log_info("Invalid period provided to 'members new'")
            return

        if period_l in ("days", "day"):
            delta = timedelta(days=amount)
        elif period_l in ("weeks", "week"):
            delta = timedelta(weeks=amount)
        else:
            delta = timedelta(days=amount * 30)

        cutoff_date = datetime.now(timezone.utc) - delta

        # Access members (requires Server Members Intent)
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

        # Filter recent members, handle tz-awareness defensively
        try:
            recent = []
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

        # README-like embed (headers + blockquote)
        title = f"# New Members\n**Range:** last **{amount} {period_l}**  •  **Found:** {len(recent)}"
        embed = discord.Embed(title="", description=title, color=discord.Color.blue())

        for i, (member, ja) in enumerate(recent[:25], start=1):
            epoch = int(ja.timestamp())
            block = (
                f"> **Member**: {member.mention} ({member.display_name})\n"
                f"> **ID**: `{member.id}`\n"
                f"> **Joined**: <t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
            )
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
        color = role.color if getattr(role.color, "value", 0) else discord.Color.blue()
        embed = discord.Embed(description=header, color=color)

        if not members_with_role:
            embed.add_field(name="## Members", value="> None", inline=False)
        else:
            chunk_size = 15
            limit = min(len(members_with_role), 60)
            for i in range(0, limit, chunk_size):
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
        events = await self._get_scheduled_events(ctx.guild, with_counts=True)
        if not events:
            await ctx.send("No scheduled events found in this server.")
            return

        far_future = datetime.max.replace(tzinfo=timezone.utc)
        try:
            events.sort(key=lambda e: e.start_time or far_future)
        except Exception:
            pass

        header = f"# Scheduled Events\n**Total:** {len(events)}"
        embed = discord.Embed(description=header, color=discord.Color.green())

        for event in events:
            status = getattr(event.status, "name", "UNKNOWN").title() if getattr(event, "status", None) else "UNKNOWN"
            user_count = getattr(event, "user_count", 0) or 0

            st = getattr(event, "start_time", None)
            if st:
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                epoch = int(st.timestamp())
                start_line = f"<t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
            else:
                start_line = "N/A"

            lines = [
                f"> **Status**: {status}",
                f"> **Start**: {start_line}",
                f"> **Interested**: {user_count}",
            ]
            desc = getattr(event, "description", None)
            if desc:
                desc_short = desc[:200] + "..." if len(desc) > 200 else desc
                lines.append(f"> **Description**: {desc_short}")
            if getattr(event, "location", None):
                lines.append(f"> **Location**: {event.location}")
            elif getattr(event, "channel", None):
                try:
                    lines.append(f"> **Channel**: {event.channel.mention}")
                except Exception:
                    pass

            name = getattr(event, "name", "Unnamed Event")
            embed.add_field(
                name=f"## `{name}`",
                value="\n".join(lines) + f"\n\n**Quick:** `[p]do event \"{name}\"`",
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
        events = await self._get_scheduled_events(ctx.guild, with_counts=True)
        event = self._event_match(events, event_name)
        if not event:
            await ctx.send(f"Event '{event_name}' not found. Use `[p]do event list` to see all events.")
            self.log_info(f"event info: not found for query={event_name!r}")
            return

        # Collect users
        interested_users = []
        try:
            async for user in event.users():
                member = ctx.guild.get_member(user.id)
                if member:
                    interested_users.append(member)
        except Exception as e:
            await ctx.send(f"Error fetching interested users: {e}")
            self.log_info(f"Error fetching users for event {getattr(event, 'id', 'unknown')}: {e}")
            return

        status = getattr(event.status, "name", "UNKNOWN").title() if getattr(event, "status", None) else "UNKNOWN"

        st = getattr(event, "start_time", None)
        if st:
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            epoch = int(st.timestamp())
            start_line = f"<t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
        else:
            start_line = "N/A"

        user_count = getattr(event, "user_count", 0) or 0
        name = getattr(event, "name", "Unnamed Event")
        desc = getattr(event, "description", None)

        title_header = f"# `{name}`"
        summary_lines = [
            f"> **Status**: {status}",
            f"> **Start**: {start_line}",
            f"> **Interested**: {user_count}",
        ]
        if desc:
            summary_lines.append(f"> **Description**: {desc[:1024]}")
        if getattr(event, "location", None):
            summary_lines.append(f"> **Location**: {event.location}")
        elif getattr(event, "channel", None):
            try:
                summary_lines.append(f"> **Channel**: {event.channel.mention}")
            except Exception:
                pass
        summary_lines.append(
            f"> **Role**: `[p]do event role create \"{name}\"` • "
            f"`[p]do event role sync \"{name}\"` • "
            f"`[p]do event role delete \"{name}\"`"
        )

        embed = discord.Embed(description=f"{title_header}\n\n" + "\n".join(summary_lines), color=discord.Color.blue())

        # Interested members
        if interested_users:
            lines = [f"{i}. {m.mention} ({m.display_name})" for i, m in enumerate(interested_users[:50], start=1)]
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
            embed.add_field(name="## Interested Members", value="> None", inline=False)

        await ctx.send(embed=embed)
        self.log_info(f"{ctx.author} viewed event info for {getattr(event, 'id', 'unknown')} in guild {ctx.guild.id}")

    @event_group.command(name="role")
    async def event_role(self, ctx, action: str, *, event_name: str):
        """
        Create, sync, or delete a role for event attendees.
        Usage: [p]do event role <create|sync|delete> <event_name>
        """
        action_l = (action or "").lower()
        if action_l not in ("create", "sync", "delete"):
            await ctx.send("Action must be 'create', 'sync', or 'delete'")
            return

        events = await self._get_scheduled_events(ctx.guild, with_counts=True)
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
            self.log_info(f"Error fetching users for event {getattr(event, 'id', 'unknown')}: {e}")
            return

        event_roles = await self.config.guild(ctx.guild).event_roles()
        event_id_str = str(getattr(event, "id", "0"))

        if action_l == "create":
            if event_id_str in event_roles:
                role = ctx.guild.get_role(event_roles[event_id_str])
                if role:
                    await ctx.send(f"Role already exists: {role.mention}")
                    return
            try:
                role = await ctx.guild.create_role(
                    name=f"Event: {getattr(event, 'name', 'Event')}",
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
                self.log_info(f"Created role {role.id} for event {event_id_str} in guild {ctx.guild.id}")
            except discord.Forbidden:
                await ctx.send("I don't have permission to create roles.")
                return

        elif action_l == "sync":
            if event_id_str not in event_roles:
                await ctx.send(f"No role exists for event **{getattr(event, 'name', 'Event')}**. Use `create` first.")
                return
            role = ctx.guild.get_role(event_roles[event_id_str])
            if not role:
                await ctx.send(f"Role no longer exists for event **{getattr(event, 'name', 'Event')}**")
                async with self.config.guild(ctx.guild).event_roles() as roles:
                    if event_id_str in roles:
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
            self.log_info(f"Synced role {role.id} for event {event_id_str} in guild {ctx.guild.id}: +{added}/-{removed}")

        elif action_l == "delete":
            if event_id_str not in event_roles:
                await ctx.send(f"No role exists for event **{getattr(event, 'name', 'Event')}**")
                return
            role = ctx.guild.get_role(event_roles[event_id_str])
            if role:
                try:
                    await role.delete(reason=f"Event role deleted by {ctx.author}")
                    await ctx.send(f"Deleted role for event **{getattr(event, 'name', 'Event')}**")
                except discord.Forbidden:
                    await ctx.send("I don't have permission to delete this role.")
                    return
            async with self.config.guild(ctx.guild).event_roles() as roles:
                if event_id_str in roles:
                    del roles[event_id_str]
            self.log_info(f"Deleted role for event {event_id_str} in guild {ctx.guild.id}")

    # ========== Debug / Logs (Owner Only) ==========

    @discoops.command(name="logs")
    @commands.is_owner()
    async def discoops_logs(self, ctx, count: Optional[int] = 10):
        """View recent in-memory logs. Default: 10"""
        try:
            count = int(count or 10)
        except Exception:
            count = 10
        count = max(1, min(count, 50))

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

# ---- Red setup compatibility (async vs sync) ----
# Red 3.5+ expects an *async* setup(bot); older Red expects a *sync* setup(bot).
# We provide both, exporting only the one that matches the runtime expectation.

# Prefer async setup if Red will await it:
try:
    # Red 3.5+ calls and awaits async setup
    async def setup(bot):
        await bot.add_cog(DiscoOps(bot))
except Exception:
    # Fallback for older Red that imports a sync setup
    def setup(bot):
        bot.add_cog(DiscoOps(bot))
