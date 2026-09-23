"""Team T5: the CLI end to end on the virtual MPS3, through the installed Engine.

CLI -> ``cli.engine.get_engine()`` (T1's Engine) -> MPS3 pack -> pyverify ->
FakeShell pinned to the fielded firmware. Every check has a negative twin. The
only network is 127.0.0.1 (the FakeShell's ephemeral ports).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from socharness.cli.engine import get_engine
from socharness.cli.main import main
from socharness.core.errors import ExitCode
from tests.fakes.t2_overlays import OTHER_STATIC_ID, make_overlay, point_pushes_at
from tests.fakes.virtual_board import VirtualMps3


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def holder_of(endpoint: str) -> object:
    eng = get_engine()
    try:
        return eng.lock_owner(eng.candidate_for(endpoint).board_id)
    finally:
        eng.close_all()


# --- info / probe ---------------------------------------------------------------------------


def test_info_tsv_and_human_agree(vboard: VirtualMps3, capsys):
    rc, out, _ = run(capsys, "--tsv", "info", vboard.shell_endpoint)
    cols = out.rstrip("\n").split("\t")
    assert rc == 0 and cols[2].lower() == "0x3f1a560f" and cols[4] == "greybox"
    rc, out, _ = run(capsys, "info", vboard.shell_endpoint)
    assert rc == 0 and "greybox" in out and "cannot     reboot_board: needs the Debug USB" in out


def test_info_while_another_engine_holds_the_board_names_the_holder(vboard, capsys):
    eng = get_engine()
    cand = eng.candidate_for(vboard.shell_endpoint)
    eng.open(cand, note="t5 holder")
    try:
        rc, out, err = run(capsys, "--json", "info", vboard.shell_endpoint)
        assert rc == ExitCode.HELD and "t5 holder" in err
        assert "t5 holder" in json.loads(out)["error"]["holder"]
    finally:
        eng.close_all()
    # Twin: released, the same verb succeeds.
    rc, _, _ = run(capsys, "info", vboard.shell_endpoint)
    assert rc == ExitCode.OK


def test_info_usb_only_board(tmp_path: Path, capsys):
    with VirtualMps3(tmp_path / "u", usb=True) as vb:
        rc, out, _ = run(capsys, "--json", "info", "-", "--serial", vb.mcc_url)
    info = json.loads(out)
    assert rc == 0 and info["health"]["control_channel"] == "offline"
    assert info["candidate"]["links"][0]["kind"] == "usb_serial"
    assert "console_controller" in info["capabilities"]          # USB links count...
    assert "deploy_partial" in info["unavailable"]               # ...and Ethernet is missing


def test_usb_only_target_without_a_usb_link_is_usage(capsys):
    rc, _, err = run(capsys, "info", "-")
    assert rc == ExitCode.USAGE and "--serial" in err


def test_probe_finds_the_virtual_board(vboard: VirtualMps3, capsys):
    rc, out, _ = run(capsys, "--json", "probe", "--host", vboard.shell_endpoint, "--no-scan",
                     "--timeout", "1")
    cands = json.loads(out)["candidates"]
    assert rc == 0 and cands[0]["evidence"] == "answered ping"


def test_probe_nothing_answers_is_exit_3(capsys):
    rc, out, _ = run(capsys, "--json", "probe", "--host", "127.0.0.1:1", "--no-scan",
                     "--timeout", "0.5")
    assert rc == ExitCode.ABSENT and json.loads(out)["error"]["data"]["candidates"] == []


# --- attach / detach ------------------------------------------------------------------------


def test_attach_for_zero_takes_and_releases(vboard: VirtualMps3, capsys):
    rc, out, _ = run(capsys, "--json", "attach", vboard.shell_endpoint, "--for", "0",
                     "--note", "smoke")
    obj = json.loads(out)
    assert rc == 0 and obj["state"] == "attached" and "smoke" in obj["holder"]["note"]
    assert obj["holder"]["pid"] == os.getpid()
    assert holder_of(vboard.shell_endpoint) is None             # released on exit


def test_attach_refused_while_another_engine_holds(vboard: VirtualMps3, capsys):
    eng = get_engine()
    eng.open(eng.candidate_for(vboard.shell_endpoint), note="someone else")
    try:
        rc, _, err = run(capsys, "attach", vboard.shell_endpoint, "--for", "0")
    finally:
        eng.close_all()
    assert rc == ExitCode.HELD and "someone else" in err


def _first_line(proc: subprocess.Popen, timeout: float) -> str:
    q: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: q.put(proc.stdout.readline()), daemon=True).start()
    return q.get(timeout=timeout)


def test_attach_holds_across_processes_until_detach(vboard: VirtualMps3, capsys):
    ep = vboard.shell_endpoint
    proc = subprocess.Popen(
        [sys.executable, "-m", "socharness.cli.main", "--json", "attach", ep, "--note", "t5-e2e"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    try:
        attached = json.loads(_first_line(proc, 30))
        assert attached["state"] == "attached" and attached["holder"]["pid"] == proc.pid

        # While it holds the board, this process is held off, and told how to detach.
        rc, _, err = run(capsys, "info", ep)
        assert rc == ExitCode.HELD and "t5-e2e" in err and f"socharness detach {ep}" in err

        rc, out, _ = run(capsys, "--json", "detach", ep)
        assert rc == 0 and json.loads(out)["state"] == "released"
        proc.wait(timeout=15)
        if os.name != "nt":                   # Windows terminates without the handler
            assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert holder_of(ep) is None
    # Twin: nothing is attached now.
    rc, _, _ = run(capsys, "detach", ep)
    assert rc == ExitCode.ALREADY
    rc, _, _ = run(capsys, "info", ep)
    assert rc == ExitCode.OK


# --- reset ----------------------------------------------------------------------------------


def test_reset_dut_reaches_the_shell(vboard: VirtualMps3, capsys):
    rc, out, _ = run(capsys, "--tsv", "reset", vboard.shell_endpoint)
    assert rc == 0 and out.split("\t")[1] == "dut"
    assert vboard.shell.resets == ["dut"]


def test_reset_rp_is_refused_before_the_wire(vboard: VirtualMps3, capsys):
    rc, _, err = run(capsys, "reset", vboard.shell_endpoint, "rp")
    assert rc == ExitCode.USAGE and "dut" in err
    assert vboard.shell.resets == []                  # the fielded shell never saw it


# --- lab --------------------------------------------------------------------------------------


def test_lab_display_query_reports_the_owner(vboard: VirtualMps3, capsys):
    rc, out, _ = run(capsys, "--json", "lab", vboard.shell_endpoint, "display", "query")
    assert rc == 0 and json.loads(out)["owner"] == "harness"
    assert vboard.shell.display_requests == []        # a query moves nothing


def test_lab_display_query_without_kvm_is_unavailable(vboard: VirtualMps3, capsys):
    vboard.shell.has_clcd_kvm = False                 # a bitstream with no CLCD KVM slave
    rc, out, err = run(capsys, "--json", "lab", vboard.shell_endpoint, "display", "query")
    assert rc == ExitCode.UNAVAILABLE and "clcd_kvm not present" in err
    assert json.loads(out)["error"]["capability"] == "mps3.display_flip"


def test_lab_display_flip_lands(vboard: VirtualMps3, capsys):
    rc, out, _ = run(capsys, "--json", "lab", vboard.shell_endpoint, "display", "dut")
    obj = json.loads(out)
    assert rc == 0 and obj["landed"] is True and obj["owner"] == "dut"
    assert vboard.shell.display_requests == ["dut"]


def test_lab_link_pulse_reaches_the_vphy(vboard: VirtualMps3, capsys):
    rc, _, _ = run(capsys, "lab", vboard.shell_endpoint, "link", "pulse")
    assert rc == 0 and vboard.shell.link_events == ["pulse"]


def test_lab_on_a_usb_only_board_needs_ethernet(tmp_path: Path, capsys):
    with VirtualMps3(tmp_path / "u", usb=True) as vb:
        rc, _, err = run(capsys, "lab", "-", "--serial", vb.mcc_url, "link", "up")
        assert vb.shell.link_events == []
    assert rc == ExitCode.UNAVAILABLE and "Ethernet" in err


def test_lab_macgen_counts_an_injected_fault(vboard: VirtualMps3, capsys):
    rc, out, _ = run(capsys, "--json", "lab", vboard.shell_endpoint, "macgen",
                     "--inject", "bad_fcs")
    obj = json.loads(out)
    assert rc == 0 and obj["tx"] == obj["rx"] > 0 and obj["err"] == 1
    rc, _, _ = run(capsys, "lab", vboard.shell_endpoint, "macgen", "--inject", "bogus")
    assert rc == ExitCode.USAGE and len(vboard.shell.macgen_calls) == 1


def test_lab_dutrx_reads_what_the_dut_sent(vboard: VirtualMps3, capsys):
    rc, out, _ = run(capsys, "--json", "lab", vboard.shell_endpoint, "dutrx")
    assert rc == 0 and json.loads(out)["frames"] == []       # idle: a result, not an error
    vboard.shell.push_dut_frame(bytes(range(64)))
    rc, out, _ = run(capsys, "--json", "lab", vboard.shell_endpoint, "dutrx")
    obj = json.loads(out)
    assert rc == 0 and obj["frames"][0]["data"] == bytes(range(64)).hex() and obj["rx"] == 1


# --- board controller over Ethernet only ------------------------------------------------------


def test_mcc_over_ethernet_only_needs_the_usb_cable(vboard: VirtualMps3, capsys):
    rc, _, err = run(capsys, "mcc", vboard.shell_endpoint, "temp")
    assert rc == ExitCode.UNAVAILABLE
    assert err.strip() == "socharness: console_controller is unavailable — needs the Debug USB cable"


def test_telemetry_never_reports_a_missing_value_as_zero(vboard: VirtualMps3, capsys):
    rc, out, _ = run(capsys, "--json", "telemetry", vboard.shell_endpoint)
    readings = json.loads(out)["readings"]
    assert rc == 0 and readings
    for r in readings:
        assert (r["value"] is None) == (not r["available"])
        if r["value"] is None:
            assert r["reason"]


# --- program / overlays / restore (T2's DeployService through the CLI) -------------------------


@pytest.fixture
def pushes(vboard: VirtualMps3, monkeypatch) -> VirtualMps3:
    point_pushes_at(monkeypatch, vboard)
    return vboard


def test_program_an_overlay_end_to_end(pushes: VirtualMps3, tmp_path: Path, capsys):
    ov = make_overlay(tmp_path / "ov", "synth").parent
    rc, out, err = run(capsys, "--json", "program", pushes.shell_endpoint, "synth", "--yes",
                       "--overlay-dir", str(ov))
    obj = json.loads(out)
    assert rc == 0, err
    assert obj["result"]["verified"] is True and obj["result"]["rm_id"] == "0x01007a57"
    assert "deploy: push" in err                                    # progress on stderr
    assert "SOCHARNESS_MPS3_OVERLAY_DIRS" not in os.environ         # the flag was scoped
    rc, out, _ = run(capsys, "--json", "info", pushes.shell_endpoint)
    assert json.loads(out)["identity"]["rm_id"] == "0x01007a57"


def test_program_for_another_shell_is_refused_and_the_board_untouched(pushes, tmp_path, capsys):
    ov = make_overlay(tmp_path / "ov", "alien", static_id=OTHER_STATIC_ID).parent
    rc, _, err = run(capsys, "program", pushes.shell_endpoint, "alien", "--yes",
                     "--overlay-dir", str(ov))
    assert rc == ExitCode.INCOMPATIBLE and "MISMATCH" in err
    assert pushes.shell.accepted_pushes == [] and pushes.shell.current_rm_id == 0


def test_program_a_corrupt_overlay_is_refused_not_incompatible(pushes, tmp_path, capsys):
    ov = make_overlay(tmp_path / "ov", "synth", corrupt_partial=True).parent
    rc, _, _ = run(capsys, "program", pushes.shell_endpoint, "synth", "--yes",
                   "--overlay-dir", str(ov))
    assert rc == ExitCode.REFUSED and pushes.shell.accepted_pushes == []


def test_overlays_lists_both_kinds(pushes: VirtualMps3, tmp_path: Path, capsys):
    make_overlay(tmp_path / "ov", "synth")
    make_overlay(tmp_path / "ov", "alien", static_id=OTHER_STATIC_ID, rm_id=0x0100_7A58)
    rc, out, _ = run(capsys, "--json", "overlays", pushes.shell_endpoint,
                     "--overlay-dir", str(tmp_path / "ov"))
    obj = json.loads(out)
    assert rc == 0 and [o["name"] for o in obj["compatible"]] == ["synth"]
    assert [o["name"] for o in obj["incompatible"]] == ["alien"]


def test_overlay_dir_that_does_not_exist_is_absent(pushes: VirtualMps3, tmp_path, capsys):
    rc, _, _ = run(capsys, "overlays", pushes.shell_endpoint, "--overlay-dir",
                   str(tmp_path / "nope"))
    assert rc == ExitCode.ABSENT


def test_restore_without_a_greybox_is_absent(pushes: VirtualMps3, tmp_path, capsys):
    rc, _, err = run(capsys, "restore", pushes.shell_endpoint)
    assert rc == ExitCode.ABSENT and "greybox" in err
