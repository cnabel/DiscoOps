# File: cogs/discoops/test_discoops.py

import unittest  # Import unittest FIRST so the cog knows we're testing
import sys
import os
from unittest.mock import Mock, MagicMock, AsyncMock, patch, create_autospec
from datetime import datetime, timedelta

# Add the cog directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Create proper mock classes instead of MagicMock
class MockConfig:
    @staticmethod
    def get_conf(cog, identifier):
        mock_conf = MagicMock()
        mock_conf.register_guild = Mock()
        mock_conf.guild = Mock(return_value=MagicMock())
        return mock_conf

class MockCommands:
    class Cog:
        def __init__(self, bot):
            self.bot = bot

    class Context:
        pass

    @staticmethod
    def group(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

    @staticmethod
    def command(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

    @staticmethod
    def guild_only():
        def decorator(func):
            return func
        return decorator

    @staticmethod
    def has_permissions(**perms):
        def decorator(func):
            return func
        return decorator

# Mock the modules before importing
sys.modules['discord'] = MagicMock()
sys.modules['discord.ext'] = MagicMock()
sys.modules['discord.ext.commands'] = MagicMock()
sys.modules['redbot'] = MagicMock()
sys.modules['redbot.core'] = MagicMock()

# Set up the specific mocks we need
sys.modules['redbot.core'].commands = MockCommands
sys.modules['redbot.core'].Config = MockConfig

# Now import the cog
from discoops import DiscoOps


class TestDiscoOps(unittest.TestCase):
    """Unit tests for the DiscoOps cog."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Mock bot
        self.bot = Mock()
        self.bot.add_cog = AsyncMock()
        
        # Create the cog
        self.cog = DiscoOps(self.bot)
        
        # Mock context
        self.ctx = Mock()
        self.ctx.send = AsyncMock()
        self.ctx.guild = Mock()
        self.ctx.guild.id = 123456789
        self.ctx.guild.members = []
        self.ctx.author = Mock()
        self.ctx.author.id = 987654321
        self.ctx.author.display_name = "TestUser"
    
    def test_cog_initialization(self):
        """Test that the cog initializes properly."""
        self.assertIsNotNone(self.cog)
        self.assertEqual(self.cog.bot, self.bot)
    
    def test_members_new_command_exists(self):
        """Test that members_new command exists."""
        self.assertTrue(hasattr(self.cog, 'members_new'))
    
    def test_members_role_command_exists(self):
        """Test that members_role command exists."""
        self.assertTrue(hasattr(self.cog, 'members_role'))
    
    def test_events_list_command_exists(self):
        """Test that events_list command exists."""
        self.assertTrue(hasattr(self.cog, 'events_list'))
    
    def test_events_members_command_exists(self):
        """Test that events_members command exists."""
        self.assertTrue(hasattr(self.cog, 'events_members'))
    
    def test_events_role_command_exists(self):
        """Test that events_role command exists."""
        self.assertTrue(hasattr(self.cog, 'events_role'))


class TestDiscoOpsAsync(unittest.IsolatedAsyncioTestCase):
    """Async unit tests for the DiscoOps cog."""
    
    async def asyncSetUp(self):
        """Set up async test fixtures."""
        # Mock bot
        self.bot = Mock()
        self.bot.add_cog = AsyncMock()
        
        # Create the cog
        self.cog = DiscoOps(self.bot)
        
        # Mock context
        self.ctx = Mock()
        self.ctx.send = AsyncMock()
        self.ctx.guild = Mock()
        self.ctx.guild.id = 123456789
        self.ctx.guild.members = []
        self.ctx.guild.fetch_scheduled_events = AsyncMock(return_value=[])
        self.ctx.author = Mock()
        self.ctx.author.id = 987654321
        self.ctx.author.display_name = "TestUser"
    
    async def test_members_new_no_recent(self):
        """Test members_new when no recent members."""
        # Setup
        old_member = Mock()
        old_member.joined_at = datetime.utcnow() - timedelta(days=30)
        old_member.display_name = "OldMember"
        self.ctx.guild.members = [old_member]
        
        # Execute
        await self.cog.members_new(self.ctx, 7, "days")
        
        # Assert
        self.ctx.send.assert_called_once()
        args = self.ctx.send.call_args
        self.assertIn("No members joined", str(args))
    
    async def test_members_new_invalid_period(self):
        """Test members_new with invalid period."""
        await self.cog.members_new(self.ctx, 7, "invalid")
        
        self.ctx.send.assert_called_once()
        args = self.ctx.send.call_args
        self.assertIn("Period must be", str(args))
    
    async def test_events_list_no_events(self):
        """Test events_list when no events exist."""
        await self.cog.events_list(self.ctx)
        
        self.ctx.send.assert_called_once()
        args = self.ctx.send.call_args
        self.assertIn("No scheduled events", str(args))
    
    async def test_events_members_not_found(self):
        """Test events_members when event doesn't exist."""
        await self.cog.events_members(self.ctx, event_name="NonExistent")
        
        self.ctx.send.assert_called_once()
        args = self.ctx.send.call_args
        self.assertIn("not found", str(args))


class TestDiscoOpsSetup(unittest.IsolatedAsyncioTestCase):
    """Test the setup function."""
    
    async def test_setup_function(self):
        """Test that setup function works."""
        bot = Mock()
        bot.add_cog = AsyncMock()
        
        from discoops import setup
        await setup(bot)
        
        bot.add_cog.assert_called_once()
        # Check that a DiscoOps instance was passed
        call_args = bot.add_cog.call_args[0]
        self.assertIsInstance(call_args[0], DiscoOps)


if __name__ == '__main__':
    # Run with verbose output
    unittest.main(verbosity=2)
