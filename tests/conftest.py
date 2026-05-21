"""Shared test configuration for battle_buddy tests."""
import sys
from pathlib import Path

# Ensure the project root is on sys.path so all test files can import
# project modules without per-file sys.path hacks (avoids ruff E402).
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
