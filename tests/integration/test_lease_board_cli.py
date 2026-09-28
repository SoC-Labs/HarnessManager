"""LEASE-BOARD: the CLI names fpgahub's physical board (``mps3_01``), and still leases the target.

david asked why the lease said ``mps3_01_pl`` and whether that is deprecated. It is not
(fpgahub 0.3.0 keeps ``fpgahub lease …`` as the single-target shortcut, UPGRADING.md "top-level
fpgahub lease … unchanged"), and it is the name pyverify's lease dialect uses, so Harness
Manager keeps leasing on it (docs/HUB_MODE.md "Boards and targets"). What changes is what
people read: ``mps3-01 (mps3_01 on HUB, target mps3_01_pl)``, JSON ``board`` beside
``target``, a BOARD column appended to the ``lease`` TSV.

Real ``LeaseService`` and ``HubClient`` over L1's lab rig (fake ssh, fake hub): nothing
reaches a hub. Each check has its negative twin: a hub that maps the target to no board
shows the target exactly as before.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness_manager.core.errors import ExitCode
from tests.fakes.l1_rig import BOARD_IP, HUB, TARGET, lab
from tests.fakes.virtual_board import VirtualMps3
from tests.integration.test_l1_cli import run

BOARD = "mps3_01"
WHERE = f"mps3-01 ({BOARD} on {HUB}, target {TARGET})"
WHERE_UNKNOWN = f"mps3-01 ({TARGET} on {HUB})"            # today's words, kept for the twin


@pytest.fixture
def rig(tmp_path, monkeypatch):
    sd = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=sd) as r:
        yield r


def board_lists(rig) -> int:
    return sum(1 for c in rig.hub.calls if c[1:3] == ["board", "list"])


def test_show_acquire_release_name_the_board_with_the_target_as_a_detail(capsys, rig):
    rc, out, _ = run(capsys, "lease", "show", BOARD_IP)
    assert rc == ExitCode.OK and out.splitlines()[0] == f"{WHERE}: not leased"
    rc, out, err = run(capsys, "lease", "acquire", BOARD_IP, "--holder", "david-b0")
    assert rc == ExitCode.OK, err
    assert out.splitlines()[0].startswith(f"{WHERE}: held by david-b0 until ")
    assert f"asking {HUB} for {WHERE_UNKNOWN} as david-b0" in err   # before the hub answered
    rc, out, _ = run(capsys, "lease", "show", BOARD_IP)
    assert out.splitlines()[0].startswith(f"{WHERE}: held by david-b0 (user ") and "yours" in out
    rc, out, _ = run(capsys, "lease", "release", BOARD_IP)
    assert rc == ExitCode.OK and out.strip() == f"{WHERE}: released" and rig.hub.current is None


def test_the_lease_is_still_taken_on_the_target_never_the_board(capsys, rig):
    """Interop (pyverify.lease, the Linux lead's soaks): one lease form on the board."""
    run(capsys, "lease", "acquire", BOARD_IP, "--holder", "david-b0")
    run(capsys, "lease", "release", BOARD_IP)
    lease_calls = [c for c in rig.hub.calls if "lease" in c[1:3]]
    assert [c[:4] for c in lease_calls] == [["fpgahub", "lease", "acquire", TARGET],
                                            ["fpgahub", "lease", "release", TARGET]]
    assert not any(c[1:3] == ["board", "lease"] for c in rig.hub.calls)


def test_json_adds_board_and_keeps_target(capsys, rig):
    rc, out, _ = run(capsys, "--json", "lease", "acquire", BOARD_IP, "--holder", "david-b0")
    d = json.loads(out)
    assert rc == ExitCode.OK and d["board"] == BOARD
    assert d["lease"]["target"] == TARGET and d["lease"]["board"] == BOARD
    rc, out, _ = run(capsys, "--json", "lease", "show", BOARD_IP)
    d = json.loads(out)
    assert d["board"] == BOARD and d["lease"]["target"] == TARGET and d["lease"]["board"] == BOARD
    rc, out, _ = run(capsys, "--tsv", "lease", "show", BOARD_IP)
    row = out.rstrip("\n").split("\t")
    assert row[0] == TARGET and row[-1] == BOARD                 # TARGET first, BOARD appended
    rc, out, _ = run(capsys, "--json", "lease", "release", BOARD_IP)
    d = json.loads(out)
    assert d["board"] == BOARD and d["released"]["target"] == TARGET
    assert d["released"]["board"] == BOARD


def test_the_board_is_asked_for_once_per_command_at_most(capsys, rig):
    for argv in (("lease", "show", BOARD_IP), ("lease", "acquire", BOARD_IP, "--holder", "x"),
                 ("lease", "release", BOARD_IP)):
        before = board_lists(rig)
        run(capsys, *argv)
        assert board_lists(rig) - before == 1, argv


def test_negative_twin_a_target_no_board_owns_shows_the_target_as_before(capsys, rig):
    rig.hub.boards = {}                                          # board list: no group has it
    rc, out, _ = run(capsys, "lease", "show", BOARD_IP)
    assert rc == ExitCode.OK and out.splitlines()[0] == f"{WHERE_UNKNOWN}: not leased"
    rc, out, err = run(capsys, "--json", "lease", "acquire", BOARD_IP, "--holder", "david-b0")
    d = json.loads(out)
    assert rc == ExitCode.OK, err
    assert d["board"] is None and d["lease"]["board"] is None and d["lease"]["target"] == TARGET
    rc, out, _ = run(capsys, "--tsv", "lease", "show", BOARD_IP)
    assert out.rstrip("\n").split("\t")[-1] == "-"                 # the TSV's empty field
    rc, out, _ = run(capsys, "lease", "release", BOARD_IP)
    assert out.strip() == f"{WHERE_UNKNOWN}: released"


def test_negative_twin_a_single_target_board_named_as_its_target_has_no_detail(capsys, rig):
    rig.hub.boards = {TARGET: [(TARGET, None)]}                  # fpgahub's standalone group
    rc, out, _ = run(capsys, "lease", "show", BOARD_IP)
    assert out.splitlines()[0] == f"{WHERE_UNKNOWN}: not leased"
    rc, out, _ = run(capsys, "--json", "lease", "show", BOARD_IP)
    assert json.loads(out)["board"] == TARGET


def test_boards_toml_hub_board_names_it_with_no_hub_call(capsys, tmp_path, monkeypatch):
    toml = (f'[boards.lab]\nmatch = ["{BOARD_IP}"]\nvia = "ssh:{HUB}"\n'
            f'hub = {{ host = "{HUB}", target = "{TARGET}", board = "mps3_09" }}\n')
    sd = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=sd, toml=toml) as r:
        rc, out, _ = run(capsys, "lease", "show", BOARD_IP)
        assert rc == ExitCode.OK
        # N1 names the board from hub.board too (mps3-09); the lease text names the hub's board
        assert out.splitlines()[0] == f"mps3-09 (mps3_09 on {HUB}, target {TARGET}): not leased"
        rc, out, _ = run(capsys, "lease", "acquire", BOARD_IP, "--holder", "david-b0")
        assert out.splitlines()[0].startswith(f"mps3-09 (mps3_09 on {HUB}, target {TARGET}): held")
        assert board_lists(r) == 0                               # stated, never asked
