"""FIX-PACK-1 item 3: the Live display never keeps a stale ``lcd_mirror`` fact.

On 2026-09-28 the lab MPS3 rebooted from a Linux image WITH ``lcd_mirror`` into one WITHOUT
it. The Live display kept its cached facts (the engine is there), reconnected over its old
SSH forward to a port nothing listened on, and spun on "connecting" for ever.

Now (``harness_manager_mps3.display``, ``services.display``, ``daemon.display_api``,
``engine.info``):

- a stream that closes before HELLO re-gates on a live ``version`` read: an image without
  the engine ends the display with "the Live display needs a harness image with lcd_mirror
  (this image has none...)";
- the engine still named but nothing listening on 6940: it stops after ``no_service_max``
  (3) tries with ssh's reason;
- a reboot, a power cycle, a board-changing job or a changed SSH host key drops the
  adapter's cached facts AND its forward (``display_forget``), and nothing is seeded from
  the open's (older) identity until a fresh read;
- every identity read is noted on the adapter (``display_note_identity``), and the daemon
  ends an open display whose gate now says no.

The MPS3 checks run through LM2's rig (the REAL claim forward, ``tunnel.SshTunnel`` on
``FakeSsh``, LM1's ``FakeLcdMirror`` and the REAL compositor); the daemon's wiring on LM3's
rig (a real uvicorn, a fake pack). Every check has its negative twin.
"""

# ruff: noqa: F811 - the tests take the fixture imported from the lane test below

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from harness_manager.core.display import DisplayUnavailable
from harness_manager.core.errors import UnavailableError
from harness_manager.core.events import Event
from harness_manager.core.model import BoardIdentity
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.display_api import refusal
from harness_manager.engine import Engine
from harness_manager.services.display import DisplayTimings
from harness_manager_mps3 import display as D
from tests.fakes.lm1_fake_lcd_mirror import FakeLcdMirror, ViewerModel, bind_ephemeral
from tests.fakes.lm3_display_rig import BOARD, FakeDisplayAdapter, Tab, bid_path, display_rig
from tests.fakes.t1_fakes import FakePack, candidate
from tests.fakes.t13_daemon import TOKEN, headers, state_dir
from tests.integration.test_lm2_display_mps3 import (  # noqa: F401 - the fixture
    BID,
    BITS,
    ENGINE,
    LCDM,
    harness_board,
    rig_factory,
    shows,
    wait_for,
)

#: LM2's quick clocks with a no-service retry long enough to act between tries.
PATIENT = DisplayTimings(grace_s=30.0, ping_s=0.1, stale_s=2.0, dead_s=5.0, tick_s=0.02,
                         hello_timeout_s=5.0, backoff_s=(0.2, 0.3, 0.4), refused_retry_s=0.3,
                         no_service_retry_s=1.0, dim_persist_s=0.2, fps_window_s=1.0)


def quiet_for(rig: Any, seconds: float) -> int:
    """The upstream's connects after ``seconds`` more: it must not have tried again."""
    before = rig.status()["counters"]["connects"]
    time.sleep(seconds)
    after = rig.status()["counters"]["connects"]
    assert after == before, f"it kept connecting ({before} -> {after})"
    return after


# --- 1. the day's case: a reboot into an image without the engine ----------------------------------


def test_a_reboot_into_an_image_without_lcd_mirror_ends_with_that_reason(rig_factory: Any):
    rig = rig_factory()
    board, grid = harness_board()
    with board:
        rig.serve(board)
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: shows(vm, v, grid), what="live")
        # The board reboots into an image whose version names no lcd_mirror: nothing listens.
        rig.shell.features, rig.shell.block = BITS, None
        board.stop_service()
        wait_for(lambda: rig.status()["state"] == "down"
                 and rig.status()["reason"] == D.NO_ENGINE, what="down: no engine")
        assert rig.status()["reason"].startswith(
            "the Live display needs a harness image with lcd_mirror (this image has none")
        quiet_for(rig, 1.2)                        # past every back-off: it stopped for good
        assert rig.svc.boards() == [] and rig.adapter.tunnel_status() is None
        # The routes answer the same, typed (422), from the facts the re-gate read.
        assert rig.adapter.display_gate() == D.NO_ENGINE
        err = refusal(rig.adapter, rig.session, rig.leases)
        assert isinstance(err, UnavailableError) and err.reason == D.NO_ENGINE
        v.close()


def test_twin_a_reboot_into_an_image_that_still_serves_it_goes_live_again(rig_factory: Any):
    rig = rig_factory(timings=PATIENT)
    board, grid = harness_board()
    with board:
        rig.serve(board)
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: shows(vm, v, grid), what="live")
        board.stop_service()                       # the same image: the service comes back
        board.start()
        wait_for(lambda: rig.status()["counters"]["connects"] >= 2
                 and rig.status()["state"] == "live", what="live again")
        assert rig.adapter.display_gate() == "" and rig.svc.boards() == [BID]
        v.close()


