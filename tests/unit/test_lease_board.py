"""LEASE-BOARD: the lease service names fpgahub's physical board, and leases the target.

``board`` is additive everywhere a lease is shown (the view's ``lease``, the acquire and
release results, ``lease.state``) and ``target`` stays. The board is looked up once per hub
and kept: an event or a message never asks the hub, and boards.toml ``hub.board`` answers
with no hub call at all. The token file never gains a ``board`` key (an older Harness Manager
on the same machine reads it with ``StoredLease(**data)``). Every check has its twin.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness_manager.core.errors import AbsentError
from harness_manager.core.events import EventBus
from harness_manager.demo_showcase import DemoHubClient, DemoHubState
from harness_manager.services.lease import (
    LeaseService,
    LeaseStore,
    StoredLease,
    lease_detail,
    lease_name,
)

ME = "david@mapstone-dev"
HOST = "hub.example"
TARGET = "mps3_01_pl"


class CountingClient(DemoHubClient):
    """The demo's in-memory hub client, counting ``board_id`` asks; ``board`` may raise."""

    def __init__(self, state: DemoHubState, *, fail: bool = False) -> None:
        super().__init__(state)
        self.host = HOST
        self.asked = 0
        self.fail = fail

    def board_id(self) -> str:
        self.asked += 1
        if self.fail:
            raise AbsentError(f"no board on the hub {HOST} has the target {TARGET}")
        return super().board_id()


def make_hub(board: str = "mps3_01", *, fail: bool = False, configured: str = "") -> Any:
    client = CountingClient(DemoHubState(ME, target=TARGET, board=board, free=True), fail=fail)
    config = SimpleNamespace(board=configured) if configured else None
    return SimpleNamespace(host=HOST, target=TARGET, client=client, config=config)


@pytest.fixture
def events():
    bus = EventBus()
    seen: list[dict[str, Any]] = []
    bus.subscribe("lease.*", lambda ev: seen.append({"topic": ev.topic, **ev.data}))
    return bus, seen


def service(tmp_path: Path, bus: EventBus | None = None) -> LeaseService:
    return LeaseService(tmp_path / "state", bus)


# --- the words --------------------------------------------------------------------------------------


def test_the_name_is_the_board_and_the_detail_adds_the_target():
    assert lease_name("mps3_01", TARGET) == "mps3_01"
    assert lease_detail("mps3_01", TARGET) == "mps3_01 (target mps3_01_pl)"


@pytest.mark.parametrize("board", [None, "", "  ", TARGET])
def test_negative_twin_no_board_or_the_target_itself_is_the_target_unchanged(board):
    assert lease_name(board, TARGET) == TARGET
    assert lease_detail(board, TARGET) == TARGET


# --- the view and the results ----------------------------------------------------------------------------


def test_the_view_names_the_board_in_the_lease_and_keeps_the_target(tmp_path):
    s, hub = service(tmp_path), make_hub()
    s.acquire(hub, board_id="b1", ttl_s=600, holder="hm", heartbeat=False)
    v = s.view(hub)
    assert v["board"] == "mps3_01"
    assert v["lease"]["board"] == "mps3_01" and v["lease"]["target"] == TARGET


def test_the_board_is_asked_once_and_kept(tmp_path):
    s, hub = service(tmp_path), make_hub()
    for _ in range(3):
        s.view(hub)
        s.forget(hub)                               # a hub event drops the view, not the board
    assert hub.client.asked == 1
    assert s.board_of(hub, ask=False) == "mps3_01"


def test_negative_twin_a_hub_that_maps_the_target_to_no_board_shows_the_target(tmp_path):
    s, hub = service(tmp_path), make_hub(fail=True)
    s.acquire(hub, board_id="b1", ttl_s=600, holder="hm", heartbeat=False)
    v = s.view(hub)
    assert v["board"] is None and v["lease"]["board"] is None and v["lease"]["target"] == TARGET
    assert s.board_of(hub) == "" and lease_name(s.board_of(hub), TARGET) == TARGET
    s.view(hub)
    assert hub.client.asked == 1                    # a failure is not asked again at once


