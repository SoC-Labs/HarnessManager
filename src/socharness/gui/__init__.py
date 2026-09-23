"""PySide6 front-end (Team T6). Renders what the engine reports; never talks to a board."""

from __future__ import annotations

import sys


def main() -> int:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("socharness-gui: PySide6 is not installed — pip install 'socharness[gui]'",
              file=sys.stderr)
        return 12
    print("socharness-gui: the GUI arrives with Team T6", file=sys.stderr)
    return 12
