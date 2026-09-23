"""T9 SYSMON readers against the REAL tools, when installed (skipped otherwise). No hardware.

- **xsdb** (Vivado/Vivado Lab): our generated script runs in the real xsdb and meets a
  closed port on 127.0.0.1, which proves the script is accepted up to ``connect`` and the
  no-hw_server path reports ``UnavailableError``. (``jtag sequence`` option syntax was
  checked separately against xsdb 2024.1 offline; a real hw_server needs the J17 cable.)
- **OpenOCD** with ``remote_bitbang``: the reader drives ``FakeRbbServer``, a KU115-shaped
  TAP on 127.0.0.1. This proves the argv order, the IR/DR scan sequence, the
  read-on-the-next-shift pipelining, the IDCODE guard, and that no write is ever sent.

``HARNESS_MANAGER_TEST_REAL_XSDB`` / ``HARNESS_MANAGER_TEST_REAL_OPENOCD`` point at a binary that is
not on PATH (e.g. ~/tools/oss-cad-suite/bin/openocd).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess

import pytest

from harness_manager.core.errors import UnavailableError
from harness_manager_mps3.sysmon import READ_REGS, OpenOcdSysmon, XsdbSysmon, sysmon_readings
from tests.fakes.t9_sysmon import GOOD_REGS, FakeRbbServer

XSDB = os.environ.get("HARNESS_MANAGER_TEST_REAL_XSDB") or shutil.which("xsdb")
OPENOCD = os.environ.get("HARNESS_MANAGER_TEST_REAL_OPENOCD") or shutil.which("openocd")


def _has_remote_bitbang(binary: str | None) -> bool:
    if not binary:
        return False
    try:
        out = subprocess.run([binary, "-c", "adapter list", "-c", "shutdown"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "remote_bitbang" in out.stdout + out.stderr


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _rbb(port: int) -> list[str]:
    return ["adapter driver remote_bitbang", "remote_bitbang host 127.0.0.1", f"remote_bitbang port {port}"]


@pytest.mark.slow
@pytest.mark.skipif(not XSDB, reason="no xsdb (set HARNESS_MANAGER_TEST_REAL_XSDB)")
def test_real_xsdb_without_a_hw_server_is_unavailable():
    reader = XsdbSysmon(xsdb=XSDB, hw_server=f"tcp:127.0.0.1:{_closed_port()}", timeout_s=120)
    with pytest.raises(UnavailableError) as err:
        reader.read()
    assert "cannot reach hw_server at tcp:127.0.0.1:" in err.value.reason
    assert "refused" in err.value.reason.lower()


def needs_openocd(fn):
    """slow, and skipped without an openocd that has the remote_bitbang adapter."""
    fn = pytest.mark.skipif(bool(OPENOCD) and not _has_remote_bitbang(OPENOCD),
                            reason=f"{OPENOCD} was built without remote_bitbang")(fn)
    fn = pytest.mark.skipif(not OPENOCD, reason="no openocd (set HARNESS_MANAGER_TEST_REAL_OPENOCD)")(fn)
    return pytest.mark.slow(fn)


@needs_openocd
def test_real_openocd_reads_sysmon_from_a_ku115_shaped_tap():
    with FakeRbbServer() as srv:
        sample = OpenOcdSysmon(openocd=OPENOCD, adapter=_rbb(srv.port), speed_khz=None,
                               timeout_s=60).read()
        commands, writes = list(srv.sysmon.commands), list(srv.sysmon.writes)
    assert sample.codes == GOOD_REGS and sample.errors == {}
    assert commands == [c for a in READ_REGS for c in ((1, a), (0, 0))]
    assert writes == []
    rows = {r.name: r for r in sysmon_readings(sample)}
    assert rows["fpga_die_temp"].value == 44.0 and rows["fpga_die_temp"].source == "sysmon-jtag (openocd)"


@needs_openocd
def test_real_openocd_never_scans_sysmon_into_another_device():
    with FakeRbbServer(idcode=0x6BA00477) as srv:           # a Cortex-M DAP, not a KU115
        with pytest.raises(UnavailableError, match="not a KU115"):
            OpenOcdSysmon(openocd=OPENOCD, adapter=_rbb(srv.port), speed_khz=None, timeout_s=60).read()
        assert srv.sysmon.commands == [] and srv.sysmon.writes == []


@needs_openocd
def test_real_openocd_with_no_adapter_is_unavailable():
    with pytest.raises(UnavailableError, match="openocd read no SYSMON values"):
        OpenOcdSysmon(openocd=OPENOCD, adapter=_rbb(_closed_port()), speed_khz=None, timeout_s=60).read()
