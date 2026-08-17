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


def find_mod_root_or_exit(start=None, override=None):
    """Like find_mod_root but prints an error and exits if not found.
    If override is given, use that path directly."""
    if override:
        return Path(override).resolve()
    root = find_mod_root(start)
    if root is None:
        print("Error: could not find mod root (.metadata/ directory). "
              "Run from inside a mod directory.", file=sys.stderr)
        sys.exit(1)
    return root


def vanilla_root():
    env = os.environ.get("PDX_GAME_ROOT")
    return Path(env) if env else DEFAULT_VANILLA_ROOT


def find_vanilla_repo(mod_root=None, override=None):
    """Locate the vanilla-tracker bare git repo.
    Precedence: override > $PDX_VANILLA_REPO > <mod-parent>/vanilla-tracker/repo.git."""
    if override:
        p = Path(override).resolve()
        if p.exists():
            return p
        return None

    env = os.environ.get("PDX_VANILLA_REPO")
    if env:
        p = Path(env).resolve()
        if p.exists():
            return p

    if mod_root:
        candidate = mod_root.parent / "vanilla-tracker" / "repo.git"
        if candidate.exists():
            return candidate

    return None
