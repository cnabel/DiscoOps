# File: cogs/discoops/__init__.py

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

# Try async setup first (Red 3.5+), fall back to sync if needed
try:
    from redbot.core.bot import Red
    
    async def setup(bot: Red):
        """Load the DiscoOps cog (Red 3.5+)."""
        await bot.add_cog(DiscoOps(bot))
        
except ImportError:
    # Fallback for older Red versions
    def setup(bot):
        """Load the DiscoOps cog (Red 3.4 and earlier)."""
        bot.add_cog(DiscoOps(bot))
