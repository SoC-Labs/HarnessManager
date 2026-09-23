"""Log tab: every bus event plus the GUI's own action results, coloured by level.

Consumes every event on the bus (``*``), the ``BoardContext.logged`` lines
(``$ action (rc N, T s)`` results and interlocks), and WARNING+ records from the
``socharness`` Python loggers. ``console.line`` events are hidden by default:
the Consoles tab already shows those bytes.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from socharness.core.events import Event

from ..bridge import EventBridge
from ..style import item
from .system import fill_row, make_table

MAX_ROWS = 5000
FILTERS = {"all": ("info", "ok", "warning", "error"), "warnings and errors": ("warning", "error"),
           "errors": ("error",)}


def event_level(ev: Event) -> str:
    """How an event is coloured. It reads the event; it decides nothing about the board."""
    topic, data = ev.topic, ev.data
    if topic.endswith(".failed") or data.get("state") == "failed":
        return "error"
    if topic == "deploy.done":
        return "ok" if data.get("verified") else "warning"
    if topic in ("board.lost", "session.closed") or data.get("state") in ("down", "error"):
        return "warning"
    return "info"


def event_text(ev: Event) -> str:
    if not ev.data:
        return ev.topic
    parts = [f"{k}={v}" for k, v in ev.data.items()]
    return f"{ev.topic}  " + "  ".join(parts)


class _LogRelay(QObject):
    record = Signal(str, str, str)


class _QtLogHandler(logging.Handler):
    """Python logging -> the Log tab, through a queued signal (any thread)."""

    def __init__(self, relay: _LogRelay) -> None:
        super().__init__(level=logging.WARNING)
        self.relay = relay

    def emit(self, record: logging.LogRecord) -> None:
        level = "error" if record.levelno >= logging.ERROR else "warning"
        try:
            self.relay.record.emit(level, record.name, record.getMessage())
        except RuntimeError:
            pass


class LogTab(QWidget):
    def __init__(self, bridge: EventBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItems(list(FILTERS))
        self.filter.currentIndexChanged.connect(self._apply_filter)
        self.show_console = QCheckBox("show console.line events")
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear)
        top.addWidget(QLabel("Show:"))
        top.addWidget(self.filter)
        top.addWidget(self.show_console)
        top.addStretch(1)
        top.addWidget(self.clear_button)
        v.addLayout(top)
        self.table: QTableWidget = make_table(["Time", "Level", "Source", "Board", "Message"])
        self.table.setObjectName("event_log")
        v.addWidget(self.table, 1)

        bridge.event.connect(self.on_event)
        self._relay = _LogRelay(self)
        self._relay.record.connect(self.add)
        self._handler = _QtLogHandler(self._relay)
        logging.getLogger("socharness").addHandler(self._handler)

    def on_event(self, ev: Event) -> None:
        if ev.topic == "console.line" and not self.show_console.isChecked():
            return
        self.add(event_level(ev), ev.topic.split(".")[0], event_text(ev), ev.board_id, ev.at)

    def add(self, level: str, source: str, text: str, board: str = "",
            at: float | None = None) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(at if at is not None else time.time()))
        row = self.table.rowCount()
        self.table.insertRow(row)
        message = text.replace("\n", "  |  ")
        fill_row(self.table, row, [item(stamp, level), item(level.upper(), level),
                                   item(source, level), item(board, level),
                                   item(message, level, tooltip=text)])
        self.table.setRowHidden(row, level not in FILTERS[self.filter.currentText()])
        if self.table.rowCount() > MAX_ROWS:
            self.table.removeRow(0)
        self.table.scrollToBottom()

    def rows(self) -> list[tuple[str, str]]:
        """(level, message) for every row, oldest first."""
        return [(self.table.item(r, 1).text().lower(), self.table.item(r, 4).text())
                for r in range(self.table.rowCount())]

    def _apply_filter(self) -> None:
        shown = FILTERS[self.filter.currentText()]
        for r in range(self.table.rowCount()):
            self.table.setRowHidden(r, self.table.item(r, 1).text().lower() not in shown)

    def clear(self) -> None:
        self.table.setRowCount(0)

    def shutdown(self) -> None:
        logging.getLogger("socharness").removeHandler(self._handler)
