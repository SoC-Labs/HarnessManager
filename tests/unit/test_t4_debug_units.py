"""T4 debug pieces with no process at all: log parsing, argv, the MPS3 design table.

The log excerpts are verbatim OpenOCD 0.12.0+dev output, captured 2026-09-23
against tests/fakes/t4_rbb_jtag.py (a JTAG TAP behind remote_bitbang).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from socharness.core.errors import (
    ActionFailedError,
    ExitCode,
    HeldError,
    NothingOnTargetError,
    PortBoundError,
    UnavailableError,
    UnreachableError,
)
from socharness.services.debug import (
    DebugPorts,
    classify_failure,
    detect_argv,
    parse_idcodes,
    port_in_use,
    up_argv,
)
from socharness_board_mps3 import openocd as mps3ocd
from tests.fakes.t4_debug_rig import StaticDebugAdapter

FOUND = """Info : Connecting to 127.0.0.1:62427
Info : remote_bitbang driver initialized
Info : JTAG tap: nanosoc.cpu tap/device found: 0x6ba00477 (mfg: 0x23b (ARM Ltd), part: 0xba00, ver: 0x6)
Info : gdb port disabled
   TapName             Enabled  IdCode     Expected   IrLen IrCap IrMask
-- ------------------- -------- ---------- ---------- ----- ----- ------
 0 nanosoc.cpu            Y     0x6ba00477 0x6ba00477     4 0x01  0x03
