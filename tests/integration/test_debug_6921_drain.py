"""DEBUG-6921: HM's own just-ended OpenOCD session must not make the board look held.

The harness's jtag_server (6921) is single-client and accepts before it drains:
a new connection that arrives while the previous session's final ``Q`` is
unread is closed at once (an RST), and OpenOCD logs "Connection reset by peer"
(the Linux lead's root cause of B1 v4 step (g), 2026-09-25). ``FakeJtagServer``
models it with ``drain_lag`` (timing, as on the board) and ``refuse_next``
(a count, where the test needs it deterministic). OpenOCD is stub_openocd.
Every check has a negative twin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from harness_manager.core.errors import ExitCode, HeldError
from harness_manager.core.events import Event, EventBus
from harness_manager.services.debug import (
    DRAIN_HINT,
    JTAG_COOLDOWN_S,
    OWN_RACE_WINDOW_S,
    DebugService,
)
from tests.fakes.t4_console_rig import BareSession, EventLog
from tests.fakes.t4_debug_rig import StaticDebugAdapter, StubRig, use_stub
from tests.fakes.t4_rbb_jtag import FakeJtagServer

REPO = Path(__file__).resolve().parents[2]
BOARD = "mps3@drain-test"
FAST = 0.3                      # a short cooldown where the test is about the retry, not the wait
LAG = 1.0                       # the board's drain, well under JTAG_COOLDOWN_S
LONG_LAG = 6.0                  # outlasts every attempt a zero-cooldown service makes
# The hint a reset has always carried (services/debug.py classify_failure): unchanged.
HELD_HINT = ("another debugger probably holds it: the board serves one client per port "
             "(jtag_server.c:185)")


@pytest.fixture
def rig(monkeypatch, tmp_path) -> StubRig:
    return use_stub(monkeypatch, tmp_path)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def service(bus) -> Iterator[Callable[..., DebugService]]:
    made: list[DebugService] = []

    def make(**kw) -> DebugService:
        svc = DebugService(bus, start_timeout=15, **kw)
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.close()


def bare(rig: StubRig, srv: FakeJtagServer) -> BareSession:
    return BareSession(BOARD, debug=StaticDebugAdapter(rig.cfg_dir, srv.port))


def ended_file(svc: DebugService) -> Path:
    """The cross-process record of when HM's last OpenOCD on BOARD ended."""
    return svc.registry_dir / f"{BOARD.replace('@', '_')}.ended"


def ended_at(svc: DebugService) -> float:
    return json.loads(ended_file(svc).read_text())["ended_at"]            # wall time


def go(svc: DebugService, session: BareSession, call: str) -> object:
    return getattr(svc, call)(session)


def succeeded(result: object) -> bool:
    return result == "0x6ba00477" or getattr(result, "state", "") == "up"


# -- 1. back to back: detect, then up ------------------------------------------------------------


def test_back_to_back_detect_then_up_waits_out_the_drain_and_succeeds(rig, service):
    with FakeJtagServer(drain_lag=LAG) as srv:
        svc = service()                                       # the real cooldown
        session = bare(rig, srv)
        assert svc.detect(session) == "0x6ba00477"
        st = svc.up(session)
        assert st.state == "up"
        assert srv.refused == 0 and srv.accepted == 2 and len(rig.runs()) == 2
        assert srv.served_at[1] - ended_at(svc) >= JTAG_COOLDOWN_S    # it waited


def test_negative_twin_with_no_cooldown_the_drain_makes_it_held(rig, service):
    with FakeJtagServer(drain_lag=LONG_LAG) as srv:
        svc = service(cooldown=0)
        session = bare(rig, srv)
        svc.detect(session)
        with pytest.raises(HeldError) as exc:
            svc.up(session)
        assert exc.value.code == ExitCode.HELD == 4
        assert "closed the connection at once" in exc.value.message
        assert srv.accepted == 1 and srv.refused == 2     # the retry cannot outwait the drain
        assert svc.status(session).state == "down"


# -- 2. our own race: retried once --------------------------------------------------------------


@pytest.mark.parametrize("call", ["detect", "up"])
def test_a_reset_right_after_our_own_session_is_retried_once_and_succeeds(rig, service, call):
    with FakeJtagServer() as srv:
        svc = service(cooldown=FAST)
        session = bare(rig, srv)
        svc.detect(session)                          # our own session: it has just ended
        srv.refuse_next = 1                          # ...and the board is still draining it
        assert succeeded(go(svc, session, call))
        assert srv.refused == 1 and srv.accepted == 2
        assert len(rig.runs()) == 3                  # detect, the refused one, the one retry


