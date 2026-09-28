"""QUIET-POLL: Harness Manager is a good citizen on a shared lab board.

The real harness-manager-daemon over the real Engine and MPS3 pack, talking to a Linux
harness with the front-panel verbs (``PanelFakeShell``) through a SINGLE-CLIENT control port
(``tests/fakes/qp_single_client.py``) that records every connect. The rules
(``services/quiet.py``): no background contact without a viewer; none while the hub lease is
someone else's (an explicit read still works and names the holder); a refused or reset
connect backs off, and the interval grows; ``poll = off`` means none at all; and Harness
Manager never holds the single-client port longer than one request. Each has its twin.
"""

from __future__ import annotations

import threading
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")      # starlette: httpx with the TestClient is deprecated
    from fastapi.testclient import TestClient

from harness_manager.daemon.app import create_app
from harness_manager.settings import runtime
from harness_manager_mps3 import ctlgate
from harness_manager_mps3.shell import ShellProbes
from tests.fakes.clcd_panel_shell import LINUX_PANEL, PanelVirtualMps3
from tests.fakes.l4_service import H
from tests.fakes.lrb_fake_hub import LrbHub, LrbHubRef
from tests.fakes.qp_single_client import SingleClientFront, soak_call
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, state_dir

BG = {**H, "X-HM-Background": "1"}
FAR = 10_000.0                           # a beat "much later": every hello is due


class Rig:
    def __init__(self, vb: PanelVirtualMps3, front: SingleClientFront) -> None:
        self.vb, self.front = vb, front
        self.engine = engine_for(vb)
        self.app = create_app(self.engine, token=TOKEN, static_dir=None)
        self.d = self.app.state.daemon
        self.presence = self.d.presence
        self.presence.ride_wait_s = 0.0          # a due hello is sent at once
        self.presence._stop.set()                # the test drives the beat (beat_due)
        self.d.quiet.backoff = (0.3, 2.4)        # the 30 s -> 10 min back-off, scaled down
        self.client: TestClient | None = None
        self.bid = ""
        self._beats = 0

    def open(self) -> str:
        r = self.client.post("/api/v1/boards", json={"target": self.front.endpoint,
                                                     "note": "quiet"}, headers=H)
        assert r.status_code == 200, r.text
        self.bid = r.json()["board_id"]
        # A turned-away connect asks UDP identify (0.5 s) before it says "held": a loopback
        # fake has no responder, so skip the wait and keep the back-off's timing exact.
        self.engine.session(self.bid).shell.probes = ShellProbes(identify=lambda _h, _t: None)
        return self.bid

    def view(self, vid: str = "page-1", ttl_s: float = 300) -> dict[str, Any]:
        r = self.client.put(f"{bid_path(self.bid)}/viewers/{vid}", json={"ttl_s": ttl_s},
                            headers=H)
        assert r.status_code == 200, r.text
        return r.json()["background"]

    def unview(self, vid: str = "page-1") -> None:
        assert self.client.delete(f"{bid_path(self.bid)}/viewers/{vid}", headers=H).json()["ok"]

    def beat(self) -> None:
        """One presence beat for every open board, as if a whole beat interval had passed."""
        self._beats += 1
        self.presence.beat_due(time.monotonic() + FAR * self._beats)

    def background(self, what: str = "", headers: dict[str, str] | None = None) -> dict:
        """A read nobody clicked (the UI's timers): GET with ``X-HM-Background: 1``."""
        r = self.client.get(f"{bid_path(self.bid)}{what}", headers=headers or BG)
        assert r.status_code == 200, r.text
        return r.json()

    def explicit(self, what: str = "") -> Any:
        return self.client.get(f"{bid_path(self.bid)}{what}", headers=H)

    def round(self) -> list[dict]:
        """What one polling interval does in the background: a beat and the UI's reads."""
        self.beat()
        return [self.background(), self.background("/panel"), self.background("/telemetry")]


@contextmanager
def rig(tmp_path, *, turn_away: str = "rst") -> Iterator[Rig]:
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        front = SingleClientFront(vb.shell, turn_away=turn_away)
        r = Rig(vb, front)
        try:
            with TestClient(r.app) as client:
                r.client = client
                yield r
        finally:
            r.engine.close_all()
            front.close()
        front.assert_clean()             # the counting fake: no connect it forbade


