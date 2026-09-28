"""FIX-PACK-1 item 5: Harness Manager is a good citizen on the hub's sshd.

The first real-board session (2026-09-28) met "kex_exchange_identification: read: Connection
reset by peer" on ``lease show`` and the request watcher: the hub's sshd throttling new
connections (MaxStartups). SERIAL-6900 counted 2-6 long-lived plus 3-12 one-shot ssh
connections per hub, none reused, and no jitter on tunnel restarts. Now:

(a) each one-shot hub command takes one of ``HUB_ONE_SHOT_MAX`` slots per hub
    (``hub._capped``, a subclass of pyverify's ``SshHubRunner``);
(b) concurrent views of one hub share one read (``LeaseService._cached``, single-flight);
(c) a tunnel's restart wait is stretched at random (``SshTunnel``, ``BACKOFF_JITTER``);
(d) a reset in ssh's identification exchange (before authentication: nothing ran on the
    hub) is tried once more after a short pause; a command that started is never re-run.

No hub, no ssh: ``subprocess.run`` is a fake for the runner, the lease service reads a fake
client, the tunnel runs over ``FakeSsh``. Each check has its negative twin.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pyverify.lease import SshHubRunner

from harness_manager.core.errors import UnreachableError
from harness_manager.services.lease import LeaseService
from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3 import tunnel as T
from tests.fakes.l1_fake_ssh import FakeSsh
from tests.unit.test_l1_tunnel import Upstream, make_tunnel

HUB = "hub.invalid"
KEX = "kex_exchange_identification: read: Connection reset by peer\r\n"


@pytest.fixture(autouse=True)
def _fresh_slots(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(hubmod, "HUB_KEX_RETRY_S", 0.0, raising=False)
    monkeypatch.setattr(hubmod, "HUB_KEX_RETRY_JITTER_S", 0.0, raising=False)
    forget = getattr(hubmod, "forget_hub_slots", lambda: None)
    forget()
    yield
    forget()


class FakeRun:
    """``subprocess.run`` for the runner: scripted results, a gate that holds each call,
    and the most calls it ever saw at once (per destination host)."""

    def __init__(self, results: list[tuple[int, str, str]] | None = None) -> None:
        self.results = list(results or [])
        self.gate = threading.Event()
        self.gate.set()
        self.mu = threading.Lock()
        self.calls: list[list[str]] = []
        self.now: dict[str, int] = {}
        self.most: dict[str, int] = {}

    def __call__(self, cmd: Any, **kw: Any) -> Any:
        host = next((a for a in cmd if a.endswith(".invalid")), "?")
        with self.mu:
            self.calls.append(list(cmd))
            self.now[host] = self.now.get(host, 0) + 1
            self.most[host] = max(self.most.get(host, 0), self.now[host])
            rc, out, err = self.results.pop(0) if self.results else (0, "ok\n", "")
        try:
            self.gate.wait(10)
        finally:
            with self.mu:
                self.now[host] -= 1
        return subprocess.CompletedProcess(cmd, rc, out, err)


def run_many(runner: Any, n: int) -> list[threading.Thread]:
    threads = [threading.Thread(target=runner, args=(["fpgahub", "lease", "show", "x"],),
                                kwargs={"timeout": 10}) for _ in range(n)]
    for t in threads:
        t.start()
    return threads


# --- (a) the per-hub cap -------------------------------------------------------------------------


def test_one_shot_hub_commands_are_capped_per_hub(monkeypatch):
    fake = FakeRun()
    fake.gate.clear()                                  # every command is still running
    monkeypatch.setattr(subprocess, "run", fake)
    runner = hubmod.default_runner_factory(HUB, "fpga")
    assert isinstance(runner, SshHubRunner) and runner == SshHubRunner(HUB, group="fpga")
    threads = run_many(runner, 10)
    deadline = time.monotonic() + 5
    while len(fake.calls) < hubmod.HUB_ONE_SHOT_MAX and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)                                    # the rest wait for a slot
    assert len(fake.calls) == hubmod.HUB_ONE_SHOT_MAX
    fake.gate.set()
    for t in threads:
        t.join(10)
    stats = hubmod.HUB_RUN_STATS[HUB]
    assert len(fake.calls) == 10 and fake.most[HUB] == hubmod.HUB_ONE_SHOT_MAX
    assert stats.max_in_flight == hubmod.HUB_ONE_SHOT_MAX and stats.waited >= 6
    assert stats.in_flight == 0


def test_twin_two_hubs_do_not_share_their_slots(monkeypatch):
    fake = FakeRun()
    fake.gate.clear()
    monkeypatch.setattr(subprocess, "run", fake)
    a = hubmod.default_runner_factory("a.invalid", "fpga")
    b = hubmod.default_runner_factory("b.invalid", "fpga", jump="bastion.invalid")
    threads = run_many(a, 4) + run_many(b, 4)
    deadline = time.monotonic() + 5
    while len(fake.calls) < 8 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(fake.calls) == 8, "each hub has its own slots"
    fake.gate.set()
    for t in threads:
        t.join(10)


def test_a_command_that_cannot_get_a_slot_in_its_timeout_says_so(monkeypatch):
    fake = FakeRun()
    fake.gate.clear()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(hubmod, "HUB_ONE_SHOT_MAX", 1)
    runner = hubmod.default_runner_factory(HUB, "fpga")
    held = run_many(runner, 1)
    while not fake.calls:
        time.sleep(0.01)
    with pytest.raises(TimeoutError, match=f"one of 1 ssh connections to {HUB}"):
        runner(["fpgahub", "whoami"], timeout=0.2)
    fake.gate.set()
    for t in held:
        t.join(10)
    assert runner(["fpgahub", "whoami"], timeout=1).returncode == 0     # the twin: free again


# --- (d) one retry of a reset before authentication -----------------------------------------------


def test_a_kex_reset_before_authentication_is_tried_once_more(monkeypatch):
    fake = FakeRun([(255, "", KEX), (0, "lease: free\n", "")])
    monkeypatch.setattr(subprocess, "run", fake)
    out = hubmod.default_runner_factory(HUB, "fpga")(["fpgahub", "lease", "show", "x"])
    assert out.returncode == 0 and out.stdout == "lease: free\n"
    assert len(fake.calls) == 2 and fake.calls[0] == fake.calls[1]
    assert hubmod.HUB_RUN_STATS[HUB].retried == 1


@pytest.mark.parametrize("rc, out, err", [
    (255, "", "client_loop: send disconnect: Connection reset by peer\r\n"),  # it ran
    (255, "partial output\n", KEX),                                          # stdout: it ran
    (1, "", KEX),                                                            # the command's rc
    (255, "", "ssh: connect to host hub.invalid port 22: Connection refused\r\n"),
])
def test_twin_a_command_that_started_or_another_failure_is_never_rerun(monkeypatch, rc, out,
                                                                       err):
    fake = FakeRun([(rc, out, err), (0, "again\n", "")])
    monkeypatch.setattr(subprocess, "run", fake)
    got = hubmod.default_runner_factory(HUB, "fpga")(["fpgahub", "lease", "release", "x"])
    assert got.returncode == rc and len(fake.calls) == 1


def test_twin_only_one_retry_then_the_reset_is_the_answer(monkeypatch):
    fake = FakeRun([(255, "", KEX), (255, "", KEX), (0, "never\n", "")])
    monkeypatch.setattr(subprocess, "run", fake)
    got = hubmod.default_runner_factory(HUB, "fpga", jump="bastion.invalid")(["fpgahub", "x"])
    assert got.returncode == 255 and "kex_exchange_identification" in got.stderr
    assert len(fake.calls) == 2


def test_reset_before_auth_reads_sshs_own_words():
    ok = SimpleNamespace
    assert hubmod.reset_before_auth(ok(returncode=255, stdout="", stderr=KEX))
    assert hubmod.reset_before_auth(ok(returncode=255, stdout="", stderr=(
        "kex_exchange_identification: Connection closed by remote host\r\n"
        "Connection closed by 10.0.0.1 port 22\r\n")))
    assert hubmod.reset_before_auth(ok(returncode=255, stdout="",
                                       stderr="ssh_exchange_identification: read: Connection "
                                              "reset by peer"))
    assert not hubmod.reset_before_auth(ok(returncode=0, stdout="", stderr=KEX))
    assert not hubmod.reset_before_auth(ok(returncode=255, stdout="",
                                           stderr="Permission denied (publickey)."))


# --- (b) single-flight lease views ----------------------------------------------------------------


class SlowClient:
    """A hub client whose ``lease_show`` takes a while and counts its calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.go = threading.Event()
        self.fail: BaseException | None = None
        self.mu = threading.Lock()

    def lease_show(self) -> Any:
        with self.mu:
            self.calls += 1
            n = self.calls
        self.go.wait(10)
        if self.fail is not None:
            raise self.fail
        return {"held": False, "n": n}


