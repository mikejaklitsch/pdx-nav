"""Terminal output helpers: color detection, ANSI codes."""

import os
import sys

_use_color = None


def color_enabled(mode='auto'):
    """Check if color output should be used.
    mode: 'auto' (detect), 'always', or 'never'."""
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    global _use_color
    if _use_color is None:
        if os.environ.get('NO_COLOR'):
            _use_color = False
        elif os.environ.get('TERM') == 'dumb':
            _use_color = False
        else:
            _use_color = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
    return _use_color


C_RESET = "\033[0m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
