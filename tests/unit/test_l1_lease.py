"""L1: the lease service and the hub client against the fake fpgahub. Each check has a twin."""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from harness_manager.core.errors import AbsentError, ActionFailedError, ExitCode, UnavailableError
from harness_manager.core.events import EventBus
from harness_manager.services.lease import LeaseService, LeaseStore, StoredLease, heartbeat_interval
from harness_manager_mps3 import hub as hubmod
from tests.fakes.l1_fake_hub import EXPIRES, FakeHub

HUB = "mapstone-dev.ecs.soton.ac.uk"


class Hub:
    """A ``session.hub`` stand-in over the fake fpgahub."""

    def __init__(self, fake: FakeHub) -> None:
        self.host = HUB
        self.target = "mps3_01_pl"
        self.client = hubmod.HubClient(HUB, "mps3_01_pl", runner=fake)


@pytest.fixture
def fake():
    f = FakeHub()
    yield f
    f.close()


@pytest.fixture
def events():
    bus = EventBus()
    seen: list[dict] = []
    # ``board`` here is the event's board id; LEASE-BOARD's ``board`` in the data (the hub's
    # physical board) is kept as ``hub_board``.
    bus.subscribe("lease.*", lambda ev: seen.append({**ev.data, "board": ev.board_id,
                                                     "hub_board": ev.data.get("board")}))
    return bus, seen


def svc(tmp_path: Path, bus=None, **kw) -> LeaseService:
    return LeaseService(tmp_path / "state", bus, **kw)


def test_acquire_stores_the_token_privately_and_says_held(tmp_path, fake, events):
    bus, seen = events
    s = svc(tmp_path, bus)
    out = s.acquire(Hub(fake), board_id="b1", ttl_s=600, holder="hm-test", heartbeat=False)
    # LEASE-BOARD: ``board`` is additive; None here, as nothing has asked this hub for it yet
    assert out["lease"] == {"target": "mps3_01_pl", "board": None, "holder": "hm-test",
                            "expires_at": EXPIRES, "mine": True}
    assert "token" not in out["lease"]                          # the token never leaves the store
    stored = s.store.get(HUB, "mps3_01_pl")
    assert stored is not None and stored.token.startswith("tok-") and stored.ttl_s == 600
    path = next((tmp_path / "state" / "leases").glob("*.json"))
    if os.name != "nt":                                         # Windows has no POSIX modes
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert seen[-1]["state"] == "held" and seen[-1]["board"] == "b1"
    assert ["fpgahub", "lease", "acquire", "mps3_01_pl", "--holder", "hm-test", "--tier",
            "interactive", "--ttl", "600"] in fake.calls


def test_a_contended_acquire_queues_then_holds(tmp_path, fake, events):
    bus, seen = events
    fake.queue_first = 2
    phases: list[tuple[str, int]] = []
    s = svc(tmp_path, bus)
    s.acquire(Hub(fake), board_id="b1", ttl_s=600, holder="hm-test", poll_s=0.01, heartbeat=False,
              progress=lambda p, d, t: phases.append((p, d)))
    assert [e["state"] for e in seen] == ["queued", "queued", "held"]
    assert ("queued", 1) in phases and phases[-1] == ("held", 1)


def test_negative_twin_a_cancelled_wait_removes_its_queue_entry(tmp_path, fake):
    fake.queue_first = 10**6                                    # never granted
    s = svc(tmp_path)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    with pytest.raises(ActionFailedError) as exc:
        s.acquire(Hub(fake), board_id="b1", ttl_s=600, holder="hm-test", poll_s=0.05,
                  cancel=cancel, heartbeat=False)
    assert "queue entry was removed" in exc.value.message
    assert fake.queue == [] and ["fpgahub", "lease", "cancel", "mps3_01_pl", "--holder",
                                 "hm-test"] in fake.calls
    assert s.store.get(HUB, "mps3_01_pl") is None


