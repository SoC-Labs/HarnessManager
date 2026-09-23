"""Unit tests for the core contracts. Each check has a negative twin."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from harness_manager.core import capabilities as C
from harness_manager.core.capabilities import CapabilitySpec, negotiate, via
from harness_manager.core.errors import ExitCode, HeldError, UnavailableError
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import LinkKind, Reading
from harness_manager.core.registry import load_packs
from harness_manager.core.session import SessionLock

# -- exit codes -------------------------------------------------------------------------

def test_exit_codes_are_append_only_and_match_haps_contract():
    # Renumbering breaks every script written against the helpers. Pin them.
    assert (ExitCode.USAGE, ExitCode.ABSENT, ExitCode.HELD, ExitCode.UNREACHABLE) == (2, 3, 4, 7)
    assert ExitCode.UNAVAILABLE == 12 and ExitCode.NOTHING_ON_TARGET == 13


def test_unavailable_error_names_capability_and_reason():
    err = UnavailableError("reboot_board", "needs the Debug USB cable")
    assert err.code == ExitCode.UNAVAILABLE
    assert "reboot_board" in str(err) and "Debug USB" in str(err)


# -- readings ---------------------------------------------------------------------------

def test_unavailable_reading_is_none_not_zero():
    r = Reading.unavailable("board_power", "W", "no power sensor on the MPS3")
    assert r.value is None and not r.available and r.reason


def test_available_reading():
    assert Reading("mcc_temp", 35.5, "degC", "mcc-console").available


# -- capabilities -----------------------------------------------------------------------

SPECS = (
    CapabilitySpec(C.IDENTIFY, "Identify", (via(LinkKind.ETHERNET),)),
    CapabilitySpec(C.REBOOT_BOARD, "Reboot", (via(LinkKind.USB_SERIAL),),
                   needs_hint="needs the Debug USB cable"),
    CapabilitySpec(C.RESET_SHELL, "Restart shell", (via(LinkKind.ETHERNET, features=("reboot",)),)),
)


def test_negotiate_ethernet_only_explains_what_is_missing():
    avail, unavail = negotiate(SPECS, [LinkKind.ETHERNET], [])
    assert avail == {C.IDENTIFY}
    assert unavail[C.REBOOT_BOARD] == "needs the Debug USB cable"
    assert unavail[C.RESET_SHELL] == "needs harness firmware with 'reboot'"


def test_negotiate_feature_lights_up_capability():
    avail, unavail = negotiate(SPECS, [LinkKind.ETHERNET, LinkKind.USB_SERIAL], ["reboot"])
    assert avail == {C.IDENTIFY, C.REBOOT_BOARD, C.RESET_SHELL} and not unavail


def test_negotiate_route_alternatives():
    spec = CapabilitySpec("x", "X", (via(LinkKind.USB_SERIAL), via(LinkKind.HUB)))
    assert negotiate([spec], [LinkKind.HUB], [])[0] == {"x"}
    assert negotiate([spec], [LinkKind.ETHERNET], [])[0] == frozenset()


def test_route_needing_link_and_feature():
    # Shell console over Ethernet needs firmware A12; over USB it needs nothing.
    spec = CapabilitySpec("shell", "S", (via(LinkKind.USB_SERIAL),
                                         via(LinkKind.ETHERNET, features=("shell_console",))))
    avail, unavail = negotiate([spec], [LinkKind.ETHERNET], [])
    assert not avail and unavail["shell"] == "needs harness firmware with 'shell_console'"
    assert negotiate([spec], [LinkKind.ETHERNET], ["shell_console"])[0] == {"shell"}


# -- events -----------------------------------------------------------------------------

def test_event_bus_prefix_and_isolation():
    bus, seen = EventBus(), []
    bus.subscribe("deploy.*", lambda e: seen.append(e.topic))
    bus.subscribe("deploy.done", lambda e: 1 / 0)  # a broken handler must not break delivery
    bus.publish(Event("deploy.progress"))
    bus.publish(Event("deploy.done"))
    bus.publish(Event("console.line"))
    assert seen == ["deploy.progress", "deploy.done"]


def test_event_bus_unsubscribe():
    bus, seen = EventBus(), []
    unsub = bus.subscribe("*", lambda e: seen.append(e.topic))
    unsub()
    bus.publish(Event("x"))
    assert seen == []


# -- session lock -----------------------------------------------------------------------

def test_session_lock_is_exclusive_and_names_holder(tmp_path: Path):
    a = SessionLock("mps3@x", lock_dir=tmp_path, note="deploying")
    a.acquire()
    other = SessionLock("mps3@x", lock_dir=tmp_path)
    # Simulate another live process by rewriting the owner pid to our parent (alive).
    data = json.loads(a.path.read_text())
    data["pid"] = os.getppid()
    a.path.write_text(json.dumps(data))
    with pytest.raises(HeldError) as exc:
        other.acquire()
    assert exc.value.code == ExitCode.HELD and "deploying" in exc.value.holder


def test_session_lock_takes_over_stale_lock(tmp_path: Path):
    lock = SessionLock("mps3@y", lock_dir=tmp_path)
    lock.lock_dir.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(json.dumps(
        {"user": "ghost", "host": socket.gethostname(), "pid": 2**22 + 12345, "since": 0.0}))
    lock.acquire()  # dead pid on this host -> stale -> taken over
    assert lock.owner() is not None and lock.owner().pid == os.getpid()
    lock.release()
    assert not lock.path.exists()


def test_session_lock_never_releases_someone_elses(tmp_path: Path):
    lock = SessionLock("mps3@z", lock_dir=tmp_path)
    lock.acquire()
    data = json.loads(lock.path.read_text())
    data["pid"] = os.getppid()
    lock.path.write_text(json.dumps(data))
    lock.release()
    assert lock.path.exists()  # not ours any more, so left alone


# -- registry ---------------------------------------------------------------------------

def test_mps3_pack_is_registered():
    packs = load_packs()
    assert "mps3" in packs and packs["mps3"].title.startswith("Arm MPS3")


def test_session_lock_does_not_steal_a_fresh_empty_lock(tmp_path: Path):
    # CCR-2: a lock file its creator has not written yet must not be taken over.
    lock = SessionLock("mps3@e", lock_dir=tmp_path)
    tmp_path.mkdir(exist_ok=True)
    lock.path.write_text("")
    with pytest.raises(HeldError):
        lock.acquire()


def test_session_lock_takes_over_an_old_empty_lock(tmp_path: Path):
    lock = SessionLock("mps3@f", lock_dir=tmp_path)
    lock.path.write_text("")
    old = time.time() - 60
    os.utime(lock.path, (old, old))
    lock.acquire()
    assert lock.owner().pid == os.getpid()
    lock.release()
