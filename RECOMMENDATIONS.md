# DiscoOps - Actionable Recommendations

This document provides specific, actionable recommendations for improving the DiscoOps cog. Each recommendation includes code examples and priority levels.

---

## Priority 1: Critical (Must Fix Before Distribution)

### 1.1 Update Placeholder Information

**Files to Update:**
- `README.md` line 22
- `discoops/info.json` line 8

**Current Issues:**
```markdown
# README.md
[p]repo add DiscoOps https://github.com/yourusername/DiscoOps
```

```json
// info.json
"author": ["YourName"]
```

**Required Changes:**
```markdown
# README.md - Update to actual repository
[p]repo add DiscoOps https://github.com/cnabel/DiscoOps
```

```json
// info.json - Update to actual author
"author": ["cnabel"]
```

**Estimated Time**: 5 minutes  
**Impact**: High - Prevents confusion during installation

---

## Priority 2: High (Recommended for Next Release)

### 2.1 Improve Exception Handling

**Current Pattern** (appears ~15 times):
```python
except Exception:
    pass
```

**Recommended Pattern**:
```python
except discord.Forbidden as e:
    await self.log_info(f"Permission denied: {e}")
    # Handle appropriately
except discord.HTTPException as e:
    await self.log_info(f"Discord API error: {e}")
    # Handle appropriately
except Exception as e:
    await self.log_info(f"Unexpected error in {context}: {e}")
    # Decide whether to re-raise
```

**Locations to Update:**
1. Line 58: `log_info` exception handler
2. Line 86: `_truncate_to_max_bytes` exception handler
3. Line 113: `_time_prune_older_than` exception handler
4. Line 136: `_logs_tail` exception handler
5. Line 162-168: `_get_scheduled_events` exception handlers
6. Line 263-264: Member access exception handler
7. Line 285-288: Member filtering exception handler

**Estimated Time**: 2-3 hours  
**Impact**: High - Better debugging and error tracking

---

### 2.2 Add Role Hierarchy Check

**Location**: `event_role` command (around line 547)

**Current Code**:
```python
try:
    role = await ctx.guild.create_role(
        name=f"Event: {getattr(event, 'name', 'Event')}",
        color=discord.Color.random(),
        mentionable=True,
        reason=f"Event role created by {ctx.author}"
    )
```

**Recommended Enhancement**:
```python
try:
    # Create role
    role = await ctx.guild.create_role(
        name=f"Event: {getattr(event, 'name', 'Event')}",
        color=discord.Color.random(),
        mentionable=True,
        reason=f"Event role created by {ctx.author}"
    )
    
    # Verify hierarchy before attempting to assign
    if ctx.guild.me.top_role <= role:
        await ctx.send(
            "⚠️ **Role Hierarchy Issue**\n"
            "The newly created role is at or above my highest role. "
            "I won't be able to assign it to members.\n\n"
            "**To fix**: Go to Server Settings → Roles and drag my role above the event role."
        )
        await self.log_info(f"Role hierarchy issue: bot role {ctx.guild.me.top_role.name} <= event role {role.name}")
```

**Estimated Time**: 1 hour  
**Impact**: High - Prevents user confusion from silent failures

---

### 2.3 Enhanced Error Messages

**Current Messages** (various locations):
```python
await ctx.send("I don't have permission to create roles.")
await ctx.send("Event not found.")
```

**Recommended Enhanced Messages**:

```python
# Role creation permission error (line ~560)
await ctx.send(
    "❌ **Permission Error**\n"
    "I don't have permission to create roles.\n\n"
    "**Required Permission**: Manage Roles\n"
    "**How to Fix**: Go to Server Settings → Roles → [Bot's Role] and enable 'Manage Roles'\n"
    f"**Command**: `[p]do event role create \"{event_name}\"`"
)

# Event not found (line ~429, ~516)
await ctx.send(
    f"❌ **Event Not Found**: '{event_name}'\n\n"
    f"**Tip**: Use `[p]do event list` to see all scheduled events, then copy the exact event name.\n"
    f"**Note**: Event names are case-insensitive and support partial matches."
)

# No members found (line ~293)
await ctx.send(
    f"ℹ️ No members joined in the last {amount} {period_l}.\n\n"
    f"**Note**: Make sure the bot has been running and has cached member data. "
    f"Members who joined before the bot was added won't be tracked."
)
```

**Estimated Time**: 1-2 hours  
**Impact**: Medium-High - Better user experience

---

## Priority 3: Medium (Quality Improvements)

### 3.1 Add Type Hints

**Recommended Additions**:

```python
from typing import Optional, List, Dict, Tuple
import discord
from datetime import datetime

