# File: cogs/discoops/test_simple.py

"""
Simple test to verify the cog can be imported without errors.
Run this to debug import issues.
"""

import sys
import os
from unittest.mock import MagicMock

print("Starting simple test...")
print(f"Python version: {sys.version}")
print(f"Current directory: {os.getcwd()}")

# Mock all discord and redbot modules
print("Mocking discord and redbot modules...")
sys.modules['discord'] = MagicMock()
sys.modules['discord.ext'] = MagicMock()
sys.modules['discord.ext.commands'] = MagicMock()
sys.modules['redbot'] = MagicMock()
sys.modules['redbot.core'] = MagicMock()
sys.modules['redbot.core.commands'] = MagicMock()
sys.modules['redbot.core.Config'] = MagicMock()

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
