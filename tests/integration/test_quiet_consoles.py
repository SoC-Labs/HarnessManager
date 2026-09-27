"""QUIET-POLL follow-up 1: consoles on a board whose hub lease is someone else's.

The console broker over FakeShell's uart0 behind ``SingleClientProxy`` (one client, and it
counts every connection). While ``lease_holder`` names someone else: a live connection is
kept until it drops, then no re-dial ("paused: lease held by X") until the lease is ours or
free; a new explicit console is refused naming the holder. Each rule has its twin.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from harness_manager.core.errors import HeldError
from harness_manager.core.events import Event, EventBus
from harness_manager.services import pty as _pty
from harness_manager.services.console import ConsoleBroker
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t4_console_rig import EventLog, SingleClientProxy, read_until
from tests.fakes.virtual_board import VirtualMps3

BANNER = b"nanosoc boot\n"
ALICE = "alice@lab-pc"


class Lease:
    """The daemon's ``lease_holder``: the holder when the lease is someone else's, else ""."""

    def __init__(self) -> None:
        self.holder = ""
        self.asked = 0

    def __call__(self, _board_id: str) -> str:
        self.asked += 1
        return self.holder


@pytest.fixture
def proxy(vboard: VirtualMps3) -> Iterator[SingleClientProxy]:
    p = SingleClientProxy(vboard.console_ports["uart0"])
    yield p
    p.close()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def lease() -> Lease:
    return Lease()


def make_broker(bus: EventBus, lease: Lease, recheck_s: float = 0.2) -> ConsoleBroker:
    b = ConsoleBroker(bus, backoff=(0.05, 0.2), lease_recheck_s=recheck_s)
    b.lease_holder = lease
    return b


@pytest.fixture
def broker(bus: EventBus, lease: Lease) -> Iterator[ConsoleBroker]:
    b = make_broker(bus, lease)
    yield b
    b.shutdown()


@pytest.fixture
def session(vboard: VirtualMps3, proxy: SingleClientProxy):
    ports = dict(vboard.console_ports, uart0=proxy.port)
    pack = Mps3Pack(console_ports=ports, console_pace_s=0.0)
    return pack.open(pack.candidate_for_host(vboard.shell_endpoint))


def wait(pred, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def paused(log: EventLog, mark: int) -> Event:
    return log.wait_for(lambda e: e.topic == "console.state" and e.data["state"] == "paused",
                        after=mark)


def test_a_live_console_is_kept_and_never_re_dialled_while_the_lease_is_elsewhere(
        broker, session, proxy, bus, lease):
    log = EventLog(bus)
    sub = broker.subscribe(session, "uart0")
    assert read_until(sub, BANNER) == BANNER and proxy.accepted == 1
    lease.holder = ALICE                              # the lease moves on
    time.sleep(0.5)
    assert broker.state(session.candidate.board_id, "uart0") == "up", "kept until it drops"
    mark = log.mark()
    proxy.drop()                                      # the board drops it (a swap, a reset)
    ev = paused(log, mark)
    assert ev.data["detail"] == f"paused: lease held by {ALICE}"
    time.sleep(1.2)                                   # ~6 back-off steps and ~6 lease looks
    assert proxy.accepted == 1, "no re-dial while the lease is someone else's"
    assert broker.state(session.candidate.board_id, "uart0") == "paused"
    # It resumes once the lease is ours or free.
    lease.holder = ""
    bus.publish(Event("lease.state", session.candidate.board_id, {"state": "held"}))
    wait(lambda: proxy.accepted == 2)
    assert read_until(sub, BANNER) == BANNER


def test_twin_without_someone_elses_lease_a_dropped_console_re_dials(broker, session, proxy):
    sub = broker.subscribe(session, "uart0")
    assert read_until(sub, BANNER) == BANNER
    proxy.drop()
    wait(lambda: proxy.accepted == 2)


def test_a_lease_change_event_resumes_it_at_once(vboard, session, proxy, bus, lease):
    b = make_broker(bus, lease, recheck_s=30.0)       # only the event can wake it in time
    try:
        lease.holder = ALICE
        b.subscribe(session, "uart0", explicit=False)  # a PTY reopened after an update
        wait(lambda: b.state(session.candidate.board_id, "uart0") == "paused")
        time.sleep(0.4)
        assert proxy.accepted == 0
        lease.holder = ""
        bus.publish(Event("lease.state", session.candidate.board_id, {"state": "released"}))
        wait(lambda: proxy.accepted == 1, timeout=3.0)
    finally:
        b.shutdown()


def test_twin_an_event_for_another_board_does_not_wake_it(vboard, session, proxy, bus, lease):
    b = make_broker(bus, lease, recheck_s=30.0)
    try:
        lease.holder = ALICE
        b.subscribe(session, "uart0", explicit=False)
        wait(lambda: b.state(session.candidate.board_id, "uart0") == "paused")
        asked = lease.asked
        lease.holder = ""
        bus.publish(Event("lease.state", "mps3@10.9.9.9:6900", {"state": "released"}))
        time.sleep(0.5)
        assert proxy.accepted == 0 and lease.asked == asked
    finally:
        b.shutdown()


def test_an_explicit_console_is_refused_naming_the_holder(broker, session, proxy, lease):
    lease.holder = ALICE
    with pytest.raises(HeldError) as got:
        broker.subscribe(session, "uart0")
    assert got.value.holder == ALICE and ALICE in got.value.message
    with pytest.raises(HeldError):
        broker.export_tcp(session, "uart0")
    if _pty.supported():
        with pytest.raises(HeldError):
            broker.pty(session, "uart0")
    time.sleep(0.3)
    assert proxy.accepted == 0, "nothing was dialled"


def test_twin_a_board_without_a_hub_or_our_own_lease_opens_as_before(broker, session, proxy,
                                                                      lease):
    lease.holder = ""                                 # no hub, our lease, or a free board
    sub = broker.subscribe(session, "uart0")
    assert read_until(sub, BANNER) == BANNER and proxy.accepted == 1


def test_twin_joining_a_live_console_is_not_refused(broker, session, proxy, lease):
    a = broker.subscribe(session, "uart0")
    assert read_until(a, BANNER) == BANNER
    lease.holder = ALICE
    b = broker.subscribe(session, "uart0")            # no new board connection
    assert read_until(b, BANNER) == BANNER            # the scrollback replay
    assert proxy.accepted == 1
