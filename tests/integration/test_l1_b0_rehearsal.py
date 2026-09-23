"""L1: B0 rehearsals through the faked lab (tunnel + hub), in the order docs/HIL_B0.md runs them.

Slot 3 (the July Linux image, net-protocol v0.7): Harness Manager must degrade
gracefully without ``version``, and ``ping``, ``diag`` and the 6910 push must
still work, all through the SSH tunnel (B0 runbook, "Slot 3"). Slot 1's
read-only set (info, a console, the MCC temperature) runs against the fielded
bare-metal profile. Each check has a twin.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode, IncompatibleError
from harness_manager.core.model import Check
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from tests.fakes.l1_profiles import JULY_LINUX_STATIC_ID, LED_RM_ID, UNKNOWN_OP, JulyLinuxBoard
from tests.fakes.l1_rig import BOARD_IP, lab
from tests.fakes.t2_overlays import make_overlay, ref_named, use_overlay_dirs
from tests.fakes.virtual_board import VirtualMps3


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


def ask(port: int, line: bytes) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(line + b"\n")
        s.settimeout(3)
        return json.loads(s.makefile("rb").readline())


# --- slot 3: the July Linux harness ---------------------------------------------------------


def test_the_july_profile_answers_version_exactly_as_the_qemu_proof_shows(tmp_path):
    with JulyLinuxBoard(tmp_path) as vb:
        port = vb.shell.control_port
        assert ask(port, b'{"op":"version"}') == UNKNOWN_OP
        assert ask(port, b'{"op":"stats"}') == UNKNOWN_OP
        assert ask(port, b'{"op":"ping"}') == {"ok": True, "shell_id": "0x2b082e1b",
                                               "rm_id": "0x00000000"}
        diag = ask(port, b'{"op":"diag"}')
        assert diag["ok"] and "rx_recover" not in diag                  # Linux omits these


def test_slot3_info_is_graceful_through_the_tunnel(tmp_path, monkeypatch, capsys):
    with JulyLinuxBoard(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()):
        rc = main(["--json", "info", BOARD_IP])
        out = json.loads(capsys.readouterr().out)
        assert rc == ExitCode.OK
        ident = out["identity"]
        assert ident["shell_id"] == "0x2b082e1b" and ident["rm_id"] == "0x00000000"
        assert ident["harness_version"] == "" and ident["features"] == []
        assert ident["build_check"] == Check.UNCHECKED.value              # not a pass
        assert out["health"]["reachable"] is True
        assert out["candidate"]["links"][0]["via"] == "ssh"


def test_slot3_ping_and_diag_through_the_tunnel(tmp_path, monkeypatch):
    with JulyLinuxBoard(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()):
        eng = Engine(EngineConfig(state_dir=state_dir()))
        try:
            session = eng.open(eng.candidate_for(BOARD_IP))
            health = session.health()
            assert health.reachable and health.counters                   # diag answered
            assert "rx_recover" not in health.counters
        finally:
            eng.close_all()


def test_slot3_the_6910_push_swaps_led_through_the_tunnel(tmp_path, monkeypatch):
    use_overlay_dirs(monkeypatch, make_overlay(tmp_path / "ov", "led", rm_id=LED_RM_ID,
                                               static_id=JULY_LINUX_STATIC_ID).parent)
    with JulyLinuxBoard(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        eng = Engine(EngineConfig(state_dir=state_dir()))
        try:
            session = eng.open(eng.candidate_for(BOARD_IP))
            led = ref_named(eng.deploy.overlays(session), "led")
            result = eng.deploy.deploy(session, led)
            assert result.verified and result.transport == "tcp"          # TCP-only tunnel, no TFTP
            assert session.identity().rm_id == f"0x{LED_RM_ID:08x}"
            assert vb.shell.accepted_pushes                              # it went over 6910
            assert any("6910" in " ".join(a) for a in rig.ssh.launches)
        finally:
            eng.close_all()


def test_negative_twin_a_bare_metal_overlay_is_refused_on_the_linux_static(tmp_path, monkeypatch):
    use_overlay_dirs(monkeypatch, make_overlay(tmp_path / "ov", "led", rm_id=LED_RM_ID).parent)
    with JulyLinuxBoard(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()):
        eng = Engine(EngineConfig(state_dir=state_dir()))
        try:
            session = eng.open(eng.candidate_for(BOARD_IP))
            led = ref_named(eng.deploy.overlays(session), "led")
            with pytest.raises(IncompatibleError):
                eng.deploy.deploy(session, led)
            assert vb.shell.accepted_pushes == []                        # nothing was pushed
        finally:
            eng.close_all()


# --- slot 1: the fielded bare-metal harness, read-only ------------------------------------------


def test_slot1_read_only_set_through_the_hub(tmp_path, monkeypatch, capsys):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        assert main(["--json", "info", BOARD_IP]) == ExitCode.OK
        info = json.loads(capsys.readouterr().out)
        assert info["identity"]["shell_id"] == "0x3f1a560f"
        assert "console_controller" in info["capabilities"]
        assert main(["--json", "mcc", BOARD_IP, "temp"]) == ExitCode.OK
        temps = json.loads(capsys.readouterr().out)
        assert "35.5" in json.dumps(temps)
        assert rig.hub.share_stops == [] and vb.shell.accepted_pushes == []   # read-only
