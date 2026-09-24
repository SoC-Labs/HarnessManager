"""LR-A: the hub share fixes from Q2's findings (hub.py). Each check has a twin.

1. ``ShareRelay`` closes both sockets of each relayed connection when it ends (it kept
   them until the board closed: two fds per console reconnection).
2. ``open_hub_share`` waits for OUR just-closed connection to leave the share instead of
   refusing itself the write slot; anyone else attached still makes the port read-only.

Everything runs against L1's fake lab (FakeHub, FakeSsh, a VirtualMps3); nothing reaches
a hub, runs ssh, or runs fpgahub.
"""

from __future__ import annotations

import socket
import time

import pytest

from harness_manager_mps3 import hub as hubmod
from tests.fakes.l1_fake_hub import FakeLane
from tests.fakes.l1_rig import HUB, MCC_TTY, TARGET, lab
from tests.fakes.lr_hub import LaggingShareServer
from tests.fakes.virtual_board import VirtualMps3

LANE_TTY = "/dev/mps3_01_pl/tty_02"


def _wait(predicate, timeout: float = 10.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(autouse=True)
def _fresh_share_state():
    hubmod.SHARES._closed_at.clear()
    yield
    hubmod.SHARES._closed_at.clear()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=tmp_path / "state") as r:
        yield r


def share_lists(r) -> int:
    return sum(1 for c in r.hub.calls if c[:3] == ["fpgahub", "share", "list"])


def open_mcc():
    return hubmod.open_hub_share(hubmod.ShareRef(HUB, TARGET, MCC_TTY).url.split("://", 1)[1])


def recv_until(sock: socket.socket, needle: bytes, timeout: float = 5.0) -> bytes:
    sock.settimeout(timeout)
    got = b""
    while needle not in got:
        chunk = sock.recv(4096)
        if not chunk:
            break
        got += chunk
    return got


# -- 1. the relay's sockets -------------------------------------------------------------------------


def lane_relay(r) -> hubmod.ShareRelay:
    r.hub.add_tty(LANE_TTY, FakeLane(banner=b""), share=True)
    return hubmod.SHARES.relay(hubmod.ShareRef(HUB, TARGET, LANE_TTY))


def test_a_console_that_leaves_takes_both_relay_sockets_with_it(rig):
    relay = lane_relay(rig)
    share = rig.hub.shares[LANE_TTY]
    for i in range(5):
        _wait(lambda: share.readers == 0, what="the previous client to leave the share")
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as c:
            c.sendall(f"ping {i}\n".encode())
            assert f"ping {i}".encode() in recv_until(c, f"ping {i}".encode())
            _wait(lambda: len(relay._conns) == 2, what="the connection to be relayed")
            pair = list(relay._conns)
        _wait(lambda pair=pair: all(s.fileno() == -1 for s in pair),
              what="both sockets to be closed, not just forgotten")
        assert not relay._conns


def test_negative_twin_a_live_console_is_untouched_until_the_board_closes(rig):
    relay = lane_relay(rig)
    live = socket.create_connection(("127.0.0.1", relay.port), timeout=5)
    try:
        live.sendall(b"first\n")
        recv_until(live, b"first")
        for _ in range(3):                                   # others come and go
            socket.create_connection(("127.0.0.1", relay.port), timeout=5).close()
        _wait(lambda: len(relay._conns) == 2, what="only the live connection to remain")
        live.sendall(b"second\n")
        assert b"second" in recv_until(live, b"second")
        hubmod.SHARES.close_for(HUB, TARGET)                 # the board closes: now it goes
        assert recv_until(live, b"never", timeout=5) == b""
        assert not relay._conns
    finally:
        live.close()


def test_a_share_that_stops_ends_the_console_and_frees_both_sockets(rig):
    relay = lane_relay(rig)
    with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as c:
        c.sendall(b"hello\n")
        recv_until(c, b"hello")
        pair = _wait(lambda: list(relay._conns) if len(relay._conns) == 2 else None,
                     what="the connection to be relayed")
        rig.hub.shares.pop(LANE_TTY).close()                 # david stops the share
        assert recv_until(c, b"never", timeout=5) == b""     # the console sees the end
    _wait(lambda: all(s.fileno() == -1 for s in pair), what="both relay sockets to be closed")
    assert not relay._conns


# -- 2. the write slot after our own close -------------------------------------------------------------


