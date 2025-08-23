# File: cogs/discoops/test_discoops.py

import unittest
from unittest.mock import Mock, MagicMock, AsyncMock, patch, PropertyMock
from datetime import datetime, timedelta
import sys
import os

# Add the cog directory to path if needed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TestDiscoOps(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the DiscoOps cog."""
    
    async def asyncSetUp(self):
        """Set up test fixtures."""
        # Mock discord module if not available
        if 'discord' not in sys.modules:
            sys.modules['discord'] = MagicMock()
            sys.modules['discord.ext'] = MagicMock()
            sys.modules['discord.ext.commands'] = MagicMock()
        
        # Mock redbot modules if not available
        if 'redbot' not in sys.modules:
            sys.modules['redbot'] = MagicMock()
            sys.modules['redbot.core'] = MagicMock()
            sys.modules['redbot.core.commands'] = MagicMock()
            sys.modules['redbot.core.Config'] = MagicMock()
        
        # Import after mocking
        global discord, Config, DiscoOps
        import discord
        from redbot.core import Config
        
        # Mock bot
        self.bot = Mock()
        self.bot.add_cog = AsyncMock()
        
        # Mock Config
        with patch('discoops.Config.get_conf') as mock_config:
            self.mock_config_instance = MagicMock()
            mock_config.return_value = self.mock_config_instance
            
            # Setup config methods
            self.mock_config_instance.register_guild = Mock()
            self.mock_config_instance.guild = Mock()
            
            # Import and initialize cog
            from discoops import DiscoOps
            self.cog = DiscoOps(self.bot)
        
        # Mock context
        self.ctx = AsyncMock()
        self.ctx.send = AsyncMock()
        self.ctx.guild = Mock(spec=discord.Guild)
        self.ctx.guild.id = 123456789
        self.ctx.author = Mock(spec=discord.Member)
        self.ctx.author.id = 987654321
        self.ctx.author.display_name = "TestUser"
        self.ctx.author.guild_permissions.manage_guild = True
        self.ctx.author.guild_permissions.manage_events = True
    
    # ========== Test Members Commands ==========
    
    async def test_members_new_recent_joins(self):
        """Test listing recently joined members."""
        # Create mock members with different join dates
        now = datetime.utcnow()
        
        member1 = Mock()
        member1.joined_at = now - timedelta(days=2)
        member1.display_name = "NewMember1"
        member1.id = 111
        
        member2 = Mock()
        member2.joined_at = now - timedelta(days=5)
        member2.display_name = "NewMember2"
        member2.id = 222
        
        member3 = Mock()
        member3.joined_at = now - timedelta(days=10)
        member3.display_name = "OldMember"
        member3.id = 333
        
        self.ctx.guild.members = [member1, member2, member3]
        
        # Test: Get members from last 7 days
        await self.cog.members_new(self.ctx, 7, "days")
        
        # Verify embed was sent
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args
        embed = call_args[1]['embed'] if 'embed' in call_args[1] else call_args[0][0]
        
        # Check embed content
        self.assertIn("New Members", embed.title)
        self.assertIn("Last 7 days", embed.title)
        self.assertEqual(len(embed.fields), 2)  # Should have 2 members
    
    async def test_members_new_no_recent_joins(self):
        """Test when no members joined recently."""
        # Create mock member with old join date
        old_member = Mock()
        old_member.joined_at = datetime.utcnow() - timedelta(days=30)
        old_member.display_name = "OldMember"
        
        self.ctx.guild.members = [old_member]
        
        # Test: Get members from last 7 days
        await self.cog.members_new(self.ctx, 7, "days")
        
        # Verify message sent
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args[0][0]
        self.assertIn("No members joined", call_args)
    
    async def test_members_new_invalid_period(self):
        """Test with invalid time period."""
        await self.cog.members_new(self.ctx, 7, "years")
        
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args[0][0]
        self.assertIn("Period must be", call_args)
    
    async def test_members_role_with_members(self):
        """Test listing members with a specific role."""
        # Create mock role
        role = Mock()
        role.name = "Moderator"
        role.id = 456
        role.color = MagicMock()
        role.created_at = datetime.utcnow() - timedelta(days=100)
        role.position = 5
        role.mentionable = True
        
        # Create mock members with role
        member1 = Mock()
        member1.display_name = "Mod1"
        member1.mention = "@Mod1"
        member1.id = 111
        
        member2 = Mock()
        member2.display_name = "Mod2"
        member2.mention = "@Mod2"
        member2.id = 222
        
        role.members = [member1, member2]
        
        # Test command
        await self.cog.members_role(self.ctx, role=role)
        
        # Verify embed was sent
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args
        embed = call_args[1]['embed'] if 'embed' in call_args[1] else call_args[0][0]
        
        # Check embed content
        self.assertIn("Moderator", embed.title)
        self.assertIn("Total: 2 member(s)", embed.description)
        # Should have member list and role statistics
        self.assertGreaterEqual(len(embed.fields), 2)
    
    async def test_members_role_empty(self):
        """Test listing members when role has no members."""
        # Create mock role with no members
        role = Mock()
        role.name = "EmptyRole"
        role.members = []
        role.color = MagicMock()
        role.created_at = datetime.utcnow()
        role.position = 1
        role.mentionable = False
        
        await self.cog.members_role(self.ctx, role=role)
        
        # Verify embed was sent
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args
        embed = call_args[1]['embed'] if 'embed' in call_args[1] else call_args[0][0]
        
        # Check embed shows no members
        self.assertIn("Total: 0 member(s)", embed.description)
    
    # ========== Test Events Commands ==========
    
    async def test_events_list(self):
        """Test listing Discord scheduled events."""
        # Create mock events
        event1 = Mock()
        event1.name = "Game Night"
        event1.description = "Friday gaming session"
        event1.status = MagicMock()
        event1.status.name = "scheduled"
        event1.start_time = datetime.utcnow() + timedelta(days=2)
        event1.user_count = 5
        event1.location = "Discord Voice Channel"
        
        event2 = Mock()
        event2.name = "Movie Watch Party"
        event2.description = "Weekend movie"
        event2.status = MagicMock()
        event2.status.name = "active"
        event2.start_time = datetime.utcnow()
        event2.user_count = 10
        event2.location = None
        
        self.ctx.guild.fetch_scheduled_events = AsyncMock(return_value=[event1, event2])
        
        # Test command
        await self.cog.events_list(self.ctx)
        
        # Verify embed was sent
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args
        embed = call_args[1]['embed'] if 'embed' in call_args[1] else call_args[0][0]
        
        # Check embed content
        self.assertIn("Scheduled Events", embed.title)
        self.assertIn("Total: 2 event(s)", embed.description)
        self.assertEqual(len(embed.fields), 2)
    
    async def test_events_list_empty(self):
        """Test when no events exist."""
        self.ctx.guild.fetch_scheduled_events = AsyncMock(return_value=[])
        
        await self.cog.events_list(self.ctx)
        
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args[0][0]
        self.assertIn("No scheduled events found", call_args)
    
    async def test_events_members(self):
        """Test listing members interested in an event."""
        # Create mock event
        event = Mock()
        event.id = 999
        event.name = "Game Night"
        event.description = "Fun gaming"
        event.status = MagicMock()
        event.status.name = "scheduled"
        event.start_time = datetime.utcnow() + timedelta(days=1)
        
        # Create mock interested users
        user1 = Mock()
        user1.id = 111
        user2 = Mock()
        user2.id = 222
        
        # Create corresponding members
        member1 = Mock()
        member1.id = 111
        member1.display_name = "Player1"
        member1.mention = "@Player1"
        
        member2 = Mock()
        member2.id = 222
        member2.display_name = "Player2"
        member2.mention = "@Player2"
        
        # Setup async iterator for users
        async def async_users():
            for user in [user1, user2]:
                yield user
        
        event.users = async_users
        
        self.ctx.guild.fetch_scheduled_events = AsyncMock(return_value=[event])
        self.ctx.guild.get_member = Mock(side_effect=lambda id: member1 if id == 111 else member2)
        
        # Test command
        await self.cog.events_members(self.ctx, event_name="Game Night")
        
        # Verify embed was sent
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args
        embed = call_args[1]['embed'] if 'embed' in call_args[1] else call_args[0][0]
        
        # Check embed content
        self.assertIn("Game Night", embed.title)
        self.assertIn("Total: 2 member(s)", embed.description)
    
    async def test_events_members_not_found(self):
        """Test when event doesn't exist."""
        self.ctx.guild.fetch_scheduled_events = AsyncMock(return_value=[])
        
        await self.cog.events_members(self.ctx, event_name="Nonexistent Event")
        
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args[0][0]
        self.assertIn("not found", call_args)
    
    async def test_events_role_create(self):
        """Test creating a role for an event."""
        # Create mock event
        event = Mock()
        event.id = 999
        event.name = "Game Night"
        
        # Create mock user and member
        user1 = Mock()
        user1.id = 111
        
        member1 = Mock()
        member1.id = 111
        member1.add_roles = AsyncMock()
        
        # Setup async iterator for users
        async def async_users():
            yield user1
        
        event.users = async_users
        
        # Mock role creation
        new_role = Mock()
        new_role.id = 777
        new_role.mention = "@Event: Game Night"
        
        self.ctx.guild.fetch_scheduled_events = AsyncMock(return_value=[event])
        self.ctx.guild.get_member = Mock(return_value=member1)
        self.ctx.guild.create_role = AsyncMock(return_value=new_role)
        self.ctx.guild.get_role = Mock(return_value=None)
        
        # Mock config
        mock_event_roles = {}
        self.mock_config_instance.guild().event_roles = AsyncMock(return_value=mock_event_roles)
        self.mock_config_instance.guild().event_roles.__aenter__ = AsyncMock(return_value=mock_event_roles)
        self.mock_config_instance.guild().event_roles.__aexit__ = AsyncMock()
        
        # Test command
        await self.cog.events_role(self.ctx, "create", event_name="Game Night")
        
        # Verify role was created
        self.ctx.guild.create_role.assert_called_once()
        
        # Verify success message
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args[0][0]
        self.assertIn("Created role", call_args)
    
    # ========== Test Help Command ==========
    
    async def test_help_command(self):
        """Test the help command displays all information."""
        await self.cog.discoops_help(self.ctx)
        
        # Verify embed was sent
        self.ctx.send.assert_called_once()
        call_args = self.ctx.send.call_args
        embed = call_args[1]['embed'] if 'embed' in call_args[1] else call_args[0][0]
        
        # Check embed has all sections
        self.assertIn("DiscoOps Help", embed.title)
        # Should have member commands, event commands, and notes
        self.assertGreaterEqual(len(embed.fields), 3)


class TestDiscoOpsIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration tests for DiscoOps cog."""
    
    async def test_cog_setup(self):
        """Test that the cog can be set up properly."""
        # Mock discord and redbot modules
        if 'discord' not in sys.modules:
            sys.modules['discord'] = MagicMock()
        if 'redbot' not in sys.modules:
            sys.modules['redbot'] = MagicMock()
            sys.modules['redbot.core'] = MagicMock()
        
        bot = Mock()
        bot.add_cog = AsyncMock()
        
        # Import setup function
        from discoops import setup
        
        # Run setup
        await setup(bot)
        
        # Verify cog was added
        bot.add_cog.assert_called_once()
        cog_instance = bot.add_cog.call_args[0][0]
        self.assertIsNotNone(cog_instance)


if __name__ == '__main__':
    unittest.main()
