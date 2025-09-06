# File: discoops/discoops.py

import discord
from redbot.core import commands, Config
from datetime import datetime, timedelta, timezone
import asyncio
from typing import Optional
from collections import deque
import unicodedata

MAX_MSG = 1900  # stay safely below Discord's 2000 char limit

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
            return await guild.fetch_scheduled_events(with_counts=with_counts)
        except TypeError:
            try:
                return await guild.fetch_scheduled_events()
            except Exception:
                return []
        except AttributeError:
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

    @staticmethod
    async def _send_paginated(ctx, chunks, header=None, footer=None):
        """
        Send plain text chunks split below Discord's limit.
        `chunks` can be a list of strings (sections).
        """
        header = header or ""
        footer = footer or ""
        pages = []
        current = header + ("\n\n" if header else "")
        for part in chunks:
            sep = "" if current.endswith("\n") or current == "" else "\n"
            addition = f"{sep}{part}"
            if len(current) + len(addition) + (len("\n\n" + footer) if footer else 0) > MAX_MSG:
                pages.append(current.rstrip())
                current = part
            else:
                current += addition
        if current.strip():
            pages.append((current + ("\n\n" + footer if footer and len(current) + len("\n\n" + footer) <= MAX_MSG else "")).rstrip())

        # If footer didn't fit on the last page, push separately
        if footer and (not pages or not pages[-1].endswith(footer)):
            pages.append(footer)

        for page in pages:
            if page.strip():
                await ctx.send(page)

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

        # Build plain markdown sections and paginate
        header = f"# New Members\n**Range:** last **{amount} {period_l}**  •  **Found:** {len(recent)}"
        sections = []
        for (member, ja) in recent:
            epoch = int(ja.timestamp())
            block = (
                f"## `{member.display_name}`\n"
                f"> **Member**: {member.mention} ({member.display_name})\n"
                f"> **ID**: `{member.id}`\n"
                f"> **Joined**: <t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
            )
            sections.append(block)

        await self._send_paginated(ctx, sections, header=header)
        self.log_info(f"Sent recent members list ({len(recent)} found)")

    @members_group.command(name="role")
    async def members_role(self, ctx, *, role: discord.Role):
        """List all members with a specific role and show count."""
        self.log_info(f"{ctx.author} invoked 'members role' for role {role.id} in guild {ctx.guild.id}")
        members_with_role = role.members

        header = f"# Members with role\n**Role:** `{role.name}`  •  **Total:** {len(members_with_role)}"
        sections = []

        if not members_with_role:
            sections.append("## Members\n> None")
        else:
            # Chunk the member list into readable blocks
            chunk_size = 20
            for i in range(0, len(members_with_role), chunk_size):
                chunk = members_with_role[i:i + chunk_size]
                lines = [f"{i+j+1}. {m.mention} ({m.display_name})" for j, m in enumerate(chunk)]
                sections.append(f"## Members {i+1}-{i+len(chunk)}\n" + "\n".join(lines))

        role_info = (
            f"## Role Info\n"
            f"> **Created**: {role.created_at.strftime('%Y-%m-%d')}\n"
            f"> **Position**: {role.position}\n"
            f"> **Mentionable**: {'Yes' if role.mentionable else 'No'}\n"
            f"> **Color**: {str(role.color)}"
        )
        sections.append(role_info)

        await self._send_paginated(ctx, sections, header=header)
        self.log_info(f"Sent members-with-role list ({len(members_with_role)} members)")

    # ========== Event Commands (Using Discord's Scheduled Events) ==========

    @discoops.group(name="event", aliases=["events"], invoke_without_command=True)
    async def event_group(self, ctx, *, event_name: Optional[str] = None):
        """
        Event management commands.

        - `[p]do event list` — list scheduled events (plain messages, auto-paginated)
        - `[p]do event "Name"` — show summary + interested members (plain messages, auto-paginated)
        - `[p]do event role <create|sync|delete> "Name"`
        """
        if event_name:
            await self._event_info_with_members(ctx, event_name)
        else:
            await ctx.send_help(ctx.command)

    @event_group.command(name="list", aliases=["ls"])
    async def event_list(self, ctx):
        """List scheduled events as normal messages (no embed), with pagination."""
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
        sections = []
        for event in events:
            name = getattr(event, "name", "Unnamed Event")
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

            section = [
                f"## `{name}`",
                f"> **Status**: {status}",
                f"> **Start**: {start_line}",
                f"> **Interested**: {user_count}",
            ]
            desc = getattr(event, "description", None)
            if desc:
                desc_short = desc[:200] + "..." if len(desc) > 200 else desc
                section.append(f"> **Description**: {desc_short}")
            if getattr(event, "location", None):
                section.append(f"> **Location**: {event.location}")
            elif getattr(event, "channel", None):
                try:
                    section.append(f"> **Channel**: {event.channel.mention}")
                except Exception:
                    pass

            section.append(f"\n**Quick:** `[p]do event \"{name}\"`")
            sections.append("\n".join(section))

        await self._send_paginated(ctx, sections, header=header)
        self.log_info(f"{ctx.author} listed events (plain messages) in guild {ctx.guild.id}")

    @event_group.command(name="members")  # deprecated path, kept for compatibility
    async def event_members_legacy(self, ctx, *, event_name: str):
        """[Deprecated] Use: `[p]do event "Name"` instead."""
        await ctx.send("`members` is deprecated. Use: `[p]do event \"Event Name\"`.\nShowing the info below:")
        await self._event_info_with_members(ctx, event_name)

    async def _event_info_with_members(self, ctx, event_name: str):
        """Show one event summary + interested members as plain messages (auto-paginated)."""
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

        header = f"# `{name}`"
        sections = []

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
        sections.append("\n".join(summary_lines))

        # Interested members (chunk into sections)
        if interested_users:
            lines = [f"{i}. {m.mention} ({m.display_name})" for i, m in enumerate(interested_users, start=1)]
            chunk_size = 20
            for i in range(0, len(lines), chunk_size):
                chunk = lines[i:i + chunk_size]
                sections.append(f"## Interested Members {i+1}-{i+len(chunk)}\n" + "\n".join(chunk))
        else:
            sections.append("## Interested Members\n> None")

        await self._send_paginated(ctx, sections, header=header)
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
        # logs can be long; paginate too
        header = "# DiscoOps Logs"
        # split the content into sections around newlines
        raw_lines = content.split("\n")
        chunks, cur = [], ""
        for ln in raw_lines:
            nxt = (cur + ("\n" if cur else "") + ln)
            if len(nxt) > MAX_MSG:
                chunks.append(cur)
                cur = ln
            else:
                cur = nxt
        if cur:
            chunks.append(cur)
        await self._send_paginated(ctx, chunks, header=header)

    @discoops.command(name="debug")
    @commands.is_owner()
    async def discoops_debug(self, ctx):
        """Show basic debug information (owner only)."""
        g = ctx.guild
        me = g.me
        perms = g.me.guild_permissions
        msg = (
            "# DiscoOps Debug\n"
            f"**Guild**: {g.name} (ID {g.id})  •  **Members**: {g.member_count}\n\n"
            f"**Bot**: {me} (ID {me.id})\n\n"
            "## Key Permissions\n"
            f"- Manage Roles: {perms.manage_roles}\n"
            f"- Manage Guild: {perms.manage_guild}\n"
            f"- View Audit Log: {perms.view_audit_log}\n"
            f"- Send Messages: {perms.send_messages}\n"
            f"- Embed Links: {perms.embed_links}\n"
        )
        await self._send_paginated(ctx, [msg])

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
        help_md = (
            "# DiscoOps Help\n"
            "## Members\n"
            "`[p]do members new <amount> <days|weeks|months>` — List recent joins\n"
            "`[p]do members role <@role>` — List members with a role\n\n"
            "## Events\n"
            "`[p]do event list` — List scheduled events (plain messages, paginated)\n"
            "`[p]do event \"Event Name\"` — Show one event (+ members)\n"
            "`[p]do event role <create|sync|delete> \"Event Name\"` — Manage event role\n"
        )
        await self._send_paginated(ctx, [help_md])

# ---- Red setup compatibility (async vs sync) ----
try:
    async def setup(bot):
        await bot.add_cog(DiscoOps(bot))
except Exception:
    def setup(bot):
        bot.add_cog(DiscoOps(bot))