# --- 2. the engine named, nothing listening: stop after N tries --------------------------------------


def test_nothing_listening_on_6940_stops_after_n_tries_with_the_reason(rig_factory: Any):
    rig = rig_factory()
    dead = bind_ephemeral()                        # nothing listens behind it
    rig.ssh.routes[("127.0.0.1", LCDM)] = ("127.0.0.1", dead.getsockname()[1])
    dead.close()
    v = rig.svc.attach(BID, rig.adapter)
    n = DisplayTimings().no_service_max
    wait_for(lambda: f"stopped after {n} tries" in rig.status()["reason"], what="stopped")
    reason = rig.status()["reason"]
    assert reason.startswith("no lcd_mirror service on the board") and "open failed" in reason
    assert rig.status()["state"] == "down" and rig.status()["counters"]["connects"] == n
    quiet_for(rig, 1.0)
    v.close()


def test_twin_two_tries_with_nothing_listening_then_the_service_starts_goes_live(
        rig_factory: Any):
    rig = rig_factory(timings=PATIENT)
    board, grid = harness_board()
    with board:
        rig.serve(board)
        board.stop_service()                       # the service is still starting
        vm = ViewerModel()
        v = rig.svc.attach(BID, rig.adapter)
        wait_for(lambda: sum(p.channel_failures for p in rig.ssh.procs) >= 2,
                 what="two tries that found nothing listening")
        board.start()
        wait_for(lambda: shows(vm, v, grid), what="live once it listens")
        assert rig.status()["state"] == "live" and rig.status()["reason"] == ""
        v.close()


# --- 3. the adapter: forget on a reboot, re-seed from a new identity ----------------------------------


def test_a_forget_drops_the_facts_and_the_forward_and_the_next_open_re_gates(rig_factory: Any):
    rig = rig_factory()
    with FakeLcdMirror() as board:
        rig.serve(board)
        rig.adapter.display_connect().close()      # the forward is up; facts from version
        assert rig.adapter.tunnel_status() is not None and rig.adapter.display_gate() == ""
        rig.shell.features, rig.shell.block = BITS, None      # it came back with another image
        assert rig.adapter.display_gate() == "", "the cached facts still say yes (the bug)"
        rig.adapter.display_forget("the board rebooted (MCC REBOOT)")
        assert rig.adapter.tunnel_status() is None and rig.adapter.forgets == 1
        # Not seeded from the open's identity (it named lcd_mirror): not known, not "yes".
        assert rig.adapter.known_facts() is None and rig.adapter.display_gate() == ""
        reads = rig.shell.reads
        with pytest.raises(DisplayUnavailable) as exc:
            rig.adapter.display_connect()
        assert exc.value.reason == D.NO_ENGINE and exc.value.retry_s is None
        assert rig.shell.reads == reads + 1 and rig.adapter.display_gate() == D.NO_ENGINE
        assert rig.adapter.opens == 1                                  # no new forward


def test_twin_a_forget_on_an_unchanged_image_opens_a_new_forward(rig_factory: Any):
    rig = rig_factory()
    with FakeLcdMirror() as board:
        rig.serve(board)
        rig.adapter.display_connect().close()
        rig.adapter.display_forget("the board was power cycled")
        sock = rig.adapter.display_connect()
        try:
            assert sock.recv(2) == b"LM"                             # HELLO's framing
        finally:
            sock.close()
        assert rig.adapter.opens == 2 and rig.adapter.facts().source == "version"


def test_an_identity_read_updates_the_display_gate(rig_factory: Any):
    rig = rig_factory()
    assert rig.adapter.display_gate() == ""
    rig.adapter.display_note_identity(SimpleNamespace(harness_impl="linux", features=BITS))
    assert rig.adapter.display_gate() == D.NO_ENGINE
    # the twin: an identity that names the engine says yes again
    rig.adapter.display_note_identity(SimpleNamespace(harness_impl="linux", features=ENGINE))
    assert rig.adapter.display_gate() == ""
    # ... and one with no features at all (UDP identify while another client holds 6900, a
    # version that answered EBUSY) says nothing: it never gates the engine away
    rig.adapter.display_note_identity(SimpleNamespace(harness_impl="linux", features=()))
    assert rig.adapter.display_gate() == ""
    assert rig.shell.reads == 0                                     # never a read of the board


