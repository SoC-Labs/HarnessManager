"""HARNESS-CAT (H8): the harness versions routes on the real daemon, and the version the
update routes now carry (``update_api``: the install used to drop it).

The board is a VirtualMps3 on the ILA static (release 1.1.0) with the Debug USB, opened
through the API; the channel is the HARNESS-DIST spike's signed multi-version catalogue on
127.0.0.1 (``tests/fakes/hcat_catalog.py``, a throwaway key). Every route has a negative twin.
"""

from __future__ import annotations

import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.cli.output import jsonable
from harness_manager.daemon.app import create_app
from tests.fakes.hcat_catalog import BoardRig, CatalogWorld
from tests.fakes.l4_service import H, events_until, wait_job
from tests.fakes.t13_daemon import TOKEN, bid_path

EVENTS = f"/api/v1/events?token={TOKEN}&topics=job.*,harness.*"


class Hub:
    host = "mapstone-dev.ecs.soton.ac.uk"
    target = "mps3_01_pl"

    def close(self) -> None:
        pass


class Leases:
    def __init__(self, lease: dict | None) -> None:
        self.lease = lease

    def view(self, hub) -> dict:
        return {"lease": self.lease, "hub": hub.host}


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = CatalogWorld(tmp_path / "world")
    world.publish()
    rig = BoardRig(tmp_path, world, monkeypatch)
    with world.serve() as srv:
        try:
            with TestClient(create_app(rig.engine, token=TOKEN, static_dir=None)) as client:
                r = client.post("/api/v1/boards", headers=H,
                                json={"candidate": jsonable(rig.vb.candidate(usb=True)),
                                      "note": "hcat"})
                assert r.status_code == 200, r.text
                yield {"client": client, "rig": rig, "srv": srv, "world": world,
                       "bid": r.json()["board_id"], "src": srv.source()}
        finally:
            rig.close()


def post(w, path: str, body: dict | None = None):
    return w["client"].post(f"/api/v1{path}", json=body or {}, headers=H)


def done(w, r) -> dict:
    assert r.status_code == 202, r.text
    state = wait_job(w["client"], r.json()["job"])
    assert state["state"] == "done", state
    return state["result"]


def refresh(w, **body) -> dict:
    return done(w, post(w, "/harness/catalog/refresh",
                        {"board_id": w["bid"], "source": w["src"], **body}))