shutdown command invoked
"""
UNEXPECTED = """Info : JTAG tap: nanosoc.cpu tap/device found: 0x4ba00477 (mfg: 0x23b (ARM Ltd), part: 0xba00, ver: 0x4)
Warn : JTAG tap: nanosoc.cpu       UNEXPECTED: 0x4ba00477 (mfg: 0x23b (ARM Ltd), part: 0xba00, ver: 0x4)
Error: JTAG tap: nanosoc.cpu  expected 1 of 1: 0x6ba00477 (mfg: 0x23b (ARM Ltd), part: 0xba00, ver: 0x6)
"""
ALL_ZEROES = """Info : remote_bitbang driver initialized
Error: JTAG scan chain interrogation failed: all zeroes
Error: Check JTAG interface, timings, target power, etc.
Error: Trying to use configured scan chain anyway...
Error: nanosoc.cpu: IR capture error; saw 0x00 not 0x01
Error: Invalid ACK (0) in DAP response
"""
SLAMMED = """Info : remote_bitbang driver initialized
Error: Error on socket 'remote_bitbang_fill_buf': errno==104, message: Connection reset by peer.
Error: Trying to use configured scan chain anyway...
Error: Error on socket 'remote_bitbang_putc': errno==32, message: Broken pipe.
"""
REFUSED = """Info : Connecting to 127.0.0.1:1
Error: Error on socket 'Failed to connect': errno==9, message: Bad file descriptor.
"""
GDB_TAKEN = """Info : starting gdb server for nanosoc.cpu0 on 46369
Error: couldn't bind gdb to socket on port 46369: Address already in use
"""
AFTER_INIT = "Error: The 'gdb_port' command must be used before 'init'.\n"


def test_parse_idcodes_reads_the_found_line_and_the_scan_chain_table():
    assert parse_idcodes(FOUND) == ["0x6ba00477"]
    table_only = FOUND.split("Info : gdb port disabled\n")[1]
    assert parse_idcodes(table_only) == ["0x6ba00477"]
    assert parse_idcodes(UNEXPECTED) == ["0x4ba00477"]           # what is there, not what is wanted


def test_negative_twin_no_idcode_in_a_dead_chain():
    assert parse_idcodes(ALL_ZEROES) == []
    zero_row = " 0 nanosoc.cpu            Y     0x00000000 0x6ba00477     4 0x01  0x03\n"
    assert parse_idcodes(zero_row) == []


@pytest.mark.parametrize("log, error, code", [
    (GDB_TAKEN, PortBoundError, ExitCode.PORT_BOUND),
    (REFUSED, UnreachableError, ExitCode.UNREACHABLE),
    (SLAMMED, HeldError, ExitCode.HELD),
    (ALL_ZEROES, NothingOnTargetError, ExitCode.NOTHING_ON_TARGET),
    ("Error: Can't find nanosoc_mps3_jtag.cfg\n", UnavailableError, ExitCode.UNAVAILABLE),
    ("Error: The specified debug interface was not found (remote_bitbang)\n", UnavailableError,
     ExitCode.UNAVAILABLE),
    (AFTER_INIT, ActionFailedError, ExitCode.ACTION_FAILED),
    ("Error: something new\n", ActionFailedError, ExitCode.ACTION_FAILED),
])
def test_classify_failure_maps_openocd_logs_to_exit_codes(log, error, code):
    err = classify_failure(log, 1, target="remote_bitbang 127.0.0.1:6921")
    assert isinstance(err, error) and err.code == code


def test_unknown_failure_keeps_openocd_s_own_words():
    err = classify_failure("Info : x\nError: the flux capacitor\n", 1)
    assert "the flux capacitor" in str(err) and "exit code 1" in str(err)


def test_up_argv_shape(tmp_path: Path):
    adapter = StaticDebugAdapter(tmp_path, 6921)
    ports = DebugPorts.block(23300)
    argv = up_argv("openocd", adapter, adapter.openocd_config(), ports,
                   ("nanosoc.cpu0 configure -event gdb-attach nanosoc_halt_examine",))
    assert argv == [
        "openocd", "-s", str(tmp_path),
        "-c", "set RBB_HOST 127.0.0.1", "-c", "set RBB_PORT 6921",
        "-f", "nanosoc_mps3_jtag.cfg", "-f", "nanosoc_ops.tcl",
        "-c", "nanosoc.cpu0 configure -event gdb-attach nanosoc_halt_examine",
        "-c", "gdb_port 23300; telnet_port 23302; tcl_port 23303; bindto 127.0.0.1",
    ]
    assert ports.reserved() == (23300, 23301, 23302, 23303)   # +1 kept for a second target


def test_detect_argv_never_serves_and_never_examines(tmp_path: Path):
    adapter = StaticDebugAdapter(tmp_path, 6921)
    argv = detect_argv("openocd", adapter, adapter.openocd_config())
    assert argv[-8:] == ["-c", "gdb_port disabled; telnet_port disabled; tcl_port disabled",
                         "-c", "init", "-c", "scan_chain", "-c", "shutdown"]
    assert not any("configure -event" in a for a in argv)          # no hooks, no halt


def test_port_in_use_sees_a_listener_and_nothing_else():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    assert port_in_use(port)
    s.close()
    assert not port_in_use(port)           # negative twin: a closed listener leaves it free


# -- the MPS3 design table ----------------------------------------------------------------------


@pytest.mark.parametrize("design, configs", [
    (0x0001, ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl")),
    (0x0005, ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl")),
    (0x0008, ("nanosoc_iice_chain.cfg", "nanosoc_ops.tcl")),
])
def test_designs_with_a_dap(design, configs):
    recipe = mps3ocd.design_debug(design)
    assert recipe.configs == configs
    assert recipe.gdb_attach == (("nanosoc.cpu0", "nanosoc_halt_examine"),)


@pytest.mark.parametrize("design, words", [
    (0x0003, "two-AP config not available yet"),
    (0x0000, "greybox"),
    (0x001E, "led"),
    (0x7A57, "design 0x7a57"),
])
def test_negative_twin_designs_without_a_dap(design, words):
    with pytest.raises(NothingOnTargetError, match=words) as exc:
        mps3ocd.design_debug(design)
    assert exc.value.code == ExitCode.NOTHING_ON_TARGET


def test_the_config_dir_env_is_read_at_call_time(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(mps3ocd.CFG_DIR_ENV, str(tmp_path))
    assert mps3ocd.config_dir() == tmp_path
    monkeypatch.delenv(mps3ocd.CFG_DIR_ENV)
    assert mps3ocd.config_dir() == mps3ocd.OPENOCD_CFG_DIR


def test_make_debug_adapter_needs_the_shell():
    class NoShell:
        shell = None

    assert mps3ocd.make_debug_adapter(NoShell()) is None
