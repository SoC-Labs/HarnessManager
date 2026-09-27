"""Lane P1: the front-panel routes and presence on the real harness-manager-daemon.

The app wraps the real Engine and MPS3 pack over ``PanelVirtualMps3``: the Linux harness
with the design's ``hello``/``panel``/``locate`` (``LINUX_PANEL``), and the fielded v0.11
bare-metal shell without them. Every check has a negative twin.
"""

from __future__ import annotations

import threading
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")      # starlette: httpx with the TestClient is deprecated
    from fastapi.testclient import TestClient

from harness_manager.daemon.app import create_app
from tests.fakes.clcd_panel_shell import LINUX_PANEL, V011_BARE_METAL, PanelVirtualMps3
from tests.fakes.l4_service import H, hold_board
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for


class Rig:
    def __init__(self, vb: PanelVirtualMps3, *, beat: bool) -> None:
        self.vb = vb
        self.engine = engine_for(vb)
        self.app = create_app(self.engine, token=TOKEN, static_dir=None)
        self.d = self.app.state.daemon
        self.presence = self.d.presence
        self.presence.ride_wait_s = 0.0          # send at once; riding is unit-tested (P2)
        if not beat:
            self.presence._stop.set()            # the test drives the beat itself
        self.events: list = []
        self.engine.bus.subscribe("panel.*", self.events.append)

    def open(self, client: TestClient) -> str:
        r = client.post("/api/v1/boards", json={"target": self.vb.shell_endpoint, "note": "p1"},
                        headers=H)
        assert r.status_code == 200, r.text
        bid = r.json()["board_id"]
        # QUIET-POLL: presence is background contact, so it beats only while a page views
        # the board (tests/integration/test_quiet_poll.py has the twins without a viewer).
        v = client.put(f"{bid_path(bid)}/viewers/p1-page", json={"ttl_s": 300}, headers=H)
        assert v.status_code == 200, v.text
        return bid


@contextmanager
def rig(tmp_path, profile, *, beat: bool = False) -> Iterator[tuple[Rig, TestClient]]:
    with PanelVirtualMps3(tmp_path, profile) as vb:
        r = Rig(vb, beat=beat)
        with TestClient(r.app) as client:
            yield r, client
        r.engine.close_all()


@pytest.fixture
def linux(tmp_path):
    with rig(tmp_path / "lx", LINUX_PANEL) as got:
        yield got


@pytest.fixture
def bare(tmp_path):
    with rig(tmp_path / "bm", V011_BARE_METAL) as got:
        yield got


