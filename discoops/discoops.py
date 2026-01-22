# File: discoops/discoops.py

from __future__ import annotations
import discord
from redbot.core import commands, Config
from redbot.core.data_manager import cog_data_path
from datetime import datetime, timedelta, timezone
import asyncio
from dataclasses import dataclass, field
from typing import Optional, Dict, List
import unicodedata
import os
from pathlib import Path

MAX_MSG = 1900  # stay safely below Discord's 2000 char limit
MAX_LOG_BYTES = 1_000_000  # 1 MB cap for on-disk log
MAX_LOG_DAYS = 14          # delete entries older than 14 days
CLEANUP_EVERY_WRITES = 50  # run time-based cleanup every N writes
PERSIST_COUNTER_EVERY = 10 # persist log_writes counter every N writes to reduce I/O


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
    status: str = "DRAFT"
    roles: Dict[str, RoleDraft] = field(default_factory=dict)

    # Calendar integration
    calendar_mode: str = "LINK_EXISTING"      # "LINK_EXISTING" | "NONE"
    linked_scheduled_event_id: Optional[int] = None
    linked_snapshot: dict = field(default_factory=dict)
    sync_back_to_calendar: bool = True  # toggle in Options

    # Wizard UX helpers (not persisted)
    wizard_updates_message_id: Optional[int] = None
    wizard_updates: List[str] = field(default_factory=list)
    wizard_temp_message_ids: List[int] = field(default_factory=list)


