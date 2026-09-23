"""PySide6 front-end (Team T6). Renders what the engine reports; never talks to a board.

This package imports without PySide6: everything Qt lives in submodules that
``main`` loads only after checking PySide6 is installed.

- ``app``: the bootstrap (engine -> System Selection -> the main window);
- ``selection``, ``main_window``, ``tabs/*``: the windows;
- ``panels``: panels described as data, and their one renderer;
- ``worker``: every engine call on a worker thread, with a time budget;
- ``bridge``: EventBus -> Qt signals, on the GUI thread;
- ``demo_engine``: a scripted Engine for ``--fake`` and tests (no Qt).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("socharness-gui: PySide6 is not installed: pip install 'socharness[gui]'",
              file=sys.stderr)
        return 12
    from .app import main as app_main

    return app_main(argv)
