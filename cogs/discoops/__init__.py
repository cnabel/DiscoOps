"""
DiscoOps - Discord Red Bot Cog
Operational features to make Discord server management easier.
"""

from .discoops import DiscoOps

__red_end_user_data_statement__ = (
    "This cog stores Discord event role mappings per guild. "
    "It does not store any personal user data. "
    "To delete the data, unload the cog and reset the config."
)


async def setup(bot):
    """Load the DiscoOps cog."""
    cog = DiscoOps(bot)
    await bot.add_cog(cog)
