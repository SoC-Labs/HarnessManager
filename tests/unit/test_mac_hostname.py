"""MAC-HOST (16 Oct, David's Mac): macOS's hostname flips between ``X`` and ``X.local`` with
the network. A lock or ``daemon.json`` written as one must still read as this machine as the
other; a genuinely different machine must not. Every case has its negative twin."""

from __future__ import annotations

import json
import os
import socket

import pytest

from harness_manager.core.errors import HeldError
from harness_manager.core.hostid import is_this_host, same_host, short_host
from harness_manager.core.session import SessionLock
from harness_manager.daemon import control
from harness_manager.daemon.state import DaemonInfo, daemon_json_path, write_info

DEAD_PID = 2 ** 22 + 12345          # above Linux's default pid_max: never alive


@pytest.fixture
def host_now(monkeypatch):
    """This machine answers ``DM-MacBookPro`` now (it said ``DM-MacBookPro.local`` before)."""
    monkeypatch.setattr(socket, "gethostname", lambda: "DM-MacBookPro")


def test_short_names_match_across_suffixes_and_case():
    assert short_host("DM-MacBookPro.local") == short_host("dm-macbookpro") == "dm-macbookpro"
    assert same_host("X.local", "X") and same_host("x.example.org.", "X.local")


def test_twin_different_machines_and_empty_names_do_not_match():
    assert not same_host("lab-a", "lab-b.example.org")
    assert not same_host("", "") and not same_host(None, "X")


def test_a_lock_written_as_x_local_is_this_machine_as_x(host_now):
    assert is_this_host("DM-MacBookPro.local") and is_this_host("DM-MacBookPro")


def test_twin_another_machine_s_lock_is_not_this_machine(host_now):
    assert not is_this_host("lab-pc.local")


def _write_lock(path, host, pid):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"user": "david", "host": host, "pid": pid, "since": 0.0,
                                "note": "harness-manager-daemon"}))


def test_a_dead_daemon_s_lock_from_x_local_is_stale_and_taken(tmp_path, host_now):
    lock = SessionLock("harness-manager-daemon", lock_dir=tmp_path)
    _write_lock(lock.path, "DM-MacBookPro.local", DEAD_PID)
    lock.acquire()
    assert json.loads(lock.path.read_text())["pid"] == os.getpid()
    lock.release()
    assert not lock.path.exists()


def test_twin_a_dead_pid_on_another_machine_is_never_taken(tmp_path, host_now):
    lock = SessionLock("harness-manager-daemon", lock_dir=tmp_path)
    _write_lock(lock.path, "lab-pc.local", DEAD_PID)
    with pytest.raises(HeldError):
        lock.acquire()


def test_our_own_lock_is_released_after_the_name_changed(tmp_path, host_now):
    lock = SessionLock("b1", lock_dir=tmp_path)
    _write_lock(lock.path, "DM-MacBookPro.local", os.getpid())
    lock.acquire()                      # our own pid, the old name: we hold it
    lock.release()
    assert not lock.path.exists()


def _info(hostname, pid=DEAD_PID):
    return DaemonInfo(pid=pid, port=1, token="t" * 32, started_at=0.0, version="1.1.0",
                      hostname=hostname)


def test_daemon_json_from_x_local_is_on_this_host(host_now):
    assert _info("DM-MacBookPro.local").on_this_host()


def test_twin_daemon_json_from_another_machine_is_not(host_now):
    assert not _info("lab-pc").on_this_host()


def test_stop_removes_a_dead_daemon_recorded_as_x_local(tmp_path, host_now):
    write_info(tmp_path, _info("DM-MacBookPro.local"))
    assert control.stop(tmp_path) == "stale-removed"
    assert not daemon_json_path(tmp_path).exists()


def test_twin_stop_refuses_another_machine_s_daemon(tmp_path, host_now):
    write_info(tmp_path, _info("lab-pc"))
    with pytest.raises(Exception, match="not here"):
        control.stop(tmp_path)
    assert daemon_json_path(tmp_path).exists()
