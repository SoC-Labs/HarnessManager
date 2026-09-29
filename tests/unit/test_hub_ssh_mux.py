"""SSH-MUX (2026-09-29): the service reuses one ssh connection per hub.

The hub logged 549 accepted ssh connections in an hour from the machine running the service
(~9/min, each a full key exchange): the rate that trips its sshd MaxStartups ("kex_exchange_
identification: read: Connection reset by peer"). Now the service's one-shot hub commands go
through one OpenSSH master per hub (``ControlMaster=auto``, ``ControlPath=<dir>/%C``,
``ControlPersist=10m``), set in one place (the capped runner's ``build``), off on Windows, off
when switched off, off (said once) when the socket path would be too long, and the service's
stop closes the masters it started and nothing else.

No ssh runs: ``subprocess.run`` is a fake. ``FakeMuxSsh`` models OpenSSH's multiplexing (a
connection per call without a master; one master per ControlPath, reused until it has been
idle for ControlPersist) over the L1 fake fpgahub, on a simulated clock, and counts both the
connections and the calls by kind. Each check has its negative twin.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from harness_manager.core import lifecycle
from harness_manager.services.lease import LeaseService, parse_utc
from harness_manager.services.quiet import lease_elsewhere
from harness_manager_mps3 import hub as hubmod
from tests.fakes.l1_fake_hub import EXPIRES, FakeHub

HUB = "hub.invalid"
TARGET = "mps3_01_pl"
SOCK = "0123456789abcdef0123456789abcdef01234567"      # what %C expands to (40 hex)


@pytest.fixture
def mux(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The service, with its socket directories under a short private temporary directory
    (never ~/.ssh, never the real runtime dir)."""
    base = Path(tempfile.mkdtemp(prefix="hmx", dir="/tmp"))
    monkeypatch.setenv(hubmod.HUB_SSH_MUX_DIR_ENV, str(base))
    monkeypatch.delenv(hubmod.HUB_SSH_MUX_ENV, raising=False)
    monkeypatch.setattr(hubmod, "_mux_dir", None)
    monkeypatch.setattr(hubmod, "_mux_warned", set())
    monkeypatch.setattr(hubmod, "HUB_KEX_RETRY_S", 0.0)
    monkeypatch.setattr(hubmod, "HUB_KEX_RETRY_JITTER_S", 0.0)
    hubmod.forget_hub_slots()
    lifecycle.mark_service(True)
    lifecycle.run_stop_hooks()                         # none left over from another test
    try:
        yield base
    finally:
        lifecycle.run_stop_hooks()
        lifecycle.mark_service(False)
        hubmod.forget_hub_slots()
        shutil.rmtree(base, ignore_errors=True)