def lease_hub(client: SlowClient) -> Any:
    return SimpleNamespace(host=HUB, target="mps3_01_pl", client=client)


def together(fn: Any, n: int) -> list[Any]:
    out: list[Any] = [None] * n

    def one(i: int) -> None:
        try:
            out[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - the test reads it
            out[i] = exc

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    return [threads, out]


def wait_calls(svc: LeaseService, client: SlowClient, joined: int) -> None:
    deadline = time.monotonic() + 5
    while (client.calls < 1 or svc.shared_reads < joined) and time.monotonic() < deadline:
        time.sleep(0.01)


def test_concurrent_views_of_one_hub_share_one_read(tmp_path: Path):
    svc, client = LeaseService(tmp_path), SlowClient()
    hub = lease_hub(client)
    threads, out = together(lambda: svc._show(hub), 6)
    wait_calls(svc, client, 5)
    client.go.set()
    for t in threads:
        t.join(10)
    assert client.calls == 1 and svc.hub_reads == 1 and svc.shared_reads == 5
    assert all(o == {"held": False, "n": 1} for o in out)
    assert svc._show(hub) == {"held": False, "n": 1}               # cached, no new read
    assert client.calls == 1


def test_twin_a_fresh_view_and_a_view_after_forget_never_share_an_older_read(tmp_path: Path):
    svc, client = LeaseService(tmp_path), SlowClient()
    hub = lease_hub(client)
    threads, out = together(lambda: svc._show(hub), 1)
    while client.calls < 1:
        time.sleep(0.01)
    svc.forget(hub)                                    # the hub said something changed
    later, out2 = together(lambda: svc._show(hub), 1)
    fresh, out3 = together(lambda: svc._show(hub, fresh=True), 1)
    deadline = time.monotonic() + 5
    while client.calls < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    client.go.set()
    for t in threads + later + fresh:
        t.join(10)
    assert client.calls == 3 and svc.shared_reads == 0
    assert out[0]["n"] == 1 and {out2[0]["n"], out3[0]["n"]} == {2, 3}


def test_twin_an_error_reaches_every_sharer_and_is_never_cached(tmp_path: Path):
    svc, client = LeaseService(tmp_path), SlowClient()
    client.fail = UnreachableError("kex_exchange_identification: read: Connection reset by peer")
    hub = lease_hub(client)
    threads, out = together(lambda: svc._show(hub), 4)
    wait_calls(svc, client, 3)
    client.go.set()
    for t in threads:
        t.join(10)
    assert client.calls == 1 and all(isinstance(o, UnreachableError) for o in out)
    client.fail = None
    assert svc._show(hub)["n"] == 2                    # asked again: nothing was cached


# --- (c) jitter on the tunnel's restart wait ------------------------------------------------------


def restart_waits(**kw: Any) -> list[float]:
    fake, board = FakeSsh(), Upstream()
    try:
        with make_tunnel(fake, board, **kw) as t:
            fake.current.drop()
            deadline = time.monotonic() + 10
            while (t.restarts < 1 or t.state != "up") and time.monotonic() < deadline:
                time.sleep(0.02)
            assert t.restarts == 1
            return list(t.waits)
    finally:
        board.close()


def test_a_restart_wait_is_stretched_at_random_never_shortened():
    assert T.BACKOFF_JITTER > 0
    assert restart_waits(rand=lambda: 1.0) == [pytest.approx(0.05 * (1 + T.BACKOFF_JITTER))]
    assert restart_waits(rand=lambda: 0.0) == [pytest.approx(0.05)]      # the step, at least


def test_twin_no_jitter_is_the_bare_step():
    assert restart_waits(backoff_jitter=0.0, rand=lambda: 1.0) == [pytest.approx(0.05)]
