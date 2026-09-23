"""The main window: "Harness Manager - <board>", one tab per job, ConfPro-style.

It is created only after ``engine.open()`` succeeded (in the selection
dialog), and closing it closes the board: ``engine.close(board_id)`` on a
worker, releasing the session lock.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import QMainWindow, QTabWidget, QVBoxLayout, QWidget

from socharness.core.events import Event
from socharness.core.model import BoardInfo, Candidate

from .bridge import EventBridge
from .context import BoardContext
from .style import STYLESHEET, check_level, level_label, set_level
from .tabs.consoles import ConsolesTab
from .tabs.debug import DebugTab
from .tabs.log import LogTab, event_text
from .tabs.placeholders import board_xdc_tab, clocks_tab
from .tabs.program import ProgramTab
from .tabs.reset import ResetTab
from .tabs.system import SystemTab
from .worker import Runner, TaskResult, describe_error

log = logging.getLogger(__name__)

WINDOW_SIZE = (1280, 800)
TITLE = "Harness Manager — {board}"


class MainWindow(QMainWindow):
    reselect_requested = Signal()
    closed = Signal(str)                   # board_id, once engine.close() has returned

    def __init__(self, engine: Any, candidate: Candidate, runner: Runner, bridge: EventBridge,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.candidate = candidate
        self.board_id = candidate.board_id
        self.runner = runner
        self._shut = False
        self.setWindowTitle(TITLE.format(board=candidate.board_id))
        self.setStyleSheet(STYLESHEET)
        self.ctx = BoardContext(engine, candidate, runner, bridge, parent=self)

        central = QWidget()
        v = QVBoxLayout(central)
        self.header = level_label(f"{candidate.label or candidate.board_id}   reading...")
        self.header.setObjectName("board_header")
        v.addWidget(self.header)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("board_tabs")
        self.system = SystemTab(self.ctx)
        self.program = ProgramTab(self.ctx)
        self.consoles = ConsolesTab(self.ctx)
        self.debug = DebugTab(self.ctx)
        self.reset = ResetTab(self.ctx)
        self.clocks = clocks_tab(self.ctx)
        self.board_xdc = board_xdc_tab(self.ctx)
        self.log = LogTab(bridge)
        for widget, name in ((self.system, "System"), (self.program, "Program"),
                             (self.consoles, "Consoles"), (self.debug, "Debug"),
                             (self.reset, "Reset"), (self.clocks, "Clocks"),
                             (self.board_xdc, "Board && XDC"), (self.log, "Log")):
            self.tabs.addTab(widget, name)
        v.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        menu = self.menuBar().addMenu("&File")
        reselect = QAction("Select another board...", self)
        reselect.triggered.connect(self.reselect_requested.emit)
        menu.addAction(reselect)
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

        self.ctx.logged.connect(self._log_line)
        self.ctx.info_changed.connect(self._render_header)
        self.ctx.board_event.connect(self._status_event)
        self.resize(*WINDOW_SIZE)
        self.ctx.refresh_info()

    def tab_names(self) -> list[str]:
        return [self.tabs.tabText(i).replace("&&", "&") for i in range(self.tabs.count())]

    def _log_line(self, level: str, source: str, text: str) -> None:
        self.log.add(level, source, text, self.board_id)

    def _status_event(self, ev: Event) -> None:
        if ev.topic != "console.line":
            self.statusBar().showMessage(event_text(ev), 8000)

    def _render_header(self, info: BoardInfo | None) -> None:
        if info is None:
            self.header.setText(f"{self.candidate.board_id}   cannot read the board: "
                                f"{self.ctx.info_error.splitlines()[0]}")
            set_level(self.header, "error")
            return
        ident = info.identity
        self.header.setText(
            f"{info.candidate.label or self.board_id}   |   design {ident.rm_name or '?'} "
            f"({ident.rm_id or '?'})   |   harness {ident.harness_version or '?'}, build "
            f"{ident.build_check.value}   |   session: yours")
        set_level(self.header, "warning" if check_level(ident.build_check) != "ok" else "info")

    # -- closing --------------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop readers and hand the board back to the engine. Never blocks."""
        if self._shut:
            return
        self._shut = True
        self.consoles.shutdown()
        self.log.shutdown()
        if not self.ctx.session_open:
            self.closed.emit(self.board_id)
            return
        engine, board_id = self.engine, self.board_id
        self.runner.submit(lambda: engine.close(board_id), self._closed,
                           label=f"close {board_id}", budget_s=30.0)

    def _closed(self, result: TaskResult) -> None:
        if not result.ok:
            log.warning("closing %s failed: %s", self.board_id, describe_error(result.error))
        self.closed.emit(self.board_id)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self.shutdown()
        super().closeEvent(event)
