"""UI v2 G7 (lane UI2-API-HUB): the routes that drive a board behind a hub are for the lease holder only
(409 HELD, ``error.data.reason: LEASE``), with today's ``force`` + ``consent`` escape. Each check
has a negative twin."""

from __future__ import annotations

import pytest

from tests.integration import ui2_showcase as rig
from tests.integration.ui2_showcase import (
    API,
    AUTH,
    LEASED,
    LINUX,
    SPARE,
    enc,
    open_board,
    wait_job,
)

showcase = rig.showcase                     # the fixture: the daemon over the showcase

# --- G7: the lease holder drives -----------------------------------------------------------------------------

DRIVES = [
    ("POST", "deploy", {"overlay": "nanosoc"}),
    ("POST", "restore", {}),
    ("POST", "reset", {"target": "dut"}),
    ("POST", "reset", {"target": "shell"}),
    ("POST", "clocks", {"name": "dut", "mhz": 25}),
    ("POST", "controller/reboot", {}),
    ("POST", "controller/command", {"line": "REBOOT"}),
    ("POST", "power/cycle", {}),
    ("POST", "debug/up", {}),
]


@pytest.mark.parametrize("method,route,body", DRIVES)
def test_every_drive_route_is_for_the_lease_holder_only(showcase, method, route, body):
    client, eng, _d = showcase
    open_board(client, LEASED)                                    # alice holds its lease
    before = len(eng.calls)
    r = client.request(method, f"{API}/boards/{enc(LEASED)}/{route}", json=body, headers=AUTH)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["name"] == "HELD" and err["holder"] == "alice@lab-pc-07"
    assert err["data"]["reason"] == "LEASE" and err["data"]["lease"]["here"] is False
    assert "lease holder only" in err["message"]
    assert client.get(f"{API}/jobs", headers=AUTH).json()["jobs"] == []
    assert [c for c in eng.calls[before:] if c[0] not in ("info",)] == []   # nothing sent


def test_twin_force_with_the_typed_phrase_goes_ahead_and_another_phrase_is_refused(showcase):
    client, _eng, _d = showcase
    open_board(client, LEASED)
    b = enc(LEASED)
    wrong = client.post(f"{API}/boards/{b}/restore", json={"force": True, "consent": "yes"},
                        headers=AUTH)
    assert wrong.status_code == 409 and wrong.json()["error"]["name"] == "REFUSED"
    assert f"RESET {LEASED}" in wrong.json()["error"]["hint"]
    ok = client.post(f"{API}/boards/{b}/restore",
                     json={"force": True, "consent": f"RESET {LEASED}"}, headers=AUTH)
    assert ok.status_code == 202
    assert wait_job(client, ok.json()["job"])["state"] == "done"


def test_twin_the_holder_here_and_a_board_without_a_hub_drive_as_before(showcase):
    client, _eng, _d = showcase
    open_board(client, SPARE)                                      # free: take it
    b = enc(SPARE)
    refused = client.post(f"{API}/boards/{b}/restore", json={}, headers=AUTH)
    assert refused.status_code == 409 and "nobody holds" in refused.json()["error"]["message"]
    take = client.post(f"{API}/boards/{b}/lease", json={}, headers=AUTH)
    assert wait_job(client, take.json()["job"])["state"] == "done"
    r = client.post(f"{API}/boards/{b}/restore", json={}, headers=AUTH)
    assert r.status_code == 202, r.text
    wait_job(client, r.json()["job"])
    open_board(client, LINUX)                                      # no hub, no lease
    r = client.post(f"{API}/boards/{enc(LINUX)}/restore", json={}, headers=AUTH)
    assert r.status_code == 202, r.text
    wait_job(client, r.json()["job"])




# --- the T14 mock refuses the same way (the UI lanes' browser tests may run on it) --------------------


def test_the_mock_gates_the_same_routes_and_takes_the_same_escape():
    import warnings

    from harness_manager.demo import BOARD_USB, DemoEngine
    from tests.fakes.t14_mock_api import create_app as mock_app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    eng = DemoEngine(speed=0)
    app = mock_app(eng, token="ui2-mock", serve_ui=False)
    auth = {"Authorization": "Bearer ui2-mock"}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            cands = client.post(f"{API}/probe", json={}, headers=auth).json()["candidates"]
            cand = next(c for c in cands if c["board_id"] == BOARD_USB)
            client.post(f"{API}/boards", json={"candidate": cand}, headers=auth)
            b = enc(BOARD_USB)
            app.state.sim.behind_hub(BOARD_USB, lease="elsewhere")   # your name, another session
            for route, body in (("deploy", {"overlay": "led"}), ("restore", {}),
                                ("reset", {"target": "dut"}), ("controller/reboot", {}),
                                ("debug/up", {})):
                r = client.post(f"{API}/boards/{b}/{route}", json=body, headers=auth)
                assert r.status_code == 409, (route, r.text)
                assert r.json()["error"]["data"]["reason"] == "LEASE"
            forced = client.post(f"{API}/boards/{b}/restore",
                                 json={"force": True, "consent": f"RESET {BOARD_USB}"},
                                 headers=auth)
            assert forced.status_code == 202
            wait_job(client, forced.json()["job"], headers=auth)
            app.state.sim.behind_hub(BOARD_USB, lease="mine")         # twin: held here
            assert client.post(f"{API}/boards/{b}/restore", json={},
                               headers=auth).status_code == 202
    finally:
        eng.close_all()
