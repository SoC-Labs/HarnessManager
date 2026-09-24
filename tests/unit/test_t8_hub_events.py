"""T8: fpgahub's SSE stream on the engine bus. Every check has a negative twin.

The fake hub (``tests/fakes/t8_hub_rest.py``) streams exactly as fpgahub 0.3.0 does:
``:connected``, then chunked ``event:``/``data:`` frames. Only 127.0.0.1 is reached.
"""

from __future__ import annotations

import threading
import time

import pytest

from harness_manager.core.events import EventBus
from harness_manager.transports.hub_events import (
    EVENT_TOPIC,
    STREAM_TOPIC,
    HubEventStream,
    attach_bus,
    parse_sse,
)
from tests.fakes.t8_hub_rest import FakeFpgahub, client_for, wait_until

FAST = (0.05, 0.1)


@pytest.fixture(autouse=True)
def _no_fpgahub_login(tmp_path, monkeypatch):
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)


@pytest.fixture
def hub():
    with FakeFpgahub() as h:
        yield h


@pytest.fixture
def streams():
    made: list[HubEventStream] = []
    yield made
    for s in made:
        s.close()


def _bus_log(bus: EventBus) -> list:
    seen: list = []
    bus.subscribe("hub.*", seen.append)
    return seen


# --- the parser ---------------------------------------------------------------------------------


def test_parse_sse_reads_fpgahub_frames_and_skips_the_connected_comment():
    text = (":connected\n\n"
            'event: lease.queued\ndata: {"type": "lease.queued", "ts": "t1", '
            '"data": {"board": "b", "position": 1}}\n\n'
            "event: x\ndata: {\"type\": \"x\",\ndata:  \"ts\": \"t2\", \"data\": {}}\n\n")
    got = list(parse_sse(text.splitlines(keepends=True)))
    assert got == [{"type": "lease.queued", "ts": "t1", "data": {"board": "b", "position": 1}},
                   {"type": "x", "ts": "t2", "data": {}}]


def test_parse_sse_keeps_a_frame_it_cannot_decode_as_raw_text():
    got = list(parse_sse(["event: odd\n", "data: not json\n", "\n", ": comment\n", "\n"]))
    assert got == [{"type": "odd", "ts": "", "data": {"raw": "not json"}}]


# --- queue, promote, revoke: seen through SSE -----------------------------------------------------


def test_queue_promote_and_revoke_reach_the_bus_through_sse_in_order(hub, streams):
    ta, tb, td = hub.add_token("alice"), hub.add_token("bob"), hub.add_token("david", "admin")
    a, b, d = client_for(hub, ta), client_for(hub, tb), client_for(hub, td)
    bus = EventBus()
    seen = _bus_log(bus)
    streams.append(attach_bus(bus, "mps3@lab", a, backoff_s=FAST))
    wait_until(lambda: hub.stream_count() == 1)

    a.lease_acquire("x", ttl=600)
    got: dict = {}
    t = threading.Thread(target=lambda: got.update(r=b.lease_acquire("y", ttl=600)))
    t.start()
    wait_until(lambda: any(e.data.get("type") == "lease.queued" for e in seen))
    t0 = time.monotonic()
    d.lease_revoke("force-released by bob@mapstone-dev via Harness Manager")
    wait_until(lambda: any(e.data.get("type") == "lease.admin_revoked" for e in seen))
    assert time.monotonic() - t0 < 1.0                  # real time, not a 10 s poll
    t.join(5)

    types = [e.data["type"] for e in seen if e.topic == EVENT_TOPIC]
    assert types[:2] == ["lease.acquired", "lease.queued"]
    assert types.index("lease.revoked") < types.index("lease.promoted") \
        < types.index("lease.admin_revoked")
    admin = next(e for e in seen if e.data.get("type") == "lease.admin_revoked")
    assert admin.board_id == "mps3@lab" and admin.data["target"] == "mps3_01_pl"
    assert admin.data["data"]["by"] == "token:david"
    assert admin.data["data"]["prior_holder"] == "alice@mapstone-dev"
    promoted = next(e for e in seen if e.data.get("type") == "lease.promoted")
    assert promoted.data["data"]["holder"] == "bob@mapstone-dev"
    assert "token" not in promoted.data["data"]            # fpgahub never streams a token
    assert got["r"][0].holder == "bob@mapstone-dev"
    # the client kept what history cannot give back
    assert any(r.get("by") == "token:david" for r in a.lease_history())