def test_back_to_back_opens_get_the_write_slot_and_the_bytes_reach_the_tty(rig):
    share = rig.hub.shares[MCC_TTY]
    for i in range(5):
        port = open_mcc()
        assert not port.read_only, port.read_only_reason
        cmd = f"CMD{i}\r".encode()
        port.write(cmd)
        _wait(lambda cmd=cmd: cmd in share.written, what="the write to reach the TTY")
        port.close()                                         # and at once the next one
    assert share.dropped_writes == 0


def attach_david(rig) -> socket.socket:
    """Someone else's console on the MCC share (not through us)."""
    share = rig.hub.shares[MCC_TTY]
    david = socket.create_connection(("127.0.0.1", share.port), timeout=5)
    _wait(lambda: share.readers == 1, what="david's console to attach")
    return david


def assert_read_only(port) -> None:
    try:
        assert port.read_only and "write slot" in port.read_only_reason
        with pytest.raises(OSError):
            port.write(b"REBOOT\r")
    finally:
        port.close()


def test_negative_twin_someone_elses_client_is_read_only_at_once(rig):
    david = attach_david(rig)
    try:
        before, t0 = share_lists(rig), time.monotonic()
        assert_read_only(open_mcc())
        assert share_lists(rig) - before == 1                # no waiting on a stranger
        assert time.monotonic() - t0 < hubmod.SLOT_WAIT_S
    finally:
        david.close()


def test_negative_twin_a_stranger_right_after_our_close_is_still_read_only_and_bounded(rig):
    """The hub gives a count, not names: a stranger who attaches just after we closed looks
    like our own lingering connection, so the opener waits, but no longer than SLOT_WAIT_S,
    and still refuses to write."""
    share = rig.hub.shares[MCC_TTY]
    open_mcc().close()
    _wait(lambda: share.readers == 0, what="our connection to leave")
    david = attach_david(rig)
    try:
        t0 = time.monotonic()
        assert_read_only(open_mcc())
        assert time.monotonic() - t0 < hubmod.SLOT_WAIT_S + 1.5
    finally:
        david.close()


def fake_share(readers: int) -> hubmod.ShareInfo:
    return hubmod.ShareInfo(MCC_TTY, "0.0.0.0", 4000, "127.0.0.1:5000", readers)


def settle(monkeypatch, readers_seq: list[int], lingering: int) -> tuple[hubmod.ShareInfo, int, float]:
    """``settle_write_slot`` on a scripted ``share list``, with a fake clock."""
    ref = hubmod.ShareRef(HUB, TARGET, MCC_TTY)
    seq = iter(readers_seq[1:])
    lists = {"n": 0}
    now = {"t": 1000.0}

    def resolve(_ref):
        lists["n"] += 1
        return fake_share(next(seq, readers_seq[-1])), "route"

    monkeypatch.setattr(hubmod, "resolve_share", resolve)
    monkeypatch.setattr(hubmod.SHARES, "lingering", lambda _ref: lingering)
    info, _route = hubmod.settle_write_slot(
        ref, fake_share(readers_seq[0]), "route",
        sleep=lambda s: now.__setitem__("t", now["t"] + s), clock=lambda: now["t"])
    return info, lists["n"], now["t"] - 1000.0


def test_settle_waits_while_every_client_could_be_ours_then_stops(monkeypatch):
    info, lists, waited = settle(monkeypatch, [1, 1, 0], lingering=1)
    assert info.readers == 0 and lists == 2 and waited == pytest.approx(2 * hubmod.SLOT_POLL_S)


def test_negative_twin_settle_gives_up_after_slot_wait_s_and_stays_honest(monkeypatch):
    info, lists, waited = settle(monkeypatch, [1], lingering=1)          # it never leaves
    assert info.readers == 1                                              # so: read-only
    assert waited == pytest.approx(hubmod.SLOT_WAIT_S)
    assert lists == round(hubmod.SLOT_WAIT_S / hubmod.SLOT_POLL_S)


@pytest.mark.parametrize("readers,lingering", [(0, 3), (1, 0), (2, 1)])
def test_negative_twin_settle_does_not_wait_when_it_cannot_be_us(monkeypatch, readers, lingering):
    info, lists, waited = settle(monkeypatch, [readers], lingering=lingering)
    assert (info.readers, lists, waited) == (readers, 0, 0.0)