@pytest.fixture
def q(tmp_path) -> Iterator[Rig]:
    with rig(tmp_path) as r:
        r.open()
        yield r


def quiet_kinds(answers: list[dict]) -> set[str]:
    return {(a.get("quiet") or {}).get("kind", "") for a in answers}


# --- (a) no background contact unless a UI views the board -------------------------------------


def test_no_background_connect_without_a_viewer(q):
    t0 = time.monotonic()
    answers = [a for _ in range(4) for a in q.round()]
    assert quiet_kinds(answers) == {"no_viewer"}
    assert all("identity" not in a for a in answers), "a quiet answer carries no board data"
    assert q.front.attempts(t0) == 0 and q.vb.shell.hellos == []
    assert q.presence.presence(q.bid)["quieted"] == 4


def test_twin_with_a_viewer_the_same_rounds_reach_the_board(q):
    q.view()
    t0 = time.monotonic()
    answers = [a for _ in range(2) for a in q.round()]
    assert quiet_kinds(answers) == {""} and answers[0]["identity"]["shell_id"]
    assert q.front.attempts(t0) > 0 and len(q.vb.shell.hellos) == 2


def test_closing_the_view_stops_it_and_a_lapsed_viewer_counts_as_gone(q):
    q.view()
    q.round()
    q.unview()                                            # the tab looked elsewhere / closed
    t0 = time.monotonic()
    assert quiet_kinds(q.round()) == {"no_viewer"} and q.front.attempts(t0) == 0
    q.view("page-2", ttl_s=1)                             # a page that stops refreshing
    assert quiet_kinds(q.round()) == {""}
    time.sleep(1.2)
    t1 = time.monotonic()
    assert quiet_kinds(q.round()) == {"no_viewer"} and q.front.attempts(t1) == 0


def test_a_viewing_pages_own_read_registers_it(q):
    """No race between the page's first read and its viewer PUT: the read says it looks."""
    got = q.background(headers={**BG, "X-HM-Viewer": "page-9"})
    assert "quiet" not in got and got["identity"]["shell_id"]
    assert q.d.quiet.viewers(q.bid) == 1


def background_state(r: Rig) -> dict:
    return r.explicit("/background").json()["background"]


def test_explicit_reads_are_never_gated(q):
    t0 = time.monotonic()
    body = q.explicit().json()
    assert body["ok"] and body["identity"]["shell_id"] and q.front.attempts(t0) > 0
    st = background_state(q)
    assert st["kind"] == "no_viewer" and st["allowed"] is False
    assert set(body) - {"claim"} == {"ok", "candidate", "identity", "health", "capabilities",
                                     "unavailable"}, "info keeps BoardInfo's shape"


# --- (b) the lease is someone else's: background contact stops entirely -------------------------


class _HubRef(LrbHubRef):
    """The session's ``hub`` adapter (the session closes it with the board)."""

    def close(self) -> None:
        pass


def attach_hub(r: Rig, holder: str) -> tuple[LrbHub, LrbHubRef]:
    """The board behind a hub (fpgahub 0.3.0 in miniature) whose lease ``holder`` holds; this
    Harness Manager is ``me@srv03335``. The real lease service (``d.leases``) decides mine."""
    hub = LrbHub()
    ref = _HubRef(hub.client("me@srv03335", "me"))
    r.engine.session(r.bid).hub = ref
    hub.grant_to(holder, holder.split("@")[0])
    return hub, ref


def test_lease_held_by_someone_else_means_zero_background_connects(q):
    attach_hub(q, "alice@lab-pc")
    state = q.view()                                   # even with a page viewing it
    assert state["kind"] == "lease" and state["holder"] == "alice@lab-pc"
    q.front.strict("the hub lease is alice@lab-pc's")  # the counting fake: any connect fails
    answers = [a for _ in range(6) for a in q.round()]
    q.front.lenient()
    q.front.assert_clean()
    assert quiet_kinds(answers) == {"lease"} and q.vb.shell.hellos == []
    assert {a["quiet"]["holder"] for a in answers} == {"alice@lab-pc"}
    # An explicit read still works, and names the holder (a health note; `info` prints it).
    t0 = time.monotonic()
    body = q.explicit().json()
    assert body["ok"] and body["identity"]["shell_id"] and q.front.attempts(t0) > 0
    assert any("held by alice@lab-pc" in n for n in body["health"]["notes"])
    st = background_state(q)
    assert st["holder"] == "alice@lab-pc" and "alice@lab-pc" in st["text"]


