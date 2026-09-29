"""LEASE-FRESH: the lease is what our own acquire said, at once; one hub hiccup does not undo it.

david (2026-09-29, board mps3-02): an acquire WORKED ("lease held on mps3_02 until 22:06:29"),
but the `lease show` right after it met the hub sshd's throttle ("ssh: connect to host ... port
22: Connection reset by peer"), and the page said "Lease unknown", "Paused: lease unknown" and
"Needs attention". Now:

- the acquire's (and release's, and heartbeat's) own answer is the lease state: ``lease.state``
  carries it with ``source``/``here``/``at``, and the service keeps it as the last known state;
- a failed read carries that state (``stale``) while it is valid: fewer than 3 failed reads in
  a row, confirmed within 5 min, before its expiry, the token still here; then "unknown";
- a transient ssh failure is tried again inside one read (at most 3 attempts), but a command
  that may have started on the hub (acquire, release) is never re-run.

No hub, no ssh: the L1 fake fpgahub (a runner) and a fake ``subprocess.run``. Each check has
its negative twin.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pyverify.lease import RunResult

from harness_manager.core.errors import UnreachableError
from harness_manager.core.events import EventBus
from harness_manager.services import lease as leasemod
from harness_manager.services.lease import LeaseService, iso_utc, parse_utc
from harness_manager.services.quiet import KIND_LEASE_UNKNOWN, BackgroundGate, lease_elsewhere
from harness_manager_mps3 import hub as hubmod
from tests.fakes.l1_fake_hub import EXPIRES, FakeHub

HUB = "mapstone-dev.ecs.soton.ac.uk"
TARGET = "mps3_01_pl"
RESET = f"ssh: connect to host {HUB} port 22: Connection reset by peer\r\n"
KEX = "kex_exchange_identification: read: Connection reset by peer\r\n"
DROPPED = "client_loop: send disconnect: Connection reset by peer\r\n"
CLOSED = f"Connection to {HUB} closed by remote host.\r\n"
#: the fake hub's leases end at EXPIRES; our wall clock starts an hour before
START = (parse_utc(EXPIRES) or 0.0) - 3600.0


class FlakyHub(FakeHub):
    """The L1 fake fpgahub whose next ``fail_show`` ``lease show`` calls meet the hub sshd's
    throttle (ssh's exit 255 and its words), counting every ``lease show``."""

    def __init__(self) -> None:
        super().__init__(TARGET)
        self.fail_show = 0
        self.shows = 0

    def __call__(self, argv: Any, timeout: float | None = None) -> RunResult:
        if list(argv)[:3] == ["fpgahub", "lease", "show"]:
            self.shows += 1
            if self.fail_show > 0:
                self.fail_show -= 1
                self.calls.append(list(argv))
                return RunResult(255, "", RESET)
        return super().__call__(argv, timeout)


class Hub:
    def __init__(self, fake: Any) -> None:
        self.host, self.target = HUB, TARGET
        self.client = hubmod.HubClient(HUB, TARGET, runner=fake)


class Clock:
    def __init__(self) -> None:
        self.mono, self.wall_t = 1000.0, START

    def now(self) -> float:
        return self.mono

    def wall(self) -> float:
        return self.wall_t

    def advance(self, s: float) -> None:
        self.mono += s
        self.wall_t += s


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.fake = FlakyHub()
        self.hub = Hub(self.fake)
        self.clock = Clock()
        self.bus = EventBus()
        self.seen: list[dict[str, Any]] = []
        self.bus.subscribe("lease.state", lambda ev: self.seen.append(dict(ev.data)))
        self.svc = LeaseService(tmp_path / "state", self.bus, clock=self.clock.now,
                                wall_clock=self.clock.wall)
        # the background gate as the daemon wires it (``_lease_elsewhere``), one viewer on b1
        self.gate = BackgroundGate(lease_of=lambda _b: lease_elsewhere(self.svc.view(self.hub)),
                                   clock=self.clock.now)
        self.gate.view("b1", "page-1", ttl_s=300)

    def acquire(self) -> dict[str, Any]:
        return self.svc.acquire(self.hub, board_id="b1", ttl_s=600, holder="hm-test",
                                heartbeat=False)

    def fail_reads(self, n: int, *, spaced: bool = True) -> list[Any]:
        """``n`` views that each meet a failed ``lease show`` (``spaced``: far enough apart to
        count as separate misses); each result is the view or the error it raised."""
        out: list[Any] = []
        for _ in range(n):
            if spaced:
                self.clock.advance(leasemod.READ_RETRY_S + 1.0)
            self.fake.fail_show = 1
            try:
                out.append(self.svc.view(self.hub))
            except UnreachableError as exc:
                out.append(exc)
        return out


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# --- the acquire's own answer is the state ------------------------------------------------------


def test_acquire_says_held_by_you_at_once_and_one_failed_read_keeps_it(rig):
    rig.acquire()
    ev = rig.seen[-1]
    assert (ev["state"], ev["source"], ev["here"], ev["holder"], ev["expires_at"]) == (
        "held", "acquire", True, "hm-test", EXPIRES)
    assert ev["at"] == iso_utc(START)                      # stamped with when the hub said so
    shows = rig.fake.shows
    [v] = rig.fail_reads(1, spaced=False)                  # the very next read: the sshd reset
    assert rig.fake.shows == shows + 1, "the read was made, and failed"
    assert not isinstance(v, Exception), v
    lease = v["lease"]
    assert (lease["here"], lease["mine"], lease["holder"], lease["expires_at"]) == (
        True, True, "hm-test", EXPIRES)
    assert v["stale"]["source"] == "acquire" and v["stale"]["misses"] == 1
    assert v["stale"]["confirmed_at"] == iso_utc(START)
    assert "Connection reset by peer" in v["stale"]["error"]
    # no attention: background work is not paused (it is our lease, until its expiry)
    assert rig.gate.check("b1") is None


def test_twin_three_failed_reads_in_a_row_say_unknown_and_pause_background(rig):
    rig.acquire()
    first, second, third = rig.fail_reads(3)
    assert first["stale"]["misses"] == 1 and second["stale"]["misses"] == 2
    assert isinstance(third, UnreachableError)
    assert "did not answer 3 lease reads in a row" in third.message
    assert "Connection reset by peer" in third.message                  # the hub's own words
    rig.clock.advance(leasemod.READ_RETRY_S + 1.0)
    rig.fake.fail_show = 1
    q = rig.gate.check("b1")
    assert q is not None and q.kind == KIND_LEASE_UNKNOWN              # not known is not free


def test_reads_that_fail_together_are_one_miss_and_do_not_ask_the_hub_again(rig):
    rig.acquire()
    rig.fail_reads(1)
    shows = rig.fake.shows
    for _ in range(5):                                    # the page, the gate, the panel ...
        v = rig.svc.view(rig.hub)
        assert v["stale"]["misses"] == 1 and v["lease"]["here"]
    assert rig.fake.shows == shows, "within READ_RETRY_S the carried state answers"
    # twin: after READ_RETRY_S the hub is asked again, and an answer ends the carry
    rig.clock.advance(leasemod.READ_RETRY_S + 1.0)
    v = rig.svc.view(rig.hub)
    assert rig.fake.shows == shows + 1 and "stale" not in v and v["lease"]["here"]


def test_a_read_that_works_starts_the_count_again(rig):
    rig.acquire()
    rig.fail_reads(2)
    rig.clock.advance(leasemod.READ_RETRY_S + 1.0)
    assert "stale" not in rig.svc.view(rig.hub)           # the hub answered
    again = rig.fail_reads(2)
    assert [v["stale"]["misses"] for v in again] == [1, 2]
    assert again[-1]["stale"]["source"] == "show"


def test_twin_no_known_state_and_a_failed_read_is_unknown_at_once(rig):
    [v] = rig.fail_reads(1)
    assert isinstance(v, UnreachableError) and "Connection reset by peer" in v.message


# --- release, heartbeat, expiry, the token ------------------------------------------------------


def test_release_says_not_leased_at_once(rig):
    rig.acquire()
    rig.svc.release(rig.hub, board_id="b1")
    ev = rig.seen[-1]
    assert (ev["state"], ev["source"], ev["here"]) == ("released", "release", False)
    [v] = rig.fail_reads(1, spaced=False)
    assert v["lease"] is None and v["stale"]["source"] == "release"
    # a board last seen free may have been taken since: background stays quiet (conservative)
    q = rig.gate.check("b1")
    assert q is not None and q.kind == KIND_LEASE_UNKNOWN


def test_twin_a_fresh_free_read_lets_background_go(rig):
    rig.acquire()
    rig.svc.release(rig.hub, board_id="b1")
    assert rig.svc.view(rig.hub)["lease"] is None and rig.gate.check("b1") is None


def test_expiry_passed_and_a_failed_read_is_unknown(rig):
    rig.acquire()
    rig.clock.wall_t = (parse_utc(EXPIRES) or 0.0) + 1.0       # the lease has run out ...
    rig.clock.mono += 30.0                                     # ... though confirmed lately
    [v] = rig.fail_reads(1, spaced=False)
    assert isinstance(v, UnreachableError) and "ended at" in v.message


def test_twin_a_known_state_older_than_five_minutes_is_not_carried(rig):
    rig.acquire()
    rig.clock.mono += leasemod.KNOWN_MAX_AGE_S + 1.0           # still before its expiry
    [v] = rig.fail_reads(1, spaced=False)
    assert isinstance(v, UnreachableError) and "last confirmed" in v.message


def test_a_lease_whose_token_went_is_not_carried_as_ours(rig):
    rig.acquire()
    rig.svc.store.drop(HUB, TARGET)                            # released by the CLI, say
    [v] = rig.fail_reads(1, spaced=False)
    assert isinstance(v, UnreachableError) and "token" in v.message


def test_a_heartbeat_confirms_the_lease_again(tmp_path):
    rig = Rig(tmp_path)
    rig.svc = LeaseService(tmp_path / "state", rig.bus, clock=rig.clock.now,
                           wall_clock=rig.clock.wall, heartbeat_s=0.0)
    rig.acquire()
    rig.svc.track("b1", rig.hub)                               # not announced: it will say so
    rig.clock.advance(leasemod.KNOWN_MAX_AGE_S - 10.0)
    rig.svc.beat_due(force=True)
    ev = rig.seen[-1]
    assert (ev["state"], ev["source"], ev["here"]) == ("held", "heartbeat", True)
    rig.clock.advance(60.0)                                    # 5 min after the acquire
    [v] = rig.fail_reads(1, spaced=False)
    assert v["lease"]["here"] and v["stale"]["source"] == "heartbeat"
    rig.svc.close()


# --- the retry inside one read (the ssh runner) ---------------------------------------------------


class FakeRun:
    """``subprocess.run`` for pyverify's ssh runner: scripted ``(rc, stdout, stderr)``."""

    def __init__(self, results: list[tuple[int, str, str]]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: Any, **_kw: Any) -> Any:
        self.calls.append(list(cmd))
        rc, out, err = self.results.pop(0) if self.results else (0, "", "")
        return subprocess.CompletedProcess(cmd, rc, out, err)


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(hubmod, "HUB_KEX_RETRY_S", 0.0)
    monkeypatch.setattr(hubmod, "HUB_KEX_RETRY_JITTER_S", 0.0)
    hubmod.forget_hub_slots()

    def install(results: list[tuple[int, str, str]]) -> FakeRun:
        fake = FakeRun(results)
        monkeypatch.setattr(subprocess, "run", fake)
        return fake

    yield install
    hubmod.forget_hub_slots()


def client() -> hubmod.HubClient:
    return hubmod.HubClient("hub.invalid", TARGET)             # the real (capped) ssh runner


def test_a_transient_reset_on_lease_show_is_retried_within_one_read(ssh):
    fake = ssh([(255, "", RESET), (255, "", KEX), (0, "not leased\n", "")])
    st = client().lease_status()
    assert st.held is False and len(fake.calls) == 3
    assert hubmod.HUB_RUN_STATS["hub.invalid"].retried == 2


def test_a_read_dropped_after_it_started_is_retried_too(ssh):
    fake = ssh([(255, "", DROPPED), (0, f"held by x@y (user x, expires {EXPIRES})\n", "")])
    st = client().lease_status()
    assert st.held is True and len(fake.calls) == 2


def test_twin_an_acquire_reset_after_the_command_started_is_not_retried(ssh):
    fake = ssh([(255, "", DROPPED),
                (0, f"granted token=tok-1 expires={EXPIRES} tier=interactive\n", "")])
    with pytest.raises(UnreachableError):
        client().lease_acquire("hm-test", ttl=600, timeout_s=1.0)
    assert len(fake.calls) == 1, "it may have run on the hub: never run it twice"


def test_an_acquire_turned_away_before_it_started_is_retried(ssh):
    fake = ssh([(255, "", RESET), (0, f"granted token=tok-1 expires={EXPIRES} tier=interactive\n", "")])
    lease, expires = client().lease_acquire("hm-test", ttl=600, timeout_s=1.0)
    assert lease.token == "tok-1" and expires == EXPIRES and len(fake.calls) == 2


def test_twin_a_release_closed_by_the_hub_is_not_retried(ssh):
    fake = ssh([(255, "", CLOSED), (0, "released\n", "")])
    with pytest.raises(UnreachableError):
        client().lease_release("tok-1", "hm-test")
    assert len(fake.calls) == 1


def test_twin_three_attempts_at_most_then_the_read_fails(ssh):
    fake = ssh([(255, "", RESET)] * 3 + [(0, "not leased\n", "")])
    with pytest.raises(UnreachableError, match="Connection reset by peer"):
        client().lease_status()
    assert len(fake.calls) == 3


@pytest.mark.parametrize("argv, rc, out, err, kind", [
    (["fpgahub", "lease", "acquire", TARGET], 255, "", RESET, "before"),
    (["fpgahub", "lease", "release", TARGET], 255, "", KEX, "before"),
    (["fpgahub", "lease", "show", TARGET], 255, "", DROPPED, "read"),
    (["fpgahub", "whoami", "--json"], 255, "", CLOSED, "read"),
    (["sh", "-c", "script", "hm-lease", "list", "/tmp/x", TARGET, "fpga", "req-"], 255, "", DROPPED,
     "read"),
    # twins: may have run, or not ssh turning it away at all
    (["fpgahub", "lease", "acquire", TARGET], 255, "", DROPPED, ""),
    (["fpgahub", "lease", "heartbeat", TARGET], 255, "", CLOSED, ""),
    (["fpgahub", "board", "lease", "revoke", "b"], 255, "", DROPPED, ""),
    (["sh", "-c", "script", "hm-lease", "put", "/tmp/x", TARGET, "fpga", "n"], 255, "", DROPPED, ""),
    (["fpgahub", "lease", "acquire", TARGET], 255, "granted token=t\n", KEX, ""),
    (["fpgahub", "lease", "show", TARGET], 1, "", RESET, ""),
    (["fpgahub", "lease", "show", TARGET], 255, "", "ssh: connect to host h port 22: Connection "
     "refused\r\n", ""),
])
def test_retry_kind_reads_the_command_and_sshs_words(argv, rc, out, err, kind):
    assert hubmod.retry_kind(argv, SimpleNamespace(returncode=rc, stdout=out, stderr=err)) == kind


# --- the explicit gates want a fresh answer ---------------------------------------------------------


def test_an_install_gate_refuses_a_carried_lease_it_cannot_confirm(rig):
    from harness_manager.services.update.lease_gate import lease_state

    session = SimpleNamespace(hub=rig.hub)
    rig.acquire()
    rig.fail_reads(1, spaced=False)                        # carried: fine for the page ...
    shows = rig.fake.shows
    rig.fake.fail_show = 1                                 # ... the gate's own read fails too
    st = lease_state(session, rig.svc)
    assert rig.fake.shows == shows + 1, "forget(): the gate asked the hub, not the carry"
    assert not st["mine"] and st["holder"].startswith("unknown")
    assert "cannot confirm you hold the lease" in st["reason"] and "last confirmed" in st["reason"]


def test_twin_an_install_gate_passes_when_the_hub_answers(rig):
    from harness_manager.services.update.lease_gate import lease_state

    rig.acquire()
    rig.fail_reads(1, spaced=False)
    st = lease_state(SimpleNamespace(hub=rig.hub), rig.svc)   # the hub answers this time
    assert st["mine"] and st["reason"] == ""


def test_not_fresh_says_why_only_for_a_carried_view():
    assert leasemod.not_fresh({"lease": None}) == ""
    assert leasemod.not_fresh(None) == ""
    why = leasemod.not_fresh({"stale": {"confirmed_at": iso_utc(START), "error": "reset"}})
    assert why.startswith("the hub did not answer (reset)") and "last confirmed" in why