class DiscoOps(commands.Cog):
    """Operational features to make Discord server management easier."""

    def __init__(self, bot: commands.Bot) -> None:
        # ...

    async def log_info(self, message: str) -> None:
        """Append a log line to disk, with rotation + retention."""
        # ...

    def _truncate_to_max_bytes(self) -> None:
        """Trim the log file to keep only the last <= MAX_LOG_BYTES bytes aligned to lines."""
        # ...

    async def _logs_tail(self, count: int) -> str:
        """Return the last `count` lines from disk, efficiently."""
        # ...

    @staticmethod
    def _norm_text(s: str) -> str:
        """Normalize text for comparisons (NFKC + strip quotes + casefold)."""
        # ...

    @staticmethod
    def _quote_lines(text: str) -> str:
        """Prefix every line with '> ' to keep multi-line descriptions inside the quote."""
        # ...

    @staticmethod
    async def _get_scheduled_events(
        guild: discord.Guild, 
        with_counts: bool = True
    ) -> List[discord.ScheduledEvent]:
        """Safely fetch scheduled events across discord.py versions."""
        # ...

    @classmethod
    def _event_match(
        cls, 
        events: List[discord.ScheduledEvent], 
        query: str
    ) -> Optional[discord.ScheduledEvent]:
        """Find event by normalized exact name, then partial match."""
        # ...

    @staticmethod
    async def _send_paginated(
        ctx: commands.Context,
        chunks: List[str],
        header: Optional[str] = None,
        footer: Optional[str] = None
    ) -> None:
        """Send plain text chunks split below Discord's limit."""
        # ...

    def _format_timestamp(self, dt: Optional[datetime]) -> str:
        """Format datetime as Discord timestamp + unix epoch."""
        if not dt:
            return "N/A"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        epoch = int(dt.timestamp())
        return f"<t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
```

**Estimated Time**: 3-4 hours  
**Impact**: Medium - Better IDE support and code clarity

---

### 3.2 Extract Magic Numbers to Constants

**Current** (scattered throughout):
```python
chunk_size = 20  # Line 327
chunk_size = 20  # Line 489
```

**Recommended** (at class level, around line 19):
```python
MAX_MSG = 1900  # stay safely below Discord's 2000 char limit
MAX_LOG_BYTES = 1_000_000  # 1 MB cap for on-disk log
MAX_LOG_DAYS = 14          # delete entries older than 14 days
CLEANUP_EVERY_WRITES = 50  # run time-based cleanup every N writes

# Add these:
MEMBER_LIST_CHUNK_SIZE = 20  # Members per section in role/event lists
EVENT_DESC_MAX_LENGTH = 200  # Max description length in event list
EVENT_DESC_DETAIL_MAX = 1024  # Max description in detailed view
MAX_LOGS_DISPLAY = 200  # Maximum log lines to display
```

**Then update usages**:
```python
# Line 327 and 489
chunk_size = self.MEMBER_LIST_CHUNK_SIZE

# Line 393
short = desc if len(desc) <= self.EVENT_DESC_MAX_LENGTH else desc[:self.EVENT_DESC_MAX_LENGTH] + "..."

# Line 466
desc_block = "\n" + self._quote_lines(desc[:self.EVENT_DESC_DETAIL_MAX])

# Line 629
count = max(1, min(count, self.MAX_LOGS_DISPLAY))
```

**Estimated Time**: 30 minutes  
**Impact**: Low-Medium - Better maintainability

---

### 3.3 Reduce Code Duplication - Timestamp Formatting

**Current** (duplicated in two places):
```python
# Lines 381-388
st = getattr(event, "start_time", None)
if st:
    if st.tzinfo is None:
        st = st.replace(tzinfo=timezone.utc)
    epoch = int(st.timestamp())
    start_line = f"<t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
else:
    start_line = "N/A"

# Lines 447-454 (same code)
```

**Recommended** (add helper method):
```python
def _format_timestamp(self, dt: Optional[datetime]) -> str:
    """
    Format datetime as Discord timestamp with relative time and unix epoch.
    
    Args:
        dt: datetime object to format (can be None)
        
    Returns:
        Formatted string with Discord timestamp markup and unix epoch,
        or "N/A" if dt is None
    """
    if not dt:
        return "N/A"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = int(dt.timestamp())
    return f"<t:{epoch}:F> • <t:{epoch}:R> (unix: `{epoch}`)"
```

**Usage**:
```python
# Replace both occurrences with:
start_line = self._format_timestamp(getattr(event, "start_time", None))
```

**Estimated Time**: 30 minutes  
**Impact**: Medium - DRY principle

---

## Priority 4: Low (Future Enhancements)

### 4.1 Add Unit Tests

**Recommended Test Structure**:
```
DiscoOps/
├── discoops/
│   └── ...
├── tests/
│   ├── __init__.py
│   ├── test_text_normalization.py
│   ├── test_event_matching.py
│   ├── test_pagination.py
│   └── test_timestamp_formatting.py
└── ...
```

**Sample Test File** (`tests/test_text_normalization.py`):
```python
import pytest
from discoops.discoops import DiscoOps

