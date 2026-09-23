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


def test_usb_only_session_opens_without_shell(tmp_path, mps3_pack):
    from socharness.core.transport import open_serial

    with VirtualMps3(tmp_path / "u", usb=True) as vb:
        cand = vb.candidate(ethernet=False)
        session = mps3_pack.open(cand)
        assert session.shell is None and not session.health().reachable
        # The fake MCC is reachable through the transport seam T3 builds on.
        port = open_serial(vb.mcc_url)
        assert port is vb.mcc


def test_open_serial_unknown_scheme_is_usage_error():
    from socharness.core.transport import open_serial

    with pytest.raises(UsageError):
        open_serial("bogus://x")


def test_busy_control_port_is_held_not_unreachable():
    """Accept-then-EOF (how the fielded shell refuses a 2nd client) must be HELD (4)."""
    import socket
    import threading

    from socharness.core.errors import HeldError
    from socharness_board_mps3.shell import Mps3Shell

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def accept_and_close() -> None:
        conn, _ = srv.accept()
        conn.close()

    t = threading.Thread(target=accept_and_close, daemon=True)
    t.start()
    try:
        with pytest.raises(HeldError) as exc:
            Mps3Shell("127.0.0.1", port, timeout=2).identity()
        assert exc.value.code == ExitCode.HELD
    finally:
        t.join(timeout=2)
        srv.close()


def test_clock_presets_and_refusals(vboard: VirtualMps3, mps3_pack):
    from socharness.core.errors import UsageError as UE

    session = mps3_pack.open(mps3_pack.candidate_for_host(vboard.shell_endpoint))
    assert not session.clocks.clocks()[0].available          # cannot read back before a set
    reading = session.clocks.set_clock("dut", 50)
    assert reading.value == 50.0 and session.clocks.clocks()[0].value == 50.0
    with pytest.raises(UE):
        session.clocks.set_clock("dut", 33)                  # not a preset (needs firmware A8)
    with pytest.raises(UE):
        session.clocks.set_clock("osc1", 50)                 # board oscillators are not the shell's


def test_probed_candidate_carries_identity(vboard: VirtualMps3, mps3_pack):
    from socharness.core.pack import ProbeHints

    (cand,) = mps3_pack.probe(ProbeHints(hosts=(vboard.shell_endpoint,), scan_usb=False))
    assert cand.identity is not None and cand.identity.shell_id.lower() == "0x3f1a560f"
    # Negative twin: an explicit candidate has not talked to the board, so it knows nothing.
    assert mps3_pack.candidate_for_host(vboard.shell_endpoint).identity is None
