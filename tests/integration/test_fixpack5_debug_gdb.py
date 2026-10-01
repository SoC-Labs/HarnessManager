"""FIX-PACK-5 item 2: the gdb command line ``debug up`` prints, with ``set remotetimeout 60``.

The guide walk (board 2 through the hub, a claimed Linux board, 2026-09-30 14:40): gdb's
default 2 s reply timeout failed the attach through the claim's SSH forward ("Remote replied
unexpectedly to 'vMustReplyEmpty': timeout") while OpenOCD warned "keep_alive() ... increase
"set remotetimeout""; with ``set remotetimeout 60`` the halt, registers and detach worked.

Decision: the line carries it on every route (it only bounds the wait for a reply; see
``services.debug.GDB_REMOTE_TIMEOUT_S``), so these check one line everywhere:

- ``debug up``/``status`` print it (human ``attach`` line, JSON ``gdb_command``) and the
  app's Debug card builds the same string (``sections/debug.js``); twin: no line when down;
- end to end on the virtual board, through the real Engine, MPS3 pack and a stub OpenOCD
  (the gdb port it prints is the one the stub listens on);
- a board reached through the hub prints the same line as a direct one.
"""

from __future__ import annotations

import dataclasses
import json
import random
import re
from pathlib import Path

import pytest

from harness_manager.cli.engine import ENV_NO_DAEMON, set_engine_factory
from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode
from harness_manager.core.model import Link, LinkKind
from harness_manager.services.debug import (
    GDB_REMOTE_TIMEOUT_S,
    DebugPorts,
    gdb_command,
    port_in_use,
)
from tests.fakes.t5_fake_engine import FakeEngine

DEBUG_JS = (Path(__file__).resolve().parents[2]
            / "src/harness_manager/web/static/js/sections/debug.js")


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def fake():
    eng = FakeEngine()
    previous = set_engine_factory(lambda _args: eng)
    yield eng
    set_engine_factory(previous)


# -- the line ------------------------------------------------------------------------------------


def test_the_line_sets_the_timeout_before_it_connects():
    line = gdb_command(23344)
    assert GDB_REMOTE_TIMEOUT_S == 60
    assert line == ('arm-none-eabi-gdb -ex "set remotetimeout 60" '
                    '-ex "target extended-remote 127.0.0.1:23344"')
    assert line.index("remotetimeout") < line.index("extended-remote")   # gdb runs -ex in order
    assert "'" not in line                          # double quotes: sh, cmd and PowerShell


def test_the_apps_attach_row_builds_the_same_line():
    js = DEBUG_JS.read_text(encoding="utf-8")
    # UI v2 (WORKBENCH): one gdb line per core, built by gdbCmdFor(port).
    m = re.search(r"export function gdbCmdFor\(port\) \{\s*return `([^`]*)`", js)
    assert m, "sections/debug.js no longer builds the gdb line in gdbCmdFor(port)"
    assert m.group(1).replace("${port}", "3343") == gdb_command(3343)


def test_negative_twin_the_js_check_catches_a_drift():
    drifted = "arm-none-eabi-gdb -ex 'target extended-remote :${port}'"
    assert drifted.replace("${port}", "3343") != gdb_command(3343)


# -- the CLI, scripted engine --------------------------------------------------------------------


def test_debug_up_prints_the_attach_line_and_says_where_to_run_gdb(fake, capsys):
    rc, out, err = run(capsys, "debug", "up", "127.0.0.1", "--for", "0")
    assert rc == 0
    assert f"attach     {gdb_command(29555)}" in out.splitlines()
    fake.st.debug_state = "up"                  # `up --for 0` took it down again
    rc, out, _ = run(capsys, "--json", "debug", "status", "127.0.0.1")
    body = json.loads(out)
    assert rc == 0 and body["gdb_command"] == gdb_command(body["status"]["gdb_port"])


def test_debug_up_without_for_says_it_holds_and_gdb_goes_elsewhere(fake, capsys, monkeypatch):
    import harness_manager.cli.cmd_io as cmd_io

    monkeypatch.setattr(cmd_io, "hold", lambda _s: "test")
    rc, _out, err = run(capsys, "debug", "up", "127.0.0.1")
    assert rc == 0
    assert "run gdb in another terminal" in err and "Ctrl-C" in err


def test_negative_twin_no_attach_line_when_the_session_is_down(fake, capsys):
    rc, out, _ = run(capsys, "debug", "status", "127.0.0.1")
    assert rc == 0 and "attach" not in out
    rc, out, _ = run(capsys, "--json", "debug", "down", "127.0.0.1")
    assert rc == 0 and "gdb_command" not in json.loads(out)


def test_a_board_through_the_hub_prints_the_same_line_as_a_direct_one(fake, capsys):
    fake.st.debug_state = "up"
    rc, direct, _ = run(capsys, "debug", "status", "127.0.0.1")
    orig = fake.candidate_for

    def via_hub(target, pack="mps3", via=""):
        cand = orig(target, pack)
        hub = Link(LinkKind.ETHERNET, cand.links[0].address, "shell control channel, via hub",
                   via="hub")
        return dataclasses.replace(cand, links=(hub,))

    fake.candidate_for = via_hub
    rc2, hubbed, _ = run(capsys, "debug", "status", "127.0.0.1")
    assert rc == rc2 == 0
    pick = [ln for ln in direct.splitlines() if ln.startswith("attach")]
    assert pick and pick == [ln for ln in hubbed.splitlines() if ln.startswith("attach")]


# -- end to end: virtual board, real Engine and MPS3 pack, stub OpenOCD ----------------------------


def _free_block() -> int:
    """A debug port block below the service's own 23300 range and every ephemeral range."""
    pick = random.SystemRandom()
    for _ in range(200):
        base = pick.randrange(20000, 23000)
        if not any(port_in_use(p) for p in DebugPorts.block(base).reserved()):
            return base
    raise RuntimeError("no free debug port block")


def test_end_to_end_the_printed_gdb_port_is_the_stubs(tmp_path, monkeypatch, capsys):
    from tests.fakes.t4_debug_rig import use_stub
    from tests.fakes.t4_rbb_jtag import FakeJtagServer
    from tests.fakes.t13_daemon import engine_for
    from tests.fakes.virtual_board import VirtualMps3

    rig = use_stub(monkeypatch, tmp_path)
    base = _free_block()
    monkeypatch.setenv("HARNESS_MANAGER_DEBUG_PORT_BASE", str(base))
    monkeypatch.setenv(ENV_NO_DAEMON, "1")
    with VirtualMps3(tmp_path / "nanosoc", boot_rm_id=0x01000001) as vb, \
            FakeJtagServer() as jtag:
        previous = set_engine_factory(lambda _args: engine_for(vb, rbb_port=jtag.port))
        try:
            rc, out, err = run(capsys, "--json", "debug", "up", vb.shell_endpoint, "--for", "0")
        finally:
            set_engine_factory(previous)
    body = json.loads(out)
    assert rc == ExitCode.OK, err
    port = body["status"]["gdb_port"]
    assert port == DebugPorts.block(base).gdb
    assert body["gdb_command"] == gdb_command(port)
    assert rig.runs()                                  # the stub OpenOCD really ran