def test_events_for_another_target_are_not_published(hub, streams):
    ta = hub.add_token("alice")
    a = client_for(hub, ta)
    other = client_for(hub, ta, target="kr260_01_pl")
    bus = EventBus()
    seen = _bus_log(bus)
    streams.append(attach_bus(bus, "mps3@lab", a, backoff_s=FAST))
    wait_until(lambda: hub.stream_count() == 1)
    other.lease_acquire("x", ttl=600)
    a.lease_show()
    time.sleep(0.3)
    assert [e for e in seen if e.topic == EVENT_TOPIC] == []
    a.lease_acquire("x", ttl=600)
    wait_until(lambda: [e for e in seen if e.topic == EVENT_TOPIC])


# --- drop and reconnect --------------------------------------------------------------------------------


def test_a_dropped_stream_reconnects_resyncs_and_keeps_delivering(hub, streams):
    ta = hub.add_token("alice")
    a = client_for(hub, ta)
    states, events, resyncs = [], [], []
    s = HubEventStream(a, on_event=events.append, on_state=lambda st: states.append(st["state"]),
                       on_resync=lambda: resyncs.append(1), backoff_s=FAST).start()
    streams.append(s)
    wait_until(lambda: s.state == "up")
    assert hub.drop_streams() == 1
    wait_until(lambda: s.reconnects == 1 and s.state == "up")
    assert "down" in states and states[-1] == "up" and len(resyncs) == 2
    a.lease_acquire("x", ttl=600)
    wait_until(lambda: any(e["type"] == "lease.acquired" for e in events))


def test_a_closed_stream_does_not_reconnect(hub):
    ta = hub.add_token("alice")
    a = client_for(hub, ta)
    s = HubEventStream(a, on_event=lambda e: None, backoff_s=FAST).start()
    wait_until(lambda: s.state == "up")
    s.close()
    time.sleep(0.3)
    # (the hub, like fpgahub, notices a gone client only at its next write)
    tries = [r for r in hub.requests if r["path"] == "/api/v1/events"]
    assert s.state == "closed" and s.reconnects == 0 and len(tries) == 1


def test_a_hub_that_is_down_is_retried_with_backoff(hub, streams):
    ta = hub.add_token("alice")
    a = client_for(hub, ta)
    hub.refuse_streams(503)
    s = HubEventStream(a, on_event=lambda e: None, backoff_s=FAST).start()
    streams.append(s)
    wait_until(lambda: s.state == "down" and "503" in s.detail)
    hub.refuse_streams(None)
    wait_until(lambda: s.state == "up")


def test_a_refused_token_is_not_hammered(hub, streams):
    a = client_for(hub, "revoked-token")
    s = HubEventStream(a, on_event=lambda e: None, backoff_s=(0.05, 5.0)).start()
    streams.append(s)
    wait_until(lambda: s.state == "refused")
    time.sleep(0.5)
    tries = [r for r in hub.requests if r["path"] == "/api/v1/events"]
    assert len(tries) == 1 and "401" in s.detail


def test_a_good_token_streams(hub, streams):
    a = client_for(hub, hub.add_token("alice"))
    s = HubEventStream(a, on_event=lambda e: None, backoff_s=FAST).start()
    streams.append(s)
    wait_until(lambda: s.state == "up")
    assert s.connects == 1


def test_stream_state_is_published_on_the_bus(hub, streams):
    a = client_for(hub, hub.add_token("alice"))
    bus = EventBus()
    seen = _bus_log(bus)
    streams.append(attach_bus(bus, "mps3@lab", a, backoff_s=FAST))
    wait_until(lambda: any(e.topic == STREAM_TOPIC and e.data["state"] == "up" for e in seen))
    hub.drop_streams()
    wait_until(lambda: any(e.topic == STREAM_TOPIC and e.data["state"] == "down" for e in seen))


def test_the_stream_sends_the_token_as_a_header_and_filters_by_type(hub, streams):
    tok = hub.add_token("alice")
    a = client_for(hub, tok)
    s = HubEventStream(a, on_event=lambda e: None, backoff_s=FAST).start()
    streams.append(s)
    wait_until(lambda: s.state == "up")
    req = next(r for r in hub.requests if r["path"] == "/api/v1/events")
    assert req["authorized"] and tok not in req["query"] and "lease.admin_revoked" in req["query"]
