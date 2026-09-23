"""System tab: identity, the three-state build check, health, telemetry, capabilities.

Engine calls: ``engine.info(board_id)`` (through ``BoardContext.refresh_info``)
and ``engine.telemetry.readings(engine.session(board_id))``.
Events consumed: any that change the board (``BoardContext.REFRESH_TOPICS``).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from socharness.core.model import BoardInfo, Reading

from ..context import BoardContext
from ..style import (
    LINK_NAMES,
    age_text,
    check_level,
    check_text,
    control_level,
    item,
    level_label,
    role_label,
    set_level,
)
from ..worker import TaskResult, describe_error, rc_line, seconds_text


def make_table(headers: list[str], stretch_col: int | None = None) -> QTableWidget:
    """A read-only table that fits its width: one stretch column, the rest sized to content."""
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setWordWrap(False)
    header = table.horizontalHeader()
    stretch = len(headers) - 1 if stretch_col is None else stretch_col
    for col in range(len(headers)):
        mode = (QHeaderView.ResizeMode.Stretch if col == stretch
                else QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(col, mode)
    return table


def fill_row(table: QTableWidget, row: int, cells: list) -> None:
    for col, cell in enumerate(cells):
        table.setItem(row, col, cell)


def reading_cells(r: Reading) -> list:
    if r.available:
        value = item(f"{r.value:g} {r.unit}", "ok")
    else:
        value = item("unavailable", "warning", tooltip=r.reason)
    return [item(r.name), value, item(r.source or "(none given)"),
            item(age_text(r.observed_at) if r.available else "-"),
            item(r.reason, "warning" if not r.available else "info")]


class SystemTab(QWidget):
    def __init__(self, ctx: BoardContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        outer = QVBoxLayout(self)

        top = QHBoxLayout()
        self.status = role_label("reading the board...", "rcline")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.status, 1)
        top.addWidget(self.refresh_button)
        outer.addLayout(top)

        grid = QGridLayout()
        outer.addLayout(grid, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        # Identity + build check.
        ident_box = QGroupBox("Identity")
        form = QFormLayout(ident_box)
        self.fields: dict[str, QLabel] = {}
        for key, label in (("board", "Board"), ("links", "Links"), ("shell", "Shell"),
                           ("design", "Design"), ("harness", "Harness"),
                           ("features", "Features"), ("unit", "Unit id")):
            self.fields[key] = level_label("-")
            form.addRow(label, self.fields[key])
        self.build_label = level_label("-", "warning")
        self.build_label.setObjectName("build_check")
        self.build_label.setToolTip("firmware vs fabric build check. Three states: "
                                    "OK, MISMATCH, UNCHECKED. UNCHECKED is not a pass.")
        form.addRow("Build check", self.build_label)
        grid.addWidget(ident_box, 0, 0)

        # Health.
        health_box = QGroupBox("Health")
        hv = QVBoxLayout(health_box)
        self.control_label = level_label("-")
        self.notes_label = level_label("", "warning")
        hv.addWidget(self.control_label)
        hv.addWidget(self.notes_label)
        self.counters = make_table(["Counter", "Value"], stretch_col=0)
        hv.addWidget(self.counters, 1)
        grid.addWidget(health_box, 1, 0)

        # Telemetry.
        tele_box = QGroupBox("Telemetry (every value names its source)")
        tv = QVBoxLayout(tele_box)
        trow = QHBoxLayout()
        self.telemetry_status = role_label("", "muted")
        self.telemetry_button = QPushButton("Read telemetry")
        self.telemetry_button.clicked.connect(self.refresh_telemetry)
        trow.addWidget(self.telemetry_status, 1)
        trow.addWidget(self.telemetry_button)
        tv.addLayout(trow)
        self.telemetry = make_table(["Reading", "Value", "Source", "Observed", "Note"])
        tv.addWidget(self.telemetry, 1)
        grid.addWidget(tele_box, 0, 1)

        # Capabilities.
        caps_box = QGroupBox("Capabilities")
        cv = QVBoxLayout(caps_box)
        self.capabilities = make_table(["Capability", "Status", "Why not"])
        cv.addWidget(self.capabilities, 1)
        grid.addWidget(caps_box, 1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)

        ctx.info_changed.connect(self.render_info)
        ctx.session_changed.connect(self._on_session)
        self._telemetry_inflight = False

    # -- identity, health, capabilities ---------------------------------------------------

    def refresh(self) -> None:
        self.ctx.refresh_info()
        self.refresh_telemetry()

    def render_info(self, info: BoardInfo | None) -> None:
        if info is None:
            self.status.setText(f"{self.ctx.info_rc_line}\n{self.ctx.info_error}")
            set_level(self.status, "error")
            return
        self.status.setText(self.ctx.info_rc_line)
        set_level(self.status, "info")
        ident, cand = info.identity, info.candidate
        self.fields["board"].setText(f"{cand.label}\n{cand.board_id}  ({cand.evidence})")
        self.fields["links"].setText("\n".join(
            f"{LINK_NAMES.get(lk.kind, lk.kind.value)}  {lk.address}"
            + (f"  ({lk.detail})" if lk.detail else "") for lk in cand.links) or "none")
        self.fields["shell"].setText(ident.shell_id or "unknown")
        self.fields["design"].setText(
            f"{ident.rm_name or 'unknown design'}  (rm_id {ident.rm_id or '?'})")
        sha = ident.firmware_sha or "?"
        self.fields["harness"].setText(
            f"{ident.harness_version or 'unknown'}  (firmware {sha}"
            f"{', DIRTY' if ident.firmware_dirty else ''})")
        set_level(self.fields["harness"], "warning" if ident.firmware_dirty else "info")
        self.fields["features"].setText(", ".join(ident.features) or "none reported")
        self.fields["unit"].setText(ident.unit_id or "not reported by this harness")
        self.build_label.setText(check_text(ident.build_check))
        set_level(self.build_label, check_level(ident.build_check))

        h = info.health
        self.control_label.setText(
            f"control channel: {h.control_channel}   "
            f"({'reachable' if h.reachable else 'NOT reachable'})")
        set_level(self.control_label, control_level(h.control_channel)
                  if h.reachable else "error")
        self.notes_label.setText("\n".join(h.notes))
        self.counters.setRowCount(len(h.counters))
        for row, (name, value) in enumerate(sorted(h.counters.items())):
            fill_row(self.counters, row, [item(name), item(str(value))])

        names = list(self.ctx.titles) or sorted(info.capabilities | set(info.unavailable))
        extra = sorted((info.capabilities | set(info.unavailable)) - set(names))
        rows = names + extra
        self.capabilities.setRowCount(len(rows))
        for row, name in enumerate(rows):
            title = self.ctx.title(name)
            if name in info.capabilities:
                cells = [item(title, tooltip=name), item("available", "ok"), item("")]
            else:
                why = info.unavailable.get(name, "not offered by this board pack")
                cells = [item(title, tooltip=name), item("Cannot", "warning"),
                         item(why, "warning")]
            fill_row(self.capabilities, row, cells)
        if not self.telemetry.rowCount() and not self._telemetry_inflight:
            self.refresh_telemetry()

    # -- telemetry -----------------------------------------------------------------------

    def _on_session(self, is_open: bool) -> None:
        self.telemetry_button.setEnabled(is_open)
        if not is_open:
            self.telemetry_status.setText("Cannot: no board session is open")

    def refresh_telemetry(self) -> None:
        if not self.ctx.session_open:
            self.telemetry_status.setText("Cannot: no board session is open. Nothing was run.")
            return
        if self._telemetry_inflight:
            return
        self._telemetry_inflight = True
        engine, board_id = self.ctx.engine, self.ctx.board_id
        self.telemetry_status.setText("reading...")
        self.ctx.runner.submit(lambda: engine.telemetry.readings(engine.session(board_id)),
                               self._telemetry_done, label="telemetry", budget_s=20.0,
                               on_overdue=self._telemetry_overdue)

    def _telemetry_overdue(self, handle) -> None:
        self.telemetry_status.setText(f"still running after {seconds_text(handle.budget_s)} s")

    def _telemetry_done(self, result: TaskResult) -> None:
        self._telemetry_inflight = False
        line = rc_line("telemetry", result)
        if not result.ok:
            self.telemetry_status.setText(f"{line}: {describe_error(result.error)}")
            set_level(self.telemetry_status, "error")
            return
        readings: list[Reading] = list(result.value)
        self.telemetry_status.setText(f"{line}: {len(readings)} readings")
        set_level(self.telemetry_status, "info")
        self.telemetry.setRowCount(len(readings))
        for row, r in enumerate(readings):
            fill_row(self.telemetry, row, reading_cells(r))