def test_the_view_says_whose_lease_it_is(tmp_path, fake):
    clock = [0.0]
    s = svc(tmp_path, clock=lambda: clock[0])
    # LR-B extends the view (queue, request, incoming, taken); L1's two keys are unchanged.
    assert {k: v for k, v in s.view(Hub(fake)).items() if k in ("lease", "hub")} == {
        "lease": None, "hub": HUB}
    s.acquire(Hub(fake), ttl_s=600, holder="hm-test", heartbeat=False)
    mine = s.view(Hub(fake))["lease"]
    assert mine["mine"] and mine["holder"] == "hm-test" and mine["expires_at"] == EXPIRES
    fake.steal("b0-linux")                                      # the twin: someone else's lease
    assert s.view(Hub(fake))["lease"]["holder"] == "hm-test"    # a UI poll reuses the answer ...
    clock[0] += 11.0                                            # ... for 10 s
    theirs = s.view(Hub(fake))["lease"]
    assert not theirs["mine"] and theirs["holder"] == "b0-linux"
    assert s.view(None)["lease"] is None and s.view(None)["hub"] is None   # not behind a hub


def test_heartbeat_extends_and_reports_expired_or_lost(tmp_path, fake, events):
    bus, seen = events
    s = svc(tmp_path, bus, heartbeat_s=0.0)
    hub = Hub(fake)
    s.acquire(hub, board_id="b1", ttl_s=600, holder="hm-test", heartbeat=False)
    s.track("b1", hub, announced=True)
    s.beat_due(force=True)
    assert fake.heartbeats == 1 and s.store.get(HUB, "mps3_01_pl") is not None
    fake.expire()                                               # the lease lapsed on the hub
    s.beat_due(force=True)
    assert seen[-1]["state"] == "expired" and s.store.get(HUB, "mps3_01_pl") is None
    assert s.tracked() == []
    s.acquire(hub, board_id="b1", ttl_s=600, holder="hm-test", heartbeat=False)
    s.track("b1", hub, announced=True)
    fake.steal()                                                # someone else holds it now
    s.beat_due(force=True)
    assert seen[-1]["state"] == "lost"
    s.close()


def test_negative_twin_an_unanswering_hub_keeps_the_lease_and_retries(tmp_path, fake):
    s = svc(tmp_path, heartbeat_s=0.0)
    hub = Hub(fake)
    s.acquire(hub, board_id="b1", ttl_s=600, holder="hm-test", heartbeat=False)
    s.track("b1", hub)
    fake.fail_with = "ssh: connect to host mapstone-dev port 22: Connection timed out"
    s.beat_due(force=True)
    assert s.store.get(HUB, "mps3_01_pl") is not None and s.tracked() == ["b1"]
    s.beat_due(force=True)
    assert fake.heartbeats == 1
    s.close()


def test_the_heartbeat_thread_runs_while_tracked(tmp_path, fake):
    s = svc(tmp_path, tick_s=0.02, heartbeat_s=0.05)
    hub = Hub(fake)
    s.acquire(hub, board_id="b1", ttl_s=600, holder="hm-test")
    deadline = time.monotonic() + 3
    while fake.heartbeats < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fake.heartbeats >= 2
    s.untrack("b1")
    n = fake.heartbeats
    time.sleep(0.2)
    assert fake.heartbeats <= n + 1                             # stops once untracked
    s.close()


def test_release_needs_our_token(tmp_path, fake, events):
    bus, seen = events
    s = svc(tmp_path, bus)
    hub = Hub(fake)
    with pytest.raises(AbsentError) as exc:                     # nothing of ours to release
        s.release(hub, board_id="b1")
    assert "not leased" in exc.value.message
    s.acquire(hub, board_id="b1", ttl_s=600, holder="hm-test", heartbeat=False)
    out = s.release(hub, board_id="b1")
    assert out["released"]["holder"] == "hm-test" and fake.current is None
    assert seen[-1]["state"] == "released" and s.store.get(HUB, "mps3_01_pl") is None


