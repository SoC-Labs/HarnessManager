"""Reset tab: DUT reset, shell restart, board reboot. Every one is armed by a tick box.

Engine calls (on workers), through the session's adapters as
``socharness.core.services`` prescribes: ``session.resets.reset_targets()``,
``session.resets.reset(target)``, ``session.controller.reboot(progress, wait_s)``.
Events consumed: none directly (``controller.reboot`` reaches the Log tab, and
the context re-reads the board after ``phase: up``).
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from socharness.core import capabilities as C
from socharness.core.errors import UnavailableError
from socharness.core.model import BoardInfo

from ..context import BoardContext
from ..panels import ActionEnv, PanelAction, PanelDesc, PanelField, PanelWidget
from ..worker import TaskResult

REBOOT_WAIT_S = 120.0


def _resets(env: ActionEnv, capability: str):
    adapter = env.session().resets
    if adapter is None:
        raise UnavailableError(capability, "this session has no reset adapter")
    return adapter


def call_reset_dut(env: ActionEnv) -> str:
    target = env.values.get("target") or "dut"
    _resets(env, C.RESET_DUT).reset(target)
    return f"reset {target}: done"


def call_reset_shell(env: ActionEnv) -> str:
    _resets(env, C.RESET_SHELL).reset("shell")
    return "reset shell: done"


def call_reboot(env: ActionEnv) -> str:
    controller = env.session().controller
    if controller is None:
        raise UnavailableError(C.REBOOT_BOARD, "this session has no board-controller adapter")

    def progress(phase: str, done: int, total: int) -> None:
        env.progress(f"reboot: {phase} ({done}/{total})")

    controller.reboot(progress, wait_s=REBOOT_WAIT_S)
    return "the board went down and came back"


class ResetTab(QWidget):
    def __init__(self, ctx: BoardContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._targets_read = False
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.dut = PanelWidget(PanelDesc(
            key="reset_dut", title="DUT reset",
            fields=(PanelField("target", "Target", kind="combo", choices=("dut",),
                               hint="what the board's reset adapter offers"),
                    PanelField("arm", "arm: I understand this resets the DUT CPU "
                               "(the harness keeps running)", kind="check")),
            actions=(PanelAction("reset", "Reset DUT", call=call_reset_dut,
                                 command=lambda v, _: f"reset {v.get('target') or 'dut'}",
                                 busy_label="Resetting...", budget_s=20.0, armed_by="arm",
                                 enabled_when_capability=C.RESET_DUT, render=str),),
            answer_hint="Pulses the DUT reset through the harness. Consoles stay up.",
            answer_lines=4), ctx)
        self.shell = PanelWidget(PanelDesc(
            key="reset_shell", title="Restart the shell",
            fields=(PanelField("arm", "arm: I understand every console and the debug session "
                               "drop while the shell restarts", kind="check"),),
            actions=(PanelAction("restart", "Restart shell", call=call_reset_shell,
                                 command="reset shell", busy_label="Restarting...",
                                 budget_s=60.0, armed_by="arm",
                                 enabled_when_capability=C.RESET_SHELL, render=str),),
            answer_hint="Restarts the harness firmware without reloading the FPGA.",
            answer_lines=4), ctx)
        row.addWidget(self.dut, 1)
        row.addWidget(self.shell, 1)
        v.addLayout(row)
        self.reboot = PanelWidget(PanelDesc(
            key="reboot", title="Board reboot (reload the FPGA from the config SD)",
            fields=(PanelField("arm", "arm: I understand this power-cycles the whole board; "
                               "every session, console and loaded overlay is lost",
                               kind="check"),),
            actions=(PanelAction("reboot", "Reboot board", call=call_reboot,
                                 command="mcc reboot", busy_label="Rebooting...",
                                 budget_s=REBOOT_WAIT_S + 30.0, armed_by="arm",
                                 enabled_when_capability=C.REBOOT_BOARD, render=str),),
            answer_hint="Reboots through the board controller and proves it: the board went "
                        "down, then came back. The board then runs what is on its SD card.",
            answer_lines=6), ctx)
        v.addWidget(self.reboot)
        v.addStretch(1)
        ctx.info_changed.connect(self._on_info)

    def _on_info(self, info: BoardInfo | None) -> None:
        if info is None or self._targets_read or not self.ctx.session_open:
            return
        self._targets_read = True
        engine, board_id = self.ctx.engine, self.ctx.board_id

        def work() -> list[str]:
            adapter = engine.session(board_id).resets
            return list(adapter.reset_targets()) if adapter is not None else []

        self.ctx.runner.submit(work, self._targets_done, label="reset targets", budget_s=20.0)

    def _targets_done(self, result: TaskResult) -> None:
        if result.ok:
            # "shell" has its own panel, gated on its own capability.
            targets = [t for t in result.value if t != "shell"] or ["dut"]
            self.dut.set_choices("target", targets)