def test_boards_toml_hub_board_answers_with_no_hub_call(tmp_path):
    s, hub = service(tmp_path), make_hub(configured="mps3_09")
    assert s.board_of(hub, ask=False) == "mps3_09"
    assert s.view(hub)["board"] == "mps3_09" and hub.client.asked == 0


def test_ask_false_never_calls_the_hub(tmp_path):
    s, hub = service(tmp_path), make_hub()
    assert s.board_of(hub, ask=False) == "" and hub.client.asked == 0
    assert s.board_of(hub) == "mps3_01" and hub.client.asked == 1       # the twin: ask=True
    assert s.board_of(None) == ""


def test_acquire_and_release_results_carry_the_board_once_it_is_known(tmp_path):
    s, hub = service(tmp_path), make_hub()
    s.view(hub)
    out = s.acquire(hub, board_id="b1", ttl_s=600, holder="hm", heartbeat=False)
    assert out["lease"]["board"] == "mps3_01" and out["lease"]["target"] == TARGET
    rel = s.release(hub, board_id="b1")
    assert rel["released"]["board"] == "mps3_01" and rel["released"]["target"] == TARGET


def test_negative_twin_results_never_ask_the_hub_for_the_board(tmp_path):
    s, hub = service(tmp_path), make_hub()
    out = s.acquire(hub, board_id="b1", ttl_s=600, holder="hm", heartbeat=False)
    rel = s.release(hub, board_id="b1")
    assert out["lease"]["board"] is None and rel["released"]["board"] is None
    assert hub.client.asked == 0


def test_lease_state_events_carry_the_board_without_a_hub_call(tmp_path, events):
    bus, seen = events
    s, hub = service(tmp_path, bus), make_hub()
    s.acquire(hub, board_id="b1", ttl_s=600, holder="hm", heartbeat=False)
    assert seen[-1]["topic"] == "lease.state" and seen[-1]["board"] is None   # not known yet
    assert hub.client.asked == 0
    s.view(hub)
    s.release(hub, board_id="b1")
    assert seen[-1]["state"] == "released" and seen[-1]["board"] == "mps3_01"
    assert seen[-1]["target"] == TARGET


def test_messages_name_the_board_when_it_is_known(tmp_path):
    s, hub = service(tmp_path), make_hub()
    with pytest.raises(AbsentError, match=f"holds no lease on {TARGET} "):
        s.release(hub, board_id="b1")               # the twin: not known yet, the target
    s.view(hub)
    with pytest.raises(AbsentError, match="holds no lease on mps3_01 "):
        s.release(hub, board_id="b1")


# --- the token file ------------------------------------------------------------------------------------


def test_the_token_file_never_gains_a_board_key(tmp_path):
    """An older Harness Manager reads it with ``StoredLease(**data)``: an extra key would make
    it drop the lease, and its token, without a word."""
    s, hub = service(tmp_path), make_hub()
    s.view(hub)
    s.acquire(hub, board_id="b1", ttl_s=600, holder="hm", heartbeat=False)
    path = next((tmp_path / "state" / "leases").glob("*.json"))
    data = json.loads(path.read_text())
    assert "board" not in data and set(data) == set(asdict(s.store.get(HOST, TARGET)))


def test_a_token_file_from_a_newer_version_still_loads(tmp_path):
    store = LeaseStore(tmp_path)
    store.put(StoredLease(hub=HOST, target=TARGET, holder="hm", token="tok-1", ttl_s=60))
    path = next(tmp_path.glob("*.json"))
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "board": "mps3_01", "later": 1}))
    got = store.get(HOST, TARGET)
    assert got is not None and got.token == "tok-1"


@pytest.mark.parametrize("text", ["[]", "{}", "not json", '{"hub": "h"}'])
def test_negative_twin_a_broken_token_file_is_no_lease(tmp_path, text):
    store = LeaseStore(tmp_path)
    store.put(StoredLease(hub=HOST, target=TARGET, holder="hm", token="tok-1", ttl_s=60))
    path = next(tmp_path.glob("*.json"))
    path.write_text(text)
    assert store.get(HOST, TARGET) is None
