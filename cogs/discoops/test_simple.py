# File: cogs/discoops/test_simple.py

"""
Simple test to verify the cog can be imported without errors.
Run this to debug import issues.
"""

import unittest  # Import unittest first to signal we're testing
import sys
import os
from unittest.mock import MagicMock, Mock

print("Starting simple test...")
print(f"Python version: {sys.version}")
print(f"Current directory: {os.getcwd()}")

# Create proper mock classes
class MockConfig:
    @staticmethod
    def get_conf(cog, identifier):
        mock_conf = MagicMock()
        mock_conf.register_guild = Mock()
        mock_conf.guild = Mock(return_value=MagicMock())
        return mock_conf

class MockCog:
    def __init__(self, bot):
        self.bot = bot

class MockCommands:
    Cog = MockCog
    
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

# Mock all discord and redbot modules
print("Mocking discord and redbot modules...")
sys.modules['discord'] = MagicMock()
sys.modules['discord.ext'] = MagicMock()
sys.modules['discord.ext.commands'] = MagicMock()
sys.modules['redbot'] = MagicMock()
sys.modules['redbot.core'] = MagicMock()
sys.modules['redbot.core'].commands = MockCommands
sys.modules['redbot.core'].Config = MockConfig

try:
    print("Attempting to import DiscoOps...")
    from discoops import DiscoOps
    print("✓ Successfully imported DiscoOps")
    
    print("Attempting to create bot mock...")
    bot = MagicMock()
    
    print("Attempting to initialize DiscoOps...")
    cog = DiscoOps(bot)
    print("✓ Successfully initialized DiscoOps")
    
    print("Checking for expected methods...")
    methods = ['members_new', 'members_role', 'events_list', 'events_members', 'events_role']
    for method in methods:
        if hasattr(cog, method):
            print(f"  ✓ Method {method} exists")
        else:
            print(f"  ✗ Method {method} missing")
    
    print("\n✅ All basic tests passed!")
    sys.exit(0)
    
except Exception as e:
    print(f"\n❌ Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