def wait_for(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


# --- presence ---------------------------------------------------------------------------------


def test_the_daemon_announces_an_open_board_and_stops_when_it_closes(tmp_path):
    with rig(tmp_path, LINUX_PANEL, beat=True) as (r, client):
        bid = r.open(client)
        hello = wait_for(lambda: r.vb.shell.hellos)[0]
        assert hello["role"] == "owner" and hello["app"].startswith("hm/")
        assert len(hello["sid"]) == 8 and hello["ttl"] == 90
        body = client.get(f"{bid_path(bid)}/panel", headers=H).json()
        assert body["presence"]["active"] is True and body["presence"]["sid"] == hello["sid"]
        assert client.delete(bid_path(bid), headers=H).json()["ok"]
        seen = len(r.vb.shell.hellos)
        time.sleep(2.5)                                  # two and a half ticks of the beat
        assert len(r.vb.shell.hellos) == seen and r.presence.tracked() == []


def test_bare_metal_is_never_sent_a_hello(tmp_path):
    with rig(tmp_path, V011_BARE_METAL, beat=True) as (r, client):
        bid = r.open(client)
        wait_for(lambda: r.presence.presence(bid)["reason"])
        time.sleep(1.5)
        assert r.vb.shell.hellos == [] and not {"hello", "panel", "locate"} & {
            q.get("op") for q in r.vb.shell.requests}
        assert "Linux harness" in r.presence.presence(bid)["reason"]


def test_a_tap_on_the_glass_becomes_one_panel_tap_event(linux):
    r, client = linux
    bid = r.open(client)
    r.presence.beat_due()
    r.vb.shell.tap("request")
    for _ in range(2):                    # the tap is in the ring for both beats
        r.presence.beat_due(time.monotonic() + 3600)
    taps = [e for e in r.events if e.topic == "panel.tap"]
    assert len(taps) == 1 and taps[0].board_id == bid and taps[0].data["on"] == "request"
    assert taps[0].data["notify"] == "", "standalone: there is no lease holder to notify"


def test_the_beat_skips_a_board_a_job_holds_and_resumes_after(linux):
    r, client = linux
    bid = r.open(client)
    job, release = hold_board(r.d, bid)
    try:
        r.presence.beat_due()
        assert r.vb.shell.hellos == [] and r.presence.presence(bid)["skipped"] == 1
    finally:
        release.set()
    wait_for(lambda: r.d.gates.busy(bid) is None)
    r.presence.beat_due(time.monotonic() + 3600)
    assert len(r.vb.shell.hellos) == 1


def test_a_lease_job_does_not_stop_the_beat(linux):
    r, client = linux
    bid = r.open(client)
    job, release = hold_board(r.d, bid, kind="lease_request")   # waits on the hub, not the board
    try:
        r.presence.beat_due()
        assert len(r.vb.shell.hellos) == 1
        assert r.vb.shell.hellos[0]["job"] == {"k": "lease", "p": 0}   # HM's word for it
    finally:
        release.set()


# --- GET /panel ---------------------------------------------------------------------------------


def test_get_panel_on_linux_reads_the_panel_and_offers_identify(linux):
    r, client = linux
    bid = r.open(client)
    r.presence.beat_due()
    r.vb.shell.handle_control({"op": "hello", "v": 1, "sid": "b0b0b0b0", "who": "bob@srv03340",
                               "app": "hm/0.1.0", "role": "watch", "ttl": 90})
    body = client.get(f"{bid_path(bid)}/panel", headers=H).json()
    panel = body["panel"]
    assert panel["source"] == "panel" and panel["page"] == "status" and panel["owner"] == "harness"
    assert panel["card"] == "nanosoc [A]"
    assert [(s["role"], s["mine"]) for s in panel["sessions"]] == [("owner", True),
                                                                   ("watch", False)]
    assert body["identify"] == {"available": True, "reason": "", "until": None}
    assert body["support"]["presence"] == "" and body["presence"]["active"] is True


def test_get_panel_on_bare_metal_is_rebuilt_and_identify_is_greyed_with_the_reason(bare):
    r, client = bare
    bid = r.open(client)
    body = client.get(f"{bid_path(bid)}/panel", headers=H).json()
    assert body["ok"] and body["panel"]["source"] == "rebuilt"
    assert body["panel"]["owner"] == "harness" and body["panel"]["page"] == ""
    assert "rebuilt from what Harness Manager read" in body["panel"]["note"]
    assert body["identify"]["available"] is False
    assert body["identify"]["reason"] == "needs harness feature 'locate' (Linux harness)"


def test_the_panel_routes_are_held_while_a_job_runs(linux):
    r, client = linux
    bid = r.open(client)
    job, release = hold_board(r.d, bid)
    try:
        for method, path in (("GET", "panel"), ("GET", "panel/frame"), ("POST", "identify")):
            resp = client.request(method, f"{bid_path(bid)}/{path}", headers=H,
                                  json={} if method == "POST" else None)
            assert resp.status_code == 409 and resp.json()["error"]["name"] == "HELD", path
    finally:
        release.set()
    wait_for(lambda: r.d.gates.busy(bid) is None)
    assert client.get(f"{bid_path(bid)}/panel", headers=H).status_code == 200


# --- GET /panel/frame -----------------------------------------------------------------------------


def test_the_mirror_is_the_panels_on_linux_and_rebuilt_on_bare_metal(linux, tmp_path):
    r, client = linux
    bid = r.open(client)
    frame = client.get(f"{bid_path(bid)}/panel/frame", headers=H).json()
    assert frame["source"] == "panel" and len(frame["rows"]) == 15 and len(frame["roles"]) == 600
    with rig(tmp_path / "bm2", V011_BARE_METAL) as (rb, cb):
        bid2 = rb.open(cb)
        rebuilt = cb.get(f"{bid_path(bid2)}/panel/frame", headers=H).json()
        assert rebuilt["source"] == "rebuilt" and rebuilt["note"]
        assert rebuilt["rows"][4].startswith("SID : 0x72BB0A36")


# --- POST /identify ------------------------------------------------------------------------------


def test_identify_blinks_a_linux_board(linux):
    r, client = linux
    bid = r.open(client)
    got = client.post(f"{bid_path(bid)}/identify", json={"seconds": 5}, headers=H).json()
    assert got["ok"] and got["seconds"] == 5 and got["until"] > time.time()
    assert r.vb.shell.locates[-1]["s"] == 5
    assert [e.data["state"] for e in r.events if e.topic == "panel.locate"] == ["on"]
    body = client.get(f"{bid_path(bid)}/panel", headers=H).json()
    assert body["identify"]["until"] == pytest.approx(got["until"])


def test_identify_on_bare_metal_is_422_with_the_reason_and_sends_nothing(bare):
    r, client = bare
    bid = r.open(client)
    resp = client.post(f"{bid_path(bid)}/identify", json={"seconds": 5}, headers=H)
    err = resp.json()["error"]
    assert resp.status_code == 422 and err["name"] == "UNAVAILABLE"
    assert err["capability"] == "locate" and "Linux harness" in err["reason"]
    assert r.vb.shell.locates == [] and "locate" not in {q.get("op") for q in r.vb.shell.requests}


def test_identify_seconds_are_checked_before_the_board(linux):
    r, client = linux
    bid = r.open(client)
    for bad in (31, -1, "ten", True):
        resp = client.post(f"{bid_path(bid)}/identify", json={"seconds": bad}, headers=H)
        assert resp.status_code == 400 and resp.json()["error"]["name"] == "USAGE"
    assert r.vb.shell.locates == []
    ok = client.post(f"{bid_path(bid)}/identify", json={}, headers=H).json()
    assert ok["seconds"] == 10, "the default"


def test_a_board_that_is_not_open_is_404(linux):
    _r, client = linux
    resp = client.get(f"{bid_path('mps3@127.0.0.1:1')}/panel", headers=H)
    assert resp.status_code == 404 and resp.json()["error"]["name"] == "ABSENT"


def test_the_presence_thread_stops_with_the_daemon(tmp_path):
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        r = Rig(vb, beat=True)
        with TestClient(r.app):
            thread = r.presence._thread
            assert thread is not None and thread.is_alive()
        wait_for(lambda: not thread.is_alive(), timeout=5)
        assert not any(t.name == "harness-manager-presence" and t.is_alive()
                       for t in threading.enumerate() if t is thread)
        r.engine.close_all()
