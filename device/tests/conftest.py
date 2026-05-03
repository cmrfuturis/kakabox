"""Shared pytest fixtures and sys.path setup for the device test suite."""
import sys
from pathlib import Path

# Ensure that ``import audio.cache`` etc. works when running pytest from anywhere.
DEVICE_ROOT = Path(__file__).resolve().parent.parent
if str(DEVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(DEVICE_ROOT))
