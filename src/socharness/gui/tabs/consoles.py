"""Consoles tab: one sub-tab per console, a read-only terminal view, a send line, Export to TCP.

Engine calls (on workers): ``engine.consoles.names(session)``,
``engine.consoles.subscribe(session, name)`` -> ``ConsoleStream``,
``stream.read(timeout)`` (a dedicated reader thread per console),
``stream.write(data)``, ``engine.consoles.export_tcp(session, name, 0)``.
Events consumed: ``console.state {name, state}``. Console bytes come from the
stream, not from ``console.line`` events, so a line is never shown twice.
"""

from __future__ import annotations

import codecs
import threading

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from socharness.core import capabilities as C
from socharness.core.events import Event
from socharness.core.model import BoardInfo

from ..context import BoardContext
from ..style import fixed_font, level_label, role_label, set_level
from ..worker import TaskResult, describe_error, rc_line

CONSOLE_CAPS = (C.CONSOLE_DUT, C.CONSOLE_SHELL, C.CONSOLE_CONTROLLER)
ENDINGS = {"LF": "\n", "CR": "\r", "CRLF": "\r\n"}
READ_TIMEOUT_S = 0.2

_POOL: QThreadPool | None = None


def console_pool() -> QThreadPool:
    """Long-lived readers get their own pool, so they never starve engine calls."""
    global _POOL
    if _POOL is None:
        _POOL = QThreadPool()
        _POOL.setMaxThreadCount(32)
    return _POOL


class _ReaderSignals(QObject):
    data = Signal(bytes)
    ended = Signal(object)          # None (stopped) or the exception that ended the stream


class _Reader(QRunnable):
    """Loops ``stream.read(timeout)`` on a pool thread until stopped."""

    def __init__(self, stream: object, signals: _ReaderSignals) -> None:
        super().__init__()
        self.stream = stream
        self.signals = signals
        self.stop = threading.Event()

    def run(self) -> None:
        error: BaseException | None = None
        try:
            while not self.stop.is_set():
                chunk = self.stream.read(READ_TIMEOUT_S)  # type: ignore[attr-defined]
                if chunk and not self.stop.is_set():
                    self.signals.data.emit(bytes(chunk))
        except BaseException as exc:  # noqa: BLE001 - reported on the GUI thread
            error = exc
        finally:
            try:
                self.stream.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001, S110 - closing a dead stream is best effort
                pass
        try:
            self.signals.ended.emit(None if self.stop.is_set() else error)
        except RuntimeError:
            pass


class ConsoleView(QWidget):
    """One console: the terminal view, its state, the send line and the export."""

    def __init__(self, ctx: BoardContext, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.name = name
        self.stream: object | None = None
        self._reader: _Reader | None = None
        self._signals: _ReaderSignals | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.state = level_label("not connected", "warning", wrap=False)
        self.state.setObjectName(f"console_state_{name}")
        self.export_button = QPushButton("Export to TCP")
        self.export_button.clicked.connect(self.export)
        self.export_label = role_label("", "muted")
        self.reconnect_button = QPushButton("Reconnect")
        self.reconnect_button.clicked.connect(self.start)
        self.reconnect_button.setVisible(False)
        top.addWidget(self.state)
        top.addWidget(self.export_label, 1)
        top.addWidget(self.reconnect_button)
        top.addWidget(self.export_button)
        v.addLayout(top)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(fixed_font())
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.view.setMaximumBlockCount(5000)
        self.view.setObjectName(f"console_view_{name}")
        v.addWidget(self.view, 1)

        send = QHBoxLayout()
        self.line = QLineEdit()
        self.line.setPlaceholderText(f"a line for {name}; Enter sends it")
        self.line.returnPressed.connect(self.send)
        self.ending = QComboBox()
        self.ending.addItems(list(ENDINGS))
        self.ending.setToolTip("what ends each line you send")
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self.send)
        send.addWidget(QLabel("Send:"))
        send.addWidget(self.line, 1)
        send.addWidget(self.ending)
        send.addWidget(self.send_button)
        v.addLayout(send)
        self.status = role_label("", "muted")
        v.addWidget(self.status)
        self._set_connected(False)

    # -- subscribe and read -------------------------------------------------------------

    def start(self) -> None:
        if self._reader is not None:
            return
        engine, board_id, name = self.ctx.engine, self.ctx.board_id, self.name
        self.state.setText("connecting...")
        set_level(self.state, "info")
        self.reconnect_button.setVisible(False)
        self.ctx.runner.submit(lambda: engine.consoles.subscribe(engine.session(board_id), name),
                               self._subscribed, label=f"console {name}", budget_s=20.0)

    def _subscribed(self, result: TaskResult) -> None:
        if not result.ok:
            self.state.setText("not connected")
            set_level(self.state, "error")
            self.status.setText(f"{rc_line(f'console {self.name}', result)}: "
                                f"{describe_error(result.error)}")
            self.reconnect_button.setVisible(True)
            return
        self.stream = result.value
        self._signals = _ReaderSignals(self)
        self._signals.data.connect(self._on_data, Qt.ConnectionType.QueuedConnection)
        self._signals.ended.connect(self._on_ended, Qt.ConnectionType.QueuedConnection)
        self._reader = _Reader(self.stream, self._signals)
        console_pool().start(self._reader)
        self._set_connected(True)
        self.status.setText(rc_line(f"console {self.name}", result))
        set_level(self.status, "info")
        self.state.setText("connected")
        set_level(self.state, "ok")

    @Slot(bytes)
    def _on_data(self, chunk: bytes) -> None:
        text = self._decoder.decode(chunk).replace("\r\n", "\n").replace("\r", "")
        if not text:
            return
        bar = self.view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        if at_bottom:
            bar.setValue(bar.maximum())

    @Slot(object)
    def _on_ended(self, error: object) -> None:
        self._reader = None
        self.stream = None
        self._set_connected(False)
        if error is None:
            self.state.setText("closed")
            set_level(self.state, "info")
        else:
            self.state.setText("disconnected")
            set_level(self.state, "error")
            self.status.setText(describe_error(error))  # type: ignore[arg-type]
            self.reconnect_button.setVisible(True)

    def on_state(self, state: str) -> None:
        """A ``console.state`` event from the engine (reconnects across swaps, drops)."""
        if self._reader is None:
            return
        self.state.setText(state)
        set_level(self.state, "ok" if state in ("up", "connected") else "warning")

    def stop(self) -> None:
        if self._reader is not None:
            self._reader.stop.set()

    def text(self) -> str:
        return self.view.toPlainText()

    def _set_connected(self, connected: bool) -> None:
        for w in (self.line, self.send_button, self.ending, self.export_button):
            w.setEnabled(connected)
        if not connected:
            self.status.setText("Cannot send: the console is not connected")

    # -- send and export ------------------------------------------------------------------

    def send(self) -> None:
        text = self.line.text()
        if self.stream is None:
            self.status.setText("Cannot send: the console is not connected. Nothing was run.")
            return
        if not text:
            self.status.setText("Nothing to send: the line is empty. Nothing was run.")
            return
        data = (text + ENDINGS[self.ending.currentText()]).encode()
        stream = self.stream
        command = f"console {self.name} send {text!r}"
        self.ctx.runner.submit(lambda: stream.write(data),  # type: ignore[attr-defined]
                               lambda res: self._sent(command, res), label=command,
                               budget_s=10.0)

    def _sent(self, command: str, result: TaskResult) -> None:
        line = rc_line(command, result)
        if result.ok:
            self.line.clear()
            self.status.setText(line)
            set_level(self.status, "info")
        else:
            self.status.setText(f"{line}: {describe_error(result.error)}")
            set_level(self.status, "error")

    def export(self) -> None:
        engine, board_id, name = self.ctx.engine, self.ctx.board_id, self.name
        self.export_button.setEnabled(False)
        self.export_label.setText("exporting...")
        self.ctx.runner.submit(
            lambda: engine.consoles.export_tcp(engine.session(board_id), name, 0),
            self._exported, label=f"console {name} export", budget_s=20.0)

    def _exported(self, result: TaskResult) -> None:
        self.export_button.setEnabled(self.stream is not None)
        line = rc_line(f"console {self.name} export", result)
        if result.ok:
            port = int(result.value)
            self.export_label.setText(f"on 127.0.0.1:{port} (e.g. telnet 127.0.0.1 {port})")
            set_level(self.export_label, "ok")
            self.status.setText(line)
        else:
            self.export_label.setText("")
            self.status.setText(f"{line}: {describe_error(result.error)}")
            set_level(self.status, "error")


