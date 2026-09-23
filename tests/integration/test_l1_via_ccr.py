"""L1: ``--via ssh:HOST`` on the CLI and ``via`` in the API (CCR L1-1). Each check has a twin.

These need the lead-owned half of CCR L1-1 (``ProbeHints.via``, ``--via``, the
daemon's ``via`` field). Until it is applied they skip, saying so; the
boards.toml ``via`` path is tested in test_l1_reach_virtual.py and needs no CCR.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode
from harness_manager.core.pack import ProbeHints
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from tests.fakes.l1_rig import BOARD_IP, HUB, lab
from tests.fakes.t13_daemon import TOKEN, headers
from tests.fakes.virtual_board import VirtualMps3

pytestmark = pytest.mark.skipif("via" not in ProbeHints.__dataclass_fields__,
                                reason="CCR L1-1 (ProbeHints.via, --via, the API's via) not applied")
H = headers()
VIA = f"ssh:{HUB}"


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # No boards.toml table: the route comes only from --via / via.
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(), toml="") as r:
        yield r


def test_cli_via_reaches_the_board_without_boards_toml(capsys, rig):
    assert main(["--json", "info", BOARD_IP, "--via", VIA]) == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert out["identity"]["shell_id"] == "0x3f1a560f"
    assert out["candidate"]["links"][0]["via"] == "ssh"
    assert rig.ssh.launches and rig.ssh.launches[0][-1] == HUB


def test_negative_twin_without_via_the_cli_does_not_tunnel(capsys, rig, monkeypatch):
    # Without a route the pack would dial 192.168.10.101 itself; point it at a closed port.
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    assert main(["info", f"127.0.0.1:{dead}"]) == ExitCode.UNREACHABLE
    assert rig.ssh.launches == []


def test_cli_probe_via(capsys, rig):
    rc = main(["--json", "probe", "--host", BOARD_IP, "--via", VIA, "--no-scan"])
    assert rc == ExitCode.OK
    (cand,) = json.loads(capsys.readouterr().out)["candidates"]
    assert cand["identity"]["shell_id"] == "0x3f1a560f"


def test_api_via_on_probe_and_open(rig):
    eng = Engine(EngineConfig(state_dir=state_dir()))
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            r = c.post("/api/v1/probe", json={"hosts": [BOARD_IP], "via": VIA, "scan_usb": False,
                                               "scan_network": False}, headers=H)
            assert r.status_code == 200 and len(r.json()["candidates"]) == 1
            r = c.post("/api/v1/boards", json={"target": BOARD_IP, "via": VIA}, headers=H)
            assert r.status_code == 200, r.text
            assert r.json()["info"]["identity"]["shell_id"] == "0x3f1a560f"
            bad = c.post("/api/v1/boards", json={"target": BOARD_IP, "via": "mapstone"}, headers=H)
            assert bad.status_code == 400 and bad.json()["error"]["code"] == ExitCode.USAGE
    finally:
        eng.close_all()
