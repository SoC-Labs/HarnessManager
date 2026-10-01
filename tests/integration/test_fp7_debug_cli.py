"""FIX-PACK-7 items 2 and 3: the terminal holding ``debug up`` across a swap, and ``debug
status``'s ``openocd`` line on the board path.

- E-OCD (board 2, 1 Oct): the terminal holding ``debug up`` kept showing the old gdb port after
  a swap reopened the session (ocd6_up.txt vs ocd7_status_after.txt). Now it prints the new
  block after a verified reopen, and why not after a swap that was not verified;
- ``debug status`` with where = board printed this PC's ``openocd …: has remote_bitbang`` line,
  which describes this PC. It is gone there (kept on this PC's path); --json/--tsv unchanged.

Over the T5 fake engine (the CLI), and over the real DEBUG-ONBOARD lab (the real pack, the
launcher behind the claim's ssh, the real DebugService and DeployService) for the events a
real swap publishes. Each check has its negative twin.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from harness_manager.cli.cmd_io import SWAP_REOPENED, SwapWatch
from harness_manager.core.events import Event
from harness_manager.core.services import DebugStatus
from harness_manager.services import debug_onboard as OB
from harness_manager.services.debug import gdb_command
from tests.integration import test_debug_onboard as _onboard
from tests.integration import test_fp7_debug_down_first as _fp7

BID = "mps3@127.0.0.1"
lab, debug, bus = _onboard.lab, _onboard.debug, _onboard.bus     # the fixtures


@pytest.fixture
def cli():
    from harness_manager.cli.engine import set_engine_factory
    from tests.fakes.t5_fake_engine import FakeEngine

    eng = FakeEngine()
    previous = set_engine_factory(lambda _args: eng)
    yield eng
    set_engine_factory(previous)


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    from harness_manager.cli.main import main

    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def state(st: str, **data: Any) -> Event:
    return Event("debug.state", BID, {"state": st, **data})


CLOSED = state("down", detail=OB.SWAP_REASON, where="board")
REOPENED = state("up", ports={"gdb": 44487}, pid=775, where="board",
                 detail="OpenOCD runs on the board; gdb reaches it through the board's SSH "
                        "(pid 775)", config=["nanosoc_mps3_multicore_jtag.cfg"],
                 gdb_ports=[44487, 47353], cores=["cpu0", "cpu1"])


def feeder(bus: Any, events: list[Event]) -> threading.Thread:
    """Publish ``events`` once the holding ``debug up`` listens (never before: a loaded
    machine can take longer than any fixed delay to get there)."""
    def feed() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with bus._lock:
                if bus._subs.get("debug.state"):
                    break
            time.sleep(0.02)
        for ev in events:
            bus.publish(ev)

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    return t


def hold_with(cli, capsys, events: list[Event], *argv: str) -> str:
    """``debug up`` holding for a moment while ``events`` arrive on the engine's bus."""
    t = feeder(cli.bus, events)
    rc, out, _err = run_cli(capsys, *argv, "debug", "up", "127.0.0.1", "--for", "3")
    t.join(5)
    assert rc == 0
    return out


# --- item 2: the holding terminal across a swap -----------------------------------------------------


def test_the_holding_terminal_prints_the_new_block_after_a_verified_reopen(cli, capsys):
    out = hold_with(cli, capsys, [CLOSED, state("starting", where="board"), REOPENED])
    lines = out.splitlines()
    first = lines.index("debug      mps3@127.0.0.1: up")                 # the first block
    assert "gdb        127.0.0.1:29555" in lines[first:]
    closed = lines.index(f"swap       {OB.SWAP_REASON}: the new ports follow when it reopens")
    assert closed > first
    again = lines.index(f"swap       {SWAP_REOPENED}")
    block = lines[again + 1:]
    assert block[:2] == ["debug      mps3@127.0.0.1: up",
                         "where      the board: OpenOCD runs there; gdb reaches it through "
                         "the board's SSH"]
    assert "gdb        127.0.0.1:44487  cpu0" in block and "gdb        127.0.0.1:47353  cpu1" in block
    assert [ln for ln in block if ln.startswith("attach")] == [
        f"attach     {gdb_command(44487)}", f"attach     {gdb_command(47353)}"]
    assert "pid        775" in block


def test_twin_an_unverified_swap_prints_why_it_was_not_reopened(cli, capsys):
    out = hold_with(cli, capsys, [CLOSED, state("down", where="board",
                                                detail="not reopened: the swap was not "
                                                       "verified by the board")])
    lines = out.splitlines()
    assert "swap       not reopened: the swap was not verified by the board" in lines
    assert ("           this terminal holds no session now: Ctrl-C ends it; `harness-manager "
            "debug up 127.0.0.1` starts a new one") in lines
    assert f"swap       {SWAP_REOPENED}" not in lines and "44487" not in out


