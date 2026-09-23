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


def _ensure_xcb_cursor() -> str | None:
    """Qt >= 6.5 needs libxcb-cursor.so.0 for the X11 (xcb) platform plugin.

    RHEL 8 hosts (e.g. srv03335) often lack it, and Qt then ABORTS the process
    with a core dump. That cannot be caught. So: if the system has no copy,
    preload a private one (``make gui-syslibs`` puts it in
    ``<venv>/lib/socharness-syslibs``; ``$SOCHARNESS_SYSLIBS`` overrides).
    Loading it first with RTLD_GLOBAL satisfies the plugin's dependency by
    soname. Returns an error message if xcb would fail, else None.
    """
    import ctypes
    import ctypes.util
    import os

    if sys.platform != "linux":
        return None
    platform = os.environ.get("QT_QPA_PLATFORM", "")
    if platform and not platform.startswith("xcb"):
        return None                        # offscreen/wayland/vnc do not need it
    if ctypes.util.find_library("xcb-cursor"):
        return None
    dirs = [os.environ.get("SOCHARNESS_SYSLIBS", ""),
            os.path.join(sys.prefix, "lib", "socharness-syslibs")]
    for d in dirs:
        candidate = os.path.join(d, "libxcb-cursor.so.0") if d else ""
        if candidate and os.path.exists(candidate):
            try:
                ctypes.CDLL(candidate, mode=ctypes.RTLD_GLOBAL)
                return None
            except OSError:
                continue
    if not os.environ.get("DISPLAY") and not platform:
        return None                        # no X display: Qt will pick another platform or fail cleanly
    return ("socharness-gui: this system lacks libxcb-cursor.so.0, which Qt needs for X11 — "
            "run `make gui-syslibs` in the harness-manager repo (no root needed), install the "
            "xcb-util-cursor package, or set QT_QPA_PLATFORM=vnc to serve the GUI over VNC")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("socharness-gui: PySide6 is not installed: pip install 'socharness[gui]'",
              file=sys.stderr)
        return 12
    problem = _ensure_xcb_cursor()
    if problem:
        print(problem, file=sys.stderr)
        return 12
    from .app import main as app_main

    return app_main(argv)
