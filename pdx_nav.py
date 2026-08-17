#!/usr/bin/env python3
"""Thin wrapper: delegates to the pdx_nav package.
Kept so the existing symlink (~/.local/bin/pdx-nav -> pdx_nav.py) keeps working.
"""
import sys
from pathlib import Path

# When invoked as a script, the package directory sits next to this file.
# Ensure it's importable even if cwd is elsewhere.
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from pdx_nav.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main() or 0)