class NotingDisplay:
    def __init__(self, fail: bool = False) -> None:
        self.noted: list[Any] = []
        self.fail = fail

    def display_note_identity(self, identity: Any) -> None:
        if self.fail:
            raise RuntimeError("the display's cache broke")
        self.noted.append(identity)


@pytest.mark.parametrize("fail", [False, True])
def test_engine_info_notes_every_identity_on_the_sessions_display(fail: bool):
    pack = FakePack(identity=BoardIdentity(board_type="fake", shell_id="0x1",
                                           features=("lcd_mirror",), harness_impl="linux"))
    engine = Engine(EngineConfig(state_dir=state_dir()), packs={"fake": pack})
    try:
        engine.open(candidate("fake@one"), note="fix-pack-1")
        session = pack.opened[-1]
        session.display = NotingDisplay(fail=fail)
        info = engine.info("fake@one")                    # twin: a failing hook never fails info
        assert info.identity.features == ("lcd_mirror",)
        session.set_identity(BoardIdentity(board_type="fake", shell_id="0x1", features=(),
                                           harness_impl="linux"))
        engine.info("fake@one")
        if not fail:
            assert [i.features for i in session.display.noted] == [("lcd_mirror",), ()]
    finally:
        engine.close_all()


# --- 4. the daemon: the events that mean another image ----------------------------------------------


class ForgettingAdapter(FakeDisplayAdapter):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.forgot: list[str] = []

    def display_forget(self, why: str = "") -> None:
        self.forgot.append(why)


def settle_forgets(rig: Any) -> list[str]:
    for t in list(getattr(rig.daemon.daemon, "display_forgets", [])):
        t.join(5)
    return rig.pack.adapter.forgot


FORGETS = [
    ("controller.reboot", {"phase": "up"}, "rebooted"),
    ("power.cycle", {"phase": "up"}, "power cycled"),
    ("board.claim", {"host_key": {"match": False, "pinned": "SHA256:a", "reported": "SHA256:b"}},
     "host key changed"),
]


@pytest.mark.parametrize("topic, data, words", FORGETS, ids=[f[0] for f in FORGETS])
def test_the_daemon_forgets_the_facts_on_a_reboot_or_a_new_host_key(topic: str, data: dict,
                                                                   words: str):
    with display_rig(ForgettingAdapter(None)) as rig, \
            httpx.Client(base_url=rig.daemon.base_url, headers=headers(TOKEN),
                         trust_env=False, timeout=30.0) as c:
        assert c.get(f"{bid_path()}/display").status_code == 200   # the adapter is known
        rig.engine.bus.publish(Event(topic, BOARD, data))
        got = settle_forgets(rig)
        assert len(got) == 1 and words in got[0], got
        # the twins: another board's event, and a claim refresh whose key still matches
        rig.engine.bus.publish(Event(topic, "fake@elsewhere", data))
        rig.engine.bus.publish(Event("board.claim", BOARD, {"host_key": {"match": True}}))
        assert len(settle_forgets(rig)) == 1


def test_a_board_changing_job_forgets_as_it_starts_and_ends_and_a_deploy_does_not():
    with display_rig(ForgettingAdapter(None)) as rig, \
            httpx.Client(base_url=rig.daemon.base_url, headers=headers(TOKEN),
                         trust_env=False, timeout=30.0) as c:
        c.get(f"{bid_path()}/display")
        bus = rig.engine.bus
        bus.publish(Event("job.started", BOARD, {"job": "j1", "kind": "update_harness"}))
        bus.publish(Event("job.done", BOARD, {"job": "j1", "result": {}}))
        assert settle_forgets(rig) == ["the harness image was updated"] * 2
        bus.publish(Event("job.started", BOARD, {"job": "j2", "kind": "deploy"}))  # the twin
        bus.publish(Event("job.done", BOARD, {"job": "j2", "result": {}}))
        assert len(settle_forgets(rig)) == 2


def test_an_identity_that_loses_the_engine_ends_an_open_display_with_the_gate():
    gate = D.NO_ENGINE
    with FakeLcdMirror() as board, display_rig(FakeDisplayAdapter(board)) as rig, \
            Tab(rig.daemon.display_ws()) as tab:
        tab.pump_until(lambda t: t.state == "live", what="live")
        rig.engine.bus.publish(Event("board.identity", BOARD, {"features": []}))  # twin: no gate
        time.sleep(0.5)
        assert rig.status()["state"] == "live"
        rig.pack.adapter.gate = gate                         # what the new identity says now
        rig.engine.bus.publish(Event("board.identity", BOARD, {"features": []}))
        wait_for(lambda: rig.status()["state"] == "down" and rig.status()["reason"] == gate,
                 what="ended with the gate")
        tab.pump(1.0)
        assert tab.statuses[-1]["reason"] == gate
