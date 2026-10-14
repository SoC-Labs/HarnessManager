"""HM-C: ``harness-manager lease mode`` and ``lease show`` on a desk board, in-process.

The board's lease server is a stub (no board, no hub, no ssh); ``main()`` runs for real.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace as NS

import pytest

from harness_manager.cli import cmd_hub
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.services import lease_backend as lb
from tests.fakes.lrc_lease import LeaseWorld, factory

COURTESY = lb.COURTESY_LABEL


@dataclass
class _Cand:
    board_id: str = "mps3@192.168.10.101:6900"
    name: str = "mps3-01"


@dataclass
class _Board:
    kind: str = lb.KIND_BOARD
    host: str = "192.168.10.101"
    target: str = "mps3-01"
    name: str = "mps3-01"
    single_user: bool = False
    can_change: bool = True
    reason: str = ""
    calls: list = field(default_factory=list)
    client: NS = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.client = NS(set_mode=self.set_mode, principal=lambda: "me@pc",
                         board_id=lambda: "mps3_01")

    def mode(self):
        return lb.LeaseMode(single_user=self.single_user, can_change=self.can_change,
                            change_reason=self.reason, by="alice@pc", since_s=30)

    def set_mode(self, on, take=False, confirm_board=None):
        self.calls.append((on, take))
        self.single_user = on
        return {"lease": {"holder": "me@pc", "mine": True} if take else None}


def run(capsys, *argv):
    from harness_manager.cli.main import main

    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def board(monkeypatch) -> _Board:
    b = _Board()
    monkeypatch.setattr(cmd_hub, "_lease_server", lambda ctx: (_Cand(), b))
    monkeypatch.setattr(cmd_hub, "_hub", lambda ctx: (_Cand(), b))
    monkeypatch.setattr(cmd_hub, "LeaseService", factory(LeaseWorld()))
    monkeypatch.setattr(cmd_hub, "_stdin_is_tty", lambda: False)
    return b


def test_mode_shows_shared(board, capsys):
    rc, out, _ = run(capsys, "lease", "mode", "192.168.10.101")
    assert rc == 0 and "mps3-01: shared" in out


def test_mode_json_and_tsv(board, capsys):
    board.single_user = True
    rc, out, _ = run(capsys, "lease", "mode", "192.168.10.101", "--json")
    data = json.loads(out)
    assert rc == 0 and data["mode"]["single_user"] is True and data["mode"]["label"] == COURTESY
    rc, out, _ = run(capsys, "lease", "mode", "192.168.10.101", "--tsv")
    row = out.rstrip("\n").split("\t")
    assert len(row) == len(TSV_COLUMNS["lease mode"]) and row[1] == "on"


def test_mode_on_needs_yes_without_a_terminal(board, capsys):
    rc, _, err = run(capsys, "lease", "mode", "192.168.10.101", "on")
    assert rc == 15 and board.calls == [] and "--yes" in err


def test_mode_on_with_yes_takes_the_lease(board, capsys):
    rc, out, _ = run(capsys, "lease", "mode", "192.168.10.101", "on", "--yes")
    assert rc == 0 and board.calls == [(True, True)] and "you hold the lease" in out


def test_mode_off_does_not_take(board, capsys):
    board.single_user = True
    rc, out, _ = run(capsys, "lease", "mode", "192.168.10.101", "off", "--yes")
    assert rc == 0 and board.calls == [(False, False)] and "shared again" in out


def test_mode_refused_when_not_owner(board, capsys):
    board.can_change, board.reason = False, "claim the board first"
    rc, _, err = run(capsys, "lease", "mode", "192.168.10.101", "on", "--yes")
    assert rc == 15 and board.calls == [] and "claim the board first" in err


def test_mode_already_and_behind_a_hub(board, capsys):
    rc, _, _ = run(capsys, "lease", "mode", "192.168.10.101", "off", "--yes")
    assert rc != 0 and board.calls == []
    board.kind = "hub"
    rc, _, err = run(capsys, "lease", "mode", "192.168.10.101", "--json")
    assert rc != 0 and "hub's lease already does this" in err


def test_show_on_a_desk_board_carries_server_and_mode(board, capsys):
    board.single_user = True
    rc, out, _ = run(capsys, "lease", "show", "192.168.10.101", "--json")
    data = json.loads(out)
    assert rc == 0
    assert data["server"] == {"kind": "board", "host": board.host, "name": "mps3-01"}
    assert data["mode"]["single_user"] is True


def test_hub_absent_text_names_single_user_mode(monkeypatch, capsys):
    monkeypatch.setattr(cmd_hub, "_lease_server", lambda ctx: (_Cand(), None))
    rc, _, err = run(capsys, "lease", "show", "192.168.10.101")
    assert rc != 0 and "single-user mode is off" in err and "lease mode TARGET on" in err
