"""The System Selection dialog: which boards the engine sees, who holds them, open one.

Engine calls (on workers): ``engine.probe(hints)``, ``engine.candidate_for(target)``,
``engine.lock_owner(board_id)``, ``engine.open_boards()``, ``engine.info(board_id)``
(only for boards this process already has open) and ``engine.open(candidate, note=...)``.

Shell, Design and Harness come from ``candidate.identity`` when the engine
supplies it (contract change request T6-1); otherwise they read "not read"
until the board is opened. A held board keeps its Select button: the engine
decides (it takes over a stale lock and refuses a live one), and a refusal is
shown with the holder's name.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from functools import partial
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from socharness.core.errors import HeldError
from socharness.core.model import BoardIdentity, Candidate, Health
from socharness.core.pack import ProbeHints
from socharness.core.session import LockOwner

from .style import LINK_NAMES, check_level, control_level, item, role_label, set_level
from .tabs.system import fill_row, make_table
from .worker import Runner, TaskResult, describe_error, rc_line, seconds_text

COLUMNS = ["Board", "Shell", "Design", "Harness", "Holder/Lock", "Health", "Links"]
COL = {name: i for i, name in enumerate(COLUMNS)}
GUI_NOTE = "socharness-gui"


@dataclass
class Summary:
    owner: LockOwner | None = None
    owner_error: str = ""
    identity: BoardIdentity | None = None
    health: Health | None = None
    open_here: bool = False


def summarize(engine: Any, cand: Candidate) -> Summary:
    """Runs on a worker. Reads only what the engine already offers without opening."""
    s = Summary(identity=getattr(cand, "identity", None))
    try:
        s.owner = engine.lock_owner(cand.board_id)
    except Exception as exc:  # noqa: BLE001 - shown in the row, never fatal
        s.owner_error = describe_error(exc)
    s.open_here = cand.board_id in engine.open_boards()
    if s.open_here:
        info = engine.info(cand.board_id)
        s.identity, s.health = info.identity, info.health
    return s


def owner_is_me(owner: LockOwner) -> bool:
    return owner.pid == os.getpid() and owner.host == socket.gethostname()


class SelectionDialog(QDialog):
    board_opened = Signal(object)          # the Candidate, once engine.open() succeeded

    def __init__(self, engine: Any, runner: Runner, hints: ProbeHints | None = None,
                 parent: QWidget | None = None, *, autoscan: bool = True) -> None:
        super().__init__(parent)
        self.engine = engine
        self.runner = runner
        self.hints = hints
        self.setWindowTitle("System Selection - SoC Labs Harness Manager")
        self.candidates: list[Candidate] = []
        self.summaries: dict[str, Summary] = {}
        self._manual: dict[str, Candidate] = {}
        self._gen = 0
        self._opening = ""
        self._scanning = False

        v = QVBoxLayout(self)
        v.addWidget(role_label(
            "Boards the engine can see. Select one to open it: the board is locked to you "
            "until its window closes.", "muted"))
        self.table = make_table(COLUMNS, stretch_col=COL["Holder/Lock"])
        self.table.itemSelectionChanged.connect(self.refresh_gating)
        self.table.itemDoubleClicked.connect(lambda *_: self.select())
        v.addWidget(self.table, 1)
        self.status = role_label("", "rcline")
        self.status.setObjectName("selection_status")
        v.addWidget(self.status)
        buttons = QHBoxLayout()
        self.reason = role_label("", "reason")
        self.reason.setObjectName("select_reason")
        self.select_button = QPushButton("Select")
        self.select_button.setDefault(True)
        self.select_button.clicked.connect(self.select)
        self.rescan_button = QPushButton("Rescan")
        self.rescan_button.clicked.connect(self.rescan)
        self.add_button = QPushButton("Add by address...")
        self.add_button.clicked.connect(self._ask_address)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.reason, 1)
        for b in (self.select_button, self.rescan_button, self.add_button, self.close_button):
            buttons.addWidget(b)
        v.addLayout(buttons)
        self.resize(1180, 460)
        self.refresh_gating()
        if autoscan:
            self.rescan()

    # -- scanning -------------------------------------------------------------------------

    def rescan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self.status.setText("scanning...")
        self.refresh_gating()
        engine, hints = self.engine, self.hints
        self.runner.submit(lambda: engine.probe(hints), self._probed, label="probe",
                           budget_s=30.0, on_overdue=self._scan_overdue)

    def _scan_overdue(self, handle) -> None:
        self.status.setText(f"scanning... still running after {seconds_text(handle.budget_s)} s")

    def _probed(self, result: TaskResult) -> None:
        self._scanning = False
        line = rc_line("probe", result)
        if not result.ok:
            self.status.setText(f"{line}\n{describe_error(result.error)}")
            set_level(self.status, "error")
            self.refresh_gating()
            return
        found: list[Candidate] = list(result.value)
        ids = {c.board_id for c in found}
        found += [c for bid, c in self._manual.items() if bid not in ids]
        self.status.setText(f"{line}: {len(found)} board(s)" if found else
                            f"{line}: no boards found. Check the Ethernet link, or use "
                            "Add by address.")
        set_level(self.status, "info" if found else "warning")
        self._set_candidates(found)

    def _set_candidates(self, candidates: list[Candidate]) -> None:
        self._gen += 1
        self.candidates = list(candidates)
        self.summaries = {}
        self.table.setRowCount(len(self.candidates))
        for row, cand in enumerate(self.candidates):
            self._render_row(row, cand, None)
            self._summarize(cand)
        if self.candidates and not self.table.selectionModel().selectedRows():
            self.table.selectRow(0)
        self.refresh_gating()

    def _summarize(self, cand: Candidate) -> None:
        gen = self._gen
        self.runner.submit(partial(summarize, self.engine, cand),
                           partial(self._summarized, gen, cand.board_id),
                           label=f"summary {cand.board_id}", budget_s=20.0)

    def _summarized(self, gen: int, board_id: str, result: TaskResult) -> None:
        if gen != self._gen:
            return
        row = self.row_of(board_id)
        if row < 0:
            return
        summary = result.value if result.ok else Summary(owner_error=describe_error(
            result.error))  # type: ignore[arg-type]
        self.summaries[board_id] = summary
        self._render_row(row, self.candidates[row], summary)
        self.refresh_gating()

    def _render_row(self, row: int, cand: Candidate, s: Summary | None) -> None:
        def not_read():
            return item("not read", "info",
                        tooltip="the engine reads identity once the board is opened")

        ident = s.identity if s is not None else None
        if ident is not None:
            shell = item(ident.shell_id or "?")
            design = item(f"{ident.rm_name or '?'} ({ident.rm_id or '?'})")
            harness = item(f"{ident.harness_version or '?'}  build "
                           f"{ident.build_check.value.upper()}", check_level(ident.build_check))
        else:
            shell, design, harness = not_read(), not_read(), not_read()
        if s is None:
            holder = item("checking...")
        elif s.owner_error:
            holder = item("unknown", "warning", tooltip=s.owner_error)
        elif s.open_here or (s.owner is not None and owner_is_me(s.owner)):
            holder = item("you (this app)", "ok")
        elif s.owner is not None:
            holder = item(f"held by {s.owner.describe()}", "warning")
        else:
            holder = item("free", "ok")
        if s is not None and s.health is not None:
            h = s.health
            health = item(h.control_channel, control_level(h.control_channel)
                          if h.reachable else "error")
        else:
            health = item(cand.evidence or "not probed", "ok" if cand.evidence else "info")
        links = item(", ".join(LINK_NAMES.get(lk.kind, lk.kind.value) for lk in cand.links),
                     tooltip="\n".join(f"{lk.kind.value}: {lk.address} {lk.detail}".rstrip()
                                       for lk in cand.links))
        board = item(cand.board_id, tooltip=f"{cand.label}\n{cand.evidence}".strip())
        board.setData(Qt.ItemDataRole.UserRole, cand.board_id)
        fill_row(self.table, row, [board, shell, design, harness, holder, health, links])

    # -- adding by address ---------------------------------------------------------------

    def _ask_address(self) -> None:
        text, ok = QInputDialog.getText(self, "Add by address",
                                        "Shell address (host or host:port):")
        if ok:
            self.add_by_address(text)

    def add_by_address(self, target: str) -> None:
        engine = self.engine
        self.runner.submit(lambda: engine.candidate_for(target),
                           lambda res: self._added(target, res), label=f"add {target}",
                           budget_s=20.0)

    def _added(self, target: str, result: TaskResult) -> None:
        line = rc_line(f"add {target}", result)
        if not result.ok:
            self.status.setText(f"{line}\n{describe_error(result.error)}")
            set_level(self.status, "error")
            return
        cand: Candidate = result.value
        self._manual[cand.board_id] = cand
        self.status.setText(f"{line}: {cand.board_id}")
        set_level(self.status, "info")
        if self.row_of(cand.board_id) < 0:
            self.candidates.append(cand)
            row = self.table.rowCount()
            self.table.setRowCount(row + 1)
            self._render_row(row, cand, None)
            self._summarize(cand)
        self.table.selectRow(self.row_of(cand.board_id))

    # -- selecting -----------------------------------------------------------------------

    def row_of(self, board_id: str) -> int:
        return next((i for i, c in enumerate(self.candidates) if c.board_id == board_id), -1)

    def current(self) -> Candidate | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self.candidates):
            return None
        return self.candidates[rows[0].row()]

    def select_board(self, board_id: str) -> None:
        self.table.selectRow(self.row_of(board_id))

    def gate(self) -> str:
        if self._opening:
            return f"opening {self._opening}..."
        if self._scanning:
            return "scanning..."
        if self.current() is None:
            return "select a board in the table"
        return ""

    def refresh_gating(self) -> None:
        why = self.gate()
        self.select_button.setEnabled(not why)
        self.rescan_button.setEnabled(not self._scanning and not self._opening)
        cand = self.current()
        s = self.summaries.get(cand.board_id) if cand is not None else None
        if not why and s is not None and s.owner is not None and not owner_is_me(s.owner) \
                and not s.open_here:
            why = (f"held by {s.owner.describe()}. Select asks the engine anyway: it refuses "
                   "a live lock and takes over a stale one")
        self.reason.setText(why)

    def select(self) -> bool:
        why = self.gate()
        cand = self.current()
        if why or cand is None:
            self.status.setText(f"$ open  (not run)\n{why or 'no board selected'}. "
                                "Nothing was run.")
            set_level(self.status, "warning")
            return False
        self._opening = cand.board_id
        self.refresh_gating()
        engine = self.engine
        self.runner.submit(lambda: engine.open(cand, note=GUI_NOTE),
                           lambda res: self._opened(cand, res), label=f"open {cand.board_id}",
                           budget_s=30.0)
        return True

    def _opened(self, cand: Candidate, result: TaskResult) -> None:
        self._opening = ""
        line = rc_line(f"open {cand.board_id}", result)
        if not result.ok:
            text = describe_error(result.error)  # type: ignore[arg-type]
            self.status.setText(f"{line}\n{text}\nThe board was not opened.")
            set_level(self.status, "error")
            if isinstance(result.error, HeldError) and result.error.holder:
                row = self.row_of(cand.board_id)
                if row >= 0:
                    self.table.setItem(row, COL["Holder/Lock"],
                                       item(f"held by {result.error.holder}", "warning"))
            self.refresh_gating()
            return
        self.status.setText(line)
        set_level(self.status, "info")
        self.refresh_gating()
        self.board_opened.emit(cand)
        self.accept()

    def board_ids(self) -> list[str]:
        return [c.board_id for c in self.candidates]

    def cell(self, board_id: str, column: str) -> str:
        cell = self.table.item(self.row_of(board_id), COL[column])
        return cell.text() if cell is not None else ""
