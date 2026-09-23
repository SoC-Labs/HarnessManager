"""QApplication bootstrap: engine -> System Selection -> the main window.

``socharness-gui`` uses the real ``socharness.engine.Engine``.
``socharness-gui --fake`` uses the scripted ``DemoEngine`` (three demo boards).
When the engine is not installed in this build the app says so in a dialog and
exits with code 12 (``ExitCode.UNAVAILABLE``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QObject, QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox

from socharness.core.errors import ExitCode, HarnessError
from socharness.core.model import Candidate
from socharness.core.pack import ProbeHints

from .bridge import EventBridge
from .main_window import MainWindow
from .selection import SelectionDialog
from .style import STYLESHEET
from .tabs.consoles import console_pool
from .worker import Runner, describe_error

log = logging.getLogger(__name__)

APP_NAME = "SoC Labs Harness Manager"
ENGINE_MISSING = ("The Harness Manager engine is not installed in this build "
                  "(socharness.engine is missing).\n\nInstall the full package, or run "
                  "socharness-gui --fake for the demo boards.")
SHUTDOWN_WAIT_MS = 5000


def acquire_engine(*, fake: bool = False, speed: float = 1.0) -> tuple[Any | None, int, str]:
    """(engine, 0, "") or (None, exit code, the message to show)."""
    if fake:
        from .demo_engine import DemoEngine

        return DemoEngine(speed=speed, console_chatter=True), int(ExitCode.OK), ""
    try:
        from socharness.engine import Engine
    except ModuleNotFoundError as exc:
        if exc.name == "socharness.engine":
            return None, int(ExitCode.UNAVAILABLE), ENGINE_MISSING
        raise
    try:
        return Engine(), int(ExitCode.OK), ""
    except HarnessError as exc:
        return None, int(exc.code), f"The engine could not start.\n\n{describe_error(exc)}"


class AppController(QObject):
    """Selection dialog first; the main window once a board is open; back again on request."""

    def __init__(self, engine: Any, *, hints: ProbeHints | None = None,
                 runner: Runner | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.hints = hints
        self.runner = runner or Runner(parent=self)
        self.bridge = EventBridge(engine.bus, parent=self)
        self.selection: SelectionDialog | None = None
        self.main_window: MainWindow | None = None
        self._closing: list[MainWindow] = []

    def start(self) -> None:
        self.show_selection()

    def show_selection(self, *, autoscan: bool = True) -> SelectionDialog:
        dlg = SelectionDialog(self.engine, self.runner, self.hints, autoscan=autoscan)
        dlg.setStyleSheet(STYLESHEET)
        dlg.board_opened.connect(self._open_main)
        self.selection = dlg
        dlg.show()
        return dlg

    def _open_main(self, candidate: Candidate) -> None:
        win = MainWindow(self.engine, candidate, self.runner, self.bridge)
        win.reselect_requested.connect(self._reselect)
        self.main_window = win
        win.show()          # shown before the dialog closes, so the app never has no window

    def _reselect(self) -> None:
        win = self.main_window
        if win is None:
            return
        self.main_window = None
        self._closing.append(win)
        dlg = self.show_selection(autoscan=False)     # scan once the engine has let go
        win.closed.connect(lambda _bid: self._after_close(win, dlg))
        win.close()

    def _after_close(self, win: MainWindow, dlg: SelectionDialog) -> None:
        if win in self._closing:
            self._closing.remove(win)
        dlg.rescan()

    def shutdown(self) -> None:
        """After the event loop: stop readers, close every board, wait for the workers."""
        if self.main_window is not None:
            self.main_window.shutdown()
        self.runner.wait(SHUTDOWN_WAIT_MS)
        try:
            self.engine.close_all()
        except Exception:  # noqa: BLE001 - exiting anyway; say why
            log.exception("closing the engine failed")
        self.bridge.close()
        console_pool().waitForDone(SHUTDOWN_WAIT_MS)


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="socharness-gui", description=APP_NAME)
    p.add_argument("--fake", action="store_true",
                   help="use the scripted demo engine (three demo boards, no hardware)")
    p.add_argument("--host", action="append", default=[],
                   help="a shell address to probe (host[:port]); repeatable")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args, qt_args = parser.parse_known_args(list(sys.argv[1:] if argv is None else argv))
    app = QApplication.instance() or QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)
    pool = QThreadPool.globalInstance()
    pool.setMaxThreadCount(max(8, pool.maxThreadCount()))

    engine, rc, message = acquire_engine(fake=args.fake)
    if engine is None:
        QMessageBox.critical(None, APP_NAME, message)
        return rc

    hints = ProbeHints(hosts=tuple(args.host)) if args.host else None
    ctl = AppController(engine, hints=hints)
    ctl.start()
    rc = app.exec()
    ctl.shutdown()
    return int(rc)