def test_a_board_without_a_hub_has_no_lease_capability(tmp_path):
    with pytest.raises(UnavailableError) as exc:
        svc(tmp_path).acquire(None, board_id="b1")
    assert exc.value.code == ExitCode.UNAVAILABLE and "boards.toml" in exc.value.message


def test_acquiring_a_lease_we_hold_is_not_a_second_queue_entry(tmp_path, fake):
    s = svc(tmp_path)
    hub = Hub(fake)
    s.acquire(hub, ttl_s=600, holder="hm-test", heartbeat=False)
    n = len([c for c in fake.calls if c[2] == "acquire"])
    again = s.acquire(hub, ttl_s=600, holder="hm-test", heartbeat=False)
    assert again["already"] and len([c for c in fake.calls if c[2] == "acquire"]) == n


def test_heartbeat_interval_is_a_third_of_the_ttl_within_bounds():
    assert heartbeat_interval(3600) == 600.0
    assert heartbeat_interval(300) == 100.0
    assert heartbeat_interval(60) == 30.0                       # never more often than 30 s
    assert heartbeat_interval(7200) == 600.0                    # never less often than 10 min


def test_the_store_ignores_a_corrupt_file(tmp_path):
    store = LeaseStore(tmp_path)
    store.put(StoredLease(HUB, "mps3_01_pl", "h", "tok", 600))
    assert store.get(HUB, "mps3_01_pl") is not None
    next(tmp_path.glob("*.json")).write_text("{not json")
    assert store.get(HUB, "mps3_01_pl") is None


# --- the hub client ---------------------------------------------------------------------------


def test_share_list_and_start_parse_the_hub_cli(fake):
    from tests.fakes.fake_mcc import FakeMcc

    fake.add_tty("/dev/mps3_01_pl/tty_02", FakeMcc())
    client = hubmod.HubClient(HUB, runner=fake)
    assert client.share_list() == []
    started = client.share_start("/dev/mps3_01_pl/tty_02")
    assert started.host == "0.0.0.0" and started.remote_host == "127.0.0.1"
    (listed,) = client.share_list()
    assert (listed.tty, listed.port, listed.readers, listed.running) == (
        "/dev/mps3_01_pl/tty_02", started.port, 0, True)
    assert client.share_start("/dev/mps3_01_pl/tty_02").port == started.port   # returns the existing
    with pytest.raises(Exception) as exc:                       # the twin: no such device
        client.share_start("/dev/mps3_01_pl/tty_09")
    assert "tty_09" in str(exc.value)
    # MCC-FIX twin: the MCC's tty_00 is refused before the hub is asked
    from harness_manager.core.errors import RefusedError

    fake.add_tty("/dev/mps3_01_pl/tty_00", FakeMcc())
    calls = len(fake.calls)
    with pytest.raises(RefusedError, match="never starts or uses an fpgahub share"):
        client.share_start("/dev/mps3_01_pl/tty_00")
    assert len(fake.calls) == calls and "/dev/mps3_01_pl/tty_00" not in fake.shares


def test_hub_errors_map_to_exit_codes():
    cases = {
        "lease show failed: dam1n19@hub: Permission denied (publickey).": ExitCode.UNREACHABLE,
        "lease show failed: [Errno 13] Permission denied: '/run/fpgahub/fpgahub.sock'": ExitCode.UNREACHABLE,
        "lease show failed: HTTP 404: no such board: 'mps3_01'": ExitCode.ABSENT,
        "lease heartbeat failed: HTTP 409: holder or token does not match current lease": ExitCode.HELD,
        "lease heartbeat failed: HTTP 409: no current lease for board": ExitCode.HELD,
    }
    for text, code in cases.items():
        assert hubmod.classify_hub_error("x", HUB, "mps3_01_pl", text).code == code, text
    group = hubmod.classify_hub_error("x", HUB, "t", "[Errno 13] Permission denied: '/run/x.sock'")
    assert "'fpga' group" in group.hint
    key = hubmod.classify_hub_error("x", HUB, "t", "Permission denied (publickey).")
    assert "BatchMode" in key.hint
