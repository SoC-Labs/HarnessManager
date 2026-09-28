"""SIDEBAR-UX bug 5: a board added by the address its boards.toml entry matches, with
``via = "hub"``, is probed through that hub. Each check has its negative twin.

Before, the probe passed ``"hub"`` on as the tunnel's SSH host: the tunnel had no host, the
board "did not answer", and david typed the hub's host into the Add form himself. The probe
now tunnels to the hub's host, the route the board's open falls back to
(``tunnel.probe_route``). The lab is faked (tests/fakes/l1_rig.py): no network, no ssh.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.errors import UsageError
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager_mps3 import tunnel
from tests.fakes.l1_rig import BOARD_IP, HUB, TARGET, lab
from tests.fakes.t13_daemon import TOKEN, headers
from tests.fakes.virtual_board import VirtualMps3

H = headers()
HUB_TOML = f"""\
[boards.lab]
match = ["{BOARD_IP}"]
via = "hub"
hub = {{ host = "{HUB}", target = "{TARGET}" }}
"""
NAMED_TOML = f"""\
[boards.lab]
match = ["{BOARD_IP}"]
via = "hub"
hub = {{ use = "mapstone-dev", target = "{TARGET}" }}
"""
REST_ONLY_TOML = f"""\
[boards.lab]
match = ["{BOARD_IP}"]
via = "hub"
hub = {{ url = "https://hub.example:7246", target = "{TARGET}" }}
"""


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


def probe(body: dict) -> tuple[int, dict]:
    eng = Engine(EngineConfig(state_dir=state_dir()))
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            r = c.post("/api/v1/probe", json=body, headers=H)
            return r.status_code, r.json()
    finally:
        eng.close_all()


ADD = {"hosts": [BOARD_IP], "scan_usb": False, "scan_network": False}


@pytest.mark.parametrize("given", ["", "hub"], ids=["boards.toml-via", "the-form's-via"])
def test_an_address_its_entry_routes_via_hub_is_probed_through_the_hubs_tunnel(tmp_path, monkeypatch,
                                                                               given):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml=HUB_TOML) as rig:
        status, out = probe({**ADD, **({"via": given} if given else {})})
        assert status == 200, out
        (cand,) = out["candidates"]
        assert cand["identity"]["shell_id"] == "0x3f1a560f"          # the board answered
        eth = next(lk for lk in cand["links"] if lk["kind"] == "ethernet")
        assert eth["via"] == "hub"                                   # opens through the hub
        assert rig.ssh.launches and rig.ssh.launches[0][-1] == HUB
        assert not rig.ssh.live()                                    # the probe's tunnel is gone


def test_a_named_hub_is_probed_through_its_host(tmp_path, monkeypatch):
    (state_dir()).mkdir(parents=True, exist_ok=True)
    (state_dir() / "settings.toml").write_text(f'[hubs.mapstone-dev]\nhost = "{HUB}"\n')
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml=NAMED_TOML) as rig:
        status, out = probe(ADD)
        assert status == 200 and len(out["candidates"]) == 1, out
        assert rig.ssh.launches[0][-1] == HUB


def test_negative_twin_a_rest_only_hub_has_no_tunnel_to_probe_with(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml=REST_ONLY_TOML) as rig:
        status, out = probe(ADD)
        assert status == 200 and out["candidates"] == []
        assert rig.ssh.launches == []                                # nothing was dialled


def test_negative_twin_without_a_hub_route_the_probe_never_tunnels(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml=f'[boards.lab]\nmatch = ["{BOARD_IP}"]\n') as rig:
        cand = Engine(EngineConfig(state_dir=state_dir())).candidate_for(BOARD_IP)
        assert tunnel.probe_route(cand) == ("", "")
        assert rig.ssh.launches == []


# --- probe_route alone ------------------------------------------------------------------------------------


def _cand(via: str, detail: str = "shell control channel") -> Candidate:
    return Candidate("mps3", f"mps3@{BOARD_IP}:6900",
                     (Link(LinkKind.ETHERNET, f"{BOARD_IP}:6900", detail, via=via),))


def test_probe_route_keeps_an_ssh_route_and_resolves_the_hub():
    assert tunnel.probe_route(_cand("")) == ("", "")
    ssh = tunnel.with_via(_cand(""), f"ssh:{HUB}")
    assert tunnel.probe_route(ssh) == (f"ssh:{HUB}", "")


def test_negative_twin_via_hub_without_a_hub_table_is_a_usage_error():
    # no boards.toml at all: "hub" names nothing to tunnel to (it used to become host "")
    with pytest.raises(UsageError, match="needs a hub table"):
        tunnel.probe_route(_cand("hub"))
    with pytest.raises(UsageError, match="ssh:HOST"):
        with tunnel.probe_reach(f"{BOARD_IP}:6900", "hub"):
            pass


# --- bugs 3 and 4 with the real pack: listed with no contact, opened through its own hub -----------------


def test_a_listed_boards_toml_board_is_opened_through_its_hub_and_never_contacted_before(
        tmp_path, monkeypatch):
    from harness_manager.daemon import configured

    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml=HUB_TOML) as rig:
        eng = Engine(EngineConfig(state_dir=state_dir()))
        try:
            with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
                (row,) = c.get("/api/v1/boards", headers=H).json()["boards"]
                assert row["source"] == "config" and row["open"] is False
                assert row["configured"]["via"] == "hub"
                assert rig.ssh.launches == [] and rig.runners == []    # listing touched nothing
                r = c.post("/api/v1/boards", json={"candidate": row["candidate"], "note": "t"},
                           headers=H)
                assert r.status_code == 200, r.text
                assert r.json()["info"]["identity"]["shell_id"] == "0x3f1a560f"
                assert rig.ssh.launches and rig.ssh.launches[0][-1] == HUB   # its own route
                (row,) = c.get("/api/v1/boards", headers=H).json()["boards"]
                assert row["source"] == "open"
                assert row["candidate"]["evidence"] == configured.OPENED
        finally:
            eng.close_all()
