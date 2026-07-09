# File: discoops/discoops.py

from __future__ import annotations
import discord
from redbot.core import commands, Config
from redbot.core.data_manager import cog_data_path
from datetime import datetime, timedelta, timezone
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List
import unicodedata
import os
from pathlib import Path

MAX_MSG = 1900  # stay safely below Discord's 2000 char limit
MAX_LOG_BYTES = 1_000_000  # 1 MB cap for on-disk log
MAX_LOG_DAYS = 14          # delete entries older than 14 days
CLEANUP_EVERY_WRITES = 50  # run time-based cleanup every N writes

ACTIVITY_FLUSH_SECS = 60       # batch activity counters to config this often
ACTIVITY_RETENTION_DAYS = 35   # keep daily activity buckets this long


# --- Data models for Detailed Events Wizard ---

@dataclass
class RoleDraft:
    """A role option for an event (e.g., Tank, Healer, DPS)."""
    role_id: str
    division: str = ""
    role_name: str = ""
    capacity: Optional[int] = None
    emoji: Optional[str] = None
    description: Optional[str] = None


@dataclass
class EventDraft:
    """A draft event being created via the wizard."""
    event_id: str
    guild_id: int
    creator_id: int
    title: str = ""
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    comms: List[str] = field(default_factory=lambda: ["DISCORD"])  # DISCORD | SRS
    description_md: str = ""
    image_url: Optional[str] = None
    max_attendees: Optional[int] = None
    waitlist_enabled: bool = True
    dm_on_interest: bool = True
    discussion_mode: str = "THREAD"   # or "CHANNEL"
    draft_channel_id: Optional[int] = None
    preview_message_id: Optional[int] = None
    control_message_id: Optional[int] = None
    status: str = "DRAFT"
    roles: Dict[str, RoleDraft] = field(default_factory=dict)

    # Calendar integration
    calendar_mode: str = "LINK_EXISTING"      # "LINK_EXISTING" | "NONE"
    linked_scheduled_event_id: Optional[int] = None
    linked_snapshot: dict = field(default_factory=dict)
    sync_back_to_calendar: bool = True  # toggle in Options

    # Wizard UX helpers (not persisted)
    wizard_updates_message_id: Optional[int] = None
    wizard_temp_message_ids: List[int] = field(default_factory=list)
    pending_emoji_role_id: Optional[str] = None
    divisions: List[str] = field(default_factory=list)