def opts(cmd: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, word in enumerate(cmd[:-1]):
        if word == "-o":
            k, _, v = cmd[i + 1].partition("=")
            out.setdefault(k.lower(), v)               # ssh: the FIRST value wins
    return out


# --- the options -------------------------------------------------------------------------------


def test_the_services_hub_commands_reuse_one_connection_per_hub(mux):
    for runner in (hubmod.default_runner_factory(HUB, "fpga"),
                   hubmod.default_runner_factory(HUB, "fpga", jump="bastion.invalid")):
        cmd = runner.build(["fpgahub", "lease", "show", TARGET])
        o = opts(cmd)
        own = mux / str(os.getpid())
        assert o["controlmaster"] == "auto" and o["controlpersist"] == hubmod.HUB_SSH_PERSIST
        assert o["controlpath"] == f"{own}/%C"
        assert "ControlPath=none" not in cmd and o["batchmode"] == "yes"   # pyverify's rest kept
        assert cmd[-2] == HUB and "fpgahub lease show" in cmd[-1]
    assert own.is_dir() and (own.stat().st_mode & 0o777) == 0o700
    assert str(Path.home() / ".ssh") not in " ".join(cmd)


@pytest.mark.parametrize("why", ["windows", "env", "constant", "cli"])
def test_twin_no_reuse_on_windows_when_switched_off_or_outside_the_service(mux, monkeypatch, why):
    if why == "windows":
        monkeypatch.setattr(sys, "platform", "win32")
    elif why == "env":
        monkeypatch.setenv(hubmod.HUB_SSH_MUX_ENV, "0")
    elif why == "constant":
        monkeypatch.setattr(hubmod, "HUB_SSH_MUX", False)
    else:
        lifecycle.mark_service(False)
    cmd = hubmod.default_runner_factory(HUB, "fpga").build(["fpgahub", "whoami", "--json"])
    o = opts(cmd)
    assert o["controlpath"] == "none" and "controlmaster" not in o and "controlpersist" not in o
    assert hubmod.mux_off_reason()
    assert not (mux / str(os.getpid())).exists(), "nothing made when it is off"


def test_a_socket_path_over_the_unix_limit_falls_back_and_says_so_once(mux, monkeypatch, caplog):
    monkeypatch.setenv(hubmod.HUB_SSH_MUX_DIR_ENV, "/tmp/" + "x" * 60)
    caplog.set_level(logging.WARNING, logger=hubmod.__name__)
    runner = hubmod.default_runner_factory(HUB, "fpga")
    for _ in range(3):
        o = opts(runner.build(["fpgahub", "lease", "show", TARGET]))
        assert o["controlpath"] == "none" and "controlmaster" not in o
    said = [r.getMessage() for r in caplog.records if "not reused" in r.getMessage()]
    assert len(said) == 1 and "Unix limit" in said[0]
    assert not Path("/tmp/" + "x" * 60).exists()


def test_twin_a_path_just_under_the_limit_is_used(mux, monkeypatch):
    own = len(str(mux / str(os.getpid())))
    assert own + 1 + 40 + 17 < hubmod.SOCKET_PATH_MAX
    assert hubmod.mux_options()[3].startswith(f"ControlPath={mux}/")


def test_a_directory_someone_else_can_enter_is_not_used(mux, caplog):
    own = mux / str(os.getpid())
    own.mkdir(parents=True)
    os.chmod(mux, 0o755)                                # the base is open to others
    caplog.set_level(logging.WARNING, logger=hubmod.__name__)
    assert hubmod.mux_options() == []
    assert any("not a private directory" in r.getMessage() for r in caplog.records)


# --- the stop hook -------------------------------------------------------------------------------


class RecordRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: Any, **_kw: Any) -> Any:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_the_stop_hook_closes_only_the_masters_this_service_started(mux, monkeypatch):
    own = hubmod.mux_dir()
    assert own is not None
    (own / SOCK).touch()                                # a master this service started
    other = mux / "1"                                   # pid 1: another process, alive
    other.mkdir(mode=0o700)
    (other / SOCK).touch()
    stranger = mux.parent / (mux.name + "-elsewhere")   # not even ours
    stranger.mkdir()
    (stranger / SOCK).touch()
    run = RecordRun()
    monkeypatch.setattr(subprocess, "run", run)
    assert lifecycle.run_stop_hooks() == ["hub ssh masters"]
    assert run.calls == [["ssh", "-o", f"ControlPath={own / SOCK}", "-o", "BatchMode=yes",
                          "-O", "exit", "harness-manager-hub"]]
    assert not own.exists()
    assert (other / SOCK).exists() and (stranger / SOCK).exists()
    shutil.rmtree(stranger)


def test_twin_a_service_that_never_reused_a_connection_closes_nothing(mux, monkeypatch):
    run = RecordRun()
    monkeypatch.setattr(subprocess, "run", run)
    assert lifecycle.run_stop_hooks() == [] and run.calls == []
    assert hubmod.close_own_masters() == []


def test_the_empty_directories_of_services_that_are_gone_are_swept(mux):
    dead = mux / "4194303"                              # above pid_max: no such process
    dead.mkdir(parents=True, mode=0o700)
    busy = mux / "4194302"
    busy.mkdir(mode=0o700)
    (busy / SOCK).touch()                               # a master may still be alive: kept
    os.chmod(mux, 0o700)
    assert hubmod.mux_dir() is not None
    assert not dead.exists() and busy.exists()