class ConsolesTab(QWidget):
    def __init__(self, ctx: BoardContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.views: dict[str, ConsoleView] = {}
        self._loading = False
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.status = level_label("waiting for the board's capability view", "info")
        self.refresh_button = QPushButton("Refresh consoles")
        self.refresh_button.clicked.connect(self.load)
        top.addWidget(self.status, 1)
        top.addWidget(self.refresh_button)
        v.addLayout(top)
        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)
        ctx.info_changed.connect(self._on_info)
        ctx.board_event.connect(self._on_event)

    def gate(self) -> str:
        if not self.ctx.session_open:
            return "no board session is open"
        if self.ctx.service_missing("consoles"):
            return f"Cannot: {self.ctx.service_missing('consoles')}"
        states = [self.ctx.capability(c) for c in CONSOLE_CAPS]
        if any(s is None for s in states):
            return "waiting for the board's capability view"
        if not any(s[0] for s in states if s is not None):
            reasons = "; ".join(f"{self.ctx.title(c)}: {s[1]}" for c, s in
                                zip(CONSOLE_CAPS, states, strict=True) if s is not None)
            return f"Cannot: {reasons}"
        return ""

    def _on_info(self, info: BoardInfo | None) -> None:
        if info is not None and not self.views and not self._loading:
            self.load()

    def load(self) -> None:
        why = self.gate()
        if why:
            self.status.setText(why)
            set_level(self.status, "warning")
            return
        self._loading = True
        engine, board_id = self.ctx.engine, self.ctx.board_id
        self.status.setText("asking the engine for this board's consoles...")
        self.ctx.runner.submit(lambda: list(engine.consoles.names(engine.session(board_id))),
                               self._names_done, label="consoles", budget_s=20.0)

    def _names_done(self, result: TaskResult) -> None:
        self._loading = False
        line = rc_line("consoles", result)
        if not result.ok:
            self.status.setText(f"{line}: {describe_error(result.error)}")
            set_level(self.status, "error")
            return
        names: list[str] = result.value
        if not names:
            self.status.setText(f"{line}: the engine reports no consoles for this board")
            set_level(self.status, "warning")
            return
        self.status.setText(f"{line}: {', '.join(names)}")
        set_level(self.status, "info")
        for name in names:
            if name in self.views:
                continue
            view = ConsoleView(self.ctx, name)
            self.views[name] = view
            self.tabs.addTab(view, name)
            view.start()

    def _on_event(self, ev: Event) -> None:
        if ev.topic == "console.state":
            view = self.views.get(str(ev.data.get("name", "")))
            if view is not None:
                view.on_state(str(ev.data.get("state", "")))

    def shutdown(self) -> None:
        for view in self.views.values():
            view.stop()
