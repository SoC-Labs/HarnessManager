"""Lane XVC-CORE (X3): ``harness-manager xvc open|close|status|tcl|ltx`` on the virtual MPS3.

In-process (the engine factory points the pack at the virtual board), ``open`` holds the
session until ``--for`` ends; through a running harness-manager-daemon that already has
the board open, it returns and the session stays there. The harness's XVC is the fake
server on 127.0.0.1; hw_server is the fake one (``tests/fakes/fake_hw_server.py``). Each
check has a negative twin.
"""

from __future__ import annotations

import json
import os

import pytest

from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.main import main
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ExitCode
from harness_manager.services import xvc as X
from harness_manager_mps3 import xvc as MX
from tests.fakes.t13_daemon import LiveDaemon, bid_path, engine_for
from tests.fakes.virtual_board import FIELDED_3F1A560F, FIELDED_ILA_V011, VirtualMps3
from tests.fakes.xvc_server import FakeXvcServer
from tests.integration.test_xvc_api import ila_overlay  # noqa: F401 - a fixture
from tests.integration.test_xvc_mps3 import NANOSOC_ILA
from tests.unit.test_xvc_service import hw_shim, wait_for  # noqa: F401 - a fixture


@pytest.fixture
def fake(monkeypatch):
    with FakeXvcServer() as srv:
        monkeypatch.setenv(MX.XVC_PORT_ENV, str(srv.port))
        yield srv


@pytest.fixture
def board(tmp_path, fake, ila_overlay):  # noqa: F811 - the fixture above
    with VirtualMps3(tmp_path / "vb", FIELDED_ILA_V011, boot_rm_id=NANOSOC_ILA) as vb:
        yield vb


@pytest.fixture
def in_process():
    boards: list = []
    previous = set_engine_factory(lambda _args: engine_for(boards[0]))
    yield boards
    set_engine_factory(previous)


def cli(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


# --- in-process ---------------------------------------------------------------------------------------


def test_status_says_the_scope_and_the_x6_warning(board, in_process, capsys):
    in_process.append(board)
    rc, out, _ = cli(capsys, "--json", "xvc", "status", board.shell_endpoint)
    body = json.loads(out)
    assert rc == 0 and body["state"] == "down" and body["reason"] == ""
    assert "never whole-device JTAG" in body["scope"]
    assert X.UNAUTHENTICATED_WARNING in body["warnings"]
    rc, out, _ = cli(capsys, "xvc", "status", board.shell_endpoint)
    assert "scope      XVC here is scoped to the reconfigurable partition" in out
    assert "warning    XVC on this harness is unauthenticated" in out


def test_negative_twin_a_harness_without_xvc_dbgbr_says_why_and_exits_12(tmp_path, fake,
                                                                          in_process, capsys):
    with VirtualMps3(tmp_path / "old", FIELDED_3F1A560F) as vb:
        in_process.append(vb)
        rc, out, _ = cli(capsys, "--json", "xvc", "status", vb.shell_endpoint)
        assert rc == 0 and "xvc_dbgbr" in json.loads(out)["reason"]
        rc, out, _ = cli(capsys, "--json", "xvc", "open", vb.shell_endpoint, "--byo",
                         "--for", "0")
        assert rc == ExitCode.UNAVAILABLE and "xvc_dbgbr" in json.loads(out)["error"]["message"]
        assert fake.stats.connections == 0


def test_open_byo_holds_the_session_for_its_time_then_frees_the_slot(board, in_process, fake,
                                                                     capsys):
    in_process.append(board)
    rc, out, err = cli(capsys, "--json", "xvc", "open", board.shell_endpoint, "--byo",
                       "--for", "0.3")
    body = json.loads(out)
    assert rc == 0 and body["state"] == "ready" and body["mode"] == "byo"
    assert body["url"] == f"127.0.0.1:{body['relay_port']}" and body["ltx"]["preferred"] == "rm"
    wait_for(lambda: not fake.attached, what="the board's slot to free")
    assert fake.stats.connections == 1


@pytest.mark.skipif(os.name != "posix", reason="a /bin/sh shim stands in for hw_server")
def test_open_runs_hms_hw_server_and_it_is_gone_after(board, in_process, fake, hw_shim, capsys):  # noqa: F811
    in_process.append(board)
    rc, out, _ = cli(capsys, "--json", "xvc", "open", board.shell_endpoint, "--for", "0.5")
    body = json.loads(out)
    assert rc == 0 and body["mode"] == "m1" and body["hw_server_pid"] > 0
    assert body["url"] == f"localhost:{body['hw_server_port']}"
    wait_for(lambda: not X._pid_alive(body["hw_server_pid"]), what="hw_server to exit")


def test_tcl_and_ltx_for_the_loaded_design(board, in_process, ila_overlay, tmp_path, capsys):  # noqa: F811
    in_process.append(board)
    rc, out, _ = cli(capsys, "--tsv", "xvc", "tcl", board.shell_endpoint, "--byo")
    rows = [line.split("\t") for line in out.splitlines()]
    assert rc == 0 and all(len(r) == len(TSV_COLUMNS["xvc tcl"]) for r in rows)
    text = "\n".join(r[2] for r in rows)
    assert "open_hw_target -xvc_url" in text and f"PROBES.FILE {{{ila_overlay}}}" in text
    dest = tmp_path / "got"
    dest.mkdir()
    rc, out, _ = cli(capsys, "--json", "xvc", "ltx", board.shell_endpoint, "--rm", "-o", str(dest))
    body = json.loads(out)
    assert rc == 0 and body["crc_ok"] is True
    assert (dest / "nanosoc_ila.ltx").read_bytes() == ila_overlay.read_bytes()


def test_negative_twin_no_static_probes_file_on_bare_metal_is_absent(board, in_process, capsys):
    in_process.append(board)
    rc, out, _ = cli(capsys, "--json", "xvc", "ltx", board.shell_endpoint, "--static")
    assert rc == ExitCode.ABSENT and "static" in json.loads(out)["error"]["message"]


# --- through harness-manager-daemon ---------------------------------------------------------------------


def test_through_the_daemon_open_returns_and_the_session_stays_there(board, fake, capsys):
    eng = engine_for(board)
    try:
        with LiveDaemon(eng) as d, d.client() as api:
            bid = api.post("/api/v1/boards", json={"target": board.shell_endpoint}).json()[
                "board_id"]                                 # the web UI has the board open
            rc, out, err = cli(capsys, "--json", "xvc", "open", board.shell_endpoint, "--byo")
            body = json.loads(out)
            assert rc == 0 and body["state"] == "ready" and "stays with" in err
            shown = api.get(f"{bid_path(bid)}/xvc").json()
            assert shown["state"] == "ready" and shown["relay_port"] == body["relay_port"]
            assert fake.attached                             # the daemon holds the slot
            rc, out, _ = cli(capsys, "--json", "xvc", "close", board.shell_endpoint)
            assert rc == 0 and json.loads(out)["state"] == "down"
            wait_for(lambda: not fake.attached, what="the board's slot to free")
            # twin: a second close is harmless and says nothing was open
            rc, out, _ = cli(capsys, "--json", "xvc", "close", board.shell_endpoint)
            assert rc == 0 and "no XVC session" in json.loads(out)["detail"]
    finally:
        eng.close_all()


def test_the_verb_help_names_the_scope():
    from harness_manager.cli.main import make_parser

    text = make_parser()._subparsers._group_actions[0].choices["xvc"].format_help()
    assert "never whole-device JTAG" in " ".join(text.split())