def test_twin_a_reopen_that_fails_says_not_reopened_with_its_words(cli, capsys):
    out = hold_with(cli, capsys, [CLOSED, state("failed", where="board",
                                                detail="the board's JTAG is held by x")])
    assert "swap       not reopened: the board's JTAG is held by x" in out.splitlines()


def test_twin_events_of_another_board_or_without_a_swap_print_nothing(cli, capsys):
    other = Event("debug.state", "mps3@10.0.0.9", dict(CLOSED.data))
    out = hold_with(cli, capsys, [other, REOPENED, state("down", detail="stopped")])
    assert "swap" not in out and "44487" not in out


def test_json_keeps_stdout_to_the_one_result_and_says_the_swap_on_stderr(cli, capsys):
    t = feeder(cli.bus, [CLOSED, REOPENED])
    rc, out, err = run_cli(capsys, "--json", "debug", "up", "127.0.0.1", "--for", "3")
    t.join(5)
    assert rc == 0
    json.loads(out)                                   # still exactly one JSON object
    assert f"swap       {SWAP_REOPENED}" in err and "127.0.0.1:44487  cpu0" in err


def test_the_lines_of_a_real_swap_name_the_ports_the_session_reopened_on(lab, debug, bus, capsys):
    """The real DebugService's events: HM's on-board session, a verified swap through
    DeployService (DEBUG-DOWN-FIRST closes it, the reopen brings it back on its ports)."""
    rig = lab()
    debug.up(rig.session)
    watch = SwapWatch(SimpleNamespace(fmt="human", note=lambda _t: None), rig.board_id, "B")
    bus.subscribe("debug.state", watch)
    board = _fp7.Swapping(rig)
    assert _fp7.deploys(bus, debug).deploy(board, _fp7.UPY).verified
    _fp7.settle(debug)
    lines = capsys.readouterr()[0].splitlines()
    now = debug.status(rig.session)
    assert now.state == "up"
    assert lines[0] == f"swap       {OB.SWAP_REASON}: the new ports follow when it reopens"
    assert lines[1] == f"swap       {SWAP_REOPENED}"
    assert f"gdb        127.0.0.1:{now.gdb_port}" in lines
    assert f"attach     {gdb_command(now.gdb_port)}" in lines


def test_twin_a_real_failed_swap_prints_not_reopened(lab, debug, bus, capsys):
    rig = lab()
    debug.up(rig.session)
    bus.subscribe("debug.state", SwapWatch(SimpleNamespace(fmt="human", note=lambda _t: None),
                                           rig.board_id, "B"))
    board = _fp7.Swapping(rig)
    board.deploy.verified = False
    with pytest.raises(Exception, match="did not verify"):
        _fp7.deploys(bus, debug).deploy(board, _fp7.UPY)
    lines = capsys.readouterr()[0].splitlines()
    assert lines[1].startswith("swap       not reopened: the swap failed at deploy")
    assert not any(ln.startswith("gdb") for ln in lines)


# --- item 3: debug status on the board path has no "openocd" line --------------------------------


REPORT = {"ok": True, "path": "/opt/xpack-openocd/bin/openocd", "need": "remote_bitbang",
          "adapters": ["remote_bitbang"],
          "detail": "/opt/xpack-openocd/bin/openocd: has remote_bitbang (40 adapters)", "hint": ""}


def test_debug_status_on_the_board_drops_this_pcs_openocd_line(cli, capsys):
    cli.st.debug_state = "up"
    cli.debug._status = lambda: DebugStatus("up", config=("nanosoc_mps3_jtag.cfg",), pid=731,
                                            gdb_ports=(53663,), cores=("cpu0",), where="board")
    cli.debug.openocd_report = lambda _session=None: dict(REPORT)
    rc, out, _err = run_cli(capsys, "debug", "status", "127.0.0.1")
    assert rc == 0 and "where      the board" in out
    assert "openocd" not in out and "remote_bitbang" not in out
    # --json and --tsv keep their shapes
    body = json.loads(run_cli(capsys, "--json", "debug", "status", "127.0.0.1")[1])
    assert body["openocd"] == REPORT
    row = run_cli(capsys, "--tsv", "debug", "status", "127.0.0.1")[1].strip().splitlines()[-1]
    assert row.split("\t")[-3:] == ["board", "53663", "cpu0"]


def test_twin_this_pcs_path_keeps_the_openocd_line(cli, capsys):
    cli.st.debug_state = "up"
    cli.debug.openocd_report = lambda _session=None: dict(REPORT)
    rc, out, _err = run_cli(capsys, "debug", "status", "127.0.0.1")
    assert rc == 0
    assert "openocd    /opt/xpack-openocd/bin/openocd: has remote_bitbang (40 adapters)" \
        in out.splitlines()
