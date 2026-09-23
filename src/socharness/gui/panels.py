"""Panels as data: a panel is a ``PanelDesc`` table; one renderer draws them all.

Ported from the HAPS "Hardware Debug" tab (HAPS-work/fpga/haps-sx/qtinject,
*A panel is data*): adding a button is a row in an actions table, and the
rules every button follows live in ONE place, ``PanelWidget``:

- the engine call runs on a worker thread (``Runner``), never the GUI thread;
- a running button shows its own ``busy_label`` and elapsed seconds; the other
  buttons of the panel are greyed and say what they are waiting for;
- each action has a time budget; past it the panel says "still running after
  N s" and stays usable, and the late answer is still shown when it lands;
- the answer box starts with ``$ <command>  (rc N, T s)``;
- a capability the board lacks disables the button AND shows the engine's
  reason as visible text ("Cannot: needs the Debug USB cable"), never a silent
  grey;
- an intrusive action is ``armed_by`` a tick box, which clears after each run;
- an interlock that stops a click says so and ends with "Nothing was run."

What a table cannot carry is a small builder per action: ``call`` (the engine
call, run on the worker), ``guard`` (a GUI-state reason to refuse), ``prepare``
(a snapshot of GUI state taken at click time) and ``render``/``on_result``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from .context import BoardContext
from .style import fixed_font, level_label, role_label, set_level
from .worker import TaskHandle, TaskResult, describe_error, rc_line, seconds_text

NOTHING_RUN = "Nothing was run."


@dataclass(frozen=True)
class PanelField:
    """One cell: a read-only value, a tick box, a pick list or a text edit."""

    key: str
    label: str
    kind: str = "value"           # "value" | "check" | "combo" | "edit"
    default: str = ""
    hint: str = ""                # placeholder / tooltip
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionEnv:
    """What an action's ``call`` gets on the worker thread. Never a widget."""

    engine: Any
    board_id: str
    values: Mapping[str, str]     # the fields, snapshotted on the GUI thread at click time
    arg: Any                      # whatever ``prepare`` returned
    progress: Callable[[str], None]   # thread-safe: append a line to the answer box

    def session(self) -> Any:
        return self.engine.session(self.board_id)


@dataclass(frozen=True)
class PanelAction:
    """One button."""

    key: str
    label: str
    call: Callable[[ActionEnv], Any]
    command: str | Callable[[Mapping[str, str], Any], str]   # the "$ ..." text
    busy_label: str = ""
    budget_s: float = 30.0
    armed_by: str | None = None                  # key of a "check" field
    enabled_when_capability: str | None = None
    needs_service: str | None = None              # "deploy" | "consoles" | "debug"
    needs_session: bool = True
    guard: Callable[[PanelWidget], str] | None = None      # "" = allowed, else why not
    prepare: Callable[[PanelWidget], Any] | None = None
    render: Callable[[Any], str] | None = None
    on_result: Callable[[PanelWidget, TaskResult], None] | None = None


@dataclass(frozen=True)
class PanelDesc:
    key: str
    title: str
    fields: tuple[PanelField, ...] = ()
    actions: tuple[PanelAction, ...] = ()
    answer_hint: str = ""          # placeholder text of the answer box
    answer_lines: int = 5          # 0 = no answer box