class DiscoOps(commands.Cog):
    """Operational features to make Discord server management easier."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=260288776360820736)
        default_guild = {
            "event_roles": {},  # Maps scheduled_event_id(str) -> role_id
            "event_posts": {},  # Maps wizard_event_id(str) -> published post data + signups
            "wizard_divisions": ["Hugin", "Munin", "Faffne", "Fenrir", "Idun"],
            "activity_enabled": True,
            # Daily engagement buckets: {"YYYY-MM-DD": {uid(str): [messages, voice_seconds]}}
            "activity_daily": {},
        }
        self.config.register_guild(**default_guild)

        # Disk logging setup
        self._log_lock = asyncio.Lock()
        self._log_writes = 0  # in-memory only; just paces periodic cleanup
        data_dir = cog_data_path(self)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = data_dir / "discoops.log"

        # Detailed Events wizard storage
        self._drafts: Dict[int, EventDraft] = {}   # key: organizer user id -> EventDraft
        self._draft_locks: Dict[str, asyncio.Lock] = {}  # key: event_id -> lock

        # Activity tracking: counters buffer in memory and flush to config
        # periodically so busy servers don't cause a config write per message.
        # buffer: guild_id -> date str -> uid str -> [messages, voice_seconds]
        self._act_buf: Dict[int, Dict[str, Dict[str, List[int]]]] = {}
        self._voice_joined: Dict[tuple, float] = {}  # (guild_id, user_id) -> time.time()
        self._act_enabled_cache: Dict[int, bool] = {}
        self._act_flush_task = asyncio.create_task(self._activity_flush_loop())

    async def cog_unload(self):
        """Stop the flush loop and persist any buffered activity."""
        self._act_flush_task.cancel()
        try:
            await self._activity_flush()
        except Exception:
            pass

    # --------- disk logger ----------
    async def log_info(self, message: str):
        """Append a log line to disk, with rotation + retention.

        All file I/O runs in a worker thread so the event loop never blocks.
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{ts}] {message}\n"
        try:
            async with self._log_lock:
                await asyncio.to_thread(self._write_log_line, line)
        except Exception:
            # Logging must never disrupt bot flow
            pass

    def _write_log_line(self, line: str):
        """Synchronous log append + rotation; call from a thread."""
        try:
            with open(self._log_path, "a", encoding="utf-8", newline="") as f:
                f.write(line)
            self._log_writes += 1

            # Size-based cleanup first (fast path)
            if self._log_path.exists() and self._log_path.stat().st_size > MAX_LOG_BYTES:
                self._truncate_to_max_bytes()

            # Time-based cleanup periodically
            if self._log_writes % CLEANUP_EVERY_WRITES == 0:
                self._time_prune_older_than(MAX_LOG_DAYS)
                # Re-enforce size cap after time prune
                if self._log_path.exists() and self._log_path.stat().st_size > MAX_LOG_BYTES:
                    self._truncate_to_max_bytes()
        except (IOError, OSError):
            # File I/O errors - don't disrupt bot flow
            pass

    def _truncate_to_max_bytes(self):
        """Trim the log file to keep only the last <= MAX_LOG_BYTES bytes aligned to lines."""
        try:
            p = self._log_path
            if not p.exists():
                return
            size = p.stat().st_size
            if size <= MAX_LOG_BYTES:
                return
            # Read tail
            with open(p, "rb") as f:
                # read last MAX_LOG_BYTES bytes (plus small margin) if file is larger
                seek_to = max(0, size - (MAX_LOG_BYTES * 2))
                f.seek(seek_to)
                tail = f.read()
            # Keep only the last MAX_LOG_BYTES from the tail, aligned to line boundary
            tail_text = tail.decode("utf-8", errors="ignore")
            # Take last MAX_LOG_BYTES worth of text
            tail_text = tail_text[-MAX_LOG_BYTES:]
            # Ensure we start at a new line
            first_nl = tail_text.find("\n")
            if first_nl != -1:
                tail_text = tail_text[first_nl + 1 :]
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(tail_text)
        except (IOError, OSError, UnicodeDecodeError):
            # File operations can fail, but we don't want to break log truncation
            pass

    def _time_prune_older_than(self, days: int):
        """Remove lines older than N days based on timestamp prefix."""
        try:
            p = self._log_path
            if not p.exists():
                return
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            kept_lines = []
            with open(p, "r", encoding="utf-8") as f:
                for ln in f:
                    # Expected format: [YYYY-MM-DD HH:MM:SS UTC] message
                    # Parse timestamp safely; if parse fails, keep the line.
                    try:
                        close = ln.find("]")
                        if ln.startswith("[") and close != -1:
                            ts_str = ln[1:close]  # e.g. 2025-09-06 22:18:15 UTC
                            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S %Z")
                            # treat naive as UTC just in case
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            if dt >= cutoff:
                                kept_lines.append(ln)
                        else:
                            kept_lines.append(ln)
                    except ValueError:
                        # Timestamp parsing failed (strptime), keep the line
                        kept_lines.append(ln)
            # Write back
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.writelines(kept_lines)
        except (IOError, OSError, UnicodeDecodeError):
            # File operations can fail during pruning
            pass

    async def _logs_tail(self, count: int) -> str:
        """Return the last `count` lines from disk, efficiently."""
        return await asyncio.to_thread(self._logs_tail_sync, count)

    def _logs_tail_sync(self, count: int) -> str:
        try:
            p = self._log_path
            if not p.exists():
                return ""
            # Read up to ~1.2MB to be safe; file is capped at 1MB anyway.
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                to_read = min(size, 1_200_000)
                f.seek(max(0, size - to_read))
                blob = f.read().decode("utf-8", errors="ignore")
            lines = [ln for ln in blob.splitlines() if ln.strip()]
            return "\n".join(lines[-count:]) if lines else ""
        except (IOError, OSError, UnicodeDecodeError):
            return ""

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
    def _quote_lines(text: str) -> str:
        """Prefix every line with '> ' to keep multi-line descriptions inside the quote."""
        if not text:
            return ""
        lines = text.splitlines()
        return "\n".join("> " + ln for ln in lines)

    @staticmethod
    async def _get_scheduled_events(guild, with_counts: bool = True):
        """Safely fetch scheduled events across discord.py versions."""
        try:
            return await guild.fetch_scheduled_events(with_counts=with_counts)
        except TypeError:
            # Older discord.py version doesn't support with_counts parameter
            try:
                return await guild.fetch_scheduled_events()
            except (discord.Forbidden, discord.HTTPException):
                return []
        except (AttributeError, discord.Forbidden, discord.HTTPException):
            # Guild doesn't have scheduled events feature or bot lacks permissions
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
    async def _send_paginated(
        ctx,
        chunks,
        header=None,
        footer=None,
        *,
        allowed_mentions: Optional[discord.AllowedMentions] = None,
        ping: Optional[str] = None,
        ping_mentions: Optional[discord.AllowedMentions] = None,
    ):
        """
        Send plain text chunks split below Discord's limit.
        `chunks` can be a list of strings (sections).

        Mentions are disabled by default to prevent mass-pings.
        If you intentionally want a ping, pass `ping="..."` (sent as a final message).
        """
        header = header or ""
        footer = footer or ""
        allowed_mentions = allowed_mentions or discord.AllowedMentions.none()
        ping_mentions = ping_mentions or discord.AllowedMentions(
            roles=True, users=False, everyone=False, replied_user=False
        )

        # Hard-split any single section that exceeds the limit; otherwise a
        # page would exceed 2000 chars and Discord rejects it with HTTP 400.
        safe_chunks = []
        for part in chunks:
            while len(part) > MAX_MSG:
                cut = part.rfind("\n", 0, MAX_MSG)
                if cut <= 0:
                    cut = MAX_MSG
                safe_chunks.append(part[:cut])
                part = part[cut:].lstrip("\n")
            if part:
                safe_chunks.append(part)

        pages = []
        current = header + ("\n\n" if header else "")
        for part in safe_chunks:
            sep = "" if current.endswith("\n") or current == "" else "\n"
            addition = f"{sep}{part}"
            if len(current) + len(addition) + (len("\n\n" + footer) if footer else 0) > MAX_MSG:
                pages.append(current.rstrip())
                current = part
            else:
                current += addition
        if current.strip():
            pages.append(
                (
                    current
                    + (
                        "\n\n" + footer
                        if footer and len(current) + len("\n\n" + footer) <= MAX_MSG
                        else ""
                    )
                ).rstrip()
            )

        # If footer didn't fit on the last page, push separately
        if footer and (not pages or not pages[-1].endswith(footer)):
            pages.append(footer)

        for page in pages:
            if page.strip():
                await ctx.send(page, allowed_mentions=allowed_mentions)

        if ping:
            await ctx.send(ping, allowed_mentions=ping_mentions)

    # --------- Detailed Events Wizard helpers ----------

    async def _defer_ephemeral(self, interaction: discord.Interaction, *, thinking: bool = True) -> bool:
        """Defer an interaction ephemerally if not already acknowledged."""
        try:
            if interaction.response.is_done():
                return False
            await interaction.response.defer(ephemeral=True, thinking=thinking)
            return True
        except Exception:
            return False

    async def _send_ephemeral(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        view: Optional[discord.ui.View] = None,
    ):
        """Send an ephemeral interaction response safely (response or followup)."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, view=view, ephemeral=True)
            else:
                await interaction.response.send_message(content, view=view, ephemeral=True)
        except Exception:
            pass

    def _build_wizard_control_content(self, draft: EventDraft, *, mode: str) -> str:
        title = (draft.title or "Untitled Event").strip()
        when = "TBD"
        try:
            if draft.starts_at:
                when = discord.utils.format_dt(draft.starts_at, style="F")
                if draft.ends_at:
                    when += f" → {discord.utils.format_dt(draft.ends_at, style='t')}"
        except Exception:
            pass

        role_count = len(draft.roles or {})
        mode_title = {
            "main": "Main",
            "roles": "Roles",
            "options": "Options",
            "publish": "Publish",
        }.get(mode, mode)

        lines = [
            f"# Event Wizard Controls",
            f"**Draft:** {title}",
            f"**Mode:** {mode_title}",
            f"**When:** {when}",
            f"**Comms:** {self._format_comms(draft)}",
            f"**Roles:** {role_count}/24",
        ]
        if draft.pending_emoji_role_id and draft.pending_emoji_role_id in (draft.roles or {}):
            r = draft.roles[draft.pending_emoji_role_id]
            if not r.emoji:
                lines.append(f"**Emoji pending:** {self._role_display_name(r)}")

        return "\n".join(lines)[:MAX_MSG]

    async def _refresh_wizard_control(self, guild: discord.Guild, draft: EventDraft, *, mode: str):
        """Edit the control panel message to reflect the latest state."""
        try:
            if not draft.control_message_id or not draft.draft_channel_id:
                return
            ch = guild.get_channel(draft.draft_channel_id)
            if not ch:
                return
            try:
                msg = await ch.fetch_message(int(draft.control_message_id))
            except Exception:
                return
            content = self._build_wizard_control_content(draft, mode=mode)
            view = self._build_wizard_control_view(draft, mode=mode)
            await msg.edit(content=content, view=view, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    def _build_wizard_control_view(self, draft: EventDraft, *, mode: str) -> discord.ui.View:
        """Build a single in-channel control panel view (no extra bot messages)."""
        outer = self

        class ControlView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=900)

            async def _check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != draft.creator_id:
                    try:
                        await inter.response.send_message("Only the organizer can use this wizard.", ephemeral=True)
                    except Exception:
                        pass
                    return False
                return True

        view = ControlView()

        # ---- main ----
        if mode == "main":
            btn_desc = discord.ui.Button(label="Edit Description", style=discord.ButtonStyle.primary)
            async def on_desc(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                await inter.response.send_modal(self._create_description_modal(draft, return_mode="main"))
            btn_desc.callback = on_desc
            view.add_item(btn_desc)

            btn_roles = discord.ui.Button(label="Roles", style=discord.ButtonStyle.secondary)
            async def on_roles(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                await inter.response.edit_message(
                    content=outer._build_wizard_control_content(draft, mode="roles"),
                    view=outer._build_wizard_control_view(draft, mode="roles"),
                )
            btn_roles.callback = on_roles
            view.add_item(btn_roles)

            btn_opts = discord.ui.Button(label="Options", style=discord.ButtonStyle.secondary)
            async def on_opts(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                await inter.response.edit_message(
                    content=outer._build_wizard_control_content(draft, mode="options"),
                    view=outer._build_wizard_control_view(draft, mode="options"),
                )
            btn_opts.callback = on_opts
            view.add_item(btn_opts)

            btn_pub = discord.ui.Button(label="Publish", style=discord.ButtonStyle.success)
            async def on_pub(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                await inter.response.edit_message(
                    content=outer._build_wizard_control_content(draft, mode="publish"),
                    view=outer._build_wizard_control_view(draft, mode="publish"),
                )
            btn_pub.callback = on_pub
            view.add_item(btn_pub)

            btn_cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)
            async def on_cancel(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                try:
                    await inter.response.defer()
                except Exception:
                    pass
                await outer._cancel_draft(inter, draft)
            btn_cancel.callback = on_cancel
            view.add_item(btn_cancel)

            return view

        # ---- roles ----
        if mode == "roles":
            div_list = [str(d).strip() for d in (draft.divisions or []) if str(d).strip()]
            if not div_list:
                div_list = ["Hugin", "Munin", "Faffne", "Fenrir", "Idun"]
            # De-dup preserving order
            seen = set()
            div_list2 = []
            for d in div_list:
                k = outer._norm_text(d)
                if k and k not in seen:
                    seen.add(k)
                    div_list2.append(d)
            div_list = div_list2[:25]
            opts = [discord.SelectOption(label=d[:100], value=d) for d in div_list]

            div_select = discord.ui.Select(placeholder="Add role: pick a division…", options=opts, min_values=1, max_values=1)
            async def on_div(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                division = div_select.values[0]
                await inter.response.send_modal(outer._create_add_role_modal(draft, division=division, return_mode="roles"))
            div_select.callback = on_div
            view.add_item(div_select)

            # Delete role select
            role_opts = []
            for rid, r in (draft.roles or {}).items():
                label = outer._role_display_name(r)
                role_opts.append(discord.SelectOption(label=label[:100], value=str(rid)))
            role_opts = role_opts[:25]
            if role_opts:
                del_select = discord.ui.Select(placeholder="Delete a role…", options=role_opts, min_values=1, max_values=1)
                async def on_del(inter: discord.Interaction):
                    if not await view._check(inter):
                        return
                    rid = del_select.values[0]
                    if rid in draft.roles:
                        r = draft.roles[rid]
                        disp = outer._role_display_name(r)
                        del draft.roles[rid]
                        if draft.pending_emoji_role_id == rid:
                            draft.pending_emoji_role_id = None
                        await outer._refresh_preview(inter.guild, draft)
                        await inter.response.edit_message(
                            content=outer._build_wizard_control_content(draft, mode="roles"),
                            view=outer._build_wizard_control_view(draft, mode="roles"),
                        )
                del_select.callback = on_del
                view.add_item(del_select)

            # Pending emoji
            if draft.pending_emoji_role_id and draft.pending_emoji_role_id in (draft.roles or {}):
                target = draft.roles[draft.pending_emoji_role_id]
                if not target.emoji:
                    btn_set = discord.ui.Button(label=f"Set Emoji: {outer._role_display_name(target)}", style=discord.ButtonStyle.secondary)

                    async def on_set(inter: discord.Interaction):
                        if not await view._check(inter):
                            return
                        await inter.response.send_modal(outer._create_set_emoji_modal(draft, role_id=draft.pending_emoji_role_id, return_mode="roles"))

                    btn_set.callback = on_set
                    view.add_item(btn_set)

                    btn_skip = discord.ui.Button(label="Skip Emoji", style=discord.ButtonStyle.secondary)
                    async def on_skip(inter: discord.Interaction):
                        if not await view._check(inter):
                            return
                        draft.pending_emoji_role_id = None
                        await outer._refresh_preview(inter.guild, draft)
                        await inter.response.edit_message(
                            content=outer._build_wizard_control_content(draft, mode="roles"),
                            view=outer._build_wizard_control_view(draft, mode="roles"),
                        )
                    btn_skip.callback = on_skip
                    view.add_item(btn_skip)

            btn_back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
            async def on_back(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                await inter.response.edit_message(
                    content=outer._build_wizard_control_content(draft, mode="main"),
                    view=outer._build_wizard_control_view(draft, mode="main"),
                )
            btn_back.callback = on_back
            view.add_item(btn_back)
            return view

        # ---- options ----
        if mode == "options":
            comms_opts = [
                discord.SelectOption(label="Discord", value="DISCORD", default=("DISCORD" in (draft.comms or []))),
                discord.SelectOption(label="SRS", value="SRS", default=("SRS" in (draft.comms or []))),
            ]
            comms_select = discord.ui.Select(placeholder="Comms", options=comms_opts, min_values=1, max_values=2)
            async def on_comms(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                draft.comms = list(comms_select.values)
                await outer._refresh_preview(inter.guild, draft)
                await inter.response.edit_message(
                    content=outer._build_wizard_control_content(draft, mode="options"),
                    view=outer._build_wizard_control_view(draft, mode="options"),
                )
            comms_select.callback = on_comms
            view.add_item(comms_select)

            cal_opts = [
                discord.SelectOption(label="Link existing", value="LINK_EXISTING", default=(draft.calendar_mode == "LINK_EXISTING")),
                discord.SelectOption(label="No calendar", value="NONE", default=(draft.calendar_mode == "NONE")),
            ]
            cal_select = discord.ui.Select(placeholder="Calendar behavior", options=cal_opts, min_values=1, max_values=1)
            async def on_cal(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                draft.calendar_mode = cal_select.values[0]
                if draft.calendar_mode != "LINK_EXISTING":
                    draft.linked_scheduled_event_id = None
                await outer._refresh_preview(inter.guild, draft)
                await inter.response.edit_message(
                    content=outer._build_wizard_control_content(draft, mode="options"),
                    view=outer._build_wizard_control_view(draft, mode="options"),
                )
            cal_select.callback = on_cal
            view.add_item(cal_select)

            sync_btn = discord.ui.Button(
                label=("Sync edits to calendar: ON" if draft.sync_back_to_calendar else "Sync edits to calendar: OFF"),
                style=discord.ButtonStyle.secondary,
            )
            async def on_sync(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                draft.sync_back_to_calendar = not draft.sync_back_to_calendar
                await outer._refresh_preview(inter.guild, draft)
                await inter.response.edit_message(
                    content=outer._build_wizard_control_content(draft, mode="options"),
                    view=outer._build_wizard_control_view(draft, mode="options"),
                )
            sync_btn.callback = on_sync
            view.add_item(sync_btn)

            btn_back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
            async def on_back(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                await inter.response.edit_message(
                    content=outer._build_wizard_control_content(draft, mode="main"),
                    view=outer._build_wizard_control_view(draft, mode="main"),
                )
            btn_back.callback = on_back
            view.add_item(btn_back)
            return view

        # ---- publish ----
        if mode == "publish":
            guild = outer.bot.get_guild(draft.guild_id)
            if not guild:
                back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
                async def on_back(inter: discord.Interaction):
                    if not await view._check(inter):
                        return
                    await inter.response.edit_message(
                        content=outer._build_wizard_control_content(draft, mode="main"),
                        view=outer._build_wizard_control_view(draft, mode="main"),
                    )
                back.callback = on_back
                view.add_item(back)
                return view

            me = getattr(guild, "me", None)
            eligible = []
            for ch in getattr(guild, "text_channels", []) or []:
                try:
                    perms = ch.permissions_for(me) if me else None
                    if not perms:
                        continue
                    if not (perms.view_channel and perms.send_messages and perms.read_message_history):
                        continue
                    eligible.append(ch)
                except Exception:
                    continue

            by_cat: Dict[str, List[discord.TextChannel]] = {}
            for ch in eligible:
                key = str(ch.category_id) if ch.category_id else "none"
                by_cat.setdefault(key, []).append(ch)
            for k in by_cat:
                by_cat[k].sort(key=lambda c: c.position)

            cat_opts = []
            for cat in sorted(getattr(guild, "categories", []) or [], key=lambda c: c.position):
                if str(cat.id) in by_cat:
                    cat_opts.append(discord.SelectOption(label=cat.name[:100], value=str(cat.id)))
            if "none" in by_cat:
                cat_opts.append(discord.SelectOption(label="No Category", value="none"))
            cat_opts = cat_opts[:25]

            # If too many categories/channels exist, user can still use the Search button on the list
            # command or temporarily move channels into a category.

            # Create selects
            cat_select = discord.ui.Select(placeholder="Pick a category…", options=cat_opts, min_values=1, max_values=1)
            chan_select = discord.ui.Select(placeholder="Pick a channel…", options=[], min_values=1, max_values=1, disabled=True)
            pub_btn = discord.ui.Button(label="Publish Now", style=discord.ButtonStyle.success, disabled=True)
            back_btn = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)

            state = {"cat": None, "chan": None}

            async def on_cat(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                state["cat"] = cat_select.values[0]
                chans = by_cat.get(str(state["cat"]), [])
                opts = []
                for c in chans[:25]:
                    label = ("#" + c.name)[:100]
                    desc = (c.category.name if c.category else "No Category")[:100]
                    opts.append(discord.SelectOption(label=label, value=str(c.id), description=desc))
                chan_select.options = opts
                chan_select.disabled = not bool(opts)
                state["chan"] = None
                pub_btn.disabled = True
                await inter.response.edit_message(view=view)

            async def on_chan(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                state["chan"] = chan_select.values[0]
                pub_btn.disabled = False
                await inter.response.edit_message(view=view)

            async def on_publish_click(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                if not state.get("chan"):
                    return await inter.response.send_message("Pick a channel first.", ephemeral=True)
                # Acknowledge quickly to avoid "This interaction failed".
                await outer._defer_ephemeral(inter, thinking=True)
                await outer._publish_to_channel(inter, draft, channel_id=int(state["chan"]))

            async def on_back(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                await inter.response.edit_message(
                    content=outer._build_wizard_control_content(draft, mode="main"),
                    view=outer._build_wizard_control_view(draft, mode="main"),
                )

            cat_select.callback = on_cat
            chan_select.callback = on_chan
            pub_btn.callback = on_publish_click
            back_btn.callback = on_back

            view.add_item(cat_select)
            view.add_item(chan_select)
            view.add_item(pub_btn)
            view.add_item(back_btn)
            return view

        # Fallback
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        async def on_back(inter: discord.Interaction):
            if not await view._check(inter):
                return
            await inter.response.edit_message(
                content=outer._build_wizard_control_content(draft, mode="main"),
                view=outer._build_wizard_control_view(draft, mode="main"),
            )
        back.callback = on_back
        view.add_item(back)
        return view

    @staticmethod
    def _role_display_name(r: RoleDraft) -> str:
        div = (r.division or "").strip()
        rn = (r.role_name or "").strip()
        if div and rn:
            return f"{div} — {rn}"
        if rn:
            return rn
        if div:
            return div
        return "Role"

    @staticmethod
    def _role_to_dict(r: RoleDraft) -> dict:
        return {
            "role_id": r.role_id,
            "division": r.division,
            "role_name": r.role_name,
            "capacity": r.capacity,
            "emoji": r.emoji,
            "description": r.description,
        }

    @staticmethod
    def _role_from_dict(d: dict) -> RoleDraft:
        # Back-compat: older stored roles used "label".
        div = d.get("division", None)
        rn = d.get("role_name", None)
        if div is None and rn is None:
            rn = str(d.get("label") or "")
            div = ""
        return RoleDraft(
            role_id=str(d.get("role_id") or ""),
            division=str(div or ""),
            role_name=str(rn or ""),
            capacity=d.get("capacity", None),
            emoji=d.get("emoji", None),
            description=d.get("description", None),
        )

    def _public_view_custom_id(self, action: str, event_id: str) -> str:
        # Keep this stable; used by on_interaction router.
        return f"evtpub:{action}:{event_id}"

    def _build_public_markdown(self, post: dict) -> str:
        title = (post.get("title") or "Untitled Event").strip()
        starts_at = post.get("starts_at_ts")
        ends_at = post.get("ends_at_ts")
        comms = post.get("comms") or []
        comms_fmt = " + ".join(
            ["Discord" if c == "DISCORD" else "SRS" if c == "SRS" else str(c) for c in comms]
        ) or "TBD"

        when = "TBD"
        try:
            if starts_at:
                when = f"<t:{int(starts_at)}:F>"
                if ends_at:
                    when += f" → <t:{int(ends_at)}:t>"
        except Exception:
            pass

        # Counts
        signups = post.get("signups") or {}
        counts: Dict[str, int] = {}
        for _uid, rid in signups.items():
            rid = str(rid or "")
            if not rid:
                continue
            counts[rid] = counts.get(rid, 0) + 1

        roles = post.get("roles") or {}
        role_lines = []
        for rid, rd in roles.items():
            try:
                rd_obj = self._role_from_dict(rd)
            except Exception:
                continue
            occupied = counts.get(str(rid), 0)
            cap = rd_obj.capacity
            cap_str = "∞" if cap is None else str(cap)
            if cap is None:
                slot_str = f"{occupied}/{cap_str}"
            else:
                open_slots = max(0, int(cap) - occupied)
                slot_str = f"{occupied}/{cap_str} ({open_slots} open)"
            base = self._role_display_name(rd_obj)
            label = f"{rd_obj.emoji} {base}" if rd_obj.emoji else base
            extra = f" — {rd_obj.description}" if rd_obj.description else ""
            role_lines.append(f"- {label} — {slot_str}{extra}")

        interested = post.get("interested") or []
        interested_count = len(interested)

        header = f"# {title}"
        top = (
            f"**When:** {when}\n"
            f"**Comms:** {comms_fmt}\n"
            f"**Interested:** {interested_count}"
        )
        if role_lines:
            roles_block = "\n".join(["\n## Roles"] + role_lines)
        else:
            roles_block = "\n## Roles\n- None"

        footer = "\n\n> Use the buttons below to register interest and pick a role."
        out = f"{header}\n{top}{roles_block}{footer}"

        # Ensure we don't exceed the safe bound; description is sent separately.
        return out[:MAX_MSG]

    async def _update_published_post_message(self, guild: discord.Guild, event_id: str):
        try:
            posts = await self.config.guild(guild).event_posts()
            post = posts.get(str(event_id))
            if not post:
                return
            channel_id = post.get("channel_id")
            message_id = post.get("message_id")
            if not channel_id or not message_id:
                return
            ch = guild.get_channel(int(channel_id))
            if not ch:
                return
            try:
                msg = await ch.fetch_message(int(message_id))
            except Exception:
                return
            content = self._build_public_markdown(post)
            view = self._build_public_view(event_id=str(event_id), disabled=False)
            await msg.edit(content=content, embed=None, view=view, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    @staticmethod
    def _format_comms(draft: EventDraft) -> str:
        vals = set(draft.comms or [])
        if not vals:
            return "TBD"
        parts = []
        if "DISCORD" in vals:
            parts.append("Discord")
        if "SRS" in vals:
            parts.append("SRS")
        return " + ".join(parts) if parts else "TBD"

    def _draft_lock(self, event_id: str) -> asyncio.Lock:
        """Get or create a lock for a specific event draft."""
        self._draft_locks.setdefault(event_id, asyncio.Lock())
        return self._draft_locks[event_id]

    def _new_event_id_values(self, guild_id: int, author_id: int) -> str:
        """Generate a unique event ID for a new draft."""
        return f"ev-{guild_id}-{author_id}-{int(datetime.now(timezone.utc).timestamp())}"

    def _build_preview_embed(self, draft: EventDraft) -> discord.Embed:
        """Build the preview embed for an event draft."""
        title = draft.title or "Untitled Event"
        e = discord.Embed(title=f"📝 {title} • DRAFT", colour=discord.Colour.blurple())
        when = "TBD"
        if draft.starts_at:
            when = discord.utils.format_dt(draft.starts_at, style="F")
            if draft.ends_at:
                when += f" → {discord.utils.format_dt(draft.ends_at, style='t')}"
        e.description = (
            f"**Status:** DRAFT (not published)\n"
            f"**When:** {when}\n"
            f"**Comms:** {self._format_comms(draft)}\n"
            f"**Calendar Link:** {'Linked' if draft.linked_scheduled_event_id else 'None'}\n\n"
            f"{draft.description_md[:1800] or '*No description yet.*'}"
        )
        if draft.image_url:
            e.set_image(url=draft.image_url)

        # Roles summary
        if draft.roles:
            role_lines = []
            for r in draft.roles.values():
                cap = "∞" if r.capacity is None else r.capacity
                base = self._role_display_name(r)
                label = f"{r.emoji} {base}" if r.emoji else base
                extra = f" — {r.description}" if r.description else ""
                role_lines.append(f"• {label} ({cap}){extra}")
            e.add_field(name="Roles", value="\n".join(role_lines)[:1024], inline=False)

        e.set_footer(text=f"Preview • Event ID: {draft.event_id}")
        return e

    async def _refresh_preview(self, guild: discord.Guild, draft: EventDraft):
        """Refresh the preview message for an event draft."""
        if not draft.preview_message_id or not draft.draft_channel_id:
            return
        channel = guild.get_channel(draft.draft_channel_id)
        if not channel:
            return
        embed = self._build_preview_embed(draft)
        try:
            msg = await channel.fetch_message(draft.preview_message_id)
            await msg.edit(embed=embed, view=None)
        except discord.NotFound:
            new_msg = await channel.send(embed=embed, view=None)
            draft.preview_message_id = new_msg.id

    async def _resolve_scheduled_event(self, guild: discord.Guild, ev_id: int) -> Optional[discord.GuildScheduledEvent]:
        """Resolve a scheduled event by id, with fallbacks."""
        try:
            return await guild.fetch_scheduled_event(ev_id)
        except AttributeError:
            events = await self._get_scheduled_events(guild, with_counts=False)
            return discord.utils.get(events, id=ev_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            events = await self._get_scheduled_events(guild, with_counts=False)
            return discord.utils.get(events, id=ev_id)

    async def _create_draft_with_preview(
        self,
        *,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        organizer: discord.abc.User,
        preset_title: Optional[str] = None,
    ) -> EventDraft:
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            raise RuntimeError("Channel has no id")
        event_id = self._new_event_id_values(guild.id, organizer.id)
        draft = EventDraft(
            event_id=event_id,
            guild_id=guild.id,
            creator_id=organizer.id,
            draft_channel_id=channel_id,
            title=preset_title or "",
        )
        self._drafts[organizer.id] = draft

        try:
            divs = await self.config.guild(guild).wizard_divisions()
            draft.divisions = [str(d).strip() for d in (divs or []) if str(d).strip()]
        except Exception:
            draft.divisions = ["Hugin", "Munin", "Faffne", "Fenrir", "Idun"]
        embed = self._build_preview_embed(draft)
        preview = await channel.send(embed=embed, view=None)
        draft.preview_message_id = preview.id

        ctrl_content = self._build_wizard_control_content(draft, mode="main")
        ctrl_view = self._build_wizard_control_view(draft, mode="main")
        ctrl = await channel.send(ctrl_content, view=ctrl_view, allowed_mentions=discord.AllowedMentions.none())
        draft.control_message_id = ctrl.id
        return draft

    async def _open_scheduled_event_picker(
        self,
        dest: discord.abc.Messageable,
        guild: discord.Guild,
        organizer_id: int,
        channel_id: int,
    ):
        """Open the scheduled event picker; draft is created after selection.

        `dest` is anything with .send (Context or channel) so both the prefix
        command and the hub can launch the wizard.
        """
        scheduled: List[discord.GuildScheduledEvent] = await self._get_scheduled_events(guild, with_counts=False)

        outer = self

        class EventPicker(discord.ui.View):
            def __init__(self, scheduled_events: List[discord.GuildScheduledEvent]):
                super().__init__(timeout=300)
                self.scheduled_events = list(scheduled_events) if scheduled_events else []
                if self.scheduled_events:
                    self.add_item(self._build_select())

            def _build_select(self) -> discord.ui.Select:
                opts = []
                for ev in self.scheduled_events[:25]:
                    label = (ev.name or "Untitled")[:100]
                    starts = ev.start_time.strftime("%Y-%m-%d %H:%M") if ev.start_time else "TBD"
                    desc = f"{starts} • {str(ev.entity_type).split('.')[-1].title()}"
                    opts.append(discord.SelectOption(label=label, description=desc[:100], value=str(ev.id)))
                select = discord.ui.Select(placeholder="Pick a scheduled event…", options=opts)

                async def on_select(interaction: discord.Interaction):
                    if interaction.user.id != organizer_id:
                        return await outer._send_ephemeral(interaction, "Only the organizer can select an event for this wizard.")
                    await outer._defer_ephemeral(interaction)
                    ev_id = int(select.values[0])
                    ev = await outer._resolve_scheduled_event(interaction.guild, ev_id)
                    if not ev:
                        return await outer._send_ephemeral(interaction, "That scheduled event is no longer available. Use **Refresh list** and try again.")

                    try:
                        ch = interaction.guild.get_channel(channel_id) or interaction.channel
                        if not ch:
                            raise RuntimeError("Missing channel")
                        draft = await outer._create_draft_with_preview(
                            guild=interaction.guild,
                            channel=ch,
                            organizer=interaction.user,
                            preset_title=ev.name or "",
                        )
                        await outer._hydrate_draft_from_scheduled(interaction, draft, ev)
                    except Exception as e:
                        await outer.log_info(f"wizard import failed (dropdown): user={interaction.user.id} guild={interaction.guild.id} ev_id={ev_id} err={e!r}")
                        await outer._send_ephemeral(interaction, "Something went wrong while importing that event. Try again (or use Paste event ID/URL).")

                    try:
                        if interaction.message:
                            await interaction.message.edit(view=None)
                    except Exception:
                        pass

                select.callback = on_select
                return select

            @discord.ui.button(label="Refresh list", style=discord.ButtonStyle.secondary)
            async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != organizer_id:
                    return await outer._send_ephemeral(interaction, "Only the organizer can refresh.")
                new_list = await outer._get_scheduled_events(interaction.guild, with_counts=False)
                # Create a new view instead of trying to re-add buttons
                new_view = EventPicker(new_list if new_list else [])
                await interaction.response.edit_message(view=new_view)

            @discord.ui.button(label="Paste event ID/URL", style=discord.ButtonStyle.primary)
            async def paste(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != organizer_id:
                    return await outer._send_ephemeral(interaction, "Only the organizer can paste.")
                await outer._open_paste_event_modal(interaction, channel_id)

            @discord.ui.button(label="Create without calendar", style=discord.ButtonStyle.secondary)
            async def no_cal(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != organizer_id:
                    return await outer._send_ephemeral(interaction, "Only the organizer can continue.")

                await outer._defer_ephemeral(interaction)
                try:
                    ch = interaction.guild.get_channel(channel_id) or interaction.channel
                    if not ch:
                        raise RuntimeError("Missing channel")
                    draft = await outer._create_draft_with_preview(
                        guild=interaction.guild,
                        channel=ch,
                        organizer=interaction.user,
                    )
                    draft.calendar_mode = "NONE"
                    draft.linked_scheduled_event_id = None
                    await outer._refresh_preview(interaction.guild, draft)
                    await outer._refresh_wizard_control(interaction.guild, draft, mode="main")
                except Exception as e:
                    await outer.log_info(f"wizard create without calendar failed: user={interaction.user.id} guild={interaction.guild.id} err={e!r}")
                    await outer._send_ephemeral(interaction, "Something went wrong creating the draft. Try again.")

        # Send the picker prompt
        await dest.send(
            "Select a scheduled event to import details:",
            view=EventPicker(scheduled),
            delete_after=300,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _open_paste_event_modal(self, interaction: discord.Interaction, channel_id: int):
        """Open a modal to paste an event ID or URL."""
        outer = self

        class PasteModal(discord.ui.Modal, title="Link Scheduled Event"):
            ev_input = discord.ui.TextInput(label="Event ID or URL", required=True, max_length=200)

            async def on_submit(self, inter: discord.Interaction):
                await outer._defer_ephemeral(inter)

                raw = (self.ev_input.value or "").strip()
                ev_id = None
                tokenized = raw.replace("<", "").replace(">", "").split("/")
                for token in tokenized:
                    if token.isdigit():
                        ev_id = int(token)
                if not ev_id:
                    return await outer._send_ephemeral(inter, "Couldn't find an event ID in that input.")

                ev = await outer._resolve_scheduled_event(inter.guild, ev_id)
                if not ev:
                    return await outer._send_ephemeral(inter, "No scheduled event with that ID.")

                try:
                    ch = inter.guild.get_channel(channel_id) or inter.channel
                    if not ch:
                        raise RuntimeError("Missing channel")
                    draft = await outer._create_draft_with_preview(
                        guild=inter.guild,
                        channel=ch,
                        organizer=inter.user,
                        preset_title=ev.name or "",
                    )
                    await outer._hydrate_draft_from_scheduled(inter, draft, ev)
                except Exception as e:
                    await outer.log_info(f"wizard import failed (paste): user={inter.user.id} guild={inter.guild.id} ev_id={ev_id} err={e!r}")
                    await outer._send_ephemeral(inter, "Something went wrong while importing that event. Try again.")

        await interaction.response.send_modal(PasteModal())

    async def _hydrate_draft_from_scheduled(self, interaction: discord.Interaction, draft: EventDraft, ev: discord.GuildScheduledEvent):
        """Import details from a scheduled event into the draft."""
        await self._defer_ephemeral(interaction)
        if not ev:
            return await self._send_ephemeral(interaction, "That scheduled event is no longer available. Try refreshing and selecting again.")

        draft.calendar_mode = "LINK_EXISTING"
        draft.linked_scheduled_event_id = ev.id

        # Prevent multiple wizard posts for the same scheduled event.
        try:
            existing = await self._find_existing_post_for_scheduled(interaction.guild, ev.id)
            if existing:
                ch_id = existing.get("channel_id")
                msg_id = existing.get("message_id")
                jump = ""
                try:
                    if ch_id and msg_id:
                        jump = f"https://discord.com/channels/{interaction.guild.id}/{int(ch_id)}/{int(msg_id)}"
                except Exception:
                    jump = ""
                msg = "A wizard post already exists for that scheduled event. Delete it first if you want to recreate it."
                if jump:
                    msg += f"\n\nExisting post: {jump}"
                await self._send_ephemeral(interaction, msg)
                await self._cleanup_wizard_messages(interaction.guild, draft)
                self._drafts.pop(draft.creator_id, None)
                return
        except Exception:
            pass
        draft.title = ev.name or draft.title or "Untitled Event"
        draft.starts_at = ev.start_time
        draft.ends_at = ev.end_time

        draft.description_md = (ev.description or "").strip()

        try:
            if hasattr(ev, "image") and ev.image:
                draft.image_url = ev.image.url
        except Exception:
            pass

        if ev.creator:
            draft.linked_snapshot["calendar_creator_id"] = ev.creator.id

        draft.linked_snapshot.update({
            "name": ev.name,
            "start": ev.start_time.isoformat() if ev.start_time else None,
            "end": ev.end_time.isoformat() if ev.end_time else None,
            "entity_type": str(ev.entity_type),
            "location": getattr(ev.entity_metadata, "location", None) if ev.entity_metadata else None,
            "description": ev.description,
            "image": getattr(ev.image, "url", None) if hasattr(ev, "image") and ev.image else None,
        })

        await self._refresh_preview(interaction.guild, draft)
        await self._refresh_wizard_control(interaction.guild, draft, mode="main")

    def _create_description_modal(self, draft: EventDraft, *, return_mode: str = "main"):
        """Create a description modal for the given draft."""
        outer = self

        class DescriptionModal(discord.ui.Modal):
            def __init__(self):
                super().__init__(title="Event Description")

            description = discord.ui.TextInput(
                label="Long description (markdown ok)",
                style=discord.TextStyle.long,
                required=False,
                max_length=4000,
                placeholder="Add details, agenda, requirements, links…",
                default=draft.description_md or ""
            )

            async def on_submit(self, inter: discord.Interaction):
                draft.description_md = (self.description.value or "")
                try:
                    await inter.response.defer()
                except Exception:
                    pass
                await outer._refresh_preview(inter.guild, draft)
                await outer._refresh_wizard_control(inter.guild, draft, mode=return_mode)

        return DescriptionModal()

    def _create_add_role_modal(self, draft: EventDraft, *, division: str, return_mode: str = "roles") -> discord.ui.Modal:
        """Step 2: enter role name + capacity."""
        outer = self
        division = (division or "").strip()

        class AddRoleModal(discord.ui.Modal):
            def __init__(self):
                super().__init__(title="Add Division Role")

            role_name = discord.ui.TextInput(label=f"Role in {division}", required=True, max_length=50)
            capacity = discord.ui.TextInput(label="Capacity (blank = unlimited)", required=False, max_length=6)

            async def on_submit(self, inter: discord.Interaction):
                if len(draft.roles) >= 24:
                    return await inter.response.send_message("Max roles reached (24).", ephemeral=True)

                rn = (self.role_name.value or "").strip()
                if not rn:
                    return await inter.response.send_message("Role name is required.", ephemeral=True)

                # Validate duplicate (division, role_name)
                dn = outer._norm_text(division)
                rnn = outer._norm_text(rn)
                for existing in draft.roles.values():
                    if outer._norm_text(existing.division) == dn and outer._norm_text(existing.role_name) == rnn:
                        return await inter.response.send_message(
                            f"That role already exists: **{division} — {rn}**",
                            ephemeral=True,
                        )

                cap_raw = (self.capacity.value or "").strip()
                cap_val: Optional[int] = None
                if cap_raw:
                    try:
                        cap_val = max(0, int(cap_raw))
                    except ValueError:
                        return await inter.response.send_message("Capacity must be a number.", ephemeral=True)

                # len()+1 would collide with surviving IDs after a delete
                # (r1,r2,r3 minus r1 -> next would be r3 again), so derive
                # the next ID from the highest existing index instead.
                max_idx = 0
                for existing_rid in draft.roles:
                    try:
                        max_idx = max(max_idx, int(str(existing_rid).lstrip("r")))
                    except ValueError:
                        continue
                rid = f"r{max_idx + 1}"
                rd = RoleDraft(
                    role_id=rid,
                    division=division,
                    role_name=rn,
                    capacity=cap_val,
                )
                draft.roles[rid] = rd
                draft.pending_emoji_role_id = rid

                disp = outer._role_display_name(rd)
                try:
                    await inter.response.defer()
                except Exception:
                    pass

                await outer._refresh_preview(inter.guild, draft)
                await outer._refresh_wizard_control(inter.guild, draft, mode=return_mode)

        return AddRoleModal()

    def _create_set_emoji_modal(self, draft: EventDraft, *, role_id: str, return_mode: str = "roles") -> discord.ui.Modal:
        outer = self
        role_id = str(role_id or "")

        class SetEmojiModal(discord.ui.Modal):
            def __init__(self):
                super().__init__(title="Set Role Emoji")

            emoji = discord.ui.TextInput(label="Emoji", required=True, max_length=64, placeholder="React emoji or custom emoji")

            async def on_submit(self, inter: discord.Interaction):
                e = (self.emoji.value or "").strip()
                if role_id in (draft.roles or {}):
                    draft.roles[role_id].emoji = e or None
                if draft.pending_emoji_role_id == role_id:
                    draft.pending_emoji_role_id = None
                try:
                    await inter.response.defer()
                except Exception:
                    pass
                await outer._refresh_preview(inter.guild, draft)
                await outer._refresh_wizard_control(inter.guild, draft, mode=return_mode)

        return SetEmojiModal()

    async def _cleanup_wizard_messages(self, guild: discord.Guild, draft: EventDraft):
        """Best-effort cleanup of wizard messages in the draft channel."""
        try:
            if not draft.draft_channel_id:
                return
            ch = guild.get_channel(draft.draft_channel_id)
            if not ch:
                return

            ids = set()
            if draft.preview_message_id:
                ids.add(int(draft.preview_message_id))
            if draft.control_message_id:
                ids.add(int(draft.control_message_id))
            if draft.wizard_updates_message_id:
                ids.add(int(draft.wizard_updates_message_id))
            for mid in (draft.wizard_temp_message_ids or []):
                try:
                    ids.add(int(mid))
                except Exception:
                    pass

            for mid in ids:
                try:
                    msg = await ch.fetch_message(mid)
                    await msg.delete()
                except Exception:
                    pass
        except Exception:
            pass

    async def _find_existing_post_for_scheduled(self, guild: discord.Guild, scheduled_event_id: int) -> Optional[dict]:
        try:
            posts = await self.config.guild(guild).event_posts()
            for _eid, post in (posts or {}).items():
                try:
                    if int(post.get("linked_scheduled_event_id") or 0) == int(scheduled_event_id):
                        return post
                except Exception:
                    continue
        except Exception:
            pass
        return None

    async def _publish_to_channel(self, interaction: discord.Interaction, draft: EventDraft, *, channel_id: int):
        guild = interaction.guild

        # The channel may have been deleted (or perms changed) since the picker was built.
        target = guild.get_channel(int(channel_id))
        if target is None:
            return await self._send_ephemeral(
                interaction,
                "That channel is no longer available. Pick another destination and try again.",
            )

        # Prevent multiple wizard posts for the same scheduled event.
        if draft.linked_scheduled_event_id:
            existing = await self._find_existing_post_for_scheduled(guild, draft.linked_scheduled_event_id)
            if existing:
                ch_id = existing.get("channel_id")
                msg_id = existing.get("message_id")
                jump = ""
                try:
                    if ch_id and msg_id:
                        jump = f"https://discord.com/channels/{guild.id}/{int(ch_id)}/{int(msg_id)}"
                except Exception:
                    jump = ""
                msg = "An event post already exists for that scheduled event."
                if jump:
                    msg += f"\n\nExisting post: {jump}"
                return await self._send_ephemeral(interaction, msg)

        async with self._draft_lock(draft.event_id):
            try:
                detailed_msg = await self._post_canonical_event(guild, draft, channel_hint=channel_id)
            except (discord.Forbidden, discord.HTTPException, RuntimeError) as e:
                await self.log_info(f"publish failed: user={interaction.user.id} guild={guild.id} channel={channel_id} err={e!r}")
                return await self._send_ephemeral(
                    interaction,
                    "Publishing failed — I couldn't post in that channel. Check my permissions there and try again.",
                )

            # Optional: sync key fields back to calendar if linked & toggle is on
            if draft.sync_back_to_calendar and draft.calendar_mode == "LINK_EXISTING" and draft.linked_scheduled_event_id:
                try:
                    cal = await self._resolve_scheduled_event(guild, draft.linked_scheduled_event_id)
                    if cal:
                        await cal.edit(
                            name=draft.title or cal.name,
                            start_time=draft.starts_at or cal.start_time,
                            end_time=draft.ends_at or cal.end_time,
                            description=(draft.description_md[:1000] or None),
                        )
                except Exception:
                    pass

            # Cleanup wizard messages
            await self._cleanup_wizard_messages(guild, draft)

            # Cleanup transient draft
            self._drafts.pop(draft.creator_id, None)
            draft.status = "PUBLISHED"

            await self._send_ephemeral(interaction, f"Event published. Jump: {detailed_msg.jump_url}")
            await self.log_info(f"{interaction.user} published event {draft.event_id} in guild {guild.id}")

    async def _cancel_draft(self, interaction: discord.Interaction, draft: EventDraft):
        """Cancel and delete the draft."""
        await self._cleanup_wizard_messages(interaction.guild, draft)
        self._drafts.pop(draft.creator_id, None)
        await self._send_ephemeral(interaction, "Draft canceled.")
        await self.log_info(f"{interaction.user} canceled draft {draft.event_id} in guild {interaction.guild.id}")

    async def _post_canonical_event(self, guild: discord.Guild, draft: EventDraft, channel_hint: Optional[int] = None) -> discord.Message:
        """Post the final published event message."""
        channel = guild.get_channel(channel_hint) if channel_hint else None
        if channel is None:
            channel = next(iter(guild.text_channels), None)
        if channel is None:
            raise RuntimeError(f"No usable channel to publish event {draft.event_id}")
        post = {
            "event_id": draft.event_id,
            "guild_id": guild.id,
            "title": draft.title or "Untitled Event",
            "starts_at_ts": int(draft.starts_at.timestamp()) if draft.starts_at else None,
            "ends_at_ts": int(draft.ends_at.timestamp()) if draft.ends_at else None,
            "comms": list(draft.comms or []),
            "description_md": draft.description_md or "",
            "image_url": draft.image_url,
            "linked_scheduled_event_id": draft.linked_scheduled_event_id,
            "roles": {rid: self._role_to_dict(r) for rid, r in (draft.roles or {}).items()},
            "interested": [],
            "signups": {},
        }

        content = self._build_public_markdown(post)
        view = self._build_public_view(event_id=draft.event_id, disabled=False)
        msg = await channel.send(content=content, view=view, allowed_mentions=discord.AllowedMentions.none())

        # Store for later updates
        post["channel_id"] = channel.id
        post["message_id"] = msg.id
        async with self.config.guild(guild).event_posts() as posts:
            posts[str(draft.event_id)] = post

        # Long description as follow-ups (no buttons)
        desc = (draft.description_md or "").strip()
        detail_ids: List[int] = []
        if desc:
            # Keep it markdown, but split for safety
            chunks, cur = [], ""
            for ln in desc.splitlines() or [desc]:
                nxt = (cur + ("\n" if cur else "") + ln)
                if len(nxt) > MAX_MSG:
                    if cur:
                        chunks.append(cur)
                    cur = ln
                else:
                    cur = nxt
            if cur:
                chunks.append(cur)
            for i, chnk in enumerate(chunks[:5], start=1):
                header = f"## Details" if i == 1 else "## Details (continued)"
                dmsg = await channel.send(
                    f"{header}\n{chnk}"[:MAX_MSG],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                try:
                    detail_ids.append(dmsg.id)
                except Exception:
                    pass

        if detail_ids:
            try:
                async with self.config.guild(guild).event_posts() as posts:
                    post2 = posts.get(str(draft.event_id)) or {}
                    post2["details_message_ids"] = detail_ids
                    posts[str(draft.event_id)] = post2
            except Exception:
                pass

        # Create a discussion thread from the message if configured
        if draft.discussion_mode == "THREAD":
            try:
                await msg.create_thread(name=f"{draft.title} • Discussion")
            except Exception:
                pass
        return msg

    def _build_public_view(self, *, event_id: str, disabled: bool = False) -> discord.ui.View:
        """Build the public view for a published event."""
        class PublicView(discord.ui.View):
            def __init__(self, is_disabled: bool):
                super().__init__(timeout=None)
                self.add_item(
                    discord.ui.Button(
                        label="Interested",
                        style=discord.ButtonStyle.primary,
                        custom_id=outer._public_view_custom_id("interest", str(event_id)),
                        disabled=is_disabled,
                    )
                )
                self.add_item(
                    discord.ui.Button(
                        label="Sign Up / Manage Role",
                        style=discord.ButtonStyle.success,
                        custom_id=outer._public_view_custom_id("signup", str(event_id)),
                        disabled=is_disabled,
                    )
                )
                self.add_item(
                    discord.ui.Button(
                        label="View Details",
                        style=discord.ButtonStyle.secondary,
                        custom_id=outer._public_view_custom_id("view", str(event_id)),
                        disabled=is_disabled,
                    )
                )

        outer = self
        return PublicView(disabled)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Route published event button interactions (markdown post buttons)."""
        try:
            if not interaction or not getattr(interaction, "data", None):
                return
            data = interaction.data or {}
            custom_id = data.get("custom_id")
            if not custom_id or not isinstance(custom_id, str):
                return
            if not custom_id.startswith("evtpub:"):
                return
            parts = custom_id.split(":")
            if len(parts) < 3:
                return
            action = parts[1]
            event_id = ":".join(parts[2:])

            if not interaction.guild:
                return await self._send_ephemeral(interaction, "This can only be used in a server.")

            if action == "interest":
                await self._handle_public_interest(interaction, event_id)
            elif action == "signup":
                await self._handle_public_signup(interaction, event_id)
            elif action == "view":
                await self._handle_public_view_details(interaction, event_id)
        except Exception:
            # Never let interaction routing crash the cog
            pass

    async def _handle_public_interest(self, interaction: discord.Interaction, event_id: str):
        guild = interaction.guild
        uid = interaction.user.id
        # Acknowledge within Discord's 3s window before touching config/state.
        await self._defer_ephemeral(interaction, thinking=False)
        async with self.config.guild(guild).event_posts() as posts:
            post = posts.get(str(event_id))
            if not post:
                return await self._send_ephemeral(interaction, "This event post is no longer tracked.")
            interested = post.get("interested") or []
            if uid in interested:
                interested = [i for i in interested if i != uid]
                post["interested"] = interested
                posts[str(event_id)] = post
                await self._send_ephemeral(interaction, "You are no longer marked as interested.")
            else:
                interested.append(uid)
                post["interested"] = interested
                posts[str(event_id)] = post
                await self._send_ephemeral(interaction, "Marked you as interested.")

        await self._update_published_post_message(guild, event_id)

    async def _handle_public_signup(self, interaction: discord.Interaction, event_id: str):
        guild = interaction.guild
        await self._defer_ephemeral(interaction, thinking=False)
        posts = await self.config.guild(guild).event_posts()
        post = posts.get(str(event_id))
        if not post:
            return await self._send_ephemeral(interaction, "This event post is no longer tracked.")

        roles = post.get("roles") or {}
        if not roles:
            return await self._send_ephemeral(interaction, "No roles are configured for this event.")

        # Select menus only support 25 options; reserve one for withdraw.
        if len(roles) > 24:
            return await self._send_ephemeral(
                interaction,
                "This event has too many roles to show in the signup menu (max: 24). Ask an organizer to reduce the role list.",
            )

        outer = self

        # Build options
        withdraw_value = "__withdraw__"
        options = [discord.SelectOption(label="Withdraw / No role", value=withdraw_value, description="Remove your signup")]
        for rid, rd in roles.items():
            try:
                r = self._role_from_dict(rd)
            except Exception:
                continue
            cap = "∞" if r.capacity is None else str(r.capacity)
            base = self._role_display_name(r)
            label = f"{r.emoji} {base}" if r.emoji else base
            desc = f"Cap: {cap}" + (f" • {r.description}" if r.description else "")
            options.append(discord.SelectOption(label=label[:100], value=str(rid), description=desc[:100]))

        class SignupView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)

            @discord.ui.select(placeholder="Pick your role…", options=options, min_values=1, max_values=1)
            async def select_role(self, inter: discord.Interaction, select: discord.ui.Select):
                if inter.user.id != interaction.user.id:
                    return await inter.response.send_message("This menu is only for you.", ephemeral=True)
                chosen = select.values[0]
                await outer._apply_signup_choice(inter, event_id=str(event_id), role_id=str(chosen))
                try:
                    self.stop()
                except Exception:
                    pass

        await self._send_ephemeral(interaction, "Choose a role:", view=SignupView())

    async def _apply_signup_choice(self, interaction: discord.Interaction, *, event_id: str, role_id: str):
        guild = interaction.guild
        uid_str = str(interaction.user.id)

        # Acknowledge before acquiring the config lock; under concurrent clicks
        # the lock wait could otherwise exceed Discord's 3s response window.
        await self._defer_ephemeral(interaction, thinking=False)

        async with self.config.guild(guild).event_posts() as posts:
            post = posts.get(str(event_id))
            if not post:
                return await self._send_ephemeral(interaction, "This event post is no longer tracked.")
            roles = post.get("roles") or {}
            signups = post.get("signups") or {}

            prev = str(signups.get(uid_str) or "")

            is_withdraw = (role_id or "") == "__withdraw__"

            # Capacity enforcement (only if changing into a role)
            if role_id and not is_withdraw:
                rd = roles.get(str(role_id))
                if not rd:
                    return await self._send_ephemeral(interaction, "That role is no longer available.")
                r = self._role_from_dict(rd)
                if r.capacity is not None and str(role_id) != prev:
                    occ = sum(1 for rid in signups.values() if str(rid or "") == str(role_id))
                    if occ >= int(r.capacity):
                        return await self._send_ephemeral(
                            interaction,
                            f"That role is full (**{occ}/{r.capacity}**).",
                        )

            # Apply
            if is_withdraw or not role_id:
                if uid_str in signups:
                    del signups[uid_str]
                post["signups"] = signups
                posts[str(event_id)] = post
                await self._send_ephemeral(interaction, "Signup removed.")
            else:
                signups[uid_str] = str(role_id)
                post["signups"] = signups
                posts[str(event_id)] = post

                rd = roles.get(str(role_id)) or {}
                r = self._role_from_dict(rd)
                await self._send_ephemeral(interaction, f"Signed you up as **{self._role_display_name(r)}**.")

        await self._update_published_post_message(guild, event_id)

    async def _handle_public_view_details(self, interaction: discord.Interaction, event_id: str):
        guild = interaction.guild
        await self._defer_ephemeral(interaction, thinking=False)
        posts = await self.config.guild(guild).event_posts()
        post = posts.get(str(event_id))
        if not post:
            return await self._send_ephemeral(interaction, "This event post is no longer tracked.")
        title = post.get("title") or "Untitled Event"
        desc = (post.get("description_md") or "").strip()
        if not desc:
            desc = "*No details provided.*"
        # Ephemeral also has message limits; show a safe slice.
        msg = f"# {title}\n\n{desc}"[:MAX_MSG]
        await self._send_ephemeral(interaction, msg)

    # ========== Activity Tracking ==========

    def _act_bump(self, guild_id: int, user_id: int, *, msgs: int = 0, voice: int = 0):
        """Add counts to the in-memory buffer (pure dict ops, no I/O)."""
        date_key = datetime.now(timezone.utc).date().isoformat()
        day = self._act_buf.setdefault(guild_id, {}).setdefault(date_key, {})
        cur = day.setdefault(str(user_id), [0, 0])
        cur[0] += msgs
        cur[1] += voice

    async def _activity_enabled(self, guild: discord.Guild) -> bool:
        gid = guild.id
        if gid not in self._act_enabled_cache:
            try:
                self._act_enabled_cache[gid] = bool(await self.config.guild(guild).activity_enabled())
            except Exception:
                self._act_enabled_cache[gid] = True
        return self._act_enabled_cache[gid]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Count text engagement. Only counters are stored — never content."""
        try:
            if not message.guild or message.author.bot:
                return
            if not await self._activity_enabled(message.guild):
                return
            self._act_bump(message.guild.id, message.author.id, msgs=1)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Track time spent in voice channels (AFK channel doesn't count)."""
        try:
            if member.bot or not member.guild:
                return
            if not await self._activity_enabled(member.guild):
                return
            afk = member.guild.afk_channel
            was_active = before.channel is not None and before.channel != afk
            is_active = after.channel is not None and after.channel != afk
            key = (member.guild.id, member.id)
            now = time.time()
            if is_active:
                # Joining (or moving between) tracked channels: keep one open session.
                self._voice_joined.setdefault(key, now)
            elif was_active:
                joined = self._voice_joined.pop(key, None)
                if joined is not None:
                    elapsed = int(now - joined)
                    if elapsed > 0:
                        self._act_bump(member.guild.id, member.id, voice=elapsed)
        except Exception:
            pass

    async def _activity_flush_loop(self):
        try:
            while True:
                await asyncio.sleep(ACTIVITY_FLUSH_SECS)
                try:
                    await self._activity_flush()
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def _activity_flush(self):
        """Credit open voice sessions, then persist buffered counters."""
        now = time.time()

        # Credit ongoing voice sessions up to now and restart their clocks, so
        # long sessions accrue continuously and survive an unclean shutdown.
        for key, joined in list(self._voice_joined.items()):
            elapsed = int(now - joined)
            if elapsed > 0:
                self._act_bump(key[0], key[1], voice=elapsed)
                self._voice_joined[key] = now

        # Self-heal: adopt members already in voice that we have no session for
        # (e.g. they joined before the cog loaded or after a restart).
        for guild in getattr(self.bot, "guilds", []) or []:
            try:
                if not await self._activity_enabled(guild):
                    continue
                afk = guild.afk_channel
                for ch in guild.voice_channels:
                    if ch == afk:
                        continue
                    for m in ch.members:
                        if m.bot:
                            continue
                        self._voice_joined.setdefault((guild.id, m.id), now)
            except Exception:
                continue

        if not self._act_buf:
            return
        buf, self._act_buf = self._act_buf, {}

        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=ACTIVITY_RETENTION_DAYS)).isoformat()
        for gid, days in buf.items():
            try:
                async with self.config.guild_from_id(gid).activity_daily() as store:
                    for date_key, users in days.items():
                        day = store.setdefault(date_key, {})
                        for uid, (dm, dv) in users.items():
                            cur = day.get(uid) or [0, 0]
                            day[uid] = [int(cur[0]) + dm, int(cur[1]) + dv]
                    # Retention: ISO dates compare lexicographically.
                    for date_key in [d for d in store if d < cutoff]:
                        del store[date_key]
            except Exception:
                continue

    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m"
        return f"{seconds}s"

    def _activity_window(self, guild_id: int, store: dict, days: int) -> Dict[str, List[int]]:
        """Aggregate per-user [msgs, voice_secs] over the last N days,
        including counts still sitting in the unflushed buffer."""
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=max(1, days) - 1)).isoformat()
        totals: Dict[str, List[int]] = {}

        def merge(day_data: dict):
            for uid, vals in (day_data or {}).items():
                try:
                    dm, dv = int(vals[0]), int(vals[1])
                except (TypeError, ValueError, IndexError):
                    continue
                cur = totals.setdefault(str(uid), [0, 0])
                cur[0] += dm
                cur[1] += dv

        for date_key, day_data in (store or {}).items():
            if date_key >= cutoff:
                merge(day_data)
        for date_key, day_data in self._act_buf.get(guild_id, {}).items():
            if date_key >= cutoff:
                merge(day_data)
        return totals

    def _display_user(self, guild: discord.Guild, uid: str) -> str:
        member = guild.get_member(int(uid))
        if member:
            return f"{member.mention} ({member.display_name})"
        return f"`{uid}` (left server)"

    async def _activity_overview_report(self, dest, guild: discord.Guild, days: int = 7):
        """Summary of engagement over the last N days."""
        store = await self.config.guild(guild).activity_daily()
        totals = self._activity_window(guild.id, store, days)

        total_msgs = sum(v[0] for v in totals.values())
        total_voice = sum(v[1] for v in totals.values())
        text_users = sum(1 for v in totals.values() if v[0] > 0)
        voice_users = sum(1 for v in totals.values() if v[1] > 0)
        in_voice_now = sum(
            len([m for m in ch.members if not m.bot])
            for ch in guild.voice_channels
            if ch != guild.afk_channel
        )

        header = f"# Server Activity — last {days} days"
        summary = (
            f"> **Messages:** {total_msgs} from **{text_users}** members\n"
            f"> **Voice time:** {self._fmt_duration(total_voice)} from **{voice_users}** members\n"
            f"> **Engaged members (text or voice):** {len([v for v in totals.values() if v[0] or v[1]])}\n"
            f"> **In voice right now:** {in_voice_now}"
        )
        sections = [summary]

        top_text = sorted(totals.items(), key=lambda kv: kv[1][0], reverse=True)[:5]
        top_text = [(u, v) for u, v in top_text if v[0] > 0]
        if top_text:
            lines = [f"{i}. {self._display_user(guild, u)} — {v[0]} messages" for i, (u, v) in enumerate(top_text, 1)]
            sections.append("## Top text\n" + "\n".join(lines))

        top_voice = sorted(totals.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
        top_voice = [(u, v) for u, v in top_voice if v[1] > 0]
        if top_voice:
            lines = [f"{i}. {self._display_user(guild, u)} — {self._fmt_duration(v[1])}" for i, (u, v) in enumerate(top_voice, 1)]
            sections.append("## Top voice\n" + "\n".join(lines))

        if not top_text and not top_voice:
            sections.append("## No activity recorded yet\n> Tracking starts when members send messages or join voice after the cog loads.")

        await self._send_paginated(dest, sections, header=header)

    async def _activity_top_report(self, dest, guild: discord.Guild, days: int = 7, count: int = 10):
        """Leaderboards over the last N days."""
        days = max(1, min(int(days), ACTIVITY_RETENTION_DAYS))
        count = max(1, min(int(count), 25))
        store = await self.config.guild(guild).activity_daily()
        totals = self._activity_window(guild.id, store, days)

        header = f"# Most Active Members — last {days} days"
        sections = []

        top_text = [(u, v) for u, v in sorted(totals.items(), key=lambda kv: kv[1][0], reverse=True) if v[0] > 0][:count]
        sections.append(
            "## By messages\n" + ("\n".join(
                f"{i}. {self._display_user(guild, u)} — {v[0]} messages" for i, (u, v) in enumerate(top_text, 1)
            ) if top_text else "> None recorded")
        )

        top_voice = [(u, v) for u, v in sorted(totals.items(), key=lambda kv: kv[1][1], reverse=True) if v[1] > 0][:count]
        sections.append(
            "## By voice time\n" + ("\n".join(
                f"{i}. {self._display_user(guild, u)} — {self._fmt_duration(v[1])}" for i, (u, v) in enumerate(top_voice, 1)
            ) if top_voice else "> None recorded")
        )

        await self._send_paginated(dest, sections, header=header)

    async def _activity_user_report(self, dest, guild: discord.Guild, member: discord.Member, days: int = 30):
        """Engagement stats for one member."""
        days = max(1, min(int(days), ACTIVITY_RETENTION_DAYS))
        store = await self.config.guild(guild).activity_daily()
        uid = str(member.id)

        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
        msgs = voice = 0
        active_days = set()
        last_active = None
        for date_key, day_data in (store or {}).items():
            vals = (day_data or {}).get(uid)
            if not vals:
                continue
            if date_key >= cutoff:
                try:
                    msgs += int(vals[0])
                    voice += int(vals[1])
                    active_days.add(date_key)
                except (TypeError, ValueError, IndexError):
                    continue
            if last_active is None or date_key > last_active:
                last_active = date_key
        # include unflushed buffer
        for date_key, day_data in self._act_buf.get(guild.id, {}).items():
            vals = day_data.get(uid)
            if vals and date_key >= cutoff:
                msgs += vals[0]
                voice += vals[1]
                active_days.add(date_key)
                if last_active is None or date_key > last_active:
                    last_active = date_key

        vs = member.voice
        in_voice = vs.channel.mention if vs and vs.channel else "No"

        header = f"# Activity — {member.display_name}"
        section = (
            f"> **Member:** {member.mention}\n"
            f"> **Window:** last {days} days\n"
            f"> **Messages:** {msgs}\n"
            f"> **Voice time:** {self._fmt_duration(voice)}\n"
            f"> **Active days:** {len(active_days)}\n"
            f"> **Last active:** {last_active or 'never recorded'}\n"
            f"> **In voice now:** {in_voice}"
        )
        await self._send_paginated(dest, [section], header=header)

    async def _voice_now_report(self, dest, guild: discord.Guild):
        """Live snapshot of who is in voice right now."""
        sections = []
        total = 0
        for ch in guild.voice_channels:
            members = [m for m in ch.members if not m.bot]
            if not members:
                continue
            total += len(members)
            suffix = " *(AFK — not counted)*" if ch == guild.afk_channel else ""
            lines = [f"{i}. {m.mention} ({m.display_name})" for i, m in enumerate(members, 1)]
            sections.append(f"## 🔊 {ch.name} — {len(members)}{suffix}\n" + "\n".join(lines))

        header = f"# In Voice Right Now\n**Total:** {total}"
        if not sections:
            sections.append("## Channels\n> Nobody is in voice right now.")
        await self._send_paginated(dest, sections, header=header)

    # ========== Hub (primary interface) ==========

    async def _open_hub(self, ctx: commands.Context):
        content, view = await self._build_hub(ctx.guild, ctx.author.id, "main", prefix=ctx.clean_prefix)
        await ctx.send(content, view=view, allowed_mentions=discord.AllowedMentions.none())
        await self.log_info(f"{ctx.author} opened the hub in guild {ctx.guild.id}")

    async def _build_hub(self, guild: discord.Guild, invoker_id: int, mode: str, prefix: str = "[p]"):
        """Build the hub message content + view for a mode.

        The hub is one message that swaps its content/components as you
        navigate, mirroring the event wizard's control panel pattern.
        """
        outer = self

        class HubView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=900)
                self.sel_event_name: Optional[str] = None

            async def _check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != invoker_id:
                    try:
                        await inter.response.send_message(
                            f"Only the person who opened this menu can use it. Run `{prefix}do` to get your own.",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
                    return False
                return True

        view = HubView()

        async def switch_mode(inter: discord.Interaction, target_mode: str):
            """Ack first, then rebuild — building may involve HTTP calls
            (e.g. fetching scheduled events), and the interaction must be
            acknowledged within 3 seconds or Discord fails the click."""
            try:
                await inter.response.defer()
            except Exception:
                pass
            try:
                content2, view2 = await outer._build_hub(guild, invoker_id, target_mode, prefix=prefix)
                if inter.message:
                    await inter.message.edit(content=content2, view=view2)
            except Exception as e:
                await outer.log_info(f"hub switch to {target_mode!r} failed: guild={guild.id} err={e!r}")
                await outer._send_ephemeral(inter, "Couldn't open that section — try again. (Details are in the bot logs.)")

        def add_nav(label: str, target_mode: str, *, style=discord.ButtonStyle.secondary, emoji=None):
            btn = discord.ui.Button(label=label, style=style, emoji=emoji)

            async def cb(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                await switch_mode(inter, target_mode)

            btn.callback = cb
            view.add_item(btn)
            return btn

        def add_report_button(label: str, runner, *, style=discord.ButtonStyle.secondary, emoji=None):
            """Button that acknowledges silently, then posts a report in the channel."""
            btn = discord.ui.Button(label=label, style=style, emoji=emoji)

            async def cb(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                try:
                    await inter.response.defer()
                except Exception:
                    pass
                try:
                    await runner(inter)
                except Exception as e:
                    await outer.log_info(f"hub action {label!r} failed: guild={guild.id} err={e!r}")
                    await outer._send_ephemeral(inter, "Something went wrong running that action.")

            btn.callback = cb
            view.add_item(btn)
            return btn

        # ---- main ----
        if mode == "main":
            content = (
                "# DiscoOps\n"
                "Server ops hub — pick a section:\n\n"
                "> 📅 **Events** — scheduled events, attendee roles, detailed event posts\n"
                "> 👥 **Members** — recent joins, members by role\n"
                "> 📊 **Activity** — engagement stats for text and voice\n\n"
                f"-# Everything here also exists as text commands — `{prefix}do help`."
            )
            add_nav("Events", "events", style=discord.ButtonStyle.primary, emoji="📅")
            add_nav("Members", "members", style=discord.ButtonStyle.primary, emoji="👥")
            add_nav("Activity", "activity", style=discord.ButtonStyle.primary, emoji="📊")

            close_btn = discord.ui.Button(label="Close", style=discord.ButtonStyle.danger)

            async def on_close(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                try:
                    await inter.response.defer()
                    if inter.message:
                        await inter.message.delete()
                except Exception:
                    pass

            close_btn.callback = on_close
            view.add_item(close_btn)
            return content, view

        # ---- events ----
        if mode == "events":
            events = await self._get_scheduled_events(guild, with_counts=False)
            content = (
                "# DiscoOps — Events\n"
                f"**Scheduled events:** {len(events)}\n\n"
                "Pick an event, then use the buttons: **Details** shows the summary and "
                "interested members; the **Role** buttons manage an attendee role for it.\n"
                "**New Event Post** starts the detailed post wizard."
            )

            if events:
                opts = []
                for ev in events[:25]:
                    label = (ev.name or "Untitled")[:100]
                    starts = ev.start_time.strftime("%Y-%m-%d %H:%M") if ev.start_time else "TBD"
                    opts.append(discord.SelectOption(label=label, description=starts[:100], value=str(ev.id)))
                ev_select = discord.ui.Select(placeholder="Pick an event…", options=opts, min_values=1, max_values=1)
                names_by_id = {str(ev.id): (ev.name or "Untitled") for ev in events}

                async def on_pick(inter: discord.Interaction):
                    if not await view._check(inter):
                        return
                    view.sel_event_name = names_by_id.get(ev_select.values[0])
                    for o in ev_select.options:
                        o.default = o.value == ev_select.values[0]
                    await inter.response.edit_message(
                        content=content + f"\n\n**Selected:** {view.sel_event_name}",
                        view=view,
                    )

                ev_select.callback = on_pick
                view.add_item(ev_select)

            def needs_event(runner):
                async def wrapped(inter: discord.Interaction):
                    if not view.sel_event_name:
                        return await outer._send_ephemeral(inter, "Pick an event from the dropdown first.")
                    await runner(inter)
                return wrapped

            add_report_button(
                "Details", needs_event(
                    lambda inter: outer._event_info_with_members(inter.channel, guild, view.sel_event_name)
                ),
                style=discord.ButtonStyle.primary,
            )
            add_report_button(
                "Role: Create", needs_event(
                    lambda inter: outer._event_role_action(inter.channel, guild, inter.user, "create", view.sel_event_name, False)
                ),
            )
            add_report_button(
                "Role: Sync", needs_event(
                    lambda inter: outer._event_role_action(inter.channel, guild, inter.user, "sync", view.sel_event_name, False)
                ),
            )
            add_report_button(
                "Role: Delete", needs_event(
                    lambda inter: outer._event_role_action(inter.channel, guild, inter.user, "delete", view.sel_event_name, False)
                ),
            )
            add_report_button(
                "List All", lambda inter: outer._event_list_report(inter.channel, guild),
            )
            add_report_button(
                "New Event Post",
                lambda inter: outer._open_scheduled_event_picker(inter.channel, guild, inter.user.id, inter.channel.id),
                style=discord.ButtonStyle.success,
                emoji="📝",
            )
            add_nav("Back", "main")
            return content, view

        # ---- members ----
        if mode == "members":
            content = (
                "# DiscoOps — Members\n"
                "> **New joins** — pick a time range to list recent joins\n"
                "> **Members with role** — pick a role to list everyone holding it"
            )

            role_select = discord.ui.RoleSelect(placeholder="Members with role…", min_values=1, max_values=1)

            async def on_role(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                role = role_select.values[0]
                try:
                    await inter.response.defer()
                except Exception:
                    pass
                await outer.log_info(f"{inter.user} listed members with role {role.id} via hub in guild {guild.id}")
                await outer._members_role_report(inter.channel, role)

            role_select.callback = on_role
            view.add_item(role_select)

            # Preset ranges as a dropdown. (Discord modals only allow text
            # fields, so the dropdown lives in the view; Custom… opens the
            # old form for anything not covered.)
            joins_opts = [
                discord.SelectOption(label="Last 24 hours", value="1:days", emoji="🆕"),
                discord.SelectOption(label="Last 3 days", value="3:days"),
                discord.SelectOption(label="Last 7 days", value="7:days"),
                discord.SelectOption(label="Last 2 weeks", value="2:weeks"),
                discord.SelectOption(label="Last 30 days", value="30:days"),
                discord.SelectOption(label="Last 3 months", value="3:months"),
                discord.SelectOption(label="Custom…", value="custom", description="Enter your own amount and period"),
            ]
            joins_select = discord.ui.Select(placeholder="New joins: pick a range…", options=joins_opts, min_values=1, max_values=1)

            async def on_joins(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                choice = joins_select.values[0]

                if choice == "custom":
                    class NewJoinsModal(discord.ui.Modal, title="New Joins Report"):
                        amount = discord.ui.TextInput(label="Amount", placeholder="7", max_length=4)
                        period = discord.ui.TextInput(
                            label="Period (days / weeks / months)", placeholder="days", max_length=10
                        )

                        async def on_submit(self, m_inter: discord.Interaction):
                            try:
                                amt = max(1, int(str(self.amount.value).strip()))
                            except ValueError:
                                return await outer._send_ephemeral(m_inter, "Amount must be a number, e.g. `7`.")
                            try:
                                await m_inter.response.defer()
                            except Exception:
                                pass
                            await outer._members_new_report(m_inter.channel, guild, amt, str(self.period.value).strip())

                    return await inter.response.send_modal(NewJoinsModal())

                amt_str, period = choice.split(":", 1)
                try:
                    await inter.response.defer()
                except Exception:
                    pass
                await outer._members_new_report(inter.channel, guild, int(amt_str), period)

            joins_select.callback = on_joins
            view.add_item(joins_select)
            add_nav("Back", "main")
            return content, view

        # ---- activity ----
        if mode == "activity":
            enabled = await self._activity_enabled(guild)
            content = (
                "# DiscoOps — Activity\n"
                f"**Tracking:** {'ON' if enabled else 'OFF'} — message counts and voice time, no content stored.\n\n"
                "> **Overview** — 7-day engagement summary\n"
                "> **Top Members** — 30-day leaderboards\n"
                "> **Voice Now** — who is in voice right now\n"
                "> Or pick a member below for their individual stats."
            )

            user_select = discord.ui.UserSelect(placeholder="Member stats…", min_values=1, max_values=1)

            async def on_user(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                target = user_select.values[0]
                member = target if isinstance(target, discord.Member) else guild.get_member(target.id)
                if member is None:
                    return await outer._send_ephemeral(inter, "That user isn't in this server.")
                try:
                    await inter.response.defer()
                except Exception:
                    pass
                await outer._activity_user_report(inter.channel, guild, member)

            user_select.callback = on_user
            view.add_item(user_select)

            add_report_button(
                "Overview", lambda inter: outer._activity_overview_report(inter.channel, guild, days=7),
                style=discord.ButtonStyle.primary, emoji="📈",
            )
            add_report_button(
                "Top Members", lambda inter: outer._activity_top_report(inter.channel, guild, days=30),
                emoji="🏆",
            )
            add_report_button(
                "Voice Now", lambda inter: outer._voice_now_report(inter.channel, guild),
                emoji="🔊",
            )

            toggle_btn = discord.ui.Button(
                label=f"Tracking: {'ON' if enabled else 'OFF'}",
                style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger,
            )

            async def on_toggle(inter: discord.Interaction):
                if not await view._check(inter):
                    return
                new_val = not await outer._activity_enabled(guild)
                await outer.config.guild(guild).activity_enabled.set(new_val)
                outer._act_enabled_cache[guild.id] = new_val
                if not new_val:
                    for key in [k for k in outer._voice_joined if k[0] == guild.id]:
                        outer._voice_joined.pop(key, None)
                await outer.log_info(f"{inter.user} set activity tracking to {new_val} in guild {guild.id}")
                await switch_mode(inter, "activity")

            toggle_btn.callback = on_toggle
            view.add_item(toggle_btn)
            add_nav("Back", "main")
            return content, view

        # Fallback: main menu
        return await self._build_hub(guild, invoker_id, "main", prefix=prefix)

    # ============= Command Group =============

    # invoke_without_command=True stops Red's automatic help menu from being
    # sent alongside the hub when `[p]do` is run bare.
    @commands.group(name="do", aliases=["discoops"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def discoops(self, ctx):
        """DiscoOps main command group. Bare `[p]do` opens the interactive hub."""
        if ctx.invoked_subcommand is None:
            await self._open_hub(ctx)

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
        await self.log_info(f"{ctx.author} invoked 'members new' with amount={amount}, period={period} in guild {ctx.guild.id}")
        await self._members_new_report(ctx, ctx.guild, amount, period)

    async def _members_new_report(self, dest, guild: discord.Guild, amount: int, period: str):
        """Core report for recent joins; `dest` needs .send and .typing."""
        period_l = (period or "").lower()
        if period_l not in ("days", "day", "weeks", "week", "months", "month"):
            await dest.send(
                "Period must be 'days', 'weeks', or 'months'",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.log_info("Invalid period provided to 'members new'")
            return

        if period_l in ("days", "day"):
            delta = timedelta(days=amount)
        elif period_l in ("weeks", "week"):
            delta = timedelta(weeks=amount)
        else:
            delta = timedelta(days=amount * 30)

        cutoff_date = datetime.now(timezone.utc) - delta

        # Access members (requires Server Members Intent); chunking a large
        # guild can take a while, so show a typing indicator meanwhile.
        try:
            async with dest.typing():
                members = list(guild.members)
                if not members:
                    try:
                        await guild.chunk()
                        members = list(guild.members)
                    except discord.HTTPException:
                        # Chunking failed, continue with empty list
                        pass
        except AttributeError as e:
            # Programming error - guild.members not available
            members = []
            await self.log_info(f"AttributeError accessing guild members: {e}")
        except discord.Forbidden as e:
            # Permission error - missing Server Members Intent
            members = []
            await self.log_info(f"Forbidden error accessing guild members: {e}")

        if not members:
            await dest.send(
                "I couldn't access the member list. Ensure **Server Members Intent** is enabled and the bot has cached members.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.log_info("members list empty or inaccessible; likely missing Server Members Intent")
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
            await self.log_info(f"Error filtering recent members: {e}")
            await dest.send(
                "An error occurred while reading member join dates.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        recent.sort(key=lambda tup: tup[1], reverse=True)

        if not recent:
            await dest.send(
                f"ℹ️ No members joined in the last {amount} {period_l}.\n\n"
                f"**Note:** Make sure the bot has been running and has cached member data. "
                f"Members who joined before the bot was added won't be tracked.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.log_info("No recent members found")
            return

        # Build plain markdown sections and paginate
        header = f"# New Members\n**Range:** last **{amount} {period_l}**  •  **Found:** {len(recent)}"
        sections = []
        for (member, ja) in recent:
            epoch = int(ja.timestamp())
            block = (
                f"## {member.display_name}\n"
                f"> **Member**: {member.mention} ({member.display_name})\n"
                f"> **ID**: `{member.id}`\n"
                f"> **Joined**: <t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
            )
            sections.append(block)

        await self._send_paginated(dest, sections, header=header)
        await self.log_info(f"Sent recent members list ({len(recent)} found)")

    @members_group.command(name="role")
    async def members_role(self, ctx, *, role: discord.Role):
        """List all members with a specific role and show count."""
        await self.log_info(f"{ctx.author} invoked 'members role' for role {role.id} in guild {ctx.guild.id}")
        await self._members_role_report(ctx, role)

    async def _members_role_report(self, dest, role: discord.Role):
        """Core report for members holding a role; `dest` needs .send."""
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

        await self._send_paginated(dest, sections, header=header)
        await self.log_info(f"Sent members-with-role list ({len(members_with_role)} members)")

    # ========== Activity Commands ==========

    @discoops.group(name="activity", aliases=["act"], invoke_without_command=True)
    async def activity_group(self, ctx):
        """
        Server activity tracking (message counts + voice time; no content stored).

        - `[p]do activity` — 7-day overview
        - `[p]do activity top [days]` — leaderboards
        - `[p]do activity user <member> [days]` — one member's stats
        - `[p]do activity voice` — who is in voice right now
        - `[p]do activity toggle` — enable/disable tracking
        """
        if ctx.invoked_subcommand is None:
            await self._activity_overview_report(ctx, ctx.guild, days=7)

    @activity_group.command(name="top")
    async def activity_top(self, ctx, days: Optional[int] = 7, count: Optional[int] = 10):
        """Most active members over the last N days (default 7)."""
        await self._activity_top_report(ctx, ctx.guild, days=days or 7, count=count or 10)

    @activity_group.command(name="user")
    async def activity_user(self, ctx, member: discord.Member, days: Optional[int] = 30):
        """Activity stats for one member (default window: 30 days)."""
        await self._activity_user_report(ctx, ctx.guild, member, days=days or 30)

    @activity_group.command(name="voice")
    async def activity_voice(self, ctx):
        """Show who is in a voice channel right now."""
        await self._voice_now_report(ctx, ctx.guild)

    @activity_group.command(name="toggle")
    async def activity_toggle(self, ctx):
        """Enable or disable activity tracking for this server."""
        current = await self.config.guild(ctx.guild).activity_enabled()
        new_val = not current
        await self.config.guild(ctx.guild).activity_enabled.set(new_val)
        self._act_enabled_cache[ctx.guild.id] = new_val
        if not new_val:
            # Stop open voice sessions; don't credit further time while disabled.
            for key in [k for k in self._voice_joined if k[0] == ctx.guild.id]:
                self._voice_joined.pop(key, None)
        await ctx.send(
            f"Activity tracking is now **{'ON' if new_val else 'OFF'}** for this server.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.log_info(f"{ctx.author} set activity tracking to {new_val} in guild {ctx.guild.id}")

    # ========== Event Commands (Using Discord's Scheduled Events) ==========

    @discoops.group(name="event", aliases=["events"], invoke_without_command=True)
    async def event_group(self, ctx, *, event_name: Optional[str] = None):
        """
        Event management commands.

        - `[p]do event list` — list scheduled events (plain messages, auto-paginated)
        - `[p]do event "Name"` — show summary + interested members (plain messages, auto-paginated)
        - `[p]do event role <create|sync|delete> "Name"`
        - `[p]do event create` — start the detailed event wizard
        """
        if event_name:
            await self._event_info_with_members(ctx, ctx.guild, event_name)
        else:
            await ctx.send_help(ctx.command)

    @event_group.command(name="create")
    async def event_create(self, ctx: commands.Context):
        """
        Start the detailed event wizard.

        This wizard lets you create a detailed event post by:
        1. Optionally linking an existing Discord Scheduled Event
        2. Editing the description
        3. Adding custom role options (e.g., Tank, Healer, DPS)
        4. Configuring options (calendar sync)
        5. Publishing the event

        Usage: [p]do event create
        """
        await self.log_info(f"{ctx.author} started event wizard in guild {ctx.guild.id}")
        await self._open_scheduled_event_picker(ctx, ctx.guild, ctx.author.id, ctx.channel.id)

    @event_group.group(name="wizard", invoke_without_command=True)
    async def event_wizard_group(self, ctx: commands.Context):
        """Manage published Event Wizard posts."""
        await ctx.send_help(ctx.command)

    @event_wizard_group.command(name="list")
    async def event_wizard_list(self, ctx: commands.Context):
        """List published Event Wizard posts tracked in config."""
        posts = await self.config.guild(ctx.guild).event_posts()
        posts = posts or {}
        header = f"# Wizard Events\n**Tracked:** {len(posts)}"
        sections = []
        if not posts:
            sections.append("## Events\n> None")
        else:
            for event_id, post in posts.items():
                title = (post.get("title") or "Untitled").strip()
                ch_id = post.get("channel_id")
                msg_id = post.get("message_id")
                linked = post.get("linked_scheduled_event_id")
                interested = len(post.get("interested") or [])
                signups = len(post.get("signups") or {})
                jump = ""
                try:
                    if ch_id and msg_id:
                        jump = f"https://discord.com/channels/{ctx.guild.id}/{int(ch_id)}/{int(msg_id)}"
                except Exception:
                    jump = ""

                ch_mention = "(missing channel)"
                try:
                    ch = ctx.guild.get_channel(int(ch_id)) if ch_id else None
                    if ch:
                        ch_mention = ch.mention
                except Exception:
                    pass

                sec = (
                    f"## {title}\n"
                    f"> **Wizard ID**: `{event_id}`\n"
                    f"> **Channel**: {ch_mention}\n"
                    + (f"> **Jump**: {jump}\n" if jump else "")
                    + (f"> **Linked Scheduled Event ID**: `{linked}`\n" if linked else "")
                    + f"> **Interested**: {interested}  •  **Signups**: {signups}"
                )
                sections.append(sec)
        await self._send_paginated(ctx, sections, header=header)

    @event_wizard_group.command(name="delete")
    async def event_wizard_delete(self, ctx: commands.Context, *, identifier: str):
        """Delete a published wizard event (by wizard ID or message link/id)."""
        identifier = (identifier or "").strip()
        if not identifier:
            return await ctx.send("Provide a wizard event id or message link.", allowed_mentions=discord.AllowedMentions.none())

        posts = await self.config.guild(ctx.guild).event_posts()
        posts = posts or {}

        target_event_id = None

        # Parse message link
        if "/channels/" in identifier:
            try:
                parts = identifier.replace("<", "").replace(">", "").split("/")
                msg_id = None
                for tok in reversed(parts):
                    if tok.isdigit():
                        msg_id = int(tok)
                        break
                if msg_id:
                    for eid, post in posts.items():
                        if int(post.get("message_id") or 0) == msg_id:
                            target_event_id = eid
                            break
            except Exception:
                pass

        # Parse bare message id
        if target_event_id is None and identifier.isdigit():
            mid = int(identifier)
            for eid, post in posts.items():
                try:
                    if int(post.get("message_id") or 0) == mid:
                        target_event_id = eid
                        break
                except Exception:
                    continue

        # Fallback: treat as wizard id
        if target_event_id is None:
            if identifier in posts:
                target_event_id = identifier

        if not target_event_id:
            return await ctx.send("Wizard event not found.", allowed_mentions=discord.AllowedMentions.none())

        post = posts.get(str(target_event_id))
        if not post:
            return await ctx.send("Wizard event not found.", allowed_mentions=discord.AllowedMentions.none())

        deleted = 0
        ch_id = post.get("channel_id")
        msg_id = post.get("message_id")
        detail_ids = post.get("details_message_ids") or []

        try:
            ch = ctx.guild.get_channel(int(ch_id)) if ch_id else None
        except Exception:
            ch = None

        if ch:
            # Delete main message
            try:
                if msg_id:
                    m = await ch.fetch_message(int(msg_id))
                    await m.delete()
                    deleted += 1
            except Exception:
                pass
            # Delete detail messages
            for did in detail_ids:
                try:
                    m2 = await ch.fetch_message(int(did))
                    await m2.delete()
                    deleted += 1
                except Exception:
                    continue

        async with self.config.guild(ctx.guild).event_posts() as posts_mut:
            if str(target_event_id) in posts_mut:
                del posts_mut[str(target_event_id)]

        await ctx.send(
            f"Deleted wizard event `{target_event_id}`. Messages removed: {deleted}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @event_wizard_group.group(name="divisions", invoke_without_command=True)
    async def event_wizard_divisions(self, ctx: commands.Context):
        """Manage the division list used by the Roles Builder."""
        await ctx.send_help(ctx.command)

    @event_wizard_divisions.command(name="list")
    async def event_wizard_divisions_list(self, ctx: commands.Context):
        vals = await self.config.guild(ctx.guild).wizard_divisions()
        vals = [str(v).strip() for v in (vals or []) if str(v).strip()]
        header = "# Wizard Divisions"
        if not vals:
            return await self._send_paginated(ctx, ["## Divisions\n> None"], header=header)
        lines = "\n".join(f"{i}. {v}" for i, v in enumerate(vals, start=1))
        await self._send_paginated(ctx, [f"## Divisions\n{lines}"], header=header)

    @event_wizard_divisions.command(name="add")
    async def event_wizard_divisions_add(self, ctx: commands.Context, *, name: str):
        name = (name or "").strip()
        if not name:
            return await ctx.send("Division name required.", allowed_mentions=discord.AllowedMentions.none())
        async with self.config.guild(ctx.guild).wizard_divisions() as vals:
            norm = self._norm_text(name)
            if any(self._norm_text(v) == norm for v in (vals or [])):
                return await ctx.send("That division already exists.", allowed_mentions=discord.AllowedMentions.none())
            vals.append(name)
        await ctx.send(f"Added division: **{name}**", allowed_mentions=discord.AllowedMentions.none())

    @event_wizard_divisions.command(name="remove", aliases=["rm", "del"])
    async def event_wizard_divisions_remove(self, ctx: commands.Context, *, name: str):
        name = (name or "").strip()
        if not name:
            return await ctx.send("Division name required.", allowed_mentions=discord.AllowedMentions.none())
        removed = False
        async with self.config.guild(ctx.guild).wizard_divisions() as vals:
            norm = self._norm_text(name)
            new_vals = [v for v in (vals or []) if self._norm_text(v) != norm]
            removed = len(new_vals) != len(vals or [])
            vals.clear()
            vals.extend(new_vals)
        if removed:
            await ctx.send(f"Removed division: **{name}**", allowed_mentions=discord.AllowedMentions.none())
        else:
            await ctx.send("Division not found.", allowed_mentions=discord.AllowedMentions.none())

    @event_wizard_divisions.command(name="reset")
    async def event_wizard_divisions_reset(self, ctx: commands.Context):
        defaults = ["Hugin", "Munin", "Faffne", "Fenrir", "Idun"]
        await self.config.guild(ctx.guild).wizard_divisions.set(defaults)
        await ctx.send("Wizard divisions reset to defaults.", allowed_mentions=discord.AllowedMentions.none())

    @event_group.command(name="list", aliases=["ls"])
    async def event_list(self, ctx):
        """List scheduled events as normal messages (no embed), with pagination."""
        await self._event_list_report(ctx, ctx.guild)
        await self.log_info(f"{ctx.author} listed events (plain messages) in guild {ctx.guild.id}")

    async def _event_list_report(self, dest, guild: discord.Guild):
        """Core scheduled-events listing; `dest` needs .send and .typing."""
        async with dest.typing():
            events = await self._get_scheduled_events(guild, with_counts=True)
        if not events:
            await dest.send(
                "No scheduled events found in this server.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
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

            desc = getattr(event, "description", None)
            desc_block = ""
            if desc:
                short = desc if len(desc) <= 200 else desc[:200] + "..."
                desc_block = "\n" + self._quote_lines(short)

            location_line = ""
            if getattr(event, "location", None):
                location_line = f"\n> **Location**: {event.location}"
            elif getattr(event, "channel", None):
                try:
                    location_line = f"\n> **Channel**: {event.channel.mention}"
                except Exception:
                    pass

            section = (
                f"## {name}\n"
                f"> **Status**: {status}\n"
                f"> **Start**: {start_line}\n"
                f"> **Interested**: {user_count}"
                f"{desc_block}"
                f"{location_line}"
            )
            sections.append(section)

        await self._send_paginated(dest, sections, header=header)

    @event_group.command(name="members")  # deprecated path, kept for compatibility
    async def event_members_legacy(self, ctx, *, event_name: str):
        """[Deprecated] Use: `[p]do event "Name"` instead."""
        await ctx.send(
            "`members` is deprecated. Use: `[p]do event \"Event Name\"`.\nShowing the info below:",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._event_info_with_members(ctx, ctx.guild, event_name)

    async def _event_info_with_members(self, dest, guild: discord.Guild, event_name: str):
        """Show one event summary + interested members as plain messages (auto-paginated)."""
        async with dest.typing():
            events = await self._get_scheduled_events(guild, with_counts=True)
            event = self._event_match(events, event_name)
        if not event:
            await dest.send(
                f"❌ **Event Not Found:** '{event_name}'\n\n"
                f"**Tip:** Use `[p]do event list` to see all scheduled events, then copy the exact event name.\n"
                f"**Note:** Event names are case-insensitive and support partial matches.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.log_info(f"event info: not found for query={event_name!r}")
            return

        # Collect users (paged HTTP calls; keep the typing indicator going)
        interested_users = []
        try:
            async with dest.typing():
                async for user in event.users():
                    member = guild.get_member(user.id)
                    if member:
                        interested_users.append(member)
        except Exception as e:
            await dest.send(
                f"Error fetching interested users: {e}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.log_info(f"Error fetching users for event {getattr(event, 'id', 'unknown')}: {e}")
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

        total_interested = len(interested_users)
        name = getattr(event, "name", "Unnamed Event")
        desc = getattr(event, "description", None)

        header = f"# {name}"
        sections = []

        # Summary block (keep multi-line desc inside blockquote)
        desc_block = ""
        if desc:
            desc_block = "\n" + self._quote_lines(desc[:1024])

        location_line = ""
        if getattr(event, "location", None):
            location_line = f"\n> **Location**: {event.location}"
        elif getattr(event, "channel", None):
            try:
                location_line = f"\n> **Channel**: {event.channel.mention}"
            except Exception:
                pass

        summary = (
            f"> **Status**: {status}\n"
            f"> **Start**: {start_line}\n"
            f"> **Interested**: {total_interested}"
            f"{desc_block}"
            f"{location_line}"
        )
        sections.append(summary)

        # Interested members (chunk into sections)
        if total_interested:
            lines = [f"{i}. {m.mention} ({m.display_name})" for i, m in enumerate(interested_users, start=1)]
            chunk_size = 20
            for idx in range(0, len(lines), chunk_size):
                chunk = lines[idx:idx + chunk_size]
                if idx == 0:
                    sections.append(f"## Interested Members {total_interested}\n" + "\n".join(chunk))
                else:
                    sections.append("## Interested Members (continued)\n" + "\n".join(chunk))
        else:
            sections.append("## Interested Members 0\n> None")

        await self._send_paginated(dest, sections, header=header)
        await self.log_info(f"event info viewed for {getattr(event, 'id', 'unknown')} in guild {guild.id}")

    @event_group.command(name="role")
    async def event_role(self, ctx, action: str, *, event_name: str):
        """
        Create, sync, or delete a role for event attendees.

        Usage: [p]do event role <create|sync|delete> <event_name> [--ping]

        Notes:
        - Mentions are suppressed by default to prevent mass-pings.
        - Add `--ping` to send a final message that pings the event role.
        """
        raw_event_name = event_name or ""
        ping = False
        if raw_event_name.endswith(" --ping"):
            ping = True
            event_name = raw_event_name[: -len(" --ping")].rstrip()

        action_l = (action or "").lower()
        if action_l not in ("create", "sync", "delete"):
            await ctx.send(
                "Action must be 'create', 'sync', or 'delete'",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await self._event_role_action(ctx, ctx.guild, ctx.author, action_l, event_name, ping)

    async def _event_role_action(self, dest, guild: discord.Guild, author, action_l: str, event_name: str, ping: bool):
        """Create/sync/delete an attendee role for a scheduled event.

        `dest` needs .send and .typing; callable from prefix command or hub.
        """
        async with dest.typing():
            events = await self._get_scheduled_events(guild, with_counts=True)
            event = self._event_match(events, event_name)
        if not event:
            await dest.send(
                f"❌ **Event Not Found:** '{event_name}'\n\n"
                f"**Tip:** Use `[p]do event list` to see all scheduled events, then copy the exact event name.\n"
                f"**Note:** Event names are case-insensitive and support partial matches.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.log_info(f"event role: not found for query={event_name!r}")
            return

        # Interested users
        interested_users = []
        try:
            async with dest.typing():
                async for user in event.users():
                    member = guild.get_member(user.id)
                    if member:
                        interested_users.append(member)
        except Exception as e:
            await dest.send(
                f"Error fetching interested users: {e}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.log_info(f"Error fetching users for event {getattr(event, 'id', 'unknown')}: {e}")
            return

        event_roles = await self.config.guild(guild).event_roles()
        event_id_str = str(getattr(event, "id", "0"))

        if action_l == "create":
            if event_id_str in event_roles:
                role = guild.get_role(event_roles[event_id_str])
                if role:
                    await dest.send(
                        f"Role already exists: {role.mention}",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    if ping:
                        await dest.send(
                            role.mention,
                            allowed_mentions=discord.AllowedMentions(
                                roles=True, users=False, everyone=False, replied_user=False
                            ),
                        )
                    return
            try:
                role = await guild.create_role(
                    name=f"Event: {getattr(event, 'name', 'Event')}",
                    color=discord.Color.random(),
                    mentionable=True,
                    reason=f"Event role created by {author}"
                )
                async with self.config.guild(guild).event_roles() as roles:
                    roles[event_id_str] = role.id
                
                # Check role hierarchy before attempting to assign
                # Bot can only manage roles strictly below its highest role
                if guild.me.top_role <= role:
                    # Delete the unusable role
                    try:
                        await role.delete(reason="Role hierarchy issue - bot cannot manage this role")
                    except discord.Forbidden:
                        pass
                    # Remove from config
                    async with self.config.guild(guild).event_roles() as roles:
                        if event_id_str in roles:
                            del roles[event_id_str]
                    await dest.send(
                        f"❌ **Role Hierarchy Issue**\n"
                        f"The created role would be at or above my highest role, which prevents me from managing it.\n"
                        f"Role has been deleted.\n\n"
                        f"**To fix:** Go to Server Settings → Roles and drag my role higher, then try again.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    await self.log_info(f"Role hierarchy issue: bot role {guild.me.top_role.name} below event role - deleted role")
                    return
                
                if len(interested_users) > 10:
                    await dest.send(
                        f"Assigning {role.mention} to {len(interested_users)} interested members — this can take a while (Discord rate limits)…",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                added = 0
                async with dest.typing():
                    for member in interested_users:
                        try:
                            await member.add_roles(role, reason=f"Event role created by {author}")
                            added += 1
                        except discord.Forbidden:
                            pass
                await dest.send(
                    f"Created role {role.mention} and added to {added} interested members",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if ping:
                    await dest.send(
                        role.mention,
                        allowed_mentions=discord.AllowedMentions(
                            roles=True, users=False, everyone=False, replied_user=False
                        ),
                    )
                await self.log_info(f"Created role {role.id} for event {event_id_str} in guild {guild.id}")
            except discord.Forbidden:
                await dest.send(
                    "❌ **Permission Error**\n"
                    "I don't have permission to create roles.\n\n"
                    "**Required Permission:** Manage Roles\n"
                    "**How to Fix:** Go to Server Settings → Roles → [My Role] and enable 'Manage Roles'",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        elif action_l == "sync":
            if event_id_str not in event_roles:
                await dest.send(
                    f"No role exists for event **{getattr(event, 'name', 'Event')}**. Use `create` first.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            role = guild.get_role(event_roles[event_id_str])
            if not role:
                await dest.send(
                    f"Role no longer exists for event **{getattr(event, 'name', 'Event')}**",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                async with self.config.guild(guild).event_roles() as roles:
                    if event_id_str in roles:
                        del roles[event_id_str]
                return

            current_members = set(m.id for m in role.members)
            interested_member_ids = set(m.id for m in interested_users)

            to_add = interested_member_ids - current_members
            to_remove = current_members - interested_member_ids

            if len(to_add) + len(to_remove) > 10:
                await dest.send(
                    f"Syncing {role.mention}: {len(to_add)} to add, {len(to_remove)} to remove — this can take a while (Discord rate limits)…",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            added = removed = 0
            async with dest.typing():
                for member_id in to_add:
                    member = guild.get_member(member_id)
                    if member:
                        try:
                            await member.add_roles(role, reason="Event role sync")
                            added += 1
                        except discord.Forbidden:
                            pass
                for member_id in to_remove:
                    member = guild.get_member(member_id)
                    if member:
                        try:
                            await member.remove_roles(role, reason="Event role sync")
                            removed += 1
                        except discord.Forbidden:
                            pass

            await dest.send(
                f"Sync complete for {role.mention} — Added: {added} • Removed: {removed}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if ping:
                await dest.send(
                    role.mention,
                    allowed_mentions=discord.AllowedMentions(
                        roles=True, users=False, everyone=False, replied_user=False
                    ),
                )
            await self.log_info(f"Synced role {role.id} for event {event_id_str} in guild {guild.id}: +{added}/-{removed}")

        elif action_l == "delete":
            if event_id_str not in event_roles:
                await dest.send(
                    f"No role exists for event **{getattr(event, 'name', 'Event')}**",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            role = guild.get_role(event_roles[event_id_str])
            if role:
                try:
                    await role.delete(reason=f"Event role deleted by {author}")
                    await dest.send(
                        f"Deleted role for event **{getattr(event, 'name', 'Event')}**",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.Forbidden:
                    await dest.send(
                        "I don't have permission to delete this role.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
            async with self.config.guild(guild).event_roles() as roles:
                if event_id_str in roles:
                    del roles[event_id_str]
            await self.log_info(f"Deleted role for event {event_id_str} in guild {guild.id}")

    # ========== Debug / Logs (Owner Only) ==========

    @discoops.command(name="logs")
    @commands.is_owner()
    async def discoops_logs(self, ctx, count: Optional[int] = 10):
        """View recent on-disk logs. Default: 10 lines."""
        try:
            count = int(count or 10)
        except Exception:
            count = 10
        count = max(1, min(count, 200))  # allow up to 200 lines for convenience

        content = await self._logs_tail(count)
        if not content:
            await ctx.send(
                "No logs recorded yet.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # Paginate long logs too
        header = "# DiscoOps Logs"
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
        """Clear all stored logs (on disk)."""
        try:
            if self._log_path.exists():
                self._log_path.unlink()
            await ctx.send(
                "Logs cleared.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.log_info(f"Logs cleared by {ctx.author}")  # creates a fresh file with one entry
        except Exception as e:
            await ctx.send(
                f"Couldn't clear logs: {e}",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @discoops.command(name="help")
    async def discoops_help(self, ctx):
        """Show detailed help for DiscoOps commands."""
        help_md = (
            "# DiscoOps Help\n"
            "`[p]do` — Open the interactive hub (all features via buttons)\n\n"
            "## Members\n"
            "`[p]do members new <amount> <days|weeks|months>` — List recent joins\n"
            "`[p]do members role <@role>` — List members with a role\n\n"
            "## Events\n"
            "`[p]do event list` — List scheduled events (plain messages, paginated)\n"
            "`[p]do event \"Event Name\"` — Show one event (+ members)\n"
            "`[p]do event role <create|sync|delete> \"Event Name\" [--ping]` — Manage event role\n"
            "`[p]do event create` — Start the detailed event wizard\n\n"
            "## Activity\n"
            "`[p]do activity` — 7-day engagement overview (text + voice)\n"
            "`[p]do activity top [days]` — Most active members\n"
            "`[p]do activity user <@member> [days]` — One member's stats\n"
            "`[p]do activity voice` — Who is in voice right now\n"
            "`[p]do activity toggle` — Enable/disable tracking\n"
        )
        await self._send_paginated(ctx, [help_md])

# ---- Red setup compatibility (async vs sync) ----
try:
    async def setup(bot):
        await bot.add_cog(DiscoOps(bot))
except Exception:
    def setup(bot):
        bot.add_cog(DiscoOps(bot))
