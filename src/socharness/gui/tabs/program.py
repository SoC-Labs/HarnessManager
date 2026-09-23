"""Program tab: compatible overlays, the preflight table, Program, Restore baseline, progress.

Engine calls (all on workers, each with ``engine.session(board_id)``):
``engine.deploy.compatible``, ``engine.deploy.preflight``, ``engine.deploy.deploy``,
``engine.deploy.restore_baseline``.
Events consumed: ``deploy.started``, ``deploy.progress {phase, bytes, total}``,
``deploy.done {rm_id, verified}``, ``deploy.failed {reason}``.

The Program button is refused while any preflight row is MISMATCH. That
mirrors the deploy service's own rule; the service still enforces it.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from socharness.core import capabilities as C
from socharness.core.events import Event
from socharness.core.model import BoardInfo, Check
from socharness.core.pack import DeployResult, OverlayRef, PreflightItem

from ..context import BoardContext
from ..panels import ActionEnv, PanelAction, PanelDesc, PanelField, PanelWidget
from ..style import LEVEL_COLOURS, check_level, item, level_label, role_label, set_level
from ..worker import TaskResult, describe_error, rc_line
from .system import fill_row, make_table

ARM_TEXT = "arm: I understand this reconfigures the partition and resets the DUT"


def render_deploy(result: Any) -> str:
    if not isinstance(result, DeployResult):
        return str(result)
    verdict = "verified by the board" if result.verified else "WRITTEN, NOT VERIFIED"
    return (f"rm_id {result.rm_id}: {verdict}\n"
            f"transport {result.transport or '?'}, {result.seconds:.1f} s")


def _kib(n: int) -> str:
    return f"{n / 1024:.0f} KiB" if n else "?"


class ProgramTab(QWidget):
    def __init__(self, ctx: BoardContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.overlays: list[OverlayRef] = []
        self.incompatible: dict[int, str] = {}      # id(OverlayRef) -> the service's reason
        self.selected: OverlayRef | None = None
        self.preflight_for: OverlayRef | None = None
        self.preflight_items: list[PreflightItem] | None = None
        self._preflight_gen = 0
        self._loaded_once = False

        outer = QHBoxLayout(self)
        split = QSplitter()
        outer.addWidget(split)

        # Left: overlays + preflight.
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        ov_box = QGroupBox("Overlays for this shell")
        ovl = QVBoxLayout(ov_box)
        row = QHBoxLayout()
        self.overlay_status = role_label("", "muted")
        self.overlay_refresh = QPushButton("Refresh list")
        self.overlay_refresh.clicked.connect(self.load_overlays)
        row.addWidget(self.overlay_status, 1)
        row.addWidget(self.overlay_refresh)
        ovl.addLayout(row)
        self.overlay_table = make_table(["Overlay", "rm_id", "Keyed to", "Size", "IP", "Status"])
        self.overlay_table.itemSelectionChanged.connect(self._on_select)
        ovl.addWidget(self.overlay_table, 1)
        lv.addWidget(ov_box, 3)

        pf_box = QGroupBox("Preflight (every row must pass; UNCHECKED is not a pass)")
        pfl = QVBoxLayout(pf_box)
        self.preflight_status = role_label("select an overlay to run its preflight", "muted")
        pfl.addWidget(self.preflight_status)
        self.preflight_table = make_table(["Check", "Result", "Detail"])
        pfl.addWidget(self.preflight_table, 1)
        lv.addWidget(pf_box, 2)
        split.addWidget(left)

        # Right: the Program panel + progress.
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.panel = PanelWidget(PanelDesc(
            key="program", title="Program a partition",
            fields=(PanelField("overlay", "Selected", default="(none)"),
                    PanelField("arm", ARM_TEXT, kind="check")),
            actions=(
                PanelAction("program", "Program", call=self._call_program,
                            command=lambda v, ov: f"program {ov.name if ov else '?'}",
                            busy_label="Programming...", budget_s=120.0, armed_by="arm",
                            enabled_when_capability=C.DEPLOY_PARTIAL,
                            needs_service="deploy",
                            guard=self._program_guard, prepare=lambda p: self.selected,
                            render=render_deploy, on_result=self._after_deploy),
                PanelAction("restore", "Restore baseline", call=self._call_restore,
                            command="restore", busy_label="Restoring...", budget_s=120.0,
                            armed_by="arm", enabled_when_capability=C.DEPLOY_PARTIAL,
                            needs_service="deploy",
                            render=render_deploy, on_result=self._after_deploy),
            ),
            answer_hint="Program pushes the selected overlay after its preflight passes, then "
                        "re-reads the board's identity to confirm it. Restore baseline loads "
                        "the board's safe design (the greybox on the MPS3).",
            answer_lines=5), ctx)
        rv.addWidget(self.panel)

        prog_box = QGroupBox("Progress (deploy events, in the order the engine sent them)")
        pv = QVBoxLayout(prog_box)
        self.phase_label = QLabel("idle")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.done_label = level_label("", "info")
        self.done_label.setObjectName("deploy_done")
        self.events = QListWidget()
        self.events.setObjectName("deploy_events")
        pv.addWidget(self.phase_label)
        pv.addWidget(self.progress)
        pv.addWidget(self.done_label)
        pv.addWidget(self.events, 1)
        rv.addWidget(prog_box, 1)
        split.addWidget(right)
        split.setSizes([640, 600])
        split.setChildrenCollapsible(False)

        ctx.board_event.connect(self._on_event)
        ctx.info_changed.connect(self._on_info)

    # -- overlays -------------------------------------------------------------------------

    def _on_info(self, info: BoardInfo | None) -> None:
        if info is not None and not self._loaded_once and self.ctx.session_open:
            self._loaded_once = True
            self.load_overlays()

    def load_overlays(self) -> None:
        if not self.ctx.session_open:
            self.overlay_status.setText("Cannot: no board session is open. Nothing was run.")
            return
        engine, board_id = self.ctx.engine, self.ctx.board_id

        def work() -> tuple[list[OverlayRef], list[OverlayRef], dict[str, str]]:
            session = engine.session(board_id)
            every = list(engine.deploy.overlays(session))
            loadable, why = engine.deploy.compatible(session)
            return every, list(loadable), dict(why)

        self.overlay_status.setText("reading the overlay list...")
        self.overlay_refresh.setEnabled(False)
        self.ctx.runner.submit(work, self._overlays_done, label="overlays", budget_s=30.0)

    def _overlays_done(self, result: TaskResult) -> None:
        self.overlay_refresh.setEnabled(True)
        line = rc_line("overlays", result)
        if not result.ok:
            self.overlay_status.setText(f"{line}\n{describe_error(result.error)}")
            set_level(self.overlay_status, "error")
            return
        every, loadable, why = result.value
        ok = set(loadable)
        # Every overlay the service knows, loadable ones first, each row one OverlayRef
        # (two overlays may share a name when they are keyed to different shells).
        self.overlays = [o for o in every if o in ok] + [o for o in loadable if o not in every]
        self.overlays += [o for o in every if o not in ok]
        self.incompatible = {id(o): self._reason(o, why) for o in self.overlays if o not in ok}
        self.overlay_status.setText(
            f"{line}: {len(ok)} loadable, {len(self.overlays) - len(ok)} not")
        set_level(self.overlay_status, "info")
        self.overlay_table.setRowCount(len(self.overlays))
        for r, ov in enumerate(self.overlays):
            status = (item("loadable", "ok") if ov in ok
                      else item(self.incompatible[id(ov)], "error"))
            fill_row(self.overlay_table, r, [item(ov.name), item(ov.rm_id), item(ov.static_id),
                                             item(_kib(ov.size_bytes)), item(ov.ip_class),
                                             status])
        self.selected = None
        self.preflight_items = None
        self.panel.set_value("overlay", "(none)")

    @staticmethod
    def _reason(ov: OverlayRef, why: dict[str, str]) -> str:
        return (why.get(ov.name) or why.get(f"{ov.name} ({ov.static_id}, {ov.source})")
                or "not in the service's loadable list")

    def overlay_by_name(self, name: str) -> OverlayRef | None:
        return next((o for o in self.overlays if o.name == name), None)

    def select_overlay(self, name: str) -> None:
        for r, ov in enumerate(self.overlays):
            if ov.name == name:
                self.overlay_table.selectRow(r)
                return
        raise KeyError(name)

    def _on_select(self) -> None:
        rows = self.overlay_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self.overlays):
            return
        overlay = self.overlays[rows[0].row()]
        self.panel.set_value("overlay", f"{overlay.name} ({overlay.rm_id})")
        self.selected = overlay
        self.run_preflight(overlay)

    # -- preflight ------------------------------------------------------------------------

    def run_preflight(self, overlay: OverlayRef) -> None:
        self._preflight_gen += 1
        gen = self._preflight_gen
        self.preflight_items = None
        self.preflight_for = overlay
        self.preflight_table.setRowCount(0)
        self.preflight_status.setText(f"running the preflight for {overlay.name}...")
        set_level(self.preflight_status, "info")
        self.panel.refresh_gating()
        engine, board_id = self.ctx.engine, self.ctx.board_id
        self.ctx.runner.submit(
            lambda: list(engine.deploy.preflight(engine.session(board_id), overlay)),
            lambda res: self._preflight_done(gen, overlay, res),
            label=f"preflight {overlay.name}", budget_s=30.0)

    def _preflight_done(self, gen: int, overlay: OverlayRef, result: TaskResult) -> None:
        if gen != self._preflight_gen:
            return      # a newer selection superseded this one
        line = rc_line(f"preflight {overlay.name}", result)
        if not result.ok:
            self.preflight_status.setText(f"{line}\n{describe_error(result.error)}")
            set_level(self.preflight_status, "error")
            self.panel.refresh_gating()
            return
        items: list[PreflightItem] = result.value
        self.preflight_items = items
        self.preflight_table.setRowCount(len(items))
        for r, pf in enumerate(items):
            level = check_level(pf.check)
            fill_row(self.preflight_table, r, [item(pf.name, level),
                                               item(pf.check.value.upper(), level),
                                               item(pf.detail, level)])
        bad = sum(1 for pf in items if pf.check is Check.MISMATCH)
        unchecked = sum(1 for pf in items if pf.check is Check.UNCHECKED)
        summary = (f"{line}: {bad} MISMATCH" if bad else f"{line}: no MISMATCH") + (
            f", {unchecked} UNCHECKED" if unchecked else "")
        self.preflight_status.setText(summary)
        set_level(self.preflight_status, "error" if bad else ("warning" if unchecked else "ok"))
        self.panel.refresh_gating()

    def mismatch_rows(self) -> list[int]:
        """Rows painted in the error colour (for tests and the reader alike)."""
        colour = LEVEL_COLOURS["error"]
        return [r for r in range(self.preflight_table.rowCount())
                if self.preflight_table.item(r, 1).foreground().color().name() == colour]

    # -- the Program panel ----------------------------------------------------------------

    def _program_guard(self, panel: PanelWidget) -> str:
        if self.selected is None:
            return "select an overlay in the list first"
        if self.preflight_for is not self.selected or self.preflight_items is None:
            return f"waiting for the preflight of {self.selected.name}"
        bad = [pf.name for pf in self.preflight_items if pf.check is Check.MISMATCH]
        if bad:
            return f"preflight MISMATCH ({', '.join(bad)}): Program is refused"
        if id(self.selected) in self.incompatible:
            return f"{self.selected.name} cannot load: {self.incompatible[id(self.selected)]}"
        return ""

    @staticmethod
    def _call_program(env: ActionEnv) -> DeployResult:
        return env.engine.deploy.deploy(env.session(), env.arg)

    @staticmethod
    def _call_restore(env: ActionEnv) -> DeployResult:
        return env.engine.deploy.restore_baseline(env.session())

    def _after_deploy(self, panel: PanelWidget, result: TaskResult) -> None:
        if self.selected is not None:
            self.run_preflight(self.selected)

    # -- deploy events --------------------------------------------------------------------

    def _add_event(self, text: str, level: str = "info") -> None:
        entry = QListWidgetItem(text)
        colour = LEVEL_COLOURS.get(level, "")
        if colour:
            entry.setForeground(QBrush(QColor(colour)))
        self.events.addItem(entry)
        if self.events.count() > 500:
            self.events.takeItem(0)
        self.events.scrollToBottom()

    def event_texts(self) -> list[str]:
        return [self.events.item(i).text() for i in range(self.events.count())]

    def _on_event(self, ev: Event) -> None:
        if not ev.topic.startswith("deploy."):
            return
        data = ev.data
        if ev.topic == "deploy.started":
            self.events.clear()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.phase_label.setText("started")
            self.done_label.setText("running")
            set_level(self.done_label, "info")
            what = data.get("overlay") or data.get("rm_id") or ""
            self._add_event(f"started {what}".rstrip())
        elif ev.topic == "deploy.progress":
            phase = str(data.get("phase", "?"))
            done, total = int(data.get("bytes", 0) or 0), int(data.get("total", 0) or 0)
            if total > 0:
                self.progress.setRange(0, 100)
                self.progress.setValue(min(100, done * 100 // total))
                self.phase_label.setText(f"{phase}: {done:,} of {total:,} bytes")
            else:
                self.progress.setRange(0, 0)       # busy: the engine gave no total
                self.phase_label.setText(phase)
            self._add_event(f"progress {phase} {done}/{total}")
        elif ev.topic == "deploy.done":
            verified = bool(data.get("verified"))
            rm_id = data.get("rm_id", "?")
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.phase_label.setText("done")
            took = f" in {float(data['seconds']):.1f} s" if data.get("seconds") else ""
            via = f" over {data['transport']}" if data.get("transport") else ""
            if verified:
                self.done_label.setText(f"DONE: rm_id {rm_id}, verified by the board{took}{via}")
                set_level(self.done_label, "ok")
            else:
                self.done_label.setText(f"WRITTEN, NOT VERIFIED: rm_id {rm_id}")
                set_level(self.done_label, "warning")
            self._add_event(f"done rm_id {rm_id} verified={'yes' if verified else 'no'}",
                            "ok" if verified else "warning")
        elif ev.topic == "deploy.failed":
            reason = data.get("reason", "no reason given")
            stage = data.get("stage", "")
            self.progress.setRange(0, 100)
            self.phase_label.setText(f"failed at {stage}" if stage else "failed")
            what = f" {data['overlay']}" if data.get("overlay") else ""
            self.done_label.setText(f"FAILED{what}{f' at {stage}' if stage else ''}: {reason}")
            set_level(self.done_label, "error")
            self._add_event(f"failed: {reason}", "error")
