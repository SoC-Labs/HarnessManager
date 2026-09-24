"""LR-C: ``harness-manager lease request|requests|respond|force|leave|show`` in-process.

``main()`` runs for real (argparse, formats, exit codes); the board's hub adapter
is a stub and the lease service is ``tests.fakes.lrc_lease.FakeLeaseService`` over
an in-memory hub, so nothing reaches a hub or ssh. Every check has a negative twin.
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness_manager.cli import cmd_hub
from harness_manager.core.errors import ExitCode, UnreachableError
from tests.fakes.lrc_lease import ALICE, BOB, ME, FakeLeaseService, LeaseWorld, factory

TARGET_ARG = "192.168.10.101"
HUB = "mapstone-dev.ecs.soton.ac.uk"
TARGET = "mps3_01_pl"


@dataclass
class _Cand:
    board_id: str = "mps3@192.168.10.101:6900"
    name: str = "mps3-01"                     # N1: named offline from the hub table


class _Client:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def board_id(self) -> str:
        return "mps3_07"

    def principal(self) -> str:
        return ME

    def lease_cancel(self, holder: str) -> bool:
        self.cancelled.append(holder)
        return True


@dataclass
class _Hub:
    host: str = HUB
    target: str = TARGET
    client: _Client = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.client = _Client()


@pytest.fixture
def world(monkeypatch) -> LeaseWorld:
    w = LeaseWorld()
    monkeypatch.setattr(cmd_hub, "LeaseService", factory(w))
    monkeypatch.setattr(cmd_hub, "_hub", lambda ctx: (_Cand(), _Hub()))
    monkeypatch.setattr(cmd_hub, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(cmd_hub, "_stderr_is_tty", lambda: False)
    return w


def run(capsys, *argv: str) -> tuple[int, str, str]:
    from harness_manager.cli.main import main

    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def meanwhile(world: LeaseWorld, action, delay: float = 0.0) -> threading.Thread:
    """Run ``action(rid)`` once our request note is on the hub (the other side acting)."""

    def go() -> None:
        deadline = time.monotonic() + 10
        while world.my_request() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(delay)
        action(world.my_request()["id"])

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def queued_expired(world: LeaseWorld) -> str:
    """Our request, its 2:00 run out, us at the head: force is open."""
    world.queue.append(ME)
    rid = "r50"
    world.notes[rid] = {"id": rid, "by": ME, "user": "david", "host": "mapstone-dev",
                        "message": "", "created_at": "", "deadline_at": ""}
    world.expire_deadline(rid)
    return rid


# --- lease request ------------------------------------------------------------------------------


def test_request_waits_until_the_holder_releases(capsys, world):
    t = meanwhile(world, lambda rid: world.answer(rid, "release"))
    rc, out, err = run(capsys, "--json", "lease", "request", TARGET_ARG, "--message", "B1 at 3",
                       "--ttl", "900")
    t.join(5)
    assert rc == ExitCode.OK, err
    data = json.loads(out)
    assert data["ok"] and data["lease"]["mine"] and data["lease"]["holder"] == ME
    assert "queued at position 1" in err and "answer is due by" in err
    assert ("request", "mps3@192.168.10.101:6900", "B1 at 3", 900) in world.calls


def test_negative_twin_a_keep_answer_exits_held_with_the_answer(capsys, world):
    t = meanwhile(world, lambda rid: world.answer(rid, "keep", 15, "B1 running"))
    rc, out, err = run(capsys, "--json", "lease", "request", TARGET_ARG)
    t.join(5)
    assert rc == ExitCode.HELD
    e = json.loads(out)["error"]
    assert e["name"] == "HELD" and e["holder"] == ALICE and "15 more min" in e["message"]
    assert e["data"]["answered"]["minutes"] == 15 and "B1 running" in err


def after_the_countdown_read_the_deadline(world: LeaseWorld, action):
    """``action(rid)`` once the countdown thread has made its one ``view`` call, and drawn."""

    def go(rid: str) -> None:
        deadline = time.monotonic() + 10
        while ("view",) not in world.calls[1:] and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
        action(rid)

    return go


def test_request_shows_a_countdown_on_a_terminal(capsys, world, monkeypatch):
    monkeypatch.setattr(cmd_hub, "_stderr_is_tty", lambda: True)
    t = meanwhile(world, after_the_countdown_read_the_deadline(
        world, lambda rid: world.answer(rid, "release")))
    rc, _, err = run(capsys, "lease", "request", TARGET_ARG)
    t.join(5)
    assert rc == ExitCode.OK
    assert re.search(rf"\rwaiting for {ALICE} to answer: (2:00|1:5\d) left", err), err
    assert "(Ctrl-C leaves the queue)" in err


def test_negative_twin_no_terminal_no_redrawn_line(capsys, world):
    t = meanwhile(world, after_the_countdown_read_the_deadline(
        world, lambda rid: world.answer(rid, "release")))
    rc, _, err = run(capsys, "lease", "request", TARGET_ARG)
    t.join(5)
    assert rc == ExitCode.OK and "\r" not in err and "answer is due by" in err


def test_request_says_when_force_opens(capsys, world):
    def expire_then_release(rid: str) -> None:
        world.expire_deadline(rid)
        time.sleep(0.2)
        world.release()

    t = meanwhile(world, expire_then_release)
    rc, _, err = run(capsys, "lease", "request", TARGET_ARG)
    t.join(5)
    assert rc == ExitCode.OK and f"`harness-manager lease force {TARGET_ARG}`" in err


def test_ctrl_c_during_a_request_leaves_the_queue(capsys, world, monkeypatch):
    original = cmd_hub.LeaseService

    def interrupting(state_dir, bus=None, **kw):
        svc = original(state_dir, bus, **kw)
        real = svc.request

        def request(*a, progress=None, **k):
            def report(phase, done, total):
                progress(phase, done, total)
                if phase == "notified":
                    raise KeyboardInterrupt
            return real(*a, progress=report, **k)

        svc.request = request
        return svc

    monkeypatch.setattr(cmd_hub, "LeaseService", interrupting)
    rc, _, err = run(capsys, "lease", "request", TARGET_ARG)
    assert rc == ExitCode.ACTION_FAILED and "interrupted" in err
    assert "left the queue" in err and ME not in world.queue and world.my_request() is None


def test_request_for_a_board_this_principal_holds_is_already(capsys, world):
    world.holder = ME
    rc, out, err = run(capsys, "--json", "lease", "request", TARGET_ARG)
    assert rc == ExitCode.ALREADY and "you already hold mps3-01" in err
    assert json.loads(out)["error"]["name"] == "ALREADY"
    assert not any(c[0] == "request" for c in world.calls) and world.queue == []


def test_messages_name_the_board(capsys, world):
    t = meanwhile(world, lambda rid: world.answer(rid, "release"))
    rc, out, err = run(capsys, "lease", "request", TARGET_ARG)
    t.join(5)
    assert rc == ExitCode.OK and f"queued at position 1 for mps3-01 ({TARGET} on {HUB})" in err
    assert out.startswith(f"mps3-01 ({TARGET} on {HUB}): yours")
    rc, out, _ = run(capsys, "--json", "lease", "show", TARGET_ARG)
    assert json.loads(out)["name"] == "mps3-01"


def test_ctrl_c_in_acquire_cancels_with_the_principal(capsys, world, monkeypatch):
    hub = _Hub()
    monkeypatch.setattr(cmd_hub, "_hub", lambda ctx: (_Cand(), hub))

    def interrupted(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(FakeLeaseService, "acquire", interrupted)
    rc, _, err = run(capsys, "lease", "acquire", TARGET_ARG, "--holder", "david-hm")
    assert rc == ExitCode.ACTION_FAILED and "left the queue" in err
    assert hub.client.cancelled == [ME]           # not "david-hm": fpgahub queued the principal


def test_negative_twin_without_a_principal_acquire_cancels_with_the_holder(capsys, world,
                                                                           monkeypatch):
    hub = _Hub()

    def no_principal() -> str:
        raise UnreachableError("whoami failed")

    hub.client.principal = no_principal                    # type: ignore[method-assign]
    monkeypatch.setattr(cmd_hub, "_hub", lambda ctx: (_Cand(), hub))
    monkeypatch.setattr(FakeLeaseService, "acquire",
                        lambda self, *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    rc, _, _ = run(capsys, "lease", "acquire", TARGET_ARG, "--holder", "david-hm")
    assert rc == ExitCode.ACTION_FAILED and hub.client.cancelled == ["david-hm"]


def test_negative_twin_request_refuses_a_long_message_before_queueing(capsys, world):
    rc, _, err = run(capsys, "lease", "request", TARGET_ARG, "--message", "x" * 501)
    assert rc == ExitCode.USAGE and "500" in err
    rc, _, err = run(capsys, "lease", "request", TARGET_ARG, "--ttl", "5")
    assert rc == ExitCode.USAGE and "--ttl" in err
    assert not any(c[0] == "request" for c in world.calls) and world.queue == []


# --- lease requests / respond -------------------------------------------------------------------


def test_requests_lists_what_waits_for_an_answer(capsys, world):
    world.holder = ME
    rid = world.add_request(ALICE, "B1 at 3?")
    rc, out, _ = run(capsys, "--json", "lease", "requests", TARGET_ARG)
    (inc,) = json.loads(out)["incoming"]
    assert rc == ExitCode.OK and inc["id"] == rid and inc["by"] == ALICE
    rc, out, _ = run(capsys, "lease", "requests", TARGET_ARG)
    assert f"request {rid} from {ALICE}: 'B1 at 3?'" in out
    assert f"lease respond {TARGET} {rid} --release | --keep MINUTES" in out
    rc, out, _ = run(capsys, "--tsv", "lease", "requests", TARGET_ARG)
    assert out.rstrip("\n").split("\t")[:5] == [TARGET, rid, ALICE, "alice", "lab-pc"]


def test_negative_twin_no_requests_says_so(capsys, world):
    rc, out, _ = run(capsys, "lease", "requests", TARGET_ARG)
    assert rc == ExitCode.OK and "no requests" in out and "you do not hold it" in out
    rc, out, _ = run(capsys, "--tsv", "lease", "requests", TARGET_ARG)
    assert out == ""


def test_respond_keep_then_release(capsys, world):
    world.holder = ME
    rid = world.add_request(ALICE)
    rc, out, _ = run(capsys, "--json", "lease", "respond", TARGET_ARG, rid, "--keep", "15",
                     "--message", "5 more min")
    d = json.loads(out)
    assert rc == ExitCode.OK and d["answer"] == "keep" and d["minutes"] == 15
    assert world.answers[rid]["message"] == "5 more min" and world.holder == ME
    rid2 = world.add_request(BOB)
    rc, out, _ = run(capsys, "--tsv", "lease", "respond", TARGET_ARG, rid2, "--release")
    assert rc == ExitCode.OK and out.split("\t")[:3] == [TARGET, rid2, "release"]
    assert world.holder == ALICE


@pytest.mark.parametrize("args, code", [
    (["r1", "--keep", "7"], ExitCode.USAGE),
    (["r1", "--keep", "15", "--release"], ExitCode.USAGE),
    (["r1"], ExitCode.USAGE),
    (["../x", "--release"], ExitCode.USAGE),
    (["r999", "--release"], ExitCode.ABSENT),
])
def test_negative_twin_respond_refuses_bad_answers(capsys, world, args, code):
    world.holder = ME
    world.add_request(ALICE)
    rc, _, _ = run(capsys, "lease", "respond", TARGET_ARG, *args)
    assert rc == code and world.answers == {} and world.holder == ME


# --- lease force ----------------------------------------------------------------------------------


def test_force_without_a_terminal_is_refused_unless_yes(capsys, world):
    queued_expired(world)
    rc, _, err = run(capsys, "lease", "force", TARGET_ARG)
    assert rc == ExitCode.REFUSED and "no terminal" in err and "--yes" in err
    assert world.revoked == [] and world.holder == ALICE
    rc, out, err = run(capsys, "--json", "lease", "force", TARGET_ARG, "--yes")
    assert rc == ExitCode.OK, err
    assert json.loads(out)["lease"]["mine"] and world.holder == ME
    (rev,) = world.revoked
    assert rev["prior_holder"] == ALICE and "Are you sure" not in err


def test_force_asks_first_and_n_aborts(capsys, world, monkeypatch):
    queued_expired(world)
    monkeypatch.setattr(cmd_hub, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    rc, _, err = run(capsys, "lease", "force", TARGET_ARG)
    assert rc == ExitCode.REFUSED and "not confirmed" in err
    assert (f"Are you sure? This kicks {ALICE} off mps3-01 now; anything they are running on "
            "the board is interrupted. [y/N]") in err
    assert world.revoked == [] and world.holder == ALICE


def test_negative_twin_force_answered_y_goes_ahead(capsys, world, monkeypatch):
    queued_expired(world)
    monkeypatch.setattr(cmd_hub, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    # A board with no name: the prompt names the hub's board (mps3_07 shown as mps3-07).
    monkeypatch.setattr(cmd_hub, "_hub", lambda ctx: (_Cand(name=""), _Hub()))
    rc, out, err = run(capsys, "lease", "force", TARGET_ARG)
    assert rc == ExitCode.OK and f"kicks {ALICE} off mps3-07 now" in err
    assert f"{TARGET} on {HUB}: force-released" in out
    assert len(world.revoked) == 1 and world.holder == ME


@pytest.mark.parametrize("setup, code, words", [
    ("early", ExitCode.UNAVAILABLE, "left to answer"),
    ("not-head", ExitCode.REFUSED, "position 2"),
    ("none", ExitCode.REFUSED, "no request"),
    ("kept", ExitCode.REFUSED, "keep"),
])
def test_force_is_refused_before_any_prompt_or_revoke(capsys, world, setup, code, words):
    if setup != "none":
        rid = queued_expired(world)
        if setup == "early":
            world.notes[rid]["deadline_at"] = "2999-01-01T00:00:00+00:00"
        elif setup == "not-head":
            world.queue.insert(0, BOB)
        elif setup == "kept":
            world.answer(rid, "keep", 30, "long run")
    rc, out, err = run(capsys, "--json", "lease", "force", TARGET_ARG, "--yes")
    assert rc == code and words in json.loads(out)["error"]["message"]
    assert world.revoked == [] and not any(c[0] == "force" for c in world.calls)


# --- lease leave / show ---------------------------------------------------------------------------


def test_leave_leaves_the_queue(capsys, world):
    queued_expired(world)
    rc, out, _ = run(capsys, "--json", "lease", "leave", TARGET_ARG)
    assert rc == ExitCode.OK and json.loads(out)["left"] is True
    assert ME not in world.queue and world.my_request() is None
    rc, out, _ = run(capsys, "--tsv", "lease", "leave", TARGET_ARG)
    assert out == f"{TARGET}\t{HUB}\tfalse\n"


def test_negative_twin_leave_when_not_queued(capsys, world):
    rc, out, _ = run(capsys, "lease", "leave", TARGET_ARG)
    assert rc == ExitCode.OK and "not in the queue" in out and world.holder == ALICE


def test_show_prints_the_queue_and_the_requests(capsys, world):
    rid = queued_expired(world)
    rc, out, _ = run(capsys, "lease", "show", TARGET_ARG)
    assert rc == ExitCode.OK
    assert f"held by {ALICE}" in out and f"queue: 1. {ME} (you)" in out
    assert f"your request {rid}" in out and "(passed)" in out
    assert f"force-release is available: `harness-manager lease force {TARGET}`" in out
    rc, out, _ = run(capsys, "--json", "lease", "show", TARGET_ARG)
    d = json.loads(out)
    assert d["request"]["id"] == rid and d["request"]["force_available"] is True
    assert d["queue"][0]["mine"] and d["incoming"] == [] and d["taken"] is None
    rc, out, _ = run(capsys, "--tsv", "lease", "show", TARGET_ARG)
    row = out.rstrip("\n").split("\t")
    assert len(row) == len(cmd_hub.LEASE_COLUMNS) == 13
    assert row[:4] == [TARGET, HUB, "held", ALICE] and row[6:11] == ["1", "1", rid, "-", "true"]


def test_negative_twin_show_as_the_holder_lists_incoming_and_a_forced_release(capsys, world):
    world.holder = ME
    rid = world.add_request(BOB, "please")
    rc, out, _ = run(capsys, "lease", "show", TARGET_ARG)
    assert "yours" in out and f"request {rid} from {BOB}" in out and "your request" not in out
    world.force_me_off(BOB)
    rc, out, _ = run(capsys, "lease", "show", TARGET_ARG)
    assert f"your lease was force-released by {BOB}" in out
    rc, out, _ = run(capsys, "--tsv", "lease", "show", TARGET_ARG)
    assert out.rstrip("\n").split("\t")[-1] == BOB


def test_the_real_hub_adapter_path_reaches_the_new_verbs(capsys, tmp_path, monkeypatch):
    """No stubbed ``_hub``: boards.toml's hub table, L1's lab rig (fake ssh, fake hub)."""
    from tests.fakes.l1_rig import lab
    from tests.fakes.virtual_board import VirtualMps3

    w = LeaseWorld()
    monkeypatch.setattr(cmd_hub, "LeaseService", factory(w))
    sd = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=sd) as rig:
        rc, out, _ = run(capsys, "--json", "lease", "leave", TARGET_ARG)
        assert rc == ExitCode.OK and json.loads(out) == {
            "ok": True, "board_id": json.loads(out)["board_id"], "name": "mps3-01", "hub": HUB,
            "target": TARGET, "left": False}
        assert rig.hub.current is None                          # the fake hub was not asked