def show(w, version: str) -> dict:
    r = w["client"].get(f"/api/v1/harness/releases/{version}", params={"board_id": w["bid"]},
                        headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def sha(w) -> str:
    booted = w["rig"].bound["booted"]
    return booted[-1]["sha"] if booted else "d68dd0ed"


# --- the catalogue -------------------------------------------------------------------------------


def test_the_catalogue_is_refreshed_by_a_job_then_read_from_the_cache(w):
    r = w["client"].get("/api/v1/harness/catalog", params={"board_id": w["bid"]}, headers=H)
    assert r.status_code == 409 and "refresh it first" in r.json()["error"]["hint"]
    with w["client"].websocket_connect(EVENTS) as ws:
        r = post(w, "/harness/catalog/refresh", {"board_id": w["bid"], "source": w["src"],
                                                 "all": True})
        job = r.json()["job"]
        frames = events_until(ws, "job.done", job)
    assert frames[0]["data"] == {"job": job, "kind": "harness_refresh"}
    cat_ev = next(f for f in frames if f["topic"] == "harness.catalog")
    assert cat_ev["board_id"] == w["bid"] and cat_ev["data"]["running"] == "1.1.0"
    result = frames[-1]["data"]["result"]
    assert [x["version"] for x in result["releases"]] == ["2.0.0", "1.1.1", "1.1.0", "1.0.0"]
    r = w["client"].get("/api/v1/harness/catalog", params={"board_id": w["bid"]}, headers=H)
    assert r.status_code == 200 and r.json()["releases"] == result["releases"]
    verdicts = {x["version"]: x["verdict"] for x in r.json()["releases"]}
    assert verdicts == {"2.0.0": "needs-door", "1.1.1": "fits", "1.1.0": "fits",
                        "1.0.0": "re-key"}
    beta_only = w["client"].get("/api/v1/harness/catalog",
                                params={"board_id": w["bid"], "channel": "beta"}, headers=H)
    assert "2.0.0" in [x["version"] for x in beta_only.json()["releases"]]
    # the twins: a channel the cache does not hold, and another board's (none) cache
    r = w["client"].get("/api/v1/harness/catalog",
                        params={"board_id": w["bid"], "channel": "dev"}, headers=H)
    assert r.status_code == 409
    assert w["client"].get("/api/v1/harness/catalog", headers=H).status_code == 409


def test_show_needs_a_refresh_and_gives_the_plan_with_its_fingerprint(w):
    r = w["client"].get("/api/v1/harness/releases/1.1.1", params={"board_id": w["bid"]},
                        headers=H)
    assert r.status_code == 409
    refresh(w)
    out = show(w, "1.1.1")
    assert out["plan"]["version"] == "1.1.1" and len(out["plan"]["fingerprint"]) == 64
    assert out["changes"]["firmware"]["changes"] is True and out["verdict"] == "fits"
    r = w["client"].get("/api/v1/harness/releases/9.9.9", params={"board_id": w["bid"]},
                        headers=H)
    assert r.status_code == 404


# --- install -------------------------------------------------------------------------------------


def test_install_honours_the_requested_version(w):
    refresh(w)
    plan = show(w, "1.1.1")["plan"]
    with w["client"].websocket_connect(EVENTS) as ws:
        r = post(w, f"/boards/{bid_path(w['bid']).rsplit('/', 1)[-1]}/harness/install",
                 {"fingerprint": plan["fingerprint"], "version": "1.1.1"})
        job = r.json()["job"]
        frames = events_until(ws, "job.done", job)
    assert frames[-1]["data"]["result"]["result"] == "installed"
    topics = [f["topic"] for f in frames]
    assert topics.index("harness.installing") < topics.index("harness.installed")
    assert sha(w) == "0e12a0b0"


def test_negative_twin_a_fingerprint_for_another_version_is_refused_before_any_job(w):
    refresh(w)
    plan = show(w, "1.1.1")["plan"]
    before = w["rig"].vb.sd.snapshot()
    r = post(w, f"/boards/{_q(w)}/harness/install",
             {"fingerprint": plan["fingerprint"], "version": "1.0.0"})
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    assert r.json()["error"]["data"]["plan"]["version"] == "1.0.0"
    assert w["rig"].vb.sd.snapshot() == before


def test_a_rekey_install_needs_the_typed_phrase(w):
    refresh(w)
    plan = show(w, "1.0.0")["plan"]
    r = post(w, f"/boards/{_q(w)}/harness/install", {"fingerprint": plan["fingerprint"]})
    assert r.status_code == 409 and "RE-KEYS" in r.json()["error"]["message"]
    assert r.json()["error"]["data"]["plan"]["consent_phrase"] == "REKEY 0x3f1a560f"
    result = done(w, post(w, f"/boards/{_q(w)}/harness/install",
                          {"fingerprint": plan["fingerprint"],
                           "rekey_phrase": "REKEY 0x3f1a560f"}))
    assert result["result"] == "installed" and result["version"] == "1.0.0"


@pytest.mark.parametrize("lease, holder", [(None, "nobody"),
                                           ({"holder": "alice@lab-pc-07", "mine": False},
                                            "alice@lab-pc-07")])
def test_install_without_the_lease_is_409_held_before_any_job(w, lease, holder):
    refresh(w)
    plan = show(w, "1.1.1")["plan"]
    w["rig"].engine.session(w["bid"]).hub = Hub()
    w["rig"].svc.leases = Leases(lease)
    before = w["rig"].vb.sd.snapshot()
    r = post(w, f"/boards/{_q(w)}/harness/install", {"fingerprint": plan["fingerprint"]})
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["name"] == "HELD" and err["holder"] == holder
    assert not [j for j in w["client"].get("/api/v1/jobs", headers=H).json()["jobs"]
                if j["kind"] == "harness_install"]
    assert w["rig"].vb.sd.snapshot() == before and w["rig"].vb.reboots == 0


def test_negative_twin_the_lease_holder_installs_through_the_api(w):
    refresh(w)
    plan = show(w, "1.1.1")["plan"]
    w["rig"].engine.session(w["bid"]).hub = Hub()
    w["rig"].svc.leases = Leases({"holder": "me@here", "mine": True})
    result = done(w, post(w, f"/boards/{_q(w)}/harness/install",
                          {"fingerprint": plan["fingerprint"]}))
    assert result["result"] == "installed" and sha(w) == "0e12a0b0"


# --- the update routes carry the version (update_api.py:222) ---------------------------------------


def test_update_check_and_harness_install_the_requested_version(w):
    result = done(w, post(w, "/update/check", {"board_id": w["bid"], "source": w["src"],
                                               "version": "1.0.0"}))
    plan = result["plan"]
    assert (plan["version"], plan["rekey"]) == ("1.0.0", True)
    # the version is recalled from the check that issued the fingerprint
    out = done(w, post(w, f"/boards/{_q(w)}/update/harness",
                       {"fingerprint": plan["fingerprint"], "rekey_phrase": plan["consent_phrase"]}))
    assert out["version"] == "1.0.0" and out["result"] == "installed"
    assert w["rig"].bound["booted"][-1]["static_id"].lower() == "0x3f1a560f"


def test_negative_twin_update_harness_with_another_version_than_checked_is_refused(w):
    result = done(w, post(w, "/update/check", {"board_id": w["bid"], "source": w["src"]}))
    plan = result["plan"]
    assert plan["version"] == "1.1.1"
    before = w["rig"].vb.sd.snapshot()
    r = post(w, f"/boards/{_q(w)}/update/harness", {"fingerprint": plan["fingerprint"],
                                                    "version": "1.0.0"})
    assert r.status_code == 409 and r.json()["error"]["data"]["plan"]["version"] == "1.0.0"
    assert w["rig"].vb.sd.snapshot() == before
    # and the same fingerprint with the version it was made for installs it
    out = done(w, post(w, f"/boards/{_q(w)}/update/harness", {"fingerprint": plan["fingerprint"],
                                                              "version": "1.1.1"}))
    assert out["version"] == "1.1.1"


# --- pin, history, rollback ---------------------------------------------------------------------------


def _q(w) -> str:
    return bid_path(w["bid"]).rsplit("/", 1)[-1]


def test_pin_history_and_rollback_over_the_api(w):
    c, q = w["client"], _q(w)
    with c.websocket_connect(EVENTS) as ws:
        r = c.put(f"/api/v1/boards/{q}/harness/pin", json={"version": "1.1.0"}, headers=H)
        assert r.status_code == 200 and r.json()["pinned"] == "1.1.0"
        ev = events_until(ws, "harness.pinned")[-1]
    assert ev["data"]["version"] == "1.1.0" and ev["board_id"] == w["bid"]
    assert refresh(w)["offer"] == "1.1.0"
    r = c.delete(f"/api/v1/boards/{q}/harness/pin", headers=H)
    assert r.json() == {"ok": True, "board_id": w["bid"], "pinned": "", "previous": "1.1.0"}
    refresh(w)
    plan = show(w, "1.1.1")["plan"]
    done(w, post(w, f"/boards/{q}/harness/install", {"fingerprint": plan["fingerprint"]}))
    refresh(w)
    hist = c.get(f"/api/v1/boards/{q}/harness/history", headers=H).json()
    assert [h["version"] for h in hist["history"]] == ["1.1.1"]
    assert hist["rollback"][0]["version"] == "1.1.0"
    # rollback: first the plan to confirm, then with its fingerprint
    r = post(w, f"/boards/{q}/harness/rollback", {"source": w["src"]})
    assert r.status_code == 409 and "confirm this plan" in r.json()["error"]["message"]
    rb = r.json()["error"]["data"]["plan"]
    assert rb["version"] == "1.1.0"
    out = done(w, post(w, f"/boards/{q}/harness/rollback",
                       {"source": w["src"], "fingerprint": rb["fingerprint"]}))
    assert out["version"] == "1.1.0" and sha(w) == "d68dd0ed"


def test_negative_twins_pin_history_and_rollback_refusals(w):
    c, q = w["client"], _q(w)
    assert c.put(f"/api/v1/boards/{q}/harness/pin", json={}, headers=H).status_code == 400
    assert c.get("/api/v1/boards/nope/harness/history", headers=H).status_code == 404
    assert c.get(f"/api/v1/boards/{q}/harness/history", headers=H).json()["rollback"] is None
    r = post(w, f"/boards/{q}/harness/rollback", {"backup_path": "/nonexistent/backup.zip"})
    assert r.status_code == 404
    r = post(w, f"/boards/{q}/harness/rollback", {"backup_path": "rel.zip"})
    assert r.status_code == 400


# --- fetch --------------------------------------------------------------------------------------------


def test_fetch_is_an_engine_wide_job_that_fills_the_cache(w):
    result = done(w, post(w, "/harness/releases/1.1.1/fetch", {"source": w["src"]}))
    assert {c["name"]: c["result"] for c in result["components"]} == {
        "sd-HBI0309C": "fetched", "overlays-open": "fetched", "overlays-aaa": "skipped"}
    r = post(w, "/harness/releases/9.9.9/fetch", {"source": w["src"]})
    state = wait_job(w["client"], r.json()["job"])
    assert state["state"] == "failed" and state["error"]["name"] == "ABSENT"