# --- how many connections: one board, idle, 10 simulated minutes ----------------------------------


class FakeMuxSsh:
    """``subprocess.run`` for the hub runner: OpenSSH's multiplexing on a simulated clock,
    and the L1 fake fpgahub behind it. Counts connections (a TCP connect and a key exchange
    each) and calls by kind."""

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.hub = FakeHub(TARGET)
        self.masters: dict[str, float] = {}             # ControlPath -> last used
        self.connections = 0
        self.calls: Counter[str] = Counter()

    def _connect(self, o: dict[str, str]) -> None:
        path, master = o.get("controlpath", "none"), o.get("controlmaster", "no")
        now = self.clock[0]
        persist = float(o.get("controlpersist", "0").rstrip("m") or 0) * 60
        last = self.masters.get(path)
        if path != "none" and last is not None and now - last <= persist:
            self.masters[path] = now                    # a session on the master
            return
        self.connections += 1                           # a new connection
        if path != "none" and master in ("auto", "yes"):
            self.masters[path] = now

    def __call__(self, cmd: Any, **_kw: Any) -> Any:
        cmd = list(cmd)
        o = opts(cmd)
        if "-O" in cmd:
            self.masters.pop(o.get("controlpath", ""), None)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self._connect(o)
        remote = shlex.split(cmd[-1])
        if remote[:1] == ["sg"]:
            remote = shlex.split(remote[3])
        argv = [w for w in remote if not ("=" in w and w.split("=")[0].isupper())]
        if argv[:2] == ["sh", "-c"]:
            self.calls[f"note {argv[4]}"] += 1
            return subprocess.CompletedProcess(cmd, 0, "", "")
        kind = " ".join(argv[1:3]) if argv[1] in ("lease", "board", "target", "share") else argv[1]
        self.calls[kind] += 1
        if argv[1] == "whoami":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(
                {"holder": "david@mapstone-dev", "audit_id": "unix:david", "role": "admin"}), "")
        res = self.hub(argv)
        return subprocess.CompletedProcess(cmd, res.returncode, res.stdout, res.stderr)


class HubRef:
    def __init__(self) -> None:
        self.host, self.target = HUB, TARGET
        self.client = hubmod.HubClient(HUB, TARGET)     # the real (capped) ssh runner


def idle_ten_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeMuxSsh:
    """One board behind the hub, its lease held here, its page open (GET /lease every 30 s,
    the viewer's gate every 20 s), the service's heartbeat and request watch running."""
    clock = [0.0]
    fake = FakeMuxSsh(clock)
    monkeypatch.setattr(subprocess, "run", fake)
    start = (parse_utc(EXPIRES) or 0.0) - 3600.0
    svc = LeaseService(tmp_path / "state", clock=lambda: clock[0],
                       wall_clock=lambda: start + clock[0], tick_s=3600.0)
    hub = HubRef()
    try:
        svc.acquire(hub, board_id="b1", ttl_s=3600, holder="hm-test", heartbeat=False)
        svc.track("b1", hub, announced=True)
        for t in range(0, 600, 5):                      # the service thread's 5 s tick
            clock[0] = float(t)
            svc.beat_due()
            svc.watch_due()
            if t % 20 == 0:                             # the viewer's beat: the gate
                view = svc.view(hub, cached_only=True, max_age_s=60.0)
                lease_elsewhere(view if view is not None else svc.view(hub))
            if t % 30 == 0:                             # the page: GET /lease
                svc.view(hub)
    finally:
        svc.close()
    return fake


def per_minute(c: Counter[str]) -> dict[str, float]:
    return {k: round(v / 10.0, 1) for k, v in sorted(c.items())}