class TestTextNormalization:
    def test_norm_text_basic(self):
        assert DiscoOps._norm_text("Hello") == "hello"
    
    def test_norm_text_quotes(self):
        assert DiscoOps._norm_text('"Event"') == DiscoOps._norm_text('Event')
        assert DiscoOps._norm_text("'Event'") == DiscoOps._norm_text('Event')
    
    def test_norm_text_unicode(self):
        assert DiscoOps._norm_text("Café") == DiscoOps._norm_text("Cafe")
        assert DiscoOps._norm_text("Rådsmötet") == "rådsmötet"
    
    def test_norm_text_whitespace(self):
        assert DiscoOps._norm_text("  Event  ") == "event"
    
    def test_norm_text_empty(self):
        assert DiscoOps._norm_text("") == ""
        assert DiscoOps._norm_text(None) == ""

class TestQuoteLines:
    def test_quote_single_line(self):
        assert DiscoOps._quote_lines("Hello") == "> Hello"
    
    def test_quote_multi_line(self):
        text = "Line 1\nLine 2\nLine 3"
        expected = "> Line 1\n> Line 2\n> Line 3"
        assert DiscoOps._quote_lines(text) == expected
    
    def test_quote_empty(self):
        assert DiscoOps._quote_lines("") == ""
        assert DiscoOps._quote_lines(None) == ""
```

**To run tests**:
```bash
pip install pytest pytest-asyncio
pytest tests/
```

**Estimated Time**: 8-10 hours for comprehensive suite  
**Impact**: High (long-term) - Maintainability and confidence

---

### 4.2 Add Comprehensive Docstrings

**Example for `_event_match`**:

**Current**:
```python
@classmethod
def _event_match(cls, events, query: str):
    """Find event by normalized exact name, then partial match."""
```

**Enhanced**:
```python
@classmethod
def _event_match(
    cls, 
    events: List[discord.ScheduledEvent], 
    query: str
) -> Optional[discord.ScheduledEvent]:
    """
    Find a scheduled event by name with fuzzy matching.
    
    Uses a two-phase matching strategy:
    1. First attempts exact match (case-insensitive, normalized)
    2. Falls back to partial/substring match if exact match fails
    
    Args:
        events: List of Discord scheduled events to search through
        query: User-provided event name query
        
    Returns:
        Matching ScheduledEvent object, or None if no match found
        
    Examples:
        >>> events = [event1, event2]  # event1.name = "Game Night"
        >>> _event_match(events, "game night")  # Returns event1
        >>> _event_match(events, "Game")        # Returns event1 (partial)
        >>> _event_match(events, "Unknown")     # Returns None
        
    Notes:
        - Text normalization handles quotes, diacritics, and case
        - Exact matches are preferred over partial matches
        - First partial match is returned if multiple matches exist
    """
```

**Estimated Time**: 2-3 hours  
**Impact**: Medium - Better code documentation

---

### 4.3 Performance Optimization for Large Guilds

**For very large guilds (100k+ members)**, consider:

```python
@members_group.command(name="new")
async def members_new(self, ctx, amount: int, period: str):
    """List members who joined in the last X days/weeks/months."""
    # ... validation code ...
    
    # Add progress indicator for large guilds
    if ctx.guild.member_count > 50000:
        progress_msg = await ctx.send("🔄 Processing members... (this may take a moment for large servers)")
    
    try:
        # ... existing member processing ...
        
        # Build output with progress updates
        if ctx.guild.member_count > 50000 and len(recent) > 1000:
            await progress_msg.edit(content=f"🔄 Found {len(recent)} members, preparing output...")
        
        # ... pagination code ...
        
    finally:
        if ctx.guild.member_count > 50000:
            await progress_msg.delete()
```

**Estimated Time**: 2-3 hours  
**Impact**: Low-Medium - Only relevant for very large guilds

---

## Implementation Roadmap

### Week 1: Critical Items
- [ ] Update README.md placeholder URL
- [ ] Update info.json author name
- [ ] Test installation with updated info

### Week 2-3: High Priority
- [ ] Refactor exception handling throughout
- [ ] Add role hierarchy checks
- [ ] Enhance error messages
- [ ] Test error handling improvements

### Week 4-5: Medium Priority  
- [ ] Add type hints to all methods
- [ ] Extract magic numbers to constants
- [ ] Refactor timestamp formatting
- [ ] Run type checker (mypy)

### Future Releases
- [ ] Create comprehensive test suite
- [ ] Add detailed docstrings
- [ ] Performance optimization for large guilds
- [ ] Add CI/CD pipeline

---

## Testing Checklist

After implementing changes, verify:

- [ ] All commands work as expected
- [ ] Error messages are clear and helpful
- [ ] Type hints don't break functionality
- [ ] Log rotation still works
- [ ] Event role workflow (create/sync/delete) functions
- [ ] Pagination works correctly
- [ ] Unicode event names handled properly
- [ ] Permission checks function correctly
- [ ] Bot works in small and large guilds

---

## Questions or Concerns?

If you have questions about any of these recommendations:
1. Review the CODE_REVIEW.md for detailed context
2. Test changes in a development Discord server first
3. Consider implementing changes incrementally
4. Keep the original behavior as a reference

**Remember**: These are recommendations, not requirements. Prioritize based on your users' needs and your development timeline.
