"""SERIAL-6900: Harness Manager never races itself for a board's single-client control port.

On 2026-09-28 the lab MPS3 turned Harness Manager away from itself: the service's threads (a
page's refresh burst, routes outside the daemon's op gate, an explicit CLI verb) opened 6900
at the same moment, refused each other, and QUIET-POLL read every refusal as "another
client". The fix is the pack's per-board control gate (``harness_manager_mps3.ctlgate``): one
connection per board at a time in this process, FIFO, bounded, re-entrant; a wait on it is
never contention; through an SSH forward, back-to-back calls are paced (the forward's close
lags). The daemon also shares one answer between identical board reads in flight.

The board is a FakeShell behind ``SingleClientFront`` (tests/fakes/qp_single_client.py): it
turns away a second concurrent connection exactly as harnessd does (accept, then close), and
records every refusal. Every check has its negative twin, mostly the gate turned off
(``ctlgate.set_enabled(False)``: today's behaviour before the fix).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from harness_manager.core.errors import ExitCode, HarnessError, HeldError
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services import quiet
from harness_manager.services.slots import SlotService
from harness_manager_mps3 import ctlgate
from harness_manager_mps3 import deploy as deploymod
from harness_manager_mps3 import pack as packmod
from harness_manager_mps3.pack import Mps3Pack
from harness_manager_mps3.shell import ShellProbes
from tests.fakes.clcd_panel_shell import LINUX_PANEL, PanelVirtualMps3
from tests.fakes.lxslots_board import LINUX_SID, slot_board
from tests.fakes.qp_single_client import SingleClientFront, soak_call
from tests.fakes.t2_overlays import SYNTH2_RM_ID, SYNTH_RM_ID, make_overlay, use_overlay_dirs
from tests.fakes.t13_daemon import LiveDaemon, bid_path, engine_for, run_cli

NO_IDENTIFY = ShellProbes(identify=lambda _h, _t: None)
BG = {"X-HM-Background": "1", "X-HM-Viewer": "page-1"}
#: The page's refresh burst: every board read it fires when a board opens or comes back.
BURST = ("", "/session", "/telemetry", "/card", "/slots", "/panel", "/panel/frame",
         "/consoles", "/claim", "/display", "/overlays")


@pytest.fixture(autouse=True)
def _gate_on() -> Iterator[None]:
    previous = ctlgate.set_enabled(True)
    ctlgate.forget()
    yield
    ctlgate.set_enabled(previous)
    ctlgate.forget()


@contextmanager
def gate_off() -> Iterator[None]:
    """The negative twin: no gate, no pacing (the code before SERIAL-6900)."""
    previous = ctlgate.set_enabled(False)
    try:
        yield
    finally:
        ctlgate.set_enabled(previous)


def run_together(jobs: list[Callable[[], Any]]) -> list[Any]:
    """Start every job at the same moment; each result, or the exception it raised."""
    start = threading.Barrier(len(jobs))

    def one(job: Callable[[], Any]) -> Any:
        start.wait(5)
        try:
            return job()
        except Exception as exc:  # noqa: BLE001 - the test reads it
            return exc

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return list(pool.map(one, jobs))


# --- 1. the gate itself -----------------------------------------------------------------------


def test_the_gate_serves_waiters_first_come_first_served():
    gate = ctlgate.ControlGate("192.0.2.1:6900")
    order: list[int] = []
    gate.acquire()
    threads = []
    for i in range(5):
        def take(i: int = i) -> None:
            with gate.slot(wait_s=5):
                order.append(i)
        t = threading.Thread(target=take)
        t.start()
        threads.append(t)
        deadline = time.monotonic() + 5
        while gate.waiting() < i + 1 and time.monotonic() < deadline:
            time.sleep(0.005)                      # queue them in a known order
    gate.release()
    for t in threads:
        t.join(5)
    assert order == [0, 1, 2, 3, 4]
    assert gate.stats.waited == 5 and not gate.held


def test_a_nested_call_on_the_holding_thread_passes_and_is_counted():
    gate = ctlgate.ControlGate("192.0.2.1:6900")
    with gate.slot(), gate.slot(wait_s=0.1):       # no deadlock
        assert gate.held
    assert not gate.held and gate.stats.nested == 1


def test_the_bounded_wait_says_own_request_and_is_never_contention():
    gate = ctlgate.gate_for("192.0.2.1:6900")
    holding, done = threading.Event(), threading.Event()

    def hold() -> None:
        with gate.slot("the swap to synth"):
            holding.set()
            done.wait(5)

    t = threading.Thread(target=hold, name="swap-job")
    t.start()
    holding.wait(5)
    t0 = time.monotonic()
    with pytest.raises(ctlgate.OwnRequestBusyError) as info:
        with gate.slot(wait_s=0.3):
            pass
    waited = time.monotonic() - t0
    done.set()
    t.join(5)
    err = info.value
    assert 0.25 < waited < 2.0 and err.code == ExitCode.HELD
    assert "this Harness Manager's own request" in err.message and "the swap to synth" in err.message
    assert "not another client" in err.hint and err.data["reason"] == ctlgate.OWN_REQUEST
    assert quiet.is_contention(err) is False and ctlgate.is_own_request(err)
    assert gate.stats.timeouts == 1 and gate.waiting() == 0 and not gate.held
    # Twin: the board's own refusal (a HeldError from the port) is contention, as before.
    assert quiet.is_contention(HeldError("shell at x closed the connection")) is True


def test_twin_a_disabled_gate_never_waits():
    gate = ctlgate.gate_for("192.0.2.2:6900")
    gate.acquire()
    try:
        with gate_off(), gate.slot(wait_s=0.05):
            pass                                   # no error: nothing serialises
    finally:
        gate.release()


# --- 2. the in-process engine: two threads, one board ---------------------------------------------


@contextmanager
def fronted(tmp_path: Path, **front_kw: Any) -> Iterator[tuple[PanelVirtualMps3, SingleClientFront]]:
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        front = SingleClientFront(vb.shell, **front_kw)
        try:
            yield vb, front
        finally:
            front.close()


def open_session(vb: Any, front: SingleClientFront) -> Any:
    pk = Mps3Pack(console_ports=vb.console_ports, push_port=vb.shell.raw_tcp_port,
                  tftp_port=vb.shell.tftp_port)
    session = pk.open(pk.candidate_for_host(front.endpoint))
    session.shell.probes = NO_IDENTIFY
    return session


def three_threads(session: Any) -> list[Any]:
    def reads(fn: Callable[[], Any]) -> Callable[[], list[Any]]:
        return lambda: [fn() for _ in range(5)]

    return run_together([reads(session.shell.live), reads(session.health),
                         reads(session.deploy.card_status)])


def test_in_process_threads_on_one_board_are_serialised(tmp_path):
    with fronted(tmp_path, turn_away="eof", reply_delay_s=0.02) as (_vb, front):
        session = open_session(_vb, front)
        try:
            t0 = time.monotonic()
            out = three_threads(session)
            assert not [r for r in out if isinstance(r, Exception)], out
            assert all(h.control_channel == "idle" for h in out[1]), out[1]
            assert front.refused("hm", t0) == []
            assert session.shell.gate().stats.waited > 0, "they did queue"
        finally:
            session.close()


def test_twin_without_the_gate_the_threads_refuse_each_other(tmp_path):
    with fronted(tmp_path, turn_away="eof", reply_delay_s=0.02) as (_vb, front):
        session = open_session(_vb, front)
        try:
            t0 = time.monotonic()
            with gate_off():
                out = three_threads(session)
            refused = front.refused("hm", t0)
            failed = [r for r in out if isinstance(r, HarnessError)]
            busy = [h for h in (out[1] if isinstance(out[1], list) else [])
                    if h.control_channel == "busy"]
            assert refused and (failed or busy), (refused, out)
        finally:
            session.close()


# --- 3. the service: the page's burst plus explicit calls ------------------------------------------


class Service:
    """A real harness-manager-daemon (uvicorn) over the Engine, the board behind the front."""

    def __init__(self, vb: PanelVirtualMps3, front: SingleClientFront) -> None:
        self.vb, self.front = vb, front
        self.engine = engine_for(vb)
        self.live = LiveDaemon(self.engine, write_json=False)
        self.bid = ""

    def __enter__(self) -> Service:
        self.live.__enter__()
        self.d = self.live.app.state.daemon
        self.d.presence._stop.set()                # the burst drives the beat itself
        self.d.presence.ride_wait_s = 0.0
        self.http = self.live.client()
        r = self.http.post("/api/v1/boards", json={"target": self.front.endpoint,
                                                   "note": "serial-6900"})
        assert r.status_code == 200, r.text
        self.bid = r.json()["board_id"]
        self.session = self.engine.session(self.bid)
        self.session.shell.probes = NO_IDENTIFY
        r = self.http.put(f"{bid_path(self.bid)}/viewers/page-1", json={"ttl_s": 300})
        assert r.status_code == 200, r.text
        return self

    def __exit__(self, *exc: object) -> None:
        self.http.close()
        self.engine.close_all()
        self.live.__exit__(*exc)

    def get(self, suffix: str, headers: dict[str, str] | None = None) -> httpx.Response:
        with self.live.client() as c:          # one client per request: real concurrency
            return c.get(f"{bid_path(self.bid)}{suffix}", headers=headers or {})

    def burst(self) -> list[Any]:
        """The page's refresh burst (background, viewing) + a beat + an explicit overlays and
        card read (the CLI through the service), all at the same moment."""
        jobs: list[Callable[[], Any]] = [lambda s=s: self.get(s, BG) for s in BURST]
        jobs.append(lambda: self.get("/overlays"))
        jobs.append(lambda: self.get("/card"))
        jobs.append(lambda: self.d.presence.beat_due(time.monotonic() + 10_000.0))
        return run_together(jobs)


def contention_in(answers: list[Any]) -> list[str]:
    """Each answer that says the board turned us away: a quiet "busy", a 409, a 503..."""
    bad = []
    for a in answers:
        if isinstance(a, Exception):
            bad.append(repr(a))
            continue
        if not isinstance(a, httpx.Response):
            continue
        body = a.json()
        if (body.get("quiet") or {}).get("kind") == "busy":
            bad.append(f"{a.request.url.path}: quiet busy")
        err = body.get("error") or {}
        if err.get("name") in ("HELD", "UNREACHABLE"):
            bad.append(f"{a.request.url.path}: {err.get('name')} {err.get('message')}")
    return bad


@contextmanager
def service(tmp_path: Path, **front_kw: Any) -> Iterator[Service]:
    with fronted(tmp_path, **front_kw) as (vb, front), Service(vb, front) as svc:
        yield svc


@pytest.mark.parametrize("turn_away", ["eof", "rst"])
def test_the_pages_burst_and_explicit_reads_meet_no_refusal(tmp_path, turn_away):
    with service(tmp_path, turn_away=turn_away, reply_delay_s=0.01) as s:
        t0 = time.monotonic()
        for _ in range(3):
            answers = s.burst()
            assert contention_in(answers) == []
            ok = [a for a in answers if isinstance(a, httpx.Response)]
            assert all(a.status_code in (200, 422) for a in ok), [(a.request.url.path, a.text)
                                                                  for a in ok]
        assert s.front.refused("hm", t0) == []
        assert s.d.quiet.refusals(s.bid) == 0 and s.d.quiet.backing_off(s.bid) is None
        assert s.session.shell.gate().stats.nested == 0, "no path opened 6900 inside another"
        info = s.get("").json()
        assert info["health"]["control_channel"] == "idle", info["health"]


def test_twin_without_the_gate_the_burst_refuses_itself_and_backs_off(tmp_path):
    with service(tmp_path, turn_away="eof", reply_delay_s=0.01) as s, gate_off():
        t0 = time.monotonic()
        for _ in range(3):
            s.burst()
        assert s.front.refused("hm", t0), "HM's own reads turned each other away"
        assert s.d.quiet.refusals(s.bid) > 0, "and QUIET-POLL counted it as another client"


def test_a_real_other_client_is_still_another_client_and_backs_off(tmp_path):
    """The twin of the self-contention test: someone else (the soak) holds the port."""
    with service(tmp_path, turn_away="eof") as s:
        s.front.hold()
        t0 = time.monotonic()
        body = s.get("", BG).json()
        assert (body.get("quiet") or {}).get("kind") == "busy", body
        assert "another client" in body["quiet"]["text"]
        assert s.front.refused("hm", t0) and s.d.quiet.refusals(s.bid) >= 1
        assert s.d.quiet.backing_off(s.bid) is not None
        s.front.release()
        assert soak_call(s.front.port)


def test_a_wait_on_our_own_gate_is_held_not_busy_and_never_backs_off(tmp_path):
    with service(tmp_path, turn_away="eof") as s:
        s.session.shell.gate_wait_s = 0.3
        gate = s.session.shell.gate()
        holding, done = threading.Event(), threading.Event()

        def swap() -> None:                        # our own long request (a swap parks 6900)
            with gate.slot("the swap to synth"):
                holding.set()
                done.wait(10)

        t = threading.Thread(target=swap, name="deploy-job")
        t.start()
        holding.wait(5)
        t0 = time.monotonic()
        try:
            r = s.get("/session", BG)             # a read outside the daemon's op gate
            body = r.json()
            health = s.get("", BG).json()
        finally:
            done.set()
            t.join(5)
        assert r.status_code == 200 or body["error"]["name"] == "HELD", body
        assert (body.get("quiet") or {}).get("kind") != "busy"
        assert s.front.attempts(t0) == 0, "nothing was sent while our own request held the port"
        assert s.d.quiet.refusals(s.bid) == 0 and s.d.quiet.backing_off(s.bid) is None
        if "error" in health:
            err = health["error"]
            assert err["name"] == "HELD" and err["data"]["reason"] == ctlgate.OWN_REQUEST
            assert "own request" in err["message"]


# --- 4. our own ghost: the board reaps a closed client only on its next pass -----------------------
#
# ``close_lag_s`` is harnessd's accept-before-reap (coordinator_net.c accepts FIRST, then reads
# the old client's EOF): for that long after our close, a new connect is turned away unanswered.
# Silicon: 3-10 ms typically; 30 ms here outlasts the pace (20 ms), so every back-to-back call
# meets the ghost at least once and must retry past it.

REAP_S = 0.03
TURNED_AWAY = "closed the connection"


@contextmanager
def card_board(tmp_path: Path, turn_away: str = "eof",
               **front_kw: Any) -> Iterator[tuple[Any, SingleClientFront, Any]]:
    root = tmp_path / "overlays"
    make_overlay(root, "synth", rm_id=SYNTH_RM_ID, static_id=LINUX_SID)
    fake = slot_board(usd_card="da", boot_rm_id=SYNTH2_RM_ID)
    front = SingleClientFront(fake, turn_away=turn_away, **front_kw)
    pk = Mps3Pack(console_ports=fake.console_ports, push_port=fake.raw_tcp_port,
                  tftp_port=fake.tftp_port)
    session = pk.open(pk.candidate_for_host(front.endpoint))
    session.shell.probes = NO_IDENTIFY
    try:
        yield fake, front, session
    finally:
        session.close()
        front.close()
        fake.stop()


def test_card_status_includes_the_os_slots_past_the_boards_reap(tmp_path):
    with card_board(tmp_path, close_lag_s=REAP_S) as (_fake, front, session):
        seen: list[Any] = []
        session.shell.observer = seen.append
        t0 = time.monotonic()
        st = SlotService().card_status(session)
        assert st.present and st.os_slots is not None and st.os_slots.running == "A"
        assert not [n for n in st.notes if "could not be read" in n], st.notes
        assert front.refused("hm", t0), "the ghost was met"
        assert session.shell.gate().stats.reaped > 0 and all(e is None for e in seen), seen


def test_twin_card_status_back_to_back_loses_the_os_slots_to_our_own_ghost(tmp_path):
    with card_board(tmp_path, close_lag_s=REAP_S) as (_fake, front, session), gate_off():
        t0 = time.monotonic()
        try:
            st = SlotService().card_status(session)
            lost = st.os_slots is None and any("could not be read" in n for n in st.notes)
        except HeldError as exc:                   # the second read itself was turned away
            lost = TURNED_AWAY in exc.message or "reset the connection" in exc.message
        assert lost and front.refused("hm", t0)


def program(capsys: Any, monkeypatch: Any, fake: Any, front: SingleClientFront,
            tmp_path: Path) -> tuple[int, str, str]:
    """``harness-manager program TARGET synth --keep-on-card --yes``, in this process: the
    preflight, the card read, the reset guard, the swap, the card commit, the confirm."""
    use_overlay_dirs(monkeypatch, tmp_path / "overlays")
    monkeypatch.setenv("HARNESS_MANAGER_MPS3_PUSH_PORT", str(fake.raw_tcp_port))
    monkeypatch.setenv("HARNESS_MANAGER_MPS3_TFTP_PORT", str(fake.tftp_port))
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    return run_cli(capsys, "program", front.endpoint, "synth", "--keep-on-card", "--yes",
                   "--json")


def test_preflight_guard_swap_and_card_commit_pass_the_boards_reap(tmp_path, capsys,
                                                                   monkeypatch):
    """Silicon 2026-09-28: `program ... --keep-on-card` was refused at the reset guard, the
    read straight after the preflight, twice out of twice."""
    with card_board(tmp_path, close_lag_s=REAP_S) as (fake, front, _session):
        t0 = time.monotonic()
        rc, out, err = program(capsys, monkeypatch, fake, front, tmp_path)
        assert rc == 0, err
        doc = json.loads(out)
        assert doc["result"]["verified"] and doc["result"]["card"]["kept"], doc["result"]
        assert fake.commits and fake.commits[-1][0] == "synth"
        assert front.refused("hm", t0), "every step met the ghost and went past it"
        gate = ctlgate.gate_for(ctlgate.key_of_address(front.endpoint, 6900))
        assert gate.stats.reaped > 0 and gate.stats.nested == 0, gate.stats


def test_twin_the_same_program_without_the_retry_is_refused(tmp_path, capsys, monkeypatch):
    with card_board(tmp_path, close_lag_s=REAP_S) as (fake, front, _session), gate_off():
        rc, _out, err = program(capsys, monkeypatch, fake, front, tmp_path)
        assert rc != 0 and ("closed the connection" in err or "reset the connection" in err), err
        assert fake.commits == []


def test_a_swap_connect_the_board_resets_during_the_connect_is_our_ghost(tmp_path, capsys,
                                                                         monkeypatch):
    """FIX-PACK-1: the swap's connection opened inside the board's reap window, turned away
    with a RESET that lands during the connect itself (the fake fixes that order). It was
    raised outside the ghost retry, so the program failed UNREACHABLE; now it is retried like
    every other turn-away and the program goes through."""
    with card_board(tmp_path, turn_away="rst", close_lag_s=0.3) as (fake, front, _session):
        raced = front.transport("connect", base=deploymod._TimedSocketTransport)
        monkeypatch.setattr(deploymod, "_TimedSocketTransport", raced)
        rc, out, err = program(capsys, monkeypatch, fake, front, tmp_path)
        assert rc == 0, err
        assert json.loads(out)["result"]["verified"] and fake.commits
        assert raced.raised, "the swap's connect met the reset at least once"
        gate = ctlgate.gate_for(ctlgate.key_of_address(front.endpoint, 6900))
        assert gate.stats.reaped >= len(raced.raised)


def test_twin_a_refused_swap_connect_is_no_ghost():
    """Twin: nothing listening (ECONNREFUSED) is not a turn-away, with or without a tap; a
    reset during the connect (no tap yet) is."""
    assert not deploymod._ghost(ConnectionRefusedError(111, "refused"), None, None)
    assert deploymod._ghost(ConnectionResetError(104, "reset"), None, None)
    assert deploymod._ghost(ConnectionAbortedError(10053, "aborted"), None, None)
    assert not deploymod._ghost(TimeoutError("slow"), None, None)


def gaps(front: SingleClientFront, since: float) -> list[float]:
    """From each connection's close (as the board saw it) to the next one's open."""
    conns = front.hm_conns(since)
    return [b.opened - a.closed for a, b in zip(conns, conns[1:], strict=False)]


def test_a_forwarded_shell_waits_longer_after_our_close(tmp_path):
    """Through an SSH forward the pace is ``PACE_FORWARD_S`` (50 ms), directly ``PACE_S``."""
    with card_board(tmp_path) as (_fake, front, session):
        session.shell.lagging_close = True
        t0 = time.monotonic()
        for _ in range(4):
            session.shell.live()
        assert min(gaps(front, t0)) >= ctlgate.PACE_FORWARD_S - 0.005, gaps(front, t0)
        session.shell.lagging_close = False            # twin: direct, the shorter pace
        t1 = time.monotonic()
        for _ in range(4):
            session.shell.live()
        direct = gaps(front, t1)
        assert min(direct) >= ctlgate.PACE_S - 0.005, direct


def test_a_refusal_that_outlasts_the_retries_is_another_client(tmp_path):
    with card_board(tmp_path) as (_fake, front, session):
        seen: list[Any] = []
        session.shell.observer = seen.append
        session.shell.live()
        front.hold()                               # someone else takes the port at once
        t0 = time.monotonic()
        with pytest.raises(HeldError, match="(closed|reset) the connection") as info:
            session.shell.live()
        took = time.monotonic() - t0
        assert 0.8 < took < 3.0 and len(front.refused("hm", t0)) == 21
        assert session.shell.gate().stats.reaped == 20
        assert seen[-1] is info.value and quiet.is_contention(info.value)


def test_twin_with_no_recent_close_of_ours_a_refusal_is_someone_else_at_once(tmp_path,
                                                                           monkeypatch):
    monkeypatch.setattr(ctlgate, "REAP_WINDOW_S", 0.2)
    with card_board(tmp_path) as (_fake, front, session):
        session.shell.live()
        time.sleep(0.3)                            # the board has long reaped us
        front.hold()
        t0 = time.monotonic()
        with pytest.raises(HeldError, match="(closed|reset) the connection"):
            session.shell.live()
        assert len(front.refused("hm", t0)) == 1 and session.shell.gate().stats.reaped == 0


# --- 5. the pack: every shell of a board shares its gate; a tunnel's shell is paced ----------------


def test_the_pack_keys_the_gate_by_the_board_and_marks_a_tunnel(monkeypatch):
    reach = SimpleNamespace(host="127.0.0.1", tunnel=None, closed=False,
                            ports={"control": 40149, "push": 40150, "rbb": 40151, "xvc": 40152,
                                   "uart0": 40153, "uart1": 40154, "swo": 40155})
    reach.close = lambda: setattr(reach, "closed", True)
    original = packmod._hook

    def hook(module: str, attr: str) -> Any:
        if (module, attr) == ("tunnel", "open_reach"):
            return lambda cand, ports: reach
        return original(module, attr)

    monkeypatch.setattr(packmod, "_hook", hook)
    pk = Mps3Pack()
    session = pk.open(pk.candidate_for_host("192.168.10.101"))
    try:
        assert (session.shell.host, session.shell.port) == ("127.0.0.1", 40149)
        assert session.shell.gate_key == "192.168.10.101:6900" and session.shell.lagging_close
    finally:
        session.close()
    monkeypatch.setattr(packmod, "_hook", original)
    direct = pk.open(pk.candidate_for_host("192.168.10.101"))   # twin: no tunnel
    try:
        assert direct.shell.gate_key == "192.168.10.101:6900" and not direct.shell.lagging_close
    finally:
        direct.close()


# --- 6. the service shares one answer between identical reads in flight ----------------------------


def test_identical_board_reads_in_flight_share_one_answer(tmp_path):
    with service(tmp_path, turn_away="eof", reply_delay_s=0.15) as s:
        co = s.live.app.state.coalescer
        before = co.shared
        a, b = run_together([lambda: s.get("/card", BG), lambda: s.get("/card", BG)])
        assert a.status_code == b.status_code == 200 and a.json() == b.json()
        assert co.shared == before + 1
        # Twin: a background read and an explicit one are different reads (the gate may hold
        # back the first): never merged.
        before = co.shared
        a, b = run_together([lambda: s.get("/card", BG), lambda: s.get("/card")])
        assert a.status_code == b.status_code == 200 and co.shared == before


def test_which_reads_are_shared():
    from starlette.requests import Request

    from harness_manager.daemon.coalesce import GetCoalescer

    co = GetCoalescer("/api/v1")

    def key(method: str, path: str, **headers: str) -> Any:
        scope = {"type": "http", "method": method, "path": path, "query_string": b"",
                 "headers": [(k.lower().replace("_", "-").encode(), v.encode())
                             for k, v in headers.items()]}
        return co.key(Request(scope))

    bid = "/api/v1/boards/mps3%40192.168.10.101%3A6900"
    for suffix in ("", "/card", "/slots", "/panel/frame", "/lease", "/session"):
        assert key("GET", bid + suffix) is not None, suffix
    # Twins: an action, a streamed read, a route not listed, a bid with a slash in it.
    assert key("POST", bid + "/card") is None and key("GET", bid + "/display.png") is None
    assert key("GET", bid + "/debug/detect") is None
    assert key("GET", "/api/v1/boards/mps3@usb/dev/ttyUSB0/card") is None
    assert key("GET", bid, X_HM_Background="1") != key("GET", bid)


# --- 7. item 4a: `slot status` / `card status` go through the service -----------------------------


@contextmanager
def card_service(tmp_path: Path) -> Iterator[tuple[Any, LiveDaemon, str]]:
    fake = slot_board(usd_card="da")
    eng = Engine(EngineConfig(state_dir=Path(os.environ["HARNESS_MANAGER_STATE_DIR"]),
                              pack_overrides={"mps3": {
                                  "console_ports": fake.console_ports,
                                  "push_port": fake.raw_tcp_port,
                                  "tftp_port": fake.tftp_port}}))
    target = f"{fake.host}:{fake.control_port}"
    try:
        with LiveDaemon(eng, write_json=True) as live:
            live.app.state.daemon.presence._stop.set()
            with live.client() as c:
                r = c.post("/api/v1/boards", json={"target": target, "note": "the page"})
                assert r.status_code == 200, r.text
            yield fake, live, target
            eng.close_all()
    finally:
        fake.stop()


def test_card_and_slot_status_go_through_the_service_that_holds_the_board(tmp_path, capsys,
                                                                          monkeypatch):
    monkeypatch.delenv("HARNESS_MANAGER_NO_DAEMON", raising=False)
    with card_service(tmp_path) as (_fake, _live, target):
        rc, out, err = run_cli(capsys, "card", "status", target, "--json")
        assert rc == 0, err
        doc = json.loads(out)
        assert doc["present"] and doc["os_slots"]["running"] == "A", doc
        rc, out, err = run_cli(capsys, "slot", "status", target, "--json")
        assert rc == 0, err
        assert json.loads(out)["running"] == "A"
        rc, out, err = run_cli(capsys, "card", "status", target)
        assert rc == 0 and "os slots" in out, out


def test_twin_in_process_card_status_is_refused_by_the_services_lock(tmp_path, capsys,
                                                                     monkeypatch):
    with card_service(tmp_path) as (_fake, _live, target):
        monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")        # the old routing
        rc, _out, err = run_cli(capsys, "card", "status", target, "--json")
        assert rc == ExitCode.HELD and "in use" in err, err


def test_the_remote_slot_proxy_reads_once_and_leaves_the_reset_guard_to_the_service(tmp_path):
    from harness_manager.client.remote import RemoteEngine
    from harness_manager.services import reset_guard

    with card_service(tmp_path) as (fake, live, target):
        remote = RemoteEngine(live.base_url, live.token,
                              state_dir=Path(os.environ["HARNESS_MANAGER_STATE_DIR"]))
        session = remote.open(remote.candidate_for(target))
        assert session.os_slots is not None and session.card is not None
        before = len(fake.requests) if hasattr(fake, "requests") else None
        assert reset_guard.busy_job(session) is None, "the service guards its own resets"
        if before is not None:
            assert len(fake.requests) == before, "and this client read nothing for it"
        st = SlotService().status(session)             # slots_reason + status: one GET
        assert st.running == "A" and not hasattr(session.os_slots, "push")


def test_card_and_slot_changes_go_through_a_running_service_too():
    # FIX-PACK-6 item 1 (H1 Z1): the changes are the service's jobs when one runs, as program
    # and mcc reboot are; without one they run in this process (the engine factory decides).
    from harness_manager.cli.engine import wants_daemon

    ns = SimpleNamespace
    assert wants_daemon(ns(cmd="card", card_cmd="status"))
    assert wants_daemon(ns(cmd="slot", slot_cmd="status"))
    assert wants_daemon(ns(cmd="card", card_cmd="commit"))
    assert wants_daemon(ns(cmd="slot", slot_cmd="push"))
    assert not wants_daemon(ns(cmd="update"))
    assert not wants_daemon(ns(cmd="attach"))


# --- 8. item 4b: --overlay-dir while the service holds the board ------------------------------------


def test_overlay_dir_while_the_service_holds_the_board_says_how(tmp_path, capsys):
    root = tmp_path / "overlays"
    make_overlay(root, "synth")
    with card_service(tmp_path) as (_fake, _live, target):
        rc, _out, err = run_cli(capsys, "overlays", target, "--overlay-dir", str(root))
        assert rc == ExitCode.HELD, err
        assert "--overlay-dir runs in this process" in err
        assert f"config set mps3.overlay_dirs {root}" in err
    # Twin: no service holds the board: the flag works in this process, as before.
    fake = slot_board(usd_card="da")
    try:
        rc, out, err = run_cli(capsys, "overlays", f"{fake.host}:{fake.control_port}",
                               "--overlay-dir", str(root))
        assert rc == 0 and "synth" in out, err
    finally:
        fake.stop()


# --- 9. item 4c: mps3.overlay_dirs applies at the next listing -------------------------------------


def test_overlay_dirs_set_after_the_board_opened_are_seen_at_the_next_listing(tmp_path,
                                                                             monkeypatch):
    from harness_manager_mps3.overlays import OverlayCatalogue

    root = tmp_path / "later"
    make_overlay(root, "synth")
    monkeypatch.delenv("HARNESS_MANAGER_MPS3_OVERLAY_DIRS", raising=False)
    cat = OverlayCatalogue()                       # a session's catalogue, already in use
    assert [r.name for r in cat.refs()] == []
    use_overlay_dirs(monkeypatch, root)            # the Settings dialog sets the directory
    assert [r.name for r in cat.refs()] == ["synth"], "no reopen needed"
    # Twin: taken away again, it is gone at the next listing too.
    monkeypatch.delenv("HARNESS_MANAGER_MPS3_OVERLAY_DIRS")
    assert [r.name for r in cat.refs()] == []