def test_twin_our_own_lease_lets_the_background_reads_through(q):
    """REVIEW-W5 1: "our own" is THIS Harness Manager holding the lease token (``here``),
    acquired through its lease service."""
    hub, ref = attach_hub(q, "me@srv03335")
    hub.current = None                                 # free: this HM acquires it itself
    q.d.leases.acquire(ref, board_id=q.bid, ttl_s=600, heartbeat=False)
    view = q.explicit("/lease").json()
    assert view["lease"]["here"] is True and view["lease"]["mine"] is True
    assert q.view()["allowed"] is True
    t0 = time.monotonic()
    assert quiet_kinds(q.round()) == {""} and q.front.attempts(t0) > 0
    assert len(q.vb.shell.hellos) == 1 and q.vb.shell.hellos[0]["role"] == "holder"
    assert not any("held by" in n for n in q.explicit().json()["health"]["notes"])


def test_the_same_principal_in_another_session_is_elsewhere_for_background_reads(q):
    """REVIEW-W5 1: every lab session shares one principal (david@mapstone-dev). A soak
    another session runs holds the lease as "mine" by principal, but not HERE: background
    contact stops. (Before the fix this was the twin above and the reads went through.)"""
    attach_hub(q, "me@srv03335")                       # same principal, no token here
    view = q.explicit("/lease").json()
    assert view["lease"]["mine"] is True and view["lease"]["here"] is False
    state = q.view()
    assert state["kind"] == "lease" and state["holder"] == "me@srv03335"
    q.front.strict("another session of this principal holds the lease")
    answers = [a for _ in range(3) for a in q.round()]
    q.front.lenient()
    q.front.assert_clean()
    assert quiet_kinds(answers) == {"lease"} and q.vb.shell.hellos == []
    t0 = time.monotonic()                              # explicit: unchanged, still reads
    body = q.explicit().json()
    assert body["ok"] and body["identity"]["shell_id"] and q.front.attempts(t0) > 0


def test_a_free_lease_still_lets_a_viewed_board_be_read(q):
    """REVIEW-W5 1 twin: nobody holds the lease (no holder): background reads go ahead."""
    hub, _ref = attach_hub(q, "alice@lab-pc")
    hub.current = None
    assert q.explicit("/lease").json()["lease"] is None
    assert q.view()["allowed"] is True
    t0 = time.monotonic()
    assert quiet_kinds(q.round()) == {""} and q.front.attempts(t0) > 0


def test_a_lease_that_cannot_be_read_keeps_background_contact_quiet(q):
    """REVIEW-W5 2: a hub board whose lease cannot be read is quiet ("lease unknown"),
    asked again on the next background read; explicit reads carry on. Before the fix the
    gate failed OPEN (an unreadable lease read as free)."""
    from harness_manager.core.errors import UnreachableError

    hub, _ref = attach_hub(q, "alice@lab-pc")
    hub.fail_always["lease_status"] = UnreachableError("the hub did not answer")
    q.view()
    q.d.leases.forget(_ref)                            # no cached view: the hub is asked
    q.front.strict("the lease cannot be read")
    answers = [a for _ in range(3) for a in q.round()]
    q.front.lenient()
    q.front.assert_clean()
    assert quiet_kinds(answers) == {"lease_unknown"} and q.vb.shell.hellos == []
    assert all("could not be read" in a["quiet"]["text"] for a in answers)
    assert "did not answer" in answers[0]["quiet"]["detail"]
    t0 = time.monotonic()                              # explicit: unchanged
    body = q.explicit().json()
    assert body["ok"] and body["identity"]["shell_id"] and q.front.attempts(t0) > 0
    # Twin: the hub answers again (the lease is free): the next background read goes ahead.
    del hub.fail_always["lease_status"]
    hub.current = None
    t1 = time.monotonic()
    assert quiet_kinds([q.background()]) == {""} and q.front.attempts(t1) > 0


# --- (c) a refused or reset connect: back off, and the interval grows ---------------------------


