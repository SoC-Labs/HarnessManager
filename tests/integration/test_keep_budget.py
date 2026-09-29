"""KEEP-BUDGET: `program --keep-on-card` is budgeted as `card commit` is (the same card, written
the same way).

Silicon (Linux lead, 2026-09-29): the user microSD stalls for more than 30 s mid-write (a 29 MB
slot push tore twice at ~27 MB with a 30 s per-chunk limit) and reads back at ~14 KB/s, so
nanosoc's 2.47 MB pair takes ~176 s to read back. The deploy's commit pusher had the swap
push's 30 s stall limit and its control connection the swap's 300 s; both now come from
``card.commit_budget``. The board is pyverify's FakeShell (the Linux profile with the D13
store and a card) through the real MPS3 pack; every commit crosses real sockets on 127.0.0.1.

Scaled world (never a real 5-min sleep): 30 s -> 0.3 s (the swap push's stall limit), 300 s
-> 1 s (the swap wait and the commit floor), 900 s -> 3 s (``mps3.slot.push_timeout_s``),
and card rates that make the 512 B test pair's budget 4 s. The card stalls 0.6 s on one chunk
of the pair (> "30 s") and answers the commit 2 s after the push (> "300 s"); the fake board's
own idle limit on a commit is 2 s ("30 s"). Each behaviour has its negative twin.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
from pyverify.pusher import BitstreamKind, PushError
from pyverify.swap import PERSIST_FAILED, PersistResult

from harness_manager_mps3 import card as C
from harness_manager_mps3 import deploy as dep
from harness_manager_mps3 import os_slots as O
from harness_manager_mps3 import shell as sh
from tests.integration.test_l1_card_virtual import board, open_and_find, overlays  # noqa: F401

PAIR = 128 + 384                # tests.fakes.t2_overlays CLEARING + PARTIAL payload bytes
NANOSOC_PAIR = 2_470_000        # the nanosoc pair on silicon (2.47 MB)
MB29 = 29_000_000

# The scaled world (module docstring).
OLD_STALL_S = 0.3               # "30 s": LINUX_PUSH_TIMEOUT_S
OLD_WAIT_S = 1.0                # "300 s": SWAP_TIMEOUT_S and COMMIT_TIMEOUT_S
CARD_STALL_ROW_S = 3.0          # "900 s": mps3.slot.push_timeout_s
JOB_S = 4.0                     # the pair's card budget (write + read-back x1.5)
CARD_STALL_S = 0.6              # one chunk of the pair waits this long for the card
REPLY_LAG_S = 2.0               # the commit's read-back, after the push
BOARD_IDLE_S = 2.0              # the fake board's own idle limit on a commit's push


@pytest.fixture(autouse=True)
def _no_failed_pushes():
    # a twin leaves a failed (parked) commit behind: never let it reach another test
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()
    yield
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()


@pytest.fixture(autouse=True)
def _no_setting_env(monkeypatch):
    for env in (O.JOB_TIMEOUT_ENV, O.STALL_ENV, O.CARD_WRITE_BPS_ENV, O.CARD_READ_BPS_ENV):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def handed(monkeypatch):
    """The timeouts the deploy handed the swap's control connection: pyverify's ShellClient
    (``timeout=``) and the socket transport under it."""
    seen: dict[str, list[float]] = {"client": [], "transport": []}
    real_client, real_transport = dep.ShellClient, dep._TimedSocketTransport

    class Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            seen["client"].append(kw["timeout"])
            super().__init__(*a, **kw)

    class Transport(real_transport):  # type: ignore[misc, valid-type]
        def __init__(self, host, port, timeout):
            seen["transport"].append(timeout)
            super().__init__(host, port, timeout)

    monkeypatch.setattr(dep, "ShellClient", Client)
    monkeypatch.setattr(dep, "_TimedSocketTransport", Transport)
    return seen


# --- the values handed over, at the real defaults ------------------------------------------------


def test_keep_on_card_hands_the_pusher_and_the_connection_the_cards_budget(
        tmp_path, monkeypatch, overlays, handed):  # noqa: F811
    with board(tmp_path) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        result = session.deploy.deploy(ref, keep_on_card=True)
    assert result.card.kept is True
    pusher = session.deploy.last_commit_pusher
    assert pusher.timeout_s == 900.0                    # the card's stall row, not the swap's 30
    assert session.deploy.last_pusher.timeout_s == 30.0  # the swap's own push is unchanged
    # a small pair: the commit floor (300 s), which is the swap's 300 s too
    assert handed["client"] == handed["transport"] == [300.0]
    assert session.deploy.last_control_timeout_s == 300.0


def test_keep_on_card_waits_the_cards_time_for_a_pair_the_card_takes_longer_over(
        tmp_path, monkeypatch, overlays, handed):  # noqa: F811
    # a slower card (the SPI fix): the pair's write + read-back x1.5 is more than 300 s
    monkeypatch.setenv(O.CARD_WRITE_BPS_ENV, "2")
    monkeypatch.setenv(O.CARD_READ_BPS_ENV, "2")
    want = C.commit_budget(PAIR).job_s
    assert want == pytest.approx(1.5 * (PAIR / 2 + PAIR / 2)) and want > 300.0
    with board(tmp_path) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        session.deploy.deploy(ref, keep_on_card=True)
    assert handed["client"] == handed["transport"] == [pytest.approx(want)]


def test_negative_twin_a_plain_deploy_keeps_30s_and_300s(
        tmp_path, monkeypatch, overlays, handed):  # noqa: F811
    monkeypatch.setenv(O.CARD_WRITE_BPS_ENV, "2")       # a card this deploy never touches
    monkeypatch.setenv(O.CARD_READ_BPS_ENV, "2")
    with board(tmp_path) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        result = session.deploy.deploy(ref)
    assert result.card is None and session.deploy.last_commit_pusher is None
    assert session.deploy.last_pusher.timeout_s == 30.0
    assert handed["client"] == handed["transport"] == [300.0]
    assert session.deploy.last_control_timeout_s == 300.0


def test_the_nanosoc_pairs_keep_budget():
    """The numbers the silicon test runs with: 2.47 MB written at 70 KB/s and read back at
    14 KB/s (~35 s + ~176 s), x1.5 = ~318 s on the control connection, 900 s per chunk."""
    b = C.commit_budget(NANOSOC_PAIR)
    assert b.push_stall_s == 900.0
    assert b.job_s == pytest.approx(1.5 * (NANOSOC_PAIR / 70_000 + NANOSOC_PAIR / 14_000))
    assert 317.0 < b.job_s < 318.0 and b.job_s > dep.SWAP_TIMEOUT_S


def test_the_os_slot_push_budget_is_at_least_the_linux_leads():
    """Pin (Linux lead, 2026-09-29): a 29 MB ``slot push`` gets a per-chunk stall of >= 600 s
    and a whole-job cap of >= 3600 s: what the platform's pyverify was given for the same
    card in platform commit 6e6a2a9 (``--push-timeout 600``, ``--timeout 3600``). Today HM
    budgets 900 s and ~3729 s (the card's rates x1.5)."""
    t = O.slot_timeouts(MB29)
    assert t.push_stall_s >= 600.0 and t.job_s >= 3600.0
    assert t.push_stall_s == 900.0
    assert t.job_s == pytest.approx(1.5 * (MB29 / 70_000 + MB29 / 14_000))    # ~3729 s


# --- a card that stalls > "30 s" and answers after > "300 s" (scaled) ----------------------------


@pytest.fixture
def slow_card(monkeypatch):
    """The scaled world: the old limits, the card's stall row, and card rates that give the
    512 B pair a 3 s budget; the card itself stalls on the pair's partial and answers late."""
    monkeypatch.setattr(dep, "LINUX_PUSH_TIMEOUT_S", OLD_STALL_S)
    monkeypatch.setattr(C, "COMMIT_TIMEOUT_S", OLD_WAIT_S)
    monkeypatch.setenv(O.STALL_ENV, str(CARD_STALL_ROW_S))
    # a fast write (its 64 KiB chunk term stays under the stall row) and a read-back that
    # takes the 512 B pair ~2.7 s: x1.5 = ~4 s
    monkeypatch.setenv(O.CARD_WRITE_BPS_ENV, "1000000")
    monkeypatch.setenv(O.CARD_READ_BPS_ENV, "192")
    budget = C.commit_budget(PAIR)
    assert budget.job_s == pytest.approx(JOB_S, abs=0.01)
    assert budget.push_stall_s == CARD_STALL_ROW_S

    real_send = dep._ReportingPusher._send
    stalls: list[float] = []
    world: dict = {"deploy": None, "stall_s": CARD_STALL_S}

    def send(self, frame, *, kind):
        # The card stops taking bytes for ``stall_s`` on the commit's partial: the send
        # blocks that long, and a per-chunk limit shorter than that fails it as a socket
        # timeout does (PushError).
        dep_ = world["deploy"]
        if dep_ is not None and self is dep_.last_commit_pusher \
                and kind == BitstreamKind.PARTIAL:
            stalls.append(self.timeout_s)
            if self.timeout_s < world["stall_s"]:
                time.sleep(self.timeout_s)
                raise PushError(f"raw-TCP push to {self.host}:{self.tcp_port} failed: timed out")
            time.sleep(world["stall_s"])
        return real_send(self, frame, kind=kind)

    monkeypatch.setattr(dep._ReportingPusher, "_send", send)

    def arm(vb, session, *, lag_s: float = REPLY_LAG_S, stall_s: float = CARD_STALL_S):
        world.update(deploy=session.deploy, stall_s=stall_s)
        session.deploy.swap_timeout_s = OLD_WAIT_S
        vb.shell.swap_await_timeout = BOARD_IDLE_S
        real_commit = vb.shell._run_commit

        def run_commit(rm, desc):
            reply = real_commit(rm, desc)
            time.sleep(lag_s)                      # the read-back, after the last byte
            return reply

        vb.shell._run_commit = run_commit

    arm.stalls = stalls
    return arm


def test_a_keep_outlasts_a_card_stall_and_a_late_reply(
        tmp_path, monkeypatch, overlays, slow_card, handed):  # noqa: F811
    with board(tmp_path) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        slow_card(vb, session)
        result = session.deploy.deploy(ref, keep_on_card=True)
        commits = list(vb.shell.commits)
    assert result.verified and result.card.kept is True, result.card.why
    assert commits == [("synth", "A")]
    assert slow_card.stalls == [CARD_STALL_ROW_S]                  # the card's row, not 0.3
    assert session.deploy.last_commit_pusher.timeout_s == CARD_STALL_ROW_S
    assert session.deploy.last_pusher.timeout_s == OLD_STALL_S     # the swap push: unchanged
    assert handed["client"] == handed["transport"] == [pytest.approx(JOB_S, abs=0.01)]


@pytest.mark.parametrize("old", ["stall", "wait"])
def test_negative_twin_the_old_budget_loses_the_card(
        tmp_path, monkeypatch, overlays, slow_card, handed, old):  # noqa: F811
    # The deploy's old budget, one half at a time: the swap push's stall limit ("30 s") on
    # the pair, or the swap's wait ("300 s") on the commit's reply. Either alone loses it.
    real = C.commit_budget

    def old_budget(n):
        b = real(n)
        return replace(b, push_stall_s=0.0) if old == "stall" else replace(b, job_s=0.0)

    monkeypatch.setattr(C, "commit_budget", old_budget)
    with board(tmp_path) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        slow_card(vb, session)
        result = session.deploy.deploy(ref, keep_on_card=True)
    assert result.verified                                          # the swap stands
    assert result.card.kept is False and result.card.why
    if old == "stall":
        assert session.deploy.last_commit_pusher.timeout_s == OLD_STALL_S
        assert slow_card.stalls == [OLD_STALL_S]
    else:
        assert handed["client"] == handed["transport"] == [OLD_WAIT_S]
        assert "commit reply not received" in result.card.why


# --- the board's own 30 s idle abort (MPS3_SWAP_AWAIT_IDLE_MS), worded as the board's -----------


def test_a_stall_past_the_boards_own_idle_limit_is_the_boards_abort(
        tmp_path, monkeypatch, overlays, slow_card):  # noqa: F811
    # HM now waits 3 s ("900 s") on the chunk, but the board gives up after 2 s ("30 s") of no
    # bytes (overlay_store.c commit_watch, OVLSTORE_ETIMEOUT) and answers the contract name
    # "timeout": worded as the board's, never as HM's
    with board(tmp_path) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        slow_card(vb, session, lag_s=0.0, stall_s=BOARD_IDLE_S + 0.6)
        result = session.deploy.deploy(ref, keep_on_card=True)
    assert slow_card.stalls == [CARD_STALL_ROW_S]                  # HM would have waited
    assert result.verified and result.card.kept is False
    assert result.card.why.startswith("the board gave up (timeout): the card accepted no "
                                      "bytes for 30 s (the harness's own idle limit")
    assert "the swap stands" in result.card.why


@pytest.mark.parametrize("err, board_words", [
    ("timeout", True),
    ("ETIMEOUT", True),
    ("commit idle timeout", True),
    ("io", False),
    ("crc", False),
    # HM's own failures are never the board's
    ("push failed: raw-TCP push to 127.0.0.1:6910 failed: timed out", False),
    ("OSError: commit reply not received (timed out); the control channel may be out of step",
     False),
    ("TimeoutError: timed out", False),
])
def test_only_the_boards_timeout_is_worded_as_the_boards(err, board_words):
    out = dep.card_outcome(PersistResult(status=PERSIST_FAILED, err=err, warning=True))
    assert out.kept is False
    assert out.why.startswith(f"the board gave up ({err}):") is board_words
    assert out.why.startswith(f"the card write failed ({err})") is not board_words
