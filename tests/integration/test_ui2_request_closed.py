"""UI v2 G3 (lane UI2-API-HUB), "Request without opening": a board that is listed but not open can
have its lease read, requested, left and cancelled; a lease it is granted is not heartbeated until
it opens. Each check has a negative twin."""

from __future__ import annotations

import time

from harness_manager import demo_showcase as show
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

# --- G3: request without opening -------------------------------------------------------------------------------------


def test_a_closed_board_can_be_read_and_requested_and_is_not_heartbeated(showcase):
    client, _eng, d = showcase
    b = enc(SPARE)
    view = client.get(f"{API}/boards/{b}/lease", headers=AUTH)
    assert view.status_code == 200 and view.json()["lease"] is None     # free, not open
    r = client.post(f"{API}/boards/{b}/lease/request", json={"want_s": 1800}, headers=AUTH)
    assert r.status_code == 202, r.text
    done = wait_job(client, r.json()["job"])
    assert done["state"] == "done" and done["result"]["lease"]["target"] == show.SPARE_TARGET
    assert SPARE not in d.leases.tracked()                     # closed: lives its TTL
    row = next(x for x in client.get(f"{API}/boards", headers=AUTH).json()["boards"]
               if x["board_id"] == SPARE)
    assert row["open"] is False and row["lease_known"]["here"] is True
    open_board(client, SPARE)
    assert SPARE in d.leases.tracked()                          # open: heartbeated


def test_a_closed_boards_request_can_be_left(showcase):
    client, _eng, _d = showcase
    b = enc(LEASED)                                             # alice holds it: we queue
    r = client.post(f"{API}/boards/{b}/lease/request", json={}, headers=AUTH)
    assert r.status_code == 202
    deadline = time.monotonic() + 5
    while client.get(f"{API}/boards/{b}/lease", headers=AUTH).json().get("request") is None:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    left = client.delete(f"{API}/boards/{b}/lease/queue", headers=AUTH)
    assert left.status_code == 200 and left.json()["left"] is True
    assert wait_job(client, r.json()["job"])["result"] == {"left": True}


def test_twin_an_unlisted_board_is_absent_and_a_listed_one_without_a_hub_unavailable(showcase):
    client, _eng, _d = showcase
    r = client.post(f"{API}/boards/{enc('mps3@10.9.9.9:6900')}/lease/request", json={},
                    headers=AUTH)
    assert r.status_code == 404 and r.json()["error"]["name"] == "ABSENT"
    r = client.post(f"{API}/boards/{enc(LINUX)}/lease/request", json={}, headers=AUTH)
    assert r.status_code == 422 and r.json()["error"]["name"] == "UNAVAILABLE"
    view = client.get(f"{API}/boards/{enc(LINUX)}/lease", headers=AUTH).json()
    assert view["lease"] is None and view["hub"] is None
    # acquire, respond and force still need the board open
    assert client.post(f"{API}/boards/{enc(SPARE)}/lease", json={},
                       headers=AUTH).status_code == 404


def test_the_mock_takes_a_listed_board_that_is_not_open():
    import warnings

    from harness_manager.demo import BOARD_FIELDED, DemoEngine
    from tests.fakes.t14_mock_api import create_app as mock_app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    eng = DemoEngine(speed=0)
    app = mock_app(eng, token="ui2-mock", serve_ui=False)
    auth = {"Authorization": "Bearer ui2-mock"}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            client.post(f"{API}/probe", json={}, headers=auth)          # listed, not open
            app.state.sim.behind_hub(BOARD_FIELDED, lease="other")
            b = enc(BOARD_FIELDED)
            view = client.get(f"{API}/boards/{b}/lease", headers=auth)
            assert view.status_code == 200 and view.json()["lease"]["holder"] == "alice@lab-pc-07"
            r = client.post(f"{API}/boards/{b}/lease/request", json={"want_s": 600},
                            headers=auth)
            assert r.status_code == 202, r.text
            left = client.delete(f"{API}/boards/{b}/lease/queue", headers=auth)
            assert left.status_code == 200
            # twin: a board the mock never listed is 404, as before
            gone = client.get(f"{API}/boards/{enc('mps3@10.9.9.9:6900')}/lease", headers=auth)
            assert gone.status_code == 404
    finally:
        eng.close_all()
