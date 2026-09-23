"""T4 against a REAL OpenOCD, when one is installed (skipped otherwise).

The board's jtag_server is ``FakeJtagServer``: a remote_bitbang server with one
JTAG TAP (IDCODE 0x6ba00477, IR 4) and a minimal JTAG-DP, enough for
OpenOCD's ``init`` (JTAG scan + ``dap init``) with the core ``-defer-examine``.
The target half is the platform repo's real ``nanosoc_mps3_jtag.cfg``.

What this proves on a real OpenOCD: our argv is accepted in this order; the
probe overrides reach the adapter (it dials the fake, not 192.168.10.101); the
ports and bindto take effect; the gdb-attach hook is installed and survives
``init`` (which would otherwise install ``halt 1000``); detect parses real
output; and each board-side failure maps to the right exit code.
What it cannot prove: a gdb attach (the fake has no MEM-AP/Cortex-M0), or
anything about the real board's jtag_server timing.

Set ``SOCHARNESS_TEST_REAL_OPENOCD`` to a binary to run this where ``openocd``
is not on PATH (e.g. ~/tools/oss-cad-suite/bin/openocd).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import socharness.services.debug as dbg
from socharness.core.errors import HeldError, NothingOnTargetError, PortBoundError
from socharness.core.events import EventBus
from socharness.services.debug import DebugService, pid_alive, port_in_use, tcl_rpc
from socharness_board_mps3.constants import OPENOCD_CFG_DIR
from socharness_board_mps3.pack import Mps3Pack
from tests.fakes.t4_rbb_jtag import FakeJtagServer
from tests.fakes.virtual_board import VirtualMps3

REAL = os.environ.get("SOCHARNESS_TEST_REAL_OPENOCD") or shutil.which("openocd")
CFG_DIR = Path(os.environ.get("SOCHARNESS_MPS3_OPENOCD_DIR") or OPENOCD_CFG_DIR or "/nonexistent")


def _has_remote_bitbang(binary: str | None) -> bool:
    if not binary:
        return False
    try:
        out = subprocess.run([binary, "-c", "adapter list", "-c", "shutdown"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "remote_bitbang" in out.stdout + out.stderr


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not REAL, reason="no real openocd (set SOCHARNESS_TEST_REAL_OPENOCD)"),
    pytest.mark.skipif(bool(REAL) and not _has_remote_bitbang(REAL),
                       reason=f"{REAL} was built without the remote_bitbang adapter"),
    pytest.mark.skipif(not (CFG_DIR / "nanosoc_mps3_jtag.cfg").is_file(),
                       reason="the platform repo's host/openocd configs are not here"),
]


@pytest.fixture
def real(monkeypatch) -> str:
    monkeypatch.setenv("SOCHARNESS_OPENOCD", str(REAL))
    monkeypatch.setenv("SOCHARNESS_MPS3_OPENOCD_DIR", str(CFG_DIR))
    monkeypatch.delenv("SOCHARNESS_DEBUG_PORT_BASE", raising=False)
    return str(REAL)


@pytest.fixture
def board(tmp_path) -> Iterator[VirtualMps3]:
    with VirtualMps3(tmp_path / "vb", boot_rm_id=0x01000001) as vb:
        yield vb


@pytest.fixture
def debug() -> Iterator[DebugService]:
    svc = DebugService(EventBus(), start_timeout=30)
    yield svc
    svc.close()


def session_for(vb: VirtualMps3, rbb_port: int):
    pack = Mps3Pack(console_ports=vb.console_ports, rbb_port=rbb_port)
    return pack.open(pack.candidate_for_host(vb.shell_endpoint))


def test_real_openocd_detect_up_hook_and_down(real, board, debug):
    with FakeJtagServer() as jtag:
        session = session_for(board, jtag.port)
        assert debug.detect(session) == "0x6ba00477"
        st = debug.up(session)
        try:
            assert all(port_in_use(p) for p in (st.gdb_port, st.telnet_port, st.tcl_port))
            assert tcl_rpc(st.tcl_port, "version").startswith("Open On-Chip Debugger")
            # The hook is installed and init_target_events did not replace it.
            assert tcl_rpc(st.tcl_port, "nanosoc.cpu0 cget -event gdb-attach") == \
                "nanosoc_halt_examine"
            assert tcl_rpc(st.tcl_port, "nanosoc.cpu0 cget -gdb-port") == str(st.gdb_port)
            assert debug.detect(session) == "0x6ba00477"            # via the live tcl port
            assert jtag.accepted == 2 and jtag.refused == 0         # detect run + session
            assert jtag.taps[-1].dp_ops > 0                          # dap init really ran
        finally:
            down = debug.down(session)
        assert down.state == "down" and not pid_alive(st.pid)
        assert not port_in_use(st.gdb_port)


@pytest.mark.parametrize("mode, error", [("none", NothingOnTargetError), ("slam", HeldError)])
def test_negative_twin_real_openocd_board_failures(real, board, debug, mode, error):
    with FakeJtagServer(mode=mode) as jtag:
        session = session_for(board, jtag.port)
        with pytest.raises(error):
            debug.detect(session)
        with pytest.raises(error):
            debug.up(session)


def test_real_openocd_with_its_gdb_port_taken_is_killed(real, board, monkeypatch):
    # OpenOCD 0.12 logs "couldn't bind gdb" and KEEPS RUNNING (measured 2026-09-23).
    monkeypatch.setattr(dbg, "port_in_use", lambda port: False)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    base = blocker.getsockname()[1]
    svc = DebugService(EventBus(), port_base=base, start_timeout=30)
    try:
        with FakeJtagServer() as jtag:
            with pytest.raises(PortBoundError):
                svc.up(session_for(board, jtag.port))
            assert list(svc.registry_dir.glob("*.json")) == []
    finally:
        blocker.close()
        svc.close()