def test_our_closes_count_for_own_linger_s_and_once_each(rig):
    ref = hubmod.ShareRef(HUB, TARGET, MCC_TTY)
    port = open_mcc()
    port.close()
    port.close()                                             # a second close is not a second one
    assert hubmod.SHARES.lingering(ref) == 1
    hubmod.SHARES._closed_at[ref] = [time.monotonic() - hubmod.OWN_LINGER_S - 0.1]
    assert hubmod.SHARES.lingering(ref) == 0                 # twin: an old close is forgotten
    assert ref not in hubmod.SHARES._closed_at
    assert hubmod.SHARES.lingering(hubmod.ShareRef(HUB, TARGET, LANE_TTY)) == 0


# -- 3. a shared console after a quick reconnect ------------------------------------------------------


LAG_S = 0.5          # how late the fake hub notices our EOF (the real one: through ssh)


def lagging_lane(rig) -> tuple[hubmod.ShareRelay, LaggingShareServer, FakeLane]:
    lane = FakeLane(banner=b"")
    rig.hub.ttys[LANE_TTY] = lane
    rig.hub.shares[LANE_TTY] = LaggingShareServer(lane, LANE_TTY, leave_after_s=LAG_S)
    return hubmod.SHARES.relay(hubmod.ShareRef(HUB, TARGET, LANE_TTY)), rig.hub.shares[LANE_TTY], lane


def type_then_reconnect_and_type(relay: hubmod.ShareRelay) -> tuple[bytes, float]:
    """Type, leave, come straight back and type again (the UI reopening a console)."""
    with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as c:
        c.sendall(b"first\n")
        recv_until(c, b"first")
    _wait(lambda: not relay._conns, what="the relay to close its side")
    t0 = time.monotonic()
    with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as c:
        c.sendall(b"second\n")                              # at once, as a person would
        try:
            got = recv_until(c, b"second", timeout=hubmod.SLOT_WAIT_S + 2 * LAG_S)
        except TimeoutError:
            got = b""
    return got, time.monotonic() - t0


def test_the_first_keys_after_a_quick_reconnect_reach_the_tty(rig):
    relay, share, lane = lagging_lane(rig)
    got, took = type_then_reconnect_and_type(relay)
    assert b"second" in got and b"second" in lane.received
    assert share.dropped_writes == 0
    assert took < hubmod.SLOT_WAIT_S + 1.0                  # the wait is bounded


def test_negative_twin_without_the_settle_the_hub_drops_them(rig, monkeypatch):
    """The code before this fix: the relay connected at once, while the hub still gave
    the write slot to our previous connection, and the keys vanished without a trace."""
    monkeypatch.setattr(hubmod, "settle_write_slot", lambda ref, info, route, **_kw: (info, route))
    relay, share, lane = lagging_lane(rig)
    got, _took = type_then_reconnect_and_type(relay)
    assert b"second" not in got and b"second" not in lane.received
    assert share.dropped_writes >= len(b"second\n")


def test_a_stranger_on_the_lane_share_does_not_delay_the_console(rig):
    relay, share, _lane = lagging_lane(rig)
    stranger = socket.create_connection(("127.0.0.1", share.port), timeout=5)
    try:
        _wait(lambda: share.readers == 1, what="the stranger to attach")
        before, t0 = share_lists(rig), time.monotonic()
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5):
            _wait(lambda: len(relay._conns) == 2, what="the console to be relayed")
            took = time.monotonic() - t0
        assert share_lists(rig) - before == 1 and took < hubmod.SLOT_WAIT_S   # no settle
    finally:
        stranger.close()


def test_the_relay_records_a_close_only_when_the_console_left_first(rig):
    ref = hubmod.ShareRef(HUB, TARGET, LANE_TTY)
    relay, share, _lane = lagging_lane(rig)
    with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as c:
        c.sendall(b"x\n")
        recv_until(c, b"x")
    _wait(lambda: not relay._conns, what="the relay to close its side")
    assert hubmod.SHARES.lingering(ref) == 1                 # the console left: recorded
    _wait(lambda: share.readers == 0, what="the hub to let go")
    hubmod.SHARES._closed_at.clear()
    with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as c:
        c.sendall(b"y\n")
        recv_until(c, b"y")
        rig.hub.shares.pop(LANE_TTY).close()                 # twin: the share ended it
        assert recv_until(c, b"never", timeout=5) == b""
    _wait(lambda: not relay._conns, what="the relay to close its side")
    assert hubmod.SHARES.lingering(ref) == 0                 # the hub let go: nothing to wait for