@pytest.mark.parametrize("turn_away", ["rst", "eof"])
def test_a_refused_connect_backs_off_and_the_interval_grows(tmp_path, turn_away, monkeypatch):
    # SERIAL-6900: a refusal within REAP_WINDOW_S of our own close is retried as our own
    # ghost (the board reaps a closed client a pass later). Here the soak takes the port well
    # after our last connection, so every refusal is someone else's (scaled down, as above).
    monkeypatch.setattr(ctlgate, "REAP_WINDOW_S", 0.1)
    with rig(tmp_path, turn_away=turn_away) as r:
        r.open()
        r.view()
        time.sleep(0.15)
        r.front.hold()                                 # the soak holds the control port
        t0 = time.monotonic()
        answers = []
        while time.monotonic() - t0 < 3.4:             # the UI polling far faster than it does
            answers.append(r.background())
            r.beat()
            time.sleep(0.05)
        tries = r.front.refused("hm", t0)
        firsts = [tries[0]] + [b for a, b in zip(tries, tries[1:], strict=False) if b - a > 0.2]
        gaps = [b - a for a, b in zip(firsts, firsts[1:], strict=False)]
        assert len(firsts) >= 3, tries
        assert all(g2 > g1 * 1.4 for g1, g2 in zip(gaps, gaps[1:], strict=False)), gaps
        assert len(tries) <= 2 * len(firsts), tries       # a read and a beat, at most
        assert len(answers) >= 10 * len(firsts), "the rest were answered by the gate, untouched"
        assert quiet_kinds(answers) == {"busy"}, "never a red error for a background read"
        assert all("another client" in a["quiet"]["text"] for a in answers)
        # Twin: the port is free again; an explicit read is answered and ends the back-off.
        r.front.release()
        assert r.explicit().json()["ok"]
        assert quiet_kinds([r.background()]) == {""}


def test_our_own_job_is_not_another_client_and_starts_no_back_off(q):
    """REVIEW-W5 5: a background read that meets OUR OWN job (``jobs.busy_error``) is not
    "busy (another client)": today's 409 HELD (the page defers on it), and no back-off, so
    the read after the job goes ahead at once. Before the fix: busy, and a 30 s back-off."""
    q.view()
    release = threading.Event()
    q.d.jobs.submit("slot_push", q.bid, lambda progress: release.wait(20))
    try:
        deadline = time.monotonic() + 5
        while q.d.gates.busy(q.bid) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        r = q.client.get(f"{bid_path(q.bid)}/telemetry", headers=BG)
        assert r.status_code == 409, r.text
        err = r.json()["error"]
        assert err["name"] == "HELD" and " job " in f"{err.get('holder')} {err['message']}"
        st = background_state(q)
        assert st["kind"] == "" and st["refusals"] == 0 and q.d.quiet.backing_off(q.bid) is None
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while q.d.gates.busy(q.bid) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert quiet_kinds([q.background("/telemetry")]) == {""}


def test_twin_another_clients_held_port_still_backs_off(q):
    """REVIEW-W5 5 twin: a HELD that is not our job (another client on the control port)
    still backs off and says busy."""
    q.view()
    q.front.hold()
    try:
        assert quiet_kinds([q.background()]) == {"busy"}
        assert q.d.quiet.backing_off(q.bid) is not None
    finally:
        q.front.release()


def test_twin_an_explicit_read_while_busy_keeps_todays_error(q):
    q.view()
    q.front.hold()
    r = q.explicit()
    assert r.status_code == 409 and r.json()["error"]["name"] == "HELD"
    q.front.release()


def test_the_presence_beat_backs_off_too_and_never_retries_within_a_beat(q):
    q.view()
    q.front.hold()
    t0 = time.monotonic()
    q.beat()
    assert len(q.front.refused("hm", t0)) >= 1
    first = len(q.front.refused("hm", t0))
    for _ in range(5):
        q.presence.beat_due(time.monotonic() + 1.0)      # beat ticks within the back-off
    assert len(q.front.refused("hm", t0)) == first
    assert "another client" in q.d.quiet.state(q.bid)["text"]
    q.front.release()


# --- (d) poll = off: only explicit actions touch the board --------------------------------------


def write_settings(text: str) -> None:
    (state_dir() / "settings.toml").write_text(text, encoding="utf-8")
    runtime.refresh()


def write_boards(bid: str, poll: str) -> None:
    (state_dir() / "boards.toml").write_text(f'[boards."{bid}"]\npoll = "{poll}"\n',
                                             encoding="utf-8")


