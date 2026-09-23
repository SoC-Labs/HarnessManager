"""Lane L2: ``harness-manager pty`` and ``harness-manager baud`` on the virtual MPS3.

In-process (the engine factory points the pack at the virtual board) the ``pty``
verb holds the PTY until ``--for`` ends; through a running harness-manager-daemon
that already has the board open, it prints the daemon's PTY and returns. The
verbs are registered the way the lead will wire ``cmd_io.register`` into
``cli/main.py`` (``run_cli``). Every check has a negative twin.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from harness_manager.cli.engine import set_engine_factory
from harness_manager.core.errors import ExitCode
from harness_manager.services import pty as ptymod
from tests.fakes.l2_rig import (
    PtyClient,
    apply_pack_ccr,
    install_uart_baud_codec,
    l2_virtual_board,
    run_cli,
    wait_for,
)
from tests.fakes.t13_daemon import LiveDaemon, bid_path, engine_for

pytestmark = pytest.mark.skipif(not ptymod.supported(), reason="PTYs need a POSIX system")

BANNER = b"nanosoc boot\n"


@pytest.fixture(autouse=True)
def _wiring(tmp_path: Path, monkeypatch) -> None:
    apply_pack_ccr(monkeypatch)
    install_uart_baud_codec(monkeypatch)
    monkeypatch.setenv(ptymod.PTY_DIR_ENV, str(tmp_path / "ptys"))


@pytest.fixture
def in_process():
    """The CLI's engine is an in-process Engine pointed at the board given to it."""
    boards: list = []
    previous = set_engine_factory(lambda _args: engine_for(boards[0]))
    yield boards
    set_engine_factory(previous)


# -- pty ----------------------------------------------------------------------------------------------


def test_pty_in_process_holds_the_pty_until_it_ends(tmp_path, in_process, capsys):
    with l2_virtual_board(tmp_path, features=()) as vb:
        in_process.append(vb)
        result: dict = {}

        def cli() -> None:
            result["rc"] = run_cli(None, "--json", "pty", vb.shell_endpoint, "uart0",
                                   "--for", "4")[0]

        t = threading.Thread(target=cli)
        t.start()
        root = tmp_path / "ptys"
        link = wait_for(lambda: next(iter(root.glob("*/uart0")), None), what="the PTY link")
        with PtyClient(str(link)) as term:
            term.read_until(BANNER)                               # the console, through the CLI's PTY
        t.join(timeout=15)
        out, err = capsys.readouterr()
    assert result["rc"] == 0
    body = json.loads(out)
    assert body["path"] == str(link) and body["held_by"] == "this command" and body["held_here"]
    assert body["command"] == f"screen {link}"
    # Negative twin: once the command ends, the PTY is gone with it.
    assert not os.path.lexists(link)


def test_pty_human_output_is_the_path_then_the_screen_command(tmp_path, in_process, capsys):
    with l2_virtual_board(tmp_path, features=()) as vb:
        in_process.append(vb)
        rc, out, err = run_cli(capsys, "pty", vb.shell_endpoint, "uart0", "--for", "0.2")
    lines = out.splitlines()
    assert rc == 0 and len(lines) == 2
    assert lines[0].endswith("/uart0") and lines[1] == f"screen {lines[0]}"
    assert "screen" not in err or "Ctrl-C" not in err                  # --for: no Ctrl-C note


def test_negative_twin_pty_for_an_unknown_console_exits_absent(tmp_path, in_process, capsys):
    with l2_virtual_board(tmp_path, features=()) as vb:
        in_process.append(vb)
        rc, out, err = run_cli(capsys, "--json", "pty", vb.shell_endpoint, "uart9", "--for", "0")
    assert rc == ExitCode.ABSENT and "uart0" in json.loads(out)["error"]["hint"]
    assert not list((tmp_path / "ptys").glob("*/*"))


def test_pty_through_the_daemon_prints_its_pty_and_returns(tmp_path, capsys):
    with l2_virtual_board(tmp_path, features=()) as vb:
        eng = engine_for(vb)
        try:
            with LiveDaemon(eng) as d, d.client() as api:
                bid = api.post("/api/v1/boards", json={"target": vb.shell_endpoint}).json()[
                    "board_id"]                                   # the GUI has the board open
                rc, out, err = run_cli(capsys, "--json", "pty", vb.shell_endpoint, "uart0")
                body = json.loads(out)
                assert rc == 0 and body["held_by"] == "harness-manager-daemon"
                assert body["held_here"] is False
                # The CLI has exited; the daemon's PTY is still there, and it is the same one.
                assert os.readlink(body["path"]) == body["device"]
                shown = api.get(f"{bid_path(bid)}/consoles/uart0/pty").json()["pty"]
                assert shown["path"] == body["path"]
                with PtyClient(body["path"]) as term:
                    term.read_until(BANNER)
        finally:
            eng.close_all()


# -- baud ---------------------------------------------------------------------------------------------------


def test_baud_reads_the_designs_fixed_rate_and_refuses_a_change(tmp_path, in_process, capsys):
    with l2_virtual_board(tmp_path, features=()) as vb:
        in_process.append(vb)
        rc, out, _ = run_cli(capsys, "--json", "baud", vb.shell_endpoint, "uart0")
        row = json.loads(out)
        assert rc == 0 and (row["baud"], row["source"], row["settable"]) == (76800, "design", False)
        rc, out, _ = run_cli(capsys, "--json", "baud", vb.shell_endpoint, "uart0", "115200")
        err = json.loads(out)["error"]
        assert rc == ExitCode.UNAVAILABLE and "76800" in err["message"]
        rc, out, _ = run_cli(capsys, "--tsv", "baud", vb.shell_endpoint, "uart0")
        cols = out.rstrip("\n").split("\t")
        assert rc == 0 and len(cols) == 7 and cols[1:6] == ["uart0", "ethernet", "76800", "false",
                                                            "design"]


def test_negative_twin_baud_sets_uart0_when_the_harness_can(tmp_path, in_process, capsys):
    with l2_virtual_board(tmp_path) as vb:
        in_process.append(vb)
        rc, out, _ = run_cli(capsys, "--json", "baud", vb.shell_endpoint, "uart0", "115200")
        body = json.loads(out)
        assert rc == 0 and body["baud"] == 115200 and body["changed"]["source"] == "harness"
        assert vb.shell.uart["uart0"]["baud"] == 115200
        rc, out, _ = run_cli(capsys, "baud", vb.shell_endpoint, "uart0")
        assert rc == 0 and out.startswith("uart0  115200 baud  (ethernet, harness)  settable")


def test_baud_through_the_daemon(tmp_path, capsys):
    with l2_virtual_board(tmp_path) as vb:
        eng = engine_for(vb)
        try:
            with LiveDaemon(eng) as d, d.client() as api:
                api.post("/api/v1/boards", json={"target": vb.shell_endpoint})
                rc, out, _ = run_cli(capsys, "--json", "baud", vb.shell_endpoint, "uart0", "57600")
                assert rc == 0 and json.loads(out)["baud"] == 57600
                assert vb.shell.uart["uart0"]["baud"] == 57600
        finally:
            eng.close_all()
