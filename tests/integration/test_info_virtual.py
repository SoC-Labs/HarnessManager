"""End to end: CLI -> MPS3 pack -> pyverify -> FakeShell pinned to the fielded firmware."""

from __future__ import annotations

import json

import pytest

from socharness.cli.main import main
from socharness.core.errors import ExitCode, NothingOnTargetError, UsageError
from tests.fakes.virtual_board import VirtualMps3


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_info_json_reports_fielded_identity(vboard: VirtualMps3, capsys):
    rc, out, _ = run_cli(capsys, "--json", "info", vboard.shell_endpoint)
    assert rc == ExitCode.OK
    info = json.loads(out)
    ident = info["identity"]
    assert ident["shell_id"].lower() == "0x3f1a560f"
    assert ident["harness_version"] == "1.0.0"
    assert ident["build_check"] == "unchecked"          # USR_ACCESS unreadable on 0x3F1A560F
    assert ident["rm_name"] == "greybox"
    assert "deploy_partial" in info["capabilities"]
    # Ethernet only: the controller features must say why they are missing.
    assert info["unavailable"]["reboot_board"].startswith("needs the Debug USB cable")
    assert info["unavailable"]["reset_shell"] == "needs harness firmware with 'reboot'"
    # The fielded firmware has no shell console over Ethernet (handover A12).
    assert "console_shell" in info["unavailable"]
    assert "power" in info["unavailable"]["telemetry_power"]


def test_info_tsv_columns_are_stable(vboard: VirtualMps3, capsys):
    rc, out, _ = run_cli(capsys, "--tsv", "info", vboard.shell_endpoint)
    cols = out.rstrip("\n").split("\t")
    assert rc == ExitCode.OK and len(cols) == 8
    assert cols[1] == "mps3" and cols[6] == "unchecked" and cols[7] == "idle"


def test_info_unreachable_is_exit_7(capsys):
    # Negative twin: nothing listens on this port.
    rc, _, err = run_cli(capsys, "info", "127.0.0.1:1")
    assert rc == ExitCode.UNREACHABLE and "refused" in err


def test_usage_error_is_exit_2(capsys):
    rc, _, _ = run_cli(capsys, "info")
    assert rc == ExitCode.USAGE


def test_probe_finds_the_virtual_board(vboard: VirtualMps3, capsys):
    rc, out, _ = run_cli(capsys, "probe", "--host", vboard.shell_endpoint, "--timeout", "1")
    assert rc == ExitCode.OK and "answered ping" in out


def test_probe_nothing_is_exit_3(capsys):
    rc, _, _ = run_cli(capsys, "probe", "--host", "127.0.0.1:1", "--timeout", "0.5")
    assert rc == ExitCode.ABSENT


def test_fielded_profile_rejects_rp_reset(vboard: VirtualMps3, mps3_pack):
    session = mps3_pack.open(mps3_pack.candidate_for_host(vboard.shell_endpoint))
    session.resets.reset("dut")                   # the fielded firmware accepts this
    with pytest.raises(UsageError):
        session.resets.reset("rp")                # ...and only this (coordinator.c:246)


def test_debug_config_refuses_greybox(vboard: VirtualMps3, mps3_pack):
    session = mps3_pack.open(mps3_pack.candidate_for_host(vboard.shell_endpoint))
    with pytest.raises(NothingOnTargetError):
        session.debug.openocd_config()           # greybox has no DAP


def test_debug_config_for_nanosoc(tmp_path, mps3_pack):
    with VirtualMps3(tmp_path / "b2", boot_rm_id=0x01000001) as vb:
        session = mps3_pack.open(mps3_pack.candidate_for_host(vb.shell_endpoint))
        assert session.debug.openocd_config()[0] == "nanosoc_mps3_jtag.cfg"
        probe = session.debug.openocd_probe_args()
        assert probe[0].startswith("set RBB_HOST ")    # overrides come before any -f


def test_console_endpoints_point_at_the_board(vboard: VirtualMps3, mps3_pack):
    session = mps3_pack.open(mps3_pack.candidate_for_host(vboard.shell_endpoint))
    eps = session.consoles.console_endpoints()
    assert eps["uart0"] == f"tcp://{vboard.shell.host}:{vboard.console_ports['uart0']}"