class PanelWidget(QGroupBox):
    """Renders a ``PanelDesc`` against one board."""

    action_finished = Signal(str, object)      # action key, TaskResult
    _progress_line = Signal(str)

    def __init__(self, desc: PanelDesc, ctx: BoardContext, parent: QWidget | None = None) -> None:
        super().__init__(desc.title, parent)
        self.desc = desc
        self.ctx = ctx
        self.setObjectName(f"panel_{desc.key}")
        self._actions = {a.key: a for a in desc.actions}
        self.values_widgets: dict[str, QWidget] = {}
        self.buttons: dict[str, QPushButton] = {}
        self.reasons: dict[str, QLabel] = {}
        self._running: str | None = None
        self._handle: TaskHandle | None = None
        self._progress_lines: list[str] = []
        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick)
        self._progress_line.connect(self._append_progress, Qt.ConnectionType.QueuedConnection)

        grid = QGridLayout(self)
        grid.setColumnStretch(1, 1)
        row = 0
        for fdesc in desc.fields:
            widget = self._build_field(fdesc)
            self.values_widgets[fdesc.key] = widget
            if fdesc.kind == "check":
                grid.addWidget(widget, row, 0, 1, 2)
            else:
                grid.addWidget(QLabel(fdesc.label), row, 0)
                grid.addWidget(widget, row, 1)
            row += 1
        for adesc in desc.actions:
            button = QPushButton(adesc.label)
            button.setObjectName(f"action_{adesc.key}")
            button.setMinimumWidth(150)
            button.clicked.connect(partial(self.trigger, adesc.key))
            reason = role_label("", "reason")
            reason.setObjectName(f"reason_{adesc.key}")
            self.buttons[adesc.key] = button
            self.reasons[adesc.key] = reason
            grid.addWidget(button, row, 0)
            grid.addWidget(reason, row, 1)
            row += 1
        self.answer: QPlainTextEdit | None = None
        if desc.answer_lines:
            box = QPlainTextEdit()
            box.setReadOnly(True)
            box.setFont(fixed_font())
            box.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            box.setPlaceholderText(desc.answer_hint)
            box.setMinimumHeight(box.fontMetrics().lineSpacing() * desc.answer_lines + 12)
            box.setObjectName(f"answer_{desc.key}")
            grid.addWidget(box, row, 0, 1, 2)
            grid.setRowStretch(row, 1)
            self.answer = box

        ctx.info_changed.connect(self.refresh_gating)
        ctx.session_changed.connect(self.refresh_gating)
        self.refresh_gating()

    # -- fields ---------------------------------------------------------------------------

    def _build_field(self, fdesc: PanelField) -> QWidget:
        if fdesc.kind == "check":
            box = QCheckBox(fdesc.label)
            box.setObjectName(f"field_{fdesc.key}")
            box.toggled.connect(self.refresh_gating)
            return box
        if fdesc.kind == "combo":
            combo = QComboBox()
            combo.addItems(list(fdesc.choices))
            combo.setToolTip(fdesc.hint)
            combo.currentIndexChanged.connect(self.refresh_gating)
            return combo
        if fdesc.kind == "edit":
            edit = QLineEdit(fdesc.default)
            edit.setPlaceholderText(fdesc.hint)
            edit.textChanged.connect(self.refresh_gating)
            return edit
        label = level_label(fdesc.default)
        label.setObjectName(f"field_{fdesc.key}")
        label.setToolTip(fdesc.hint)
        return label

    def value(self, key: str) -> str:
        w = self.values_widgets[key]
        if isinstance(w, QCheckBox):
            return "1" if w.isChecked() else ""
        if isinstance(w, QComboBox):
            return w.currentText()
        if isinstance(w, QLineEdit):
            return w.text()
        return w.text() if isinstance(w, QLabel) else ""

    def values(self) -> dict[str, str]:
        return {k: self.value(k) for k in self.values_widgets}

    def set_value(self, key: str, text: str, level: str | None = None) -> None:
        w = self.values_widgets[key]
        if isinstance(w, QLineEdit):
            w.setText(text)
        elif isinstance(w, QLabel):
            w.setText(text)
            if level is not None:
                set_level(w, level)
        self.refresh_gating()

    def set_checked(self, key: str, checked: bool) -> None:
        w = self.values_widgets[key]
        if isinstance(w, QCheckBox):
            w.setChecked(checked)

    def set_choices(self, key: str, choices: list[str]) -> None:
        w = self.values_widgets[key]
        if isinstance(w, QComboBox):
            current = w.currentText()
            w.blockSignals(True)
            w.clear()
            w.addItems(choices)
            if current in choices:
                w.setCurrentText(current)
            w.blockSignals(False)
        self.refresh_gating()

    # -- gating ---------------------------------------------------------------------------

    @property
    def running(self) -> str | None:
        return self._running

    def gate(self, key: str) -> str:
        """Why ``key`` cannot run now, or "" when it can."""
        action = self._actions[key]
        if action.needs_session and not self.ctx.session_open:
            return "no board session is open"
        if action.needs_service and self.ctx.service_missing(action.needs_service):
            return f"Cannot: {self.ctx.service_missing(action.needs_service)}"
        if action.enabled_when_capability:
            cap = self.ctx.capability(action.enabled_when_capability)
            if cap is None:
                return ("waiting for the board's capability view"
                        if not self.ctx.info_error else f"Cannot: {self.ctx.info_error}")
            available, reason = cap
            if not available:
                return f"Cannot: {reason}"
        if self._running is not None:
            if self._running == key:
                return "running"
            busy = self._actions[self._running]
            return f"waiting for {busy.busy_label or busy.label}"
        if action.guard is not None:
            why = action.guard(self)
            if why:
                return why
        if action.armed_by and not self.value(action.armed_by):
            box = self.values_widgets[action.armed_by]
            text = box.text() if isinstance(box, QCheckBox) else action.armed_by
            return f"tick '{text}' to arm it"
        return ""

    @Slot()
    def refresh_gating(self, *_: object) -> None:
        for key, button in self.buttons.items():
            action = self._actions[key]
            why = self.gate(key)
            reason = self.reasons[key]
            if self._running == key:
                button.setEnabled(False)
                button.setText(self._busy_text(action))
                reason.setText(self._running_text())
                continue
            button.setText(action.label)
            button.setEnabled(not why)
            reason.setText(why)
            reason.setToolTip(why)

    def _busy_text(self, action: PanelAction) -> str:
        busy = action.busy_label or f"{action.label}..."
        return f"{busy} {self._handle.elapsed():.0f} s" if self._handle else busy

    def _running_text(self) -> str:
        if self._handle is None:
            return "running"
        if self._handle.overdue:
            return (f"still running after {seconds_text(self._handle.elapsed())} s (budget "
                    f"{seconds_text(self._handle.budget_s)} s); the window stays usable")
        return f"running for {self._handle.elapsed():.0f} s"

    # -- running --------------------------------------------------------------------------

    def command_text(self, key: str, values: Mapping[str, str] | None = None,
                     arg: Any = None) -> str:
        action = self._actions[key]
        if callable(action.command):
            return action.command(values if values is not None else self.values(), arg)
        return action.command

    @Slot()
    def trigger(self, key: str, *_: object) -> bool:
        """The click path. Returns False (and says why) when an interlock stops it."""
        action = self._actions[key]
        why = self.gate(key)
        if why:
            self._interlock(action, why)
            return False
        values = self.values()
        arg = action.prepare(self) if action.prepare is not None else None
        command = self.command_text(key, values, arg)
        env = ActionEnv(self.ctx.engine, self.ctx.board_id, values, arg,
                        self._progress_line.emit)
        self._running = key
        self._progress_lines = []
        self.show_answer(f"$ {command}  (running)")
        self._handle = self.ctx.runner.submit(
            partial(action.call, env), partial(self._finished, action, command),
            label=command, budget_s=action.budget_s, on_overdue=partial(self._overdue, action))
        self._ticker.start()
        self.refresh_gating()
        return True

    def _interlock(self, action: PanelAction, why: str) -> None:
        command = self.command_text(action.key)
        self.show_answer(f"$ {command}  (not run)\n{why}. {NOTHING_RUN}")
        self.ctx.log("warning", self.desc.title, f"{action.label}: {why}. {NOTHING_RUN}")

    def _overdue(self, action: PanelAction, handle: TaskHandle) -> None:
        line = (f"still running after {seconds_text(handle.budget_s)} s: the engine has not "
                "answered yet. The window stays usable; the answer lands here when it does.")
        self._append_progress(line)
        self.ctx.log("warning", self.desc.title, f"{action.label}: {line}")
        self.refresh_gating()

    def _tick(self) -> None:
        if self._running is None:
            self._ticker.stop()
            return
        self.refresh_gating()

    @Slot(str)
    def _append_progress(self, line: str) -> None:
        self._progress_lines.append(line)
        if self.answer is not None:
            self.answer.appendPlainText(line)

    def _finished(self, action: PanelAction, command: str, result: TaskResult) -> None:
        self._running = None
        self._handle = None
        self._ticker.stop()
        head = rc_line(command, result)
        if result.ok:
            body = action.render(result.value) if action.render is not None else "done"
        else:
            body = describe_error(result.error)  # type: ignore[arg-type]
        self.show_answer("\n".join([head, *self._progress_lines, body]))
        level = "info" if result.ok else "error"
        self.ctx.log(level, self.desc.title, f"{head}\n{body}")
        if action.armed_by:
            self.set_checked(action.armed_by, False)
        if action.on_result is not None:
            action.on_result(self, result)
        self.refresh_gating()
        self.action_finished.emit(action.key, result)

    def show_answer(self, text: str) -> None:
        if self.answer is not None:
            self.answer.setPlainText(text)

    def answer_text(self) -> str:
        return self.answer.toPlainText() if self.answer is not None else ""