def test_one_board_idle_for_ten_minutes_opens_one_connection_not_one_per_call(
        tmp_path, monkeypatch, mux, capsys):
    lifecycle.mark_service(False)                        # before: one connection per call
    before = idle_ten_minutes(tmp_path / "before", monkeypatch)
    lifecycle.mark_service(True)                         # after: the service reuses one
    hubmod.forget_hub_slots()
    after = idle_ten_minutes(tmp_path / "after", monkeypatch)
    with capsys.disabled():
        print(f"\nSSH-MUX, one board, 10 simulated minutes:\n"
              f"  before: {before.connections} connections, {sum(before.calls.values())} calls, "
              f"per minute {per_minute(before.calls)}\n"
              f"  after:  {after.connections} connections, {sum(after.calls.values())} calls, "
              f"per minute {per_minute(after.calls)}")
    # ~8/min, the rate the hub logged (549 an hour): one connection, one key exchange, each
    assert before.connections == sum(before.calls.values()) >= 80
    assert after.connections == 1, "one master for the whole 10 minutes"
    assert after.calls == before.calls                   # the same work, over one connection
    # where the calls come from: the holder's request watch (every 10 s, LEASE_REQUESTS.md)
    # and the page's GET /lease (every 30 s); the gate and presence reuse the cached view
    assert before.calls["note list"] == 60 and before.calls["lease show"] == 20


def test_twin_a_master_idle_past_controlpersist_is_a_new_connection(mux, monkeypatch):
    clock = [0.0]
    fake = FakeMuxSsh(clock)
    monkeypatch.setattr(subprocess, "run", fake)
    client = hubmod.HubClient(HUB, TARGET)
    client.lease_status()
    clock[0] = 300.0
    client.lease_status()                                # within 10 min: the same master
    assert fake.connections == 1
    clock[0] = 300.0 + 601.0
    client.lease_status()                                # the master had gone: a new one
    assert fake.connections == 2


class ScriptedRun:
    """``subprocess.run``: scripted ``(rc, stdout, stderr)`` per call, recording each argv."""

    def __init__(self, results: list[tuple[int, str, str]]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: Any, **_kw: Any) -> Any:
        self.calls.append(list(cmd))
        rc, out, err = self.results.pop(0) if self.results else (0, "not leased\n", "")
        return subprocess.CompletedProcess(cmd, rc, out, err)


def test_a_hub_that_refuses_a_shared_session_gets_its_own_connections(mux, monkeypatch, caplog):
    monkeypatch.setattr(hubmod, "_mux_off_hubs", set())
    caplog.set_level(logging.WARNING, logger=hubmod.__name__)
    run = ScriptedRun([(255, "", "mux_client_request_session: session request failed: Session "
                                 "open refused by peer\r\n")])
    monkeypatch.setattr(subprocess, "run", run)
    runner = hubmod.default_runner_factory(HUB, "fpga")
    got = runner(["fpgahub", "lease", "release", TARGET, "--token", "t", "--holder", "h"])
    assert got.returncode == 0 and len(run.calls) == 2       # nothing ran: run again
    assert opts(run.calls[0])["controlmaster"] == "auto"
    assert opts(run.calls[1])["controlpath"] == "none"       # on a connection of its own
    runner(["fpgahub", "lease", "show", TARGET])
    assert opts(run.calls[2])["controlpath"] == "none"       # and from then on
    assert sum("refused a session" in r.getMessage() for r in caplog.records) == 1
    # twin: another hub still shares one connection
    other = hubmod.default_runner_factory("other.invalid", "fpga")
    assert opts(other.build(["fpgahub", "whoami"]))["controlmaster"] == "auto"


def test_twin_a_release_the_shared_connection_dropped_mid_way_is_not_run_again(mux, monkeypatch):
    monkeypatch.setattr(hubmod, "_mux_off_hubs", set())
    run = ScriptedRun([(255, "", f"Connection to {HUB} closed by remote host.\r\n")])
    monkeypatch.setattr(subprocess, "run", run)
    got = hubmod.default_runner_factory(HUB, "fpga")(
        ["fpgahub", "lease", "release", TARGET, "--token", "t", "--holder", "h"])
    assert got.returncode == 255 and len(run.calls) == 1
    assert HUB not in hubmod._mux_off_hubs