def test_policy_off_means_zero_background_connects(q):
    write_settings('[general]\nbackground_poll = "off"\n')
    try:
        assert q.view()["kind"] == "off"
        q.front.strict("general.background_poll is off")
        answers = [a for _ in range(4) for a in q.round()]
        q.front.lenient()
        assert quiet_kinds(answers) == {"off"} and q.vb.shell.hellos == []
        body = q.explicit().json()                   # explicit: still works
        assert body["ok"] and body["identity"]["shell_id"]
        assert background_state(q)["policy"] == "off"
    finally:
        write_settings("")


def test_a_boards_poll_off_overrides_the_default_and_twin_on_view_overrides_off(q):
    write_boards(q.bid, "off")
    q.view()
    q.front.strict("boards.<name>.poll is off")
    assert quiet_kinds(q.round()) == {"off"}
    q.front.lenient()
    write_settings('[general]\nbackground_poll = "off"\n')
    write_boards(q.bid, "on-view")
    try:
        t0 = time.monotonic()
        assert quiet_kinds(q.round()) == {""} and q.front.attempts(t0) > 0
    finally:
        write_settings("")


# --- single-client etiquette ----------------------------------------------------------------------


def test_hm_never_holds_the_single_client_port_longer_than_one_request(q):
    """A soak calls the control port in a loop while Harness Manager polls. The fake turns a
    second client away (RST). Every HM connection carries one call (at most ping + version)
    and closes as soon as it is answered, so the soak is only ever turned away while HM is
    mid-request, never by a connection HM parked."""
    q.view()
    q.d.quiet.backoff = (0.05, 0.1)
    stop = threading.Event()
    soak = {"ok": 0, "refused": 0}

    def soak_loop() -> None:
        while not stop.is_set():
            soak["ok" if soak_call(q.front.port) else "refused"] += 1
            time.sleep(0.02)

    t0 = time.monotonic()
    worker = threading.Thread(target=soak_loop, daemon=True)
    worker.start()
    try:
        while time.monotonic() - t0 < 2.5:
            q.round()
            time.sleep(0.02)
    finally:
        stop.set()
        worker.join(timeout=5)
    conns = q.front.hm_conns(t0)
    assert conns and soak["ok"] > 20
    for c in conns:
        assert len(c.requests) <= 2, [op for _i, _o, op in c.requests]
    # Parked time as this fake measures it includes its own threads' wake-up under load:
    # the typical connection is closed within milliseconds, none lingers.
    parked = sorted(c.parked_s for c in conns)
    assert parked[len(parked) // 2] < 0.02 and parked[-1] < 0.5, parked[-5:]
    slack = 0.05
    for t in q.front.refused("soak", t0):
        assert any(c.opened - slack <= t <= (c.closed or t) + slack for c in conns), \
            f"the soak was turned away at {t - t0:.3f} s with no HM request in flight"


def test_twin_a_client_that_parks_the_port_is_caught(tmp_path):
    """The check above can fail: a connection held open between requests is measured."""
    import socket

    with rig(tmp_path) as r:
        with socket.create_connection(("127.0.0.1", r.front.port)) as s:
            s.sendall(b'{"op":"ping"}\n')
            s.recv(4096)
            time.sleep(0.3)                          # parked, idle
        time.sleep(0.1)
        parked = [c for c in r.front.hm_conns() if c.parked_s >= 0.25]
        assert parked, "the fake must see a parked connection"


# --- (e) the demo is unaffected -------------------------------------------------------------------


def test_the_demo_gate_says_yes_to_everything():
    from harness_manager.demo import DemoEngine

    engine = DemoEngine()
    try:
        app = create_app(engine, token=TOKEN, static_dir=None)
        d = app.state.daemon
        assert d.quiet.enabled is False
        with TestClient(app) as client:
            cand = client.post("/api/v1/probe", json={}, headers=H).json()["candidates"][0]
            opened = client.post("/api/v1/boards", json={"candidate": cand}, headers=H)
            assert opened.status_code == 200, opened.text
            body = client.get(bid_path(cand["board_id"]), headers=BG).json()
            assert body["ok"] and "quiet" not in body
            st = client.get(f"{bid_path(cand['board_id'])}/background", headers=H).json()
            assert st["background"]["allowed"] and st["background"]["policy"] == "demo"
    finally:
        engine.close_all()


# --- follow-up 4: the MCC's second reader inside background telemetry -------------------------------


class FakeHubRunner:
    """The hub runner for ``HubMccController``: the MCC reader's verdict, as the hub prints it.
    ``rc`` 3 is another reader of tty_00 (another client); 4 is no Cmd> (not contention)."""

    def __init__(self) -> None:
        self.rc = 0
        self.calls = 0

    def __call__(self, argv, timeout=None):
        import json as _json
        from types import SimpleNamespace

        self.calls += 1
        lines = _json.loads(argv[-1])["lines"]
        info = {"rc": self.rc}
        if self.rc == 0:
            info["replies"] = [
                f"{line}\r\n" + ("MB Device 0 Temp: 41.0 degC" if "TEMP" in line else
                                  f"MB OSC{line.split()[-1]} clock read = 50.0 MHz")
                for line in lines]
        elif self.rc == 3:
            info["others"] = [[4242, "screen /dev/mps3_01_pl/tty_00"]]
        else:
            info["reason"] = "no intact Cmd>"
        return SimpleNamespace(stdout=_json.dumps(info) + "\n", stderr="", returncode=0)


def attach_mcc(r: Rig) -> FakeHubRunner:
    """A hub-mode MCC on the open board, wired to the session's observer (CCR QUIET-1)."""
    from harness_manager.core.events import Event
    from harness_manager_mps3.hub_mcc import HubMccController

    runner = FakeHubRunner()
    session = r.engine.session(r.bid)
    session.controller = HubMccController(runner, target="mps3_01_pl",
                                          tty="/dev/mps3_01_pl/tty_00", host="hub")
    r.d._quiet_opened(Event("session.opened", r.bid, {}))    # as when the board opened
    return runner


def test_an_mcc_second_reader_in_background_telemetry_is_busy_and_backs_off(q):
    runner = attach_mcc(q)
    q.view()
    runner.rc = 3                                     # someone else reads tty_00
    got = q.background("/telemetry")
    assert got["quiet"]["kind"] == "busy" and "another client" in got["quiet"]["text"]
    assert "names the MCC console" in got["quiet"]["detail"]
    t0, calls = time.monotonic(), runner.calls
    answers = [q.background("/telemetry"), q.background()]
    assert quiet_kinds(answers) == {"busy"} and runner.calls == calls
    assert q.front.attempts(t0) == 0, "the back-off holds every background read of the board"
    # An explicit read still answers, today's way: the MCC rows say why.
    body = q.explicit("/telemetry").json()
    assert body["ok"] and any("names the MCC console" in (x.get("reason") or "")
                              for x in body["readings"])


def test_twin_an_mcc_that_is_not_at_its_prompt_is_not_another_client(q):
    runner = attach_mcc(q)
    q.view()
    runner.rc = 4                                     # no Cmd>: nothing about other clients
    got = q.background("/telemetry")
    assert "quiet" not in got and got["ok"]
    assert q.d.quiet.check(q.bid) is None
    runner.rc = 0                                     # and an answering MCC reads as before
    got = q.background("/telemetry")
    assert "quiet" not in got and any(x.get("name") == "mcc_temp" and x.get("value") == 41.0
                                      for x in got["readings"])


def test_twin_an_mcc_answer_does_not_end_a_control_port_back_off(q):
    runner = attach_mcc(q)
    session = q.engine.session(q.bid)
    q.d.quiet.note_busy(q.bid, "reset by peer", channel="control")
    assert session.controller.temperatures()[0].value == 41.0   # the MCC answers...
    assert runner.calls == 1 and q.d.quiet.backing_off(q.bid) is not None
    session.shell.call(lambda c: c.ping())            # ...the control port answering ends it
    assert q.d.quiet.backing_off(q.bid) is None


# --- follow-up 1 through the API: an explicit console under someone else's lease --------------------


def test_a_console_opened_while_the_lease_is_elsewhere_is_refused_naming_the_holder(q):
    from harness_manager.services import pty as _pty

    if not _pty.supported():
        pytest.skip("this system has no PTYs")
    attach_hub(q, "alice@lab-pc")
    r = q.client.post(f"{bid_path(q.bid)}/consoles/uart0/pty", headers=H)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["name"] == "HELD" and err["holder"] == "alice@lab-pc"


def test_twin_our_own_lease_opens_the_console(q):
    from harness_manager.services import pty as _pty

    if not _pty.supported():
        pytest.skip("this system has no PTYs")
    attach_hub(q, "me@srv03335")
    r = q.client.post(f"{bid_path(q.bid)}/consoles/uart0/pty", headers=H)
    assert r.status_code == 200, r.text
