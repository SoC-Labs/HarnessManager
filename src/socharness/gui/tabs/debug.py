"""Debug tab: Detect (IDCODE only), Open/Close the OpenOCD session, its ports and config.

Engine calls (on workers): ``engine.debug.detect(session)``,
``engine.debug.up(session)``, ``engine.debug.down(session)``,
``engine.debug.status(session)``.
Events consumed: ``debug.state {state, ports}``.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QVBoxLayout, QWidget

from socharness.core import capabilities as C
from socharness.core.events import Event
from socharness.core.model import BoardInfo
from socharness.core.services import DebugStatus

from ..context import BoardContext
from ..panels import ActionEnv, PanelAction, PanelDesc, PanelField, PanelWidget
from ..worker import TaskResult

LIVE_STATES = ("up", "starting")
STATE_LEVELS = {"up": "ok", "starting": "info", "down": "info", "failed": "error"}


def ports_text(status: DebugStatus) -> str:
    if status.state != "up" or not status.gdb_port:
        return "-"
    return (f"gdb 127.0.0.1:{status.gdb_port}   telnet 127.0.0.1:{status.telnet_port}   "
            f"tcl 127.0.0.1:{status.tcl_port}")


def render_status(status: Any) -> str:
    if not isinstance(status, DebugStatus):
        return str(status)
    lines = [f"state   {status.state}"]
    if status.state == "up":
        lines += [f"gdb     127.0.0.1:{status.gdb_port}",
                  f"telnet  127.0.0.1:{status.telnet_port}",
                  f"tcl     127.0.0.1:{status.tcl_port}",
                  f"attach  arm-none-eabi-gdb -ex 'target extended-remote :{status.gdb_port}'"]
    if status.config:
        lines.append("config  " + " ".join(status.config))
    if status.pid:
        lines.append(f"pid     {status.pid}")
    if status.detail:
        lines.append(f"detail  {status.detail}")
    return "\n".join(lines)


def render_idcode(idcode: Any) -> str:
    return f"TAP IDCODE {idcode}\n(read without reset or halt: nothing on the target changed)"


class DebugTab(QWidget):
    def __init__(self, ctx: BoardContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.debug_state = "unknown"
        self._status_read = False
        v = QVBoxLayout(self)
        self.panel = PanelWidget(PanelDesc(
            key="debug", title="DUT debug (OpenOCD)",
            fields=(PanelField("state", "Session", default="unknown"),
                    PanelField("ports", "Ports", default="-",
                               hint="gdb, telnet and tcl bind to 127.0.0.1 only"),
                    PanelField("config", "Config", default="-",
                               hint="the target-half config the engine chose for the loaded "
                                    "design"),
                    PanelField("process", "Process", default="-"),
                    PanelField("idcode", "IDCODE", default="not detected yet")),
            actions=(
                PanelAction("detect", "Detect", call=lambda env: env.engine.debug.detect(
                    env.session()), command="debug detect", busy_label="Detecting...",
                    budget_s=45.0, enabled_when_capability=C.DEBUG_DUT, needs_service="debug",
                    render=render_idcode, on_result=self._after_detect),
                PanelAction("up", "Open session", call=lambda env: env.engine.debug.up(
                    env.session()), command="debug up", busy_label="Opening...",
                    budget_s=60.0, enabled_when_capability=C.DEBUG_DUT, needs_service="debug",
                    guard=self._guard_up, render=render_status, on_result=self._after_status),
                PanelAction("down", "Close session", call=self._call_down,
                            command="debug down", busy_label="Closing...", budget_s=30.0,
                            needs_service="debug",
                            guard=self._guard_down, render=render_status,
                            on_result=self._after_status),
            ),
            answer_hint="Detect reads the TAP IDCODE only: no reset, no halt, no register "
                        "written. Open starts OpenOCD with the config for the loaded design; "
                        "gdb, telnet and tcl listen on 127.0.0.1.",
            answer_lines=9), ctx)
        v.addWidget(self.panel)
        v.addStretch(1)
        ctx.board_event.connect(self._on_event)
        ctx.info_changed.connect(self._on_info)

    # -- guards and calls -----------------------------------------------------------------

    def _guard_up(self, panel: PanelWidget) -> str:
        return "the session is already up" if self.debug_state in LIVE_STATES else ""

    def _guard_down(self, panel: PanelWidget) -> str:
        if self.debug_state in ("down", "unknown"):
            return "the session is down"
        return ""

    @staticmethod
    def _call_down(env: ActionEnv) -> DebugStatus:
        return env.engine.debug.down(env.session())

    # -- rendering ------------------------------------------------------------------------

    def show_status(self, status: DebugStatus) -> None:
        self.debug_state = status.state
        self.panel.set_value("state", status.state + (f" ({status.detail})"
                                                      if status.detail else ""),
                             STATE_LEVELS.get(status.state, "warning"))
        self.panel.set_value("ports", ports_text(status))
        self.panel.set_value("config", " ".join(status.config) or "-")
        self.panel.set_value("process", f"pid {status.pid}" if status.pid else "-")

    def _after_status(self, panel: PanelWidget, result: TaskResult) -> None:
        if result.ok and isinstance(result.value, DebugStatus):
            self.show_status(result.value)
        else:
            self.read_status()

    def _after_detect(self, panel: PanelWidget, result: TaskResult) -> None:
        if result.ok:
            panel.set_value("idcode", str(result.value), "ok")
        else:
            code = getattr(getattr(result.error, "code", None), "name", "FAILED")
            panel.set_value("idcode", code.replace("_", " "), "error")

    def _on_info(self, info: BoardInfo | None) -> None:
        if info is not None and not self._status_read and self.ctx.session_open:
            self._status_read = True
            self.read_status()

    def read_status(self) -> None:
        engine, board_id = self.ctx.engine, self.ctx.board_id
        self.ctx.runner.submit(lambda: engine.debug.status(engine.session(board_id)),
                               self._status_done, label="debug status", budget_s=20.0)

    def _status_done(self, result: TaskResult) -> None:
        if result.ok and isinstance(result.value, DebugStatus):
            self.show_status(result.value)
        elif not result.ok:
            self.panel.set_value("state", "unknown (status read failed)", "warning")

    def _on_event(self, ev: Event) -> None:
        if ev.topic != "debug.state":
            return
        state = str(ev.data.get("state", "unknown"))
        self.debug_state = state
        self.panel.set_value("state", state, STATE_LEVELS.get(state, "warning"))
        ports = ev.data.get("ports") or {}
        if state == "up" and ports:
            self.panel.set_value("ports", "   ".join(
                f"{k} 127.0.0.1:{v}" for k, v in ports.items()))
        elif state not in LIVE_STATES:
            self.panel.set_value("ports", "-")
            self.panel.set_value("process", "-")
