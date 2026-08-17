import os
import sys
from pathlib import Path

DEFAULT_VANILLA_ROOT = Path(
    "/mnt/d/Program Files (x86)/Steam/steamapps/common/Europa Universalis V/game")


def find_mod_root(start=None):
    """Walk up from start (or cwd) looking for .metadata/ to find mod root."""
    p = (start or Path.cwd()).resolve()
    while p != p.parent:
        if (p / ".metadata").is_dir():
            return p
        p = p.parent
    return None


def find_mod_root_or_exit(start=None):
    """Like find_mod_root but prints an error and exits if not found."""
    root = find_mod_root(start)
    if root is None:
        print("Error: could not find mod root (.metadata/ directory). "
              "Run from inside a mod directory.", file=sys.stderr)
        sys.exit(1)
    return root


def vanilla_root():
    env = os.environ.get("PDX_GAME_ROOT")
    return Path(env) if env else DEFAULT_VANILLA_ROOT