class DiscoOps(commands.Cog):
    """Operational features to make Discord server management easier."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=260288776360820736)
        default_guild = {
            "event_roles": {},  # Maps scheduled_event_id(str) -> role_id
            "event_posts": {},  # Maps wizard_event_id(str) -> published post data + signups
            "wizard_divisions": ["Hugin", "Munin", "Faffne", "Fenrir", "Idun"],
        }
        self.config.register_guild(**default_guild)
        # Global config for persistence across restarts
        default_global = {"log_writes": 0}
        self.config.register_global(**default_global)

        # Disk logging setup
        self._log_lock = asyncio.Lock()
        self._log_writes = None  # Loaded from config in _ensure_log_writes_loaded
        data_dir = cog_data_path(self)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = data_dir / "discoops.log"

        # Detailed Events wizard storage
        self._drafts: Dict[int, EventDraft] = {}   # key: organizer user id -> EventDraft
        self._draft_locks: Dict[str, asyncio.Lock] = {}  # key: event_id -> lock

    async def cog_unload(self):
        """Persist the log_writes counter when the cog is unloaded."""
        try:
            if self._log_writes is not None:
                await self._persist_log_writes()
        except Exception:
            # Don't block unload if persistence fails
            pass

    # --------- disk logger ----------
    async def _ensure_log_writes_loaded(self):
        """Load log_writes from persistent config if not already loaded."""
        if self._log_writes is None:
            self._log_writes = await self.config.log_writes()

    async def _persist_log_writes(self):
        """Save log_writes to persistent config."""
        await self.config.log_writes.set(self._log_writes)

    async def log_info(self, message: str):
        """Append a log line to disk, with rotation + retention."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{ts}] {message}\n"
        try:
            async with self._log_lock:
                # Ensure log_writes counter is loaded from persistent storage
                await self._ensure_log_writes_loaded()

                # Append
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

                # Persist counter periodically to reduce I/O overhead
                if self._log_writes % PERSIST_COUNTER_EVERY == 0:
                    await self._persist_log_writes()
        except (IOError, OSError):
            # File I/O errors - don't disrupt bot flow
            pass
        except Exception:
            # Unexpected errors in logging - still don't disrupt bot flow
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

    async def _wizard_note_change(self, guild: discord.Guild, draft: EventDraft, line: str):
        """Maintain a single editable 'wizard updates' message.

        This exists to keep confirmations merged across multiple actions (roles/options)
        even when the underlying interactions can't edit the same ephemeral response.
        """
        try:
            if not line:
                return
            draft.wizard_updates.append(line)
            # keep the log compact
            draft.wizard_updates = draft.wizard_updates[-12:]

            if not draft.draft_channel_id:
                return
            ch = guild.get_channel(draft.draft_channel_id)
            if not ch:
                return

            body = "\n".join(f"- {ln}" for ln in draft.wizard_updates)
            content = (
                "# Event Wizard Updates\n"
                "> This message updates as you make changes.\n\n"
                f"{body}" if body else "# Event Wizard Updates\n> No changes yet."
            )

            if draft.wizard_updates_message_id:
                try:
                    msg = await ch.fetch_message(draft.wizard_updates_message_id)
                    await msg.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
                    return
                except Exception:
                    draft.wizard_updates_message_id = None

            msg = await ch.send(content, allowed_mentions=discord.AllowedMentions.none())
            draft.wizard_updates_message_id = msg.id
            if msg.id not in draft.wizard_temp_message_ids:
                draft.wizard_temp_message_ids.append(msg.id)
        except Exception:
            pass

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

    def _new_event_id(self, ctx: commands.Context) -> str:
        return self._new_event_id_values(ctx.guild.id, ctx.author.id)

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

    def _build_preview_controls(self, draft: EventDraft, organizer_id: int) -> discord.ui.View:
        """Build the control buttons for the draft preview message."""
        outer = self

        class PreviewView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)

            async def _check(self, interaction: discord.Interaction) -> bool:
                if interaction.user.id != organizer_id:
                    await interaction.response.send_message("Only the organizer can control this draft.", ephemeral=True)
                    return False
                return True

            @discord.ui.button(label="Edit Description", style=discord.ButtonStyle.secondary, custom_id=f"evt:desc:{draft.event_id}")
            async def edit_desc(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._check(interaction):
                    return
                await outer._open_description_modal(interaction, draft)

            @discord.ui.button(label="Roles Builder", style=discord.ButtonStyle.secondary, custom_id=f"evt:roles:{draft.event_id}")
            async def roles(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._check(interaction):
                    return
                await outer._open_roles_builder(interaction, draft)

            @discord.ui.button(label="Options", style=discord.ButtonStyle.secondary, custom_id=f"evt:opts:{draft.event_id}")
            async def opts(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._check(interaction):
                    return
                await outer._open_options_view(interaction, draft)

            @discord.ui.button(label="Publish", style=discord.ButtonStyle.success, custom_id=f"evt:publish:{draft.event_id}")
            async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._check(interaction):
                    return
                await outer._publish_from_draft(interaction, draft)

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, custom_id=f"evt:cancel:{draft.event_id}")
            async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
                if not await self._check(interaction):
                    return
                await outer._cancel_draft(interaction, draft)

        return PreviewView()

    async def _refresh_preview(self, guild: discord.Guild, draft: EventDraft):
        """Refresh the preview message for an event draft."""
        if not draft.preview_message_id or not draft.draft_channel_id:
            return
        channel = guild.get_channel(draft.draft_channel_id)
        if not channel:
            return
        embed = self._build_preview_embed(draft)
        view = self._build_preview_controls(draft, organizer_id=draft.creator_id)
        try:
            msg = await channel.fetch_message(draft.preview_message_id)
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            new_msg = await channel.send(embed=embed, view=view)
            draft.preview_message_id = new_msg.id

    async def _start_wizard_with_preview(self, ctx: commands.Context, preset_title: Optional[str] = None) -> EventDraft:
        """Start the wizard and create the preview message."""
        event_id = self._new_event_id(ctx)
        draft = EventDraft(
            event_id=event_id,
            guild_id=ctx.guild.id,
            creator_id=ctx.author.id,
            draft_channel_id=ctx.channel.id,
            title=preset_title or "",
        )
        self._drafts[ctx.author.id] = draft

        embed = self._build_preview_embed(draft)
        view = self._build_preview_controls(draft, organizer_id=ctx.author.id)
        preview = await ctx.channel.send(embed=embed, view=view)
        draft.preview_message_id = preview.id
        return draft

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
        embed = self._build_preview_embed(draft)
        view = self._build_preview_controls(draft, organizer_id=organizer.id)
        preview = await channel.send(embed=embed, view=view)
        draft.preview_message_id = preview.id
        return draft

    async def _open_scheduled_event_picker(self, ctx: commands.Context):
        """Open the scheduled event picker; draft is created after selection."""
        guild: discord.Guild = ctx.guild
        scheduled: List[discord.GuildScheduledEvent] = await self._get_scheduled_events(guild, with_counts=False)
        organizer_id = ctx.author.id
        channel_id = ctx.channel.id

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
                    await outer._send_ephemeral(interaction, "Draft created without linking a calendar event.")
                    await outer._open_description_modal_followup(interaction, draft)
                except Exception as e:
                    await outer.log_info(f"wizard create without calendar failed: user={interaction.user.id} guild={interaction.guild.id} err={e!r}")
                    await outer._send_ephemeral(interaction, "Something went wrong creating the draft. Try again.")

        # Send the picker prompt
        await ctx.send(
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

        await self._send_ephemeral(interaction, f"Imported **{ev.name}** from the calendar.")
        await self._refresh_preview(interaction.guild, draft)
        await self._open_description_modal_followup(interaction, draft)

    def _create_description_modal(self, draft: EventDraft):
        """Create a description modal for the given draft."""
        outer = self

        class DescriptionModal(discord.ui.Modal, title="Event Description"):
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
                await inter.response.send_message("Description saved.", ephemeral=True)
                await outer._refresh_preview(inter.guild, draft)

        return DescriptionModal()

    async def _open_description_modal(self, interaction: discord.Interaction, draft: EventDraft):
        """Open the description editing modal (as response)."""
        await interaction.response.send_modal(self._create_description_modal(draft))

    async def _open_description_modal_followup(self, interaction: discord.Interaction, draft: EventDraft):
        """Open the description editing modal (as followup after another response)."""
        outer = self

        # Send a button to open the modal since we can't send a modal from followup
        class OpenModalView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)

            @discord.ui.button(label="Edit Description", style=discord.ButtonStyle.primary)
            async def open_modal(self, inter: discord.Interaction, button: discord.ui.Button):
                if inter.user.id != draft.creator_id:
                    return await inter.response.send_message("Only the organizer can edit.", ephemeral=True)
                await inter.response.send_modal(outer._create_description_modal(draft))

        await self._send_ephemeral(interaction, "Click to edit the event description:", view=OpenModalView())

    async def _open_roles_builder(self, interaction: discord.Interaction, draft: EventDraft):
        """Open the roles builder view."""
        outer = self

        class RolesView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)

            @discord.ui.button(label="Add Role", style=discord.ButtonStyle.primary)
            async def add_role(self, inter: discord.Interaction, button: discord.ui.Button):
                if inter.user.id != draft.creator_id:
                    return await inter.response.send_message("Only the organizer can edit roles.", ephemeral=True)
                await outer._open_add_role_division_picker(inter, draft)

            @discord.ui.button(label="Done", style=discord.ButtonStyle.success)
            async def done(self, inter: discord.Interaction, button: discord.ui.Button):
                if inter.user.id != draft.creator_id:
                    return await inter.response.send_message("Only the organizer can continue.", ephemeral=True)
                await inter.response.send_message("Roles saved.", ephemeral=True)

        # Show a compact summary as ephemeral message
        summary = "No roles yet." if not draft.roles else "\n".join(
            f"• {(r.emoji + ' ') if r.emoji else ''}{outer._role_display_name(r)} ({'∞' if r.capacity is None else r.capacity})"
            for r in draft.roles.values()
        )
        await self._send_ephemeral(interaction, f"**Roles so far:**\n{summary}", view=RolesView())

    async def _open_add_role_division_picker(self, interaction: discord.Interaction, draft: EventDraft):
        """Step 1: pick a division for the new role."""
        if len(draft.roles) >= 24:
            return await self._send_ephemeral(interaction, "You can only have up to **24** roles (Discord menu limit).")

        outer = self
        divisions = []
        try:
            divisions = await self.config.guild(interaction.guild).wizard_divisions()
        except Exception:
            divisions = ["Hugin", "Munin", "Faffne", "Fenrir", "Idun"]
        divisions = [d.strip() for d in (divisions or []) if str(d).strip()]
        if not divisions:
            divisions = ["Hugin", "Munin", "Faffne", "Fenrir", "Idun"]

        # Discord select option cap
        divisions = divisions[:25]

        opts = [discord.SelectOption(label=d[:100], value=d) for d in divisions]

        class DivisionPickView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)

            @discord.ui.select(placeholder="Pick a division…", options=opts, min_values=1, max_values=1)
            async def pick(self, inter: discord.Interaction, select: discord.ui.Select):
                if inter.user.id != draft.creator_id:
                    return await inter.response.send_message("Only the organizer can add roles.", ephemeral=True)
                division = select.values[0]
                await inter.response.send_modal(outer._create_add_role_modal(draft, division=division))

        await self._send_ephemeral(interaction, "Select the division for the new role:", view=DivisionPickView())

    def _create_add_role_modal(self, draft: EventDraft, *, division: str) -> discord.ui.Modal:
        """Step 2: enter role name + capacity."""
        outer = self
        division = (division or "").strip()

        class AddRoleModal(discord.ui.Modal, title="Add Division Role"):
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

                rid = f"r{len(draft.roles) + 1}"
                rd = RoleDraft(
                    role_id=rid,
                    division=division,
                    role_name=rn,
                    capacity=cap_val,
                )
                draft.roles[rid] = rd

                disp = outer._role_display_name(rd)
                await inter.response.send_message(
                    f"Added role **{disp}**. Next: react to the emoji prompt in the channel (or ignore to skip).",
                    ephemeral=True,
                )
                try:
                    await outer._wizard_note_change(
                        inter.guild,
                        draft,
                        f"Role added: {disp} ({'∞' if rd.capacity is None else rd.capacity})",
                    )
                except Exception:
                    pass

                # Emoji picker via reaction (native emoji picker UX)
                try:
                    ch = inter.guild.get_channel(draft.draft_channel_id) if draft.draft_channel_id else inter.channel
                    if ch:
                        prompt = await ch.send(
                            f"Pick an emoji for **{disp}** by reacting to this message (timeout: 60s).",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        try:
                            if prompt.id not in draft.wizard_temp_message_ids:
                                draft.wizard_temp_message_ids.append(prompt.id)
                        except Exception:
                            pass

                        def check(reaction: discord.Reaction, user: discord.User):
                            try:
                                return user.id == draft.creator_id and reaction.message.id == prompt.id
                            except Exception:
                                return False

                        try:
                            reaction, _user = await outer.bot.wait_for("reaction_add", timeout=60.0, check=check)
                            emoji_str = str(reaction.emoji) if reaction and reaction.emoji else None
                            if emoji_str:
                                rd.emoji = emoji_str
                                draft.roles[rid] = rd
                                await outer._wizard_note_change(inter.guild, draft, f"Role emoji set: {disp} = {emoji_str}")
                        except asyncio.TimeoutError:
                            pass
                        except Exception:
                            pass
                        try:
                            await prompt.delete()
                        except Exception:
                            pass
                except Exception:
                    pass

                await outer._refresh_preview(inter.guild, draft)

        return AddRoleModal()

    async def _open_options_view(self, interaction: discord.Interaction, draft: EventDraft):
        """Open the options configuration view."""
        outer = self

        class OptionsView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)

            @discord.ui.select(
                placeholder="Comms",
                options=[
                    discord.SelectOption(label="Discord", value="DISCORD", default=("DISCORD" in (draft.comms or []))),
                    discord.SelectOption(label="SRS", value="SRS", default=("SRS" in (draft.comms or []))),
                ],
                min_values=1,
                max_values=2,
            )
            async def comms_select(self, inter: discord.Interaction, select: discord.ui.Select):
                if inter.user.id != draft.creator_id:
                    return await inter.response.send_message("Only the organizer can change options.", ephemeral=True)
                draft.comms = list(select.values)
                await outer._wizard_note_change(inter.guild, draft, f"Comms: {outer._format_comms(draft)}")
                await inter.response.edit_message(
                    content=f"Options updated.\n\n**Comms:** {outer._format_comms(draft)}\n**Calendar mode:** {draft.calendar_mode}",
                    view=self,
                )
                await outer._refresh_preview(inter.guild, draft)

            @discord.ui.select(
                placeholder="Calendar behavior",
                options=[
                    discord.SelectOption(label="Link existing (recommended)", value="LINK_EXISTING", default=(draft.calendar_mode=="LINK_EXISTING")),
                    discord.SelectOption(label="No calendar", value="NONE", default=(draft.calendar_mode=="NONE")),
                ],
                min_values=1,
                max_values=1,
            )
            async def calmode(self, inter: discord.Interaction, select: discord.ui.Select):
                if inter.user.id != draft.creator_id:
                    return await inter.response.send_message("Only the organizer can change options.", ephemeral=True)
                draft.calendar_mode = select.values[0]
                if draft.calendar_mode != "LINK_EXISTING":
                    draft.linked_scheduled_event_id = None
                await outer._wizard_note_change(inter.guild, draft, f"Calendar mode: {draft.calendar_mode}")
                await inter.response.edit_message(
                    content=f"Options updated.\n\n**Comms:** {outer._format_comms(draft)}\n**Calendar mode:** {draft.calendar_mode}",
                    view=self,
                )
                await outer._refresh_preview(inter.guild, draft)

            @discord.ui.button(label="Sync edits to calendar: ON" if draft.sync_back_to_calendar else "Sync edits to calendar: OFF",
                               style=discord.ButtonStyle.secondary)
            async def sync_toggle(self, inter: discord.Interaction, btn: discord.ui.Button):
                if inter.user.id != draft.creator_id:
                    return await inter.response.send_message("Only the organizer can change options.", ephemeral=True)
                draft.sync_back_to_calendar = not draft.sync_back_to_calendar
                btn.label = "Sync edits to calendar: ON" if draft.sync_back_to_calendar else "Sync edits to calendar: OFF"
                try:
                    await outer._wizard_note_change(
                        inter.guild,
                        draft,
                        f"Sync back to calendar: {'ON' if draft.sync_back_to_calendar else 'OFF'}",
                    )
                except Exception:
                    pass
                await inter.response.edit_message(
                    content=f"Options updated.\n\n**Comms:** {outer._format_comms(draft)}\n**Calendar mode:** {draft.calendar_mode}",
                    view=self,
                )
                await outer._refresh_preview(inter.guild, draft)

            @discord.ui.button(label="Done", style=discord.ButtonStyle.success)
            async def done(self, inter: discord.Interaction, button: discord.ui.Button):
                if inter.user.id != draft.creator_id:
                    return await inter.response.send_message("Only the organizer can continue.", ephemeral=True)
                await inter.response.send_message("Options saved. Use **Publish** under the preview message when ready.", ephemeral=True)

        await self._send_ephemeral(interaction, "Options:", view=OptionsView())

    async def _publish_from_draft(self, interaction: discord.Interaction, draft: EventDraft):
        """Open publish destination picker (category -> channel)."""
        if interaction.user.id != draft.creator_id:
            return await self._send_ephemeral(interaction, "Only the organizer can publish this draft.")
        # Channel enumeration can take long on large servers; acknowledge quickly.
        await self._defer_ephemeral(interaction, thinking=True)
        await self._open_publish_destination(interaction, draft)

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

    async def _open_publish_destination(self, interaction: discord.Interaction, draft: EventDraft):
        """Ask organizer to select category + channel for publishing."""
        outer = self
        guild = interaction.guild

        # Build eligible channels
        eligible = []
        try:
            me = guild.me
        except Exception:
            me = None
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

        if not eligible:
            return await self._send_ephemeral(interaction, "I can't find any text channels I can post and maintain (need View Channel + Send Messages + Read Message History).")

        by_cat: Dict[str, List[discord.TextChannel]] = {}
        for ch in eligible:
            key = str(ch.category_id) if ch.category_id else "none"
            by_cat.setdefault(key, []).append(ch)
        for key in list(by_cat.keys()):
            by_cat[key].sort(key=lambda c: c.position)

        # Category options (max 25). If there are more, user can use Search.
        cat_opts = []
        # Keep server order
        for cat in sorted(getattr(guild, "categories", []) or [], key=lambda c: c.position):
            if str(cat.id) in by_cat:
                cat_opts.append(discord.SelectOption(label=cat.name[:100], value=str(cat.id)))
        if "none" in by_cat:
            cat_opts.append(discord.SelectOption(label="No Category", value="none"))
        cat_opts = cat_opts[:25]

        class ChannelSearchModal(discord.ui.Modal):
            query = discord.ui.TextInput(label="Channel name contains", required=True, max_length=50)

            def __init__(self, *, source_message_id: int):
                super().__init__(title="Search Channel")
                self.source_message_id = source_message_id

            async def on_submit(self, inter: discord.Interaction):
                q = (self.query.value or "").strip().casefold()
                matches = [c for c in eligible if q and q in (c.name or "").casefold()]
                matches.sort(key=lambda c: (c.category.position if c.category else 9999, c.position))
                view = PublishDestinationView()
                view.set_channel_options(matches[:25])
                try:
                    await inter.response.defer(ephemeral=True)
                except Exception:
                    pass
                try:
                    await inter.followup.edit_message(
                        message_id=self.source_message_id,
                        content="Pick a destination channel:",
                        view=view,
                    )
                except Exception:
                    await outer._send_ephemeral(inter, "Couldn't update the picker; try using category/channel selects instead.")

        class PublishDestinationView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                self.selected_category = None
                self.selected_channel = None

                self.category_select = discord.ui.Select(
                    placeholder="Pick a category…",
                    options=cat_opts,
                    min_values=1,
                    max_values=1,
                )
                self.category_select.callback = self.on_pick_category
                self.add_item(self.category_select)

                self.channel_select = discord.ui.Select(
                    placeholder="Pick a channel…",
                    options=[],
                    min_values=1,
                    max_values=1,
                    disabled=True,
                )
                self.channel_select.callback = self.on_pick_channel
                self.add_item(self.channel_select)

                self.publish_btn = discord.ui.Button(label="Publish", style=discord.ButtonStyle.success, disabled=True)
                self.publish_btn.callback = self.on_publish
                self.add_item(self.publish_btn)

                self.search_btn = discord.ui.Button(label="Search Channel", style=discord.ButtonStyle.secondary)
                self.search_btn.callback = self.on_search
                self.add_item(self.search_btn)

                self.cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)
                self.cancel_btn.callback = self.on_cancel
                self.add_item(self.cancel_btn)

            def set_channel_options(self, channels: List[discord.TextChannel]):
                opts = []
                for c in (channels or [])[:25]:
                    label = ("#" + c.name)[:100]
                    desc = (c.category.name if c.category else "No Category")[:100]
                    opts.append(discord.SelectOption(label=label, value=str(c.id), description=desc))
                self.channel_select.options = opts
                self.channel_select.disabled = not bool(opts)
                self.publish_btn.disabled = True

            async def _check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != draft.creator_id:
                    await inter.response.send_message("Only the organizer can publish.", ephemeral=True)
                    return False
                return True

            async def on_pick_category(self, inter: discord.Interaction):
                if not await self._check(inter):
                    return
                self.selected_category = self.category_select.values[0]
                chans = by_cat.get(str(self.selected_category), [])
                self.set_channel_options(chans)
                await inter.response.edit_message(content="Pick a destination channel:", view=self)

            async def on_pick_channel(self, inter: discord.Interaction):
                if not await self._check(inter):
                    return
                self.selected_channel = self.channel_select.values[0]
                self.publish_btn.disabled = False
                await inter.response.edit_message(view=self)

            async def on_search(self, inter: discord.Interaction):
                if not await self._check(inter):
                    return
                src_id = getattr(getattr(inter, "message", None), "id", None)
                if not src_id:
                    return await inter.response.send_message("Can't open search here; try picking a category.", ephemeral=True)
                await inter.response.send_modal(ChannelSearchModal(source_message_id=int(src_id)))

            async def on_cancel(self, inter: discord.Interaction):
                if not await self._check(inter):
                    return
                await inter.response.edit_message(content="Publish canceled.", view=None)

            async def on_publish(self, inter: discord.Interaction):
                if not await self._check(inter):
                    return
                if not self.selected_channel:
                    return await inter.response.send_message("Pick a channel first.", ephemeral=True)
                await inter.response.defer(ephemeral=True, thinking=True)
                await outer._publish_to_channel(inter, draft, channel_id=int(self.selected_channel))

        await self._send_ephemeral(interaction, "Pick a destination category/channel:", view=PublishDestinationView())

    async def _publish_to_channel(self, interaction: discord.Interaction, draft: EventDraft, *, channel_id: int):
        guild = interaction.guild

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
            detailed_msg = await self._post_canonical_event(guild, draft, channel_hint=channel_id)

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
        await interaction.response.send_message("Draft canceled and preview removed.", ephemeral=True)
        await self.log_info(f"{interaction.user} canceled draft {draft.event_id} in guild {interaction.guild.id}")

    async def _post_canonical_event(self, guild: discord.Guild, draft: EventDraft, channel_hint: Optional[int] = None) -> discord.Message:
        """Post the final published event message."""
        channel = guild.get_channel(channel_hint) if channel_hint else guild.text_channels[0]
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

        async with self.config.guild(guild).event_posts() as posts:
            post = posts.get(str(event_id))
            if not post:
                return await interaction.response.send_message("This event post is no longer tracked.", ephemeral=True)
            roles = post.get("roles") or {}
            signups = post.get("signups") or {}

            prev = str(signups.get(uid_str) or "")

            is_withdraw = (role_id or "") == "__withdraw__"

            # Capacity enforcement (only if changing into a role)
            if role_id and not is_withdraw:
                rd = roles.get(str(role_id))
                if not rd:
                    return await interaction.response.send_message("That role is no longer available.", ephemeral=True)
                r = self._role_from_dict(rd)
                if r.capacity is not None and str(role_id) != prev:
                    # count current occupants
                    occ = 0
                    for _u, rid in signups.items():
                        if str(rid or "") == str(role_id):
                            occ += 1
                    if occ >= int(r.capacity):
                        return await interaction.response.send_message(
                            f"That role is full (**{occ}/{r.capacity}**).",
                            ephemeral=True,
                        )

            # Apply
            if is_withdraw or not role_id:
                if uid_str in signups:
                    del signups[uid_str]
                post["signups"] = signups
                posts[str(event_id)] = post
                await interaction.response.send_message("Signup removed.", ephemeral=True)
            else:
                signups[uid_str] = str(role_id)
                post["signups"] = signups
                posts[str(event_id)] = post

                rd = roles.get(str(role_id)) or {}
                r = self._role_from_dict(rd)
                await interaction.response.send_message(f"Signed you up as **{self._role_display_name(r)}**.", ephemeral=True)

        await self._update_published_post_message(guild, event_id)

    async def _handle_public_view_details(self, interaction: discord.Interaction, event_id: str):
        guild = interaction.guild
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
        await self.log_info(f"{ctx.author} invoked 'members new' with amount={amount}, period={period} in guild {ctx.guild.id}")

        period_l = (period or "").lower()
        if period_l not in ("days", "day", "weeks", "week", "months", "month"):
            await ctx.send(
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

        # Access members (requires Server Members Intent)
        try:
            members = list(ctx.guild.members)
            if not members:
                try:
                    await ctx.guild.chunk()
                    members = list(ctx.guild.members)
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
            await ctx.send(
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
            await ctx.send(
                "An error occurred while reading member join dates.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        recent.sort(key=lambda tup: tup[1], reverse=True)

        if not recent:
            await ctx.send(
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

        await self._send_paginated(ctx, sections, header=header)
        await self.log_info(f"Sent recent members list ({len(recent)} found)")

    @members_group.command(name="role")
    async def members_role(self, ctx, *, role: discord.Role):
        """List all members with a specific role and show count."""
        await self.log_info(f"{ctx.author} invoked 'members role' for role {role.id} in guild {ctx.guild.id}")
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
        await self.log_info(f"Sent members-with-role list ({len(members_with_role)} members)")

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
            await self._event_info_with_members(ctx, event_name)
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
        await self._open_scheduled_event_picker(ctx)

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
        events = await self._get_scheduled_events(ctx.guild, with_counts=True)
        if not events:
            await ctx.send(
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

        await self._send_paginated(ctx, sections, header=header)
        await self.log_info(f"{ctx.author} listed events (plain messages) in guild {ctx.guild.id}")

    @event_group.command(name="members")  # deprecated path, kept for compatibility
    async def event_members_legacy(self, ctx, *, event_name: str):
        """[Deprecated] Use: `[p]do event "Name"` instead."""
        await ctx.send(
            "`members` is deprecated. Use: `[p]do event \"Event Name\"`.\nShowing the info below:",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._event_info_with_members(ctx, event_name)

    async def _event_info_with_members(self, ctx, event_name: str):
        """Show one event summary + interested members as plain messages (auto-paginated)."""
        events = await self._get_scheduled_events(ctx.guild, with_counts=True)
        event = self._event_match(events, event_name)
        if not event:
            await ctx.send(
                f"❌ **Event Not Found:** '{event_name}'\n\n"
                f"**Tip:** Use `[p]do event list` to see all scheduled events, then copy the exact event name.\n"
                f"**Note:** Event names are case-insensitive and support partial matches.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.log_info(f"event info: not found for query={event_name!r}")
            return

        # Collect users
        interested_users = []
        try:
            async for user in event.users():
                member = ctx.guild.get_member(user.id)
                if member:
                    interested_users.append(member)
        except Exception as e:
            await ctx.send(
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

        await self._send_paginated(ctx, sections, header=header)
        await self.log_info(f"{ctx.author} viewed event info for {getattr(event, 'id', 'unknown')} in guild {ctx.guild.id}")

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

        events = await self._get_scheduled_events(ctx.guild, with_counts=True)
        event = self._event_match(events, event_name)
        if not event:
            await ctx.send(
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
            async for user in event.users():
                member = ctx.guild.get_member(user.id)
                if member:
                    interested_users.append(member)
        except Exception as e:
            await ctx.send(
                f"Error fetching interested users: {e}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.log_info(f"Error fetching users for event {getattr(event, 'id', 'unknown')}: {e}")
            return

        event_roles = await self.config.guild(ctx.guild).event_roles()
        event_id_str = str(getattr(event, "id", "0"))

        if action_l == "create":
            if event_id_str in event_roles:
                role = ctx.guild.get_role(event_roles[event_id_str])
                if role:
                    await ctx.send(
                        f"Role already exists: {role.mention}",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    if ping:
                        await ctx.send(
                            role.mention,
                            allowed_mentions=discord.AllowedMentions(
                                roles=True, users=False, everyone=False, replied_user=False
                            ),
                        )
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
                
                # Check role hierarchy before attempting to assign
                # Bot can only manage roles strictly below its highest role
                if ctx.guild.me.top_role <= role:
                    # Delete the unusable role
                    try:
                        await role.delete(reason="Role hierarchy issue - bot cannot manage this role")
                    except discord.Forbidden:
                        pass
                    # Remove from config
                    async with self.config.guild(ctx.guild).event_roles() as roles:
                        if event_id_str in roles:
                            del roles[event_id_str]
                    await ctx.send(
                        f"❌ **Role Hierarchy Issue**\n"
                        f"The created role would be at or above my highest role, which prevents me from managing it.\n"
                        f"Role has been deleted.\n\n"
                        f"**To fix:** Go to Server Settings → Roles and drag my role higher, then try again.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    await self.log_info(f"Role hierarchy issue: bot role {ctx.guild.me.top_role.name} below event role - deleted role")
                    return
                
                added = 0
                for member in interested_users:
                    try:
                        await member.add_roles(role)
                        added += 1
                    except discord.Forbidden:
                        pass
                await ctx.send(
                    f"Created role {role.mention} and added to {added} interested members",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if ping:
                    await ctx.send(
                        role.mention,
                        allowed_mentions=discord.AllowedMentions(
                            roles=True, users=False, everyone=False, replied_user=False
                        ),
                    )
                await self.log_info(f"Created role {role.id} for event {event_id_str} in guild {ctx.guild.id}")
            except discord.Forbidden:
                await ctx.send(
                    "❌ **Permission Error**\n"
                    "I don't have permission to create roles.\n\n"
                    "**Required Permission:** Manage Roles\n"
                    "**How to Fix:** Go to Server Settings → Roles → [My Role] and enable 'Manage Roles'",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        elif action_l == "sync":
            if event_id_str not in event_roles:
                await ctx.send(
                    f"No role exists for event **{getattr(event, 'name', 'Event')}**. Use `create` first.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            role = ctx.guild.get_role(event_roles[event_id_str])
            if not role:
                await ctx.send(
                    f"Role no longer exists for event **{getattr(event, 'name', 'Event')}**",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
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

            await ctx.send(
                f"Sync complete for {role.mention} — Added: {added} • Removed: {removed}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if ping:
                await ctx.send(
                    role.mention,
                    allowed_mentions=discord.AllowedMentions(
                        roles=True, users=False, everyone=False, replied_user=False
                    ),
                )
            await self.log_info(f"Synced role {role.id} for event {event_id_str} in guild {ctx.guild.id}: +{added}/-{removed}")

        elif action_l == "delete":
            if event_id_str not in event_roles:
                await ctx.send(
                    f"No role exists for event **{getattr(event, 'name', 'Event')}**",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            role = ctx.guild.get_role(event_roles[event_id_str])
            if role:
                try:
                    await role.delete(reason=f"Event role deleted by {ctx.author}")
                    await ctx.send(
                        f"Deleted role for event **{getattr(event, 'name', 'Event')}**",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.Forbidden:
                    await ctx.send(
                        "I don't have permission to delete this role.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
            async with self.config.guild(ctx.guild).event_roles() as roles:
                if event_id_str in roles:
                    del roles[event_id_str]
            await self.log_info(f"Deleted role for event {event_id_str} in guild {ctx.guild.id}")

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
            "## Members\n"
            "`[p]do members new <amount> <days|weeks|months>` — List recent joins\n"
            "`[p]do members role <@role>` — List members with a role\n\n"
            "## Events\n"
            "`[p]do event list` — List scheduled events (plain messages, paginated)\n"
            "`[p]do event \"Event Name\"` — Show one event (+ members)\n"
            "`[p]do event role <create|sync|delete> \"Event Name\" [--ping]` — Manage event role\n"
            "`[p]do event create` — Start the detailed event wizard\n"
        )
        await self._send_paginated(ctx, [help_md])

# ---- Red setup compatibility (async vs sync) ----
try:
    async def setup(bot):
        await bot.add_cog(DiscoOps(bot))
except Exception:
    def setup(bot):
        bot.add_cog(DiscoOps(bot))
