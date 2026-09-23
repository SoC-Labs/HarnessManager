"""Reset tab: SD recovery first, then DUT reset, shell restart, board reboot. All armed.

Engine calls (on workers), through the session's adapters as
``socharness.core.services`` prescribes: ``session.resets.reset_targets()``,
``session.resets.reset(target)``, ``session.controller.reboot(progress, wait_s)``,
``session.storage.pending()``, ``session.storage.load_backup(path)``,
``session.storage.restore(record, progress)``.
Events consumed: none directly (``controller.reboot`` reaches the Log tab, and
the context re-reads the board after ``phase: up``).

An interrupted SD install (``storage.pending()`` returns its journal) is shown
before anything else: a panel at the top of this tab, the main window's header,
and this tab brought to the front.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from socharness.core import capabilities as C
from socharness.core.errors import RefusedError, UnavailableError
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


SD_TITLE = "Interrupted SD install \u2014 Restore"


def journal_text(journal: dict[str, Any]) -> str:
    who = f" by pid {journal['pid']} on {journal['host']}" if journal.get("pid") else ""
    text = f"{journal.get('op', '?')} {journal.get('state', '?')}{who}"
    if journal.get("current"):
        text += f"; was writing {journal['current']}"
    if journal.get("error"):
        text += f"; {journal['error']}"
    return text


def call_sd_restore(env: ActionEnv) -> str:
    storage = env.session().storage
    if storage is None:
        raise UnavailableError(C.STORAGE_INSTALL, "this session has no configuration-SD adapter")
    journal = storage.pending()
    if journal is None:
        return "nothing to restore: the SD has no interrupted install"
    path = (journal.get("backup") or {}).get("path")
    if not path:
        raise RefusedError("the interrupted install's journal names no backup",
                           hint="restore by hand: socharness sd restore <backup.zip>")
    record = storage.load_backup(Path(path))
    shown = [-1]

    def progress(phase: str, done: int, total: int) -> None:
        pct = done * 100 // total if total else 0
        if pct // 25 != shown[0]:           # a line per quarter, not per chunk
            shown[0] = pct // 25
            env.progress(f"{phase}: {pct}%")

    storage.restore(record, progress)
    left = storage.pending()
    if left is not None:
        return f"restored from {path}, but the SD still has a journal: {journal_text(left)}"
    return f"restored the SD from {path}; no install is pending now"


def read_pending(engine: Any, board_id: str) -> dict[str, Any] | None:
    storage = engine.session(board_id).storage
    return storage.pending() if storage is not None else None


class ResetTab(QWidget):
    sd_pending = Signal(object)            # the journal of an interrupted install, or None

    def __init__(self, ctx: BoardContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._targets_read = False
        self.journal: dict[str, Any] | None = None
        v = QVBoxLayout(self)
        self.sd = PanelWidget(PanelDesc(
            key="sd_restore", title=SD_TITLE,
            fields=(PanelField("journal", "Journal", default="-"),
                    PanelField("backup", "Backup", default="-"),
                    PanelField("arm", "arm: I understand this rewrites the configuration SD "
                               "from the backup taken before the install", kind="check")),
            actions=(PanelAction("restore", "Restore the SD", call=call_sd_restore,
                                 command=lambda v, _: f"sd restore {v.get('backup') or '?'}",
                                 busy_label="Restoring...", budget_s=900.0, armed_by="arm",
                                 enabled_when_capability=C.STORAGE_INSTALL, render=str,
                                 on_result=lambda panel, res: self.check_sd()),),
            answer_hint="An SD install was interrupted. Restore puts back the backup taken "
                        "before it. Nothing else on this board should be trusted until then.",
            answer_lines=3), ctx)
        self.sd.setVisible(False)
        v.addWidget(self.sd)
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

    def check_sd(self) -> None:
        """Ask the config SD for an interrupted install's journal (on a worker)."""
        if not self.ctx.session_open:
            return
        engine, board_id = self.ctx.engine, self.ctx.board_id
        self.ctx.runner.submit(lambda: read_pending(engine, board_id), self._sd_checked,
                               label="sd pending", budget_s=30.0)

    def _sd_checked(self, result: TaskResult) -> None:
        journal = result.value if result.ok else None
        self.journal = journal
        if journal is not None:
            self.sd.set_value("journal", journal_text(journal), "error")
            self.sd.set_value("backup", str((journal.get("backup") or {}).get("path", "none")))
        self.sd.setVisible(journal is not None)
        self.sd_pending.emit(journal)

    def _on_info(self, info: BoardInfo | None) -> None:
        if info is None or self._targets_read or not self.ctx.session_open:
            return
        self._targets_read = True
        self.check_sd()
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
