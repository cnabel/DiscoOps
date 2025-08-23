# File: cogs/discoops/__init__.py

"""
DiscoOps - Discord Red Bot Cog
Operational features to make Discord server management easier.
"""

try:
    from .discoops import DiscoOps
except ImportError as e:
    print(f"Failed to import DiscoOps: {e}")
    DiscoOps = None

__red_end_user_data_statement__ = (
    "This cog stores Discord event role mappings per guild. "
    "It does not store any personal user data. "
    "To delete the data, unload the cog and reset the config."
)


async def setup(bot):
    """Load the DiscoOps cog."""
    if DiscoOps is None:
        raise ImportError("DiscoOps cog could not be imported")
    cog = DiscoOps(bot)
    await bot.add_cog(cog)