@pytest.mark.parametrize("ours", ["none", "older than the window"])
@pytest.mark.parametrize("call", ["detect", "up"])
def test_negative_twin_a_reset_with_no_recent_session_of_ours_is_held_at_once(
        rig, service, call, ours):
    with FakeJtagServer() as srv:
        svc = service(cooldown=FAST)
        if ours != "none":                           # e.g. another HM process, a while ago
            svc.registry_dir.mkdir(parents=True, exist_ok=True)
            ended_file(svc).write_text(
                json.dumps({"ended_at": time.time() - OWN_RACE_WINDOW_S - 1}))
        srv.refuse_next = 1
        with pytest.raises(HeldError) as exc:
            go(svc, bare(rig, srv), call)
        assert exc.value.hint == HELD_HINT           # exactly as before this fix
        assert DRAIN_HINT not in str(exc.value)
        assert len(rig.runs()) == 1 and srv.refused == 1 and srv.accepted == 0   # no retry


# -- 3. refused again after the retry -------------------------------------------------------------


@pytest.mark.parametrize("call", ["detect", "up"])
def test_a_second_reset_after_the_retry_is_held_and_names_the_drain(rig, service, call):
    with FakeJtagServer() as srv:
        svc = service(cooldown=FAST)
        session = bare(rig, srv)
        svc.detect(session)
        srv.refuse_next = 2
        with pytest.raises(HeldError) as exc:
            go(svc, session, call)
        assert exc.value.code == ExitCode.HELD
        assert exc.value.hint == f"{HELD_HINT}, {DRAIN_HINT}"
        assert "closed the connection at once" in exc.value.message
        assert len(rig.runs()) == 3 and srv.refused == 2      # one retry, never two
        assert svc.status(session).state == "down"


# -- 4. the swap reopen ---------------------------------------------------------------------------


def _swap(bus: EventBus, log: EventLog) -> int:
    bus.publish(Event("deploy.started", BOARD, {"overlay": "nanosoc"}))    # closes, synchronously
    mark = log.mark()
    bus.publish(Event("deploy.done", BOARD, {"rm_id": "0x01000001", "verified": True}))
    return mark


def test_the_swap_reopen_waits_out_the_drain(rig, service, bus):
    with FakeJtagServer(drain_lag=LAG) as srv:
        svc = service()
        session = bare(rig, srv)
        log = EventLog(bus, "debug.state")
        first = svc.up(session)
        mark = _swap(bus, log)
        log.wait_for(lambda e: e.data["state"] == "up", after=mark)
        assert srv.served_at[1] - ended_at(svc) >= JTAG_COOLDOWN_S
        assert srv.refused == 0
        again = svc.status(session)
        assert again.state == "up" and again.gdb_port == first.gdb_port


def test_negative_twin_without_the_cooldown_the_swap_reopen_is_refused(rig, service, bus):
    with FakeJtagServer(drain_lag=LONG_LAG) as srv:
        svc = service(cooldown=0)
        session = bare(rig, srv)
        log = EventLog(bus, "debug.state")
        svc.up(session)
        mark = _swap(bus, log)
        ev = log.wait_for(lambda e: e.data["state"] == "failed", after=mark)
        assert "closed the connection at once" in ev.data["detail"]
        assert DRAIN_HINT in ev.data["detail"]       # retried once, then said why
        assert srv.refused == 2 and svc.status(session).state == "down"


# -- the record crosses processes (a CLI run after a CLI run; the daemon after an update) -----


ONE_DETECT = '''
import sys
from pathlib import Path
from harness_manager.services.debug import DebugService
from tests.fakes.t4_console_rig import BareSession
from tests.fakes.t4_debug_rig import StaticDebugAdapter
session = BareSession(sys.argv[3], debug=StaticDebugAdapter(Path(sys.argv[1]), int(sys.argv[2])))
print(DebugService(None).detect(session))
'''


def _detect_in_another_process(rig: StubRig, srv: FakeJtagServer) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), env.get("PYTHONPATH", "")])
    res = subprocess.run([sys.executable, "-c", ONE_DETECT, str(rig.cfg_dir), str(srv.port),
                          BOARD], cwd=str(REPO), env=env, capture_output=True, text=True,
                         timeout=30)
    assert res.stdout.strip() == "0x6ba00477", res.stderr


def test_another_process_s_session_is_waited_out_too(rig, service):
    with FakeJtagServer(drain_lag=LAG) as srv:
        _detect_in_another_process(rig, srv)
        svc = service()
        assert svc.up(bare(rig, srv)).state == "up"
        assert srv.refused == 0
        assert srv.served_at[1] - ended_at(svc) >= JTAG_COOLDOWN_S


def test_negative_twin_without_the_record_another_process_s_drain_looks_held(rig, service):
    with FakeJtagServer(drain_lag=LONG_LAG) as srv:
        _detect_in_another_process(rig, srv)
        svc = service()
        ended_file(svc).unlink()                    # what HM knew before this fix
        with pytest.raises(HeldError) as exc:
            svc.up(bare(rig, srv))
        assert exc.value.hint == HELD_HINT and srv.refused == 1
