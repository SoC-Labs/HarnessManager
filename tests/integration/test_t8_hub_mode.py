"""T8: today's LeaseService (lane L1) driven through the REST hub client, unchanged.

The drop-in claim, end to end: the service that heartbeats a board's lease cannot tell
the REST client from the SSH one. Only the fake hub on 127.0.0.1 is reached.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import pytest

from harness_manager.core.errors import ExitCode, RefusedError
from harness_manager.core.events import EventBus
from harness_manager.services.lease import LeaseService
from harness_manager.transports import hub_events
from tests.fakes.t8_hub_rest import FakeFpgahub, client_for, wait_until

BID = "mps3@192.168.10.101:6900"


@pytest.fixture(autouse=True)
def _no_fpgahub_login(tmp_path, monkeypatch):
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)


@dataclass
class Adapter:
    """``session.hub`` as ``LeaseService`` sees it: host, target, client."""

    client: Any

    @property
    def host(self) -> str:
        return self.client.host

    @property
    def target(self) -> str:
        return self.client.target


@pytest.fixture
def bg_queue():
    stops: list[tuple[threading.Event, threading.Thread]] = []

    def queue(client):
        stop = threading.Event()

        def sleep(s):
            if stop.wait(s):
                raise RuntimeError("stopped")

        def run():
            try:
                client.lease_acquire("q", ttl=600, sleep=sleep)
            except Exception:  # noqa: BLE001 - stopped or promoted-then-closed
                pass

        t = threading.Thread(target=run, daemon=True)
        t.start()
        stops.append((stop, t))

    yield queue
    for stop, t in stops:
        stop.set()
        t.join(5)


@pytest.fixture
def rig(tmp_path):
    with FakeFpgahub() as hub:
        bus = EventBus()
        states: list[dict] = []
        bus.subscribe("lease.state", lambda ev: states.append(ev.data))
        svc = LeaseService(tmp_path / "state", bus, tick_s=3600)
        yield hub, bus, svc, states
        svc.close()


def test_the_lease_service_queues_then_holds_heartbeats_and_releases_over_rest(rig):
    hub, _, svc, states = rig
    alice = client_for(hub, hub.add_token("alice"))
    bob = Adapter(client_for(hub, hub.add_token("bob")))
    held, _ = alice.lease_acquire("x", ttl=600)
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(r=svc.acquire(
        bob, board_id=BID, ttl_s=600, holder=bob.client.principal(), poll_s=0.2)))
    t.start()
    wait_until(lambda: any(s["state"] == "queued" for s in states))
    alice.lease_release(held.token, "x")
    t.join(5)
    assert out["r"]["lease"]["holder"] == "bob@mapstone-dev"
    assert states[-1]["state"] == "held"
    view = svc.view(bob)
    assert view["lease"]["mine"] is True and view["hub"] == "127.0.0.1"
    svc.beat_due(force=True)
    assert [e for e in hub.emitted() if e == "lease.heartbeat"]
    assert svc.release(bob, board_id=BID)["ok"] is True
    assert states[-1]["state"] == "released" and not alice.lease_show().held


def test_a_force_release_to_a_waiter_is_seen_as_lost_at_the_next_heartbeat(rig, bg_queue):
    hub, _, svc, states = rig
    bob = Adapter(client_for(hub, hub.add_token("bob")))
    alice = client_for(hub, hub.add_token("alice"))
    admin = client_for(hub, hub.add_token("david", "admin"))
    svc.acquire(bob, board_id=BID, ttl_s=600, holder=bob.client.principal())
    bg_queue(alice)
    wait_until(lambda: alice.lease_status().queue)
    admin.lease_revoke("taken for the demo")
    svc.beat_due(force=True)
    assert states[-1]["state"] == "lost"
    assert svc.store.get(bob.host, bob.target) is None


def test_a_force_release_with_nobody_waiting_reads_as_expired_at_the_heartbeat(rig):
    """The twin, and a limit: the hub answers "no current lease" for both. Only the event
    stream (lease.admin_revoked) or the history (lease.revoked) tells a revoke apart."""
    hub, _, svc, states = rig
    bob = Adapter(client_for(hub, hub.add_token("bob")))
    admin = client_for(hub, hub.add_token("david", "admin"))
    svc.acquire(bob, board_id=BID, ttl_s=600, holder=bob.client.principal())
    admin.lease_revoke("taken for the demo")
    svc.beat_due(force=True)
    assert states[-1]["state"] == "expired"
    assert any(r["event"] == "lease.revoked" for r in bob.client.lease_history())


def test_a_read_only_token_cannot_take_the_board_through_the_service(rig):
    hub, _, svc, states = rig
    carol = Adapter(client_for(hub, hub.add_token("carol", "read")))
    with pytest.raises(RefusedError) as ei:
        svc.acquire(carol, board_id=BID, ttl_s=600)
    assert ei.value.code == ExitCode.REFUSED
    assert not any(st["state"] == "held" for st in states)


def test_sse_carries_the_force_release_to_the_bus_before_any_heartbeat(rig):
    hub, bus, svc, _ = rig
    bob = Adapter(client_for(hub, hub.add_token("bob")))
    admin = client_for(hub, hub.add_token("david", "admin"))
    svc.acquire(bob, board_id=BID, ttl_s=600, holder=bob.client.principal(), heartbeat=False)
    seen: list = []
    bus.subscribe(hub_events.EVENT_TOPIC, seen.append)
    stream = hub_events.attach_bus(bus, BID, bob.client, backoff_s=(0.05,))
    try:
        wait_until(lambda: hub.stream_count() == 1)
        admin.lease_revoke("taken for the demo")
        ev = wait_until(lambda: next((e for e in seen
                                      if e.data["type"] == "lease.admin_revoked"), None))
        assert ev.board_id == BID and ev.data["data"]["prior_holder"] == "bob@mapstone-dev"
        assert "taken for the demo" in ev.data["data"]["reason"]
    finally:
        stream.close()
