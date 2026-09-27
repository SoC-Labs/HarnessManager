"""Lane OTA-D: a real apply, restart, health check and rollback, with real processes.

A throwaway install under ``/tmp/otad-*`` (``tests/fakes/otad_install.py``) holds REAL venvs,
one per "version", each a copy of this checkout: 0.1.0 (the installer's), 0.2.0 (good),
0.3.0 (its daemon exits at start), 0.4.0 (it answers ``/health``, then dies 2 s later).
The test starts the 0.1.0 daemon on a port it chose, with its own HOME, XDG dirs, state dir
and PTY dir, and a virtual MPS3 in this process. ``POST /update/app/apply`` then does the
rest by itself: the real ``--self-test``, the drain, the resume file, the detached helper
under the OLD interpreter, the pointer switch, the new daemon with ``--resume``, the health
check and, for the broken versions, the rollback. Every process a test starts is stopped by
it, pass or fail: the daemon through its API, then every process that still names the
test's own dir (each daemon and helper takes ``--state-dir``), the helper first, whether or
not ``daemon.json`` names it yet (``tests/fakes/proc_sweep.py``). ``/tmp/otad-*`` is removed
at the end, after the same sweep over the whole install; a reaper does both if the test
process itself is killed, and the session guard (``tests/conftest.py``) fails a run that
leaves a daemon behind.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from harness_manager.services import pty as ptymod
from tests.fakes import proc_sweep
from tests.fakes.l2_rig import PtyClient
from tests.fakes.otad_install import FakeInstall, env
from tests.fakes.t13_daemon import pack_overrides, recv_json_until, ws_connect
from tests.fakes.virtual_board import VirtualMps3

pytestmark = [
    pytest.mark.timeout(300),
    pytest.mark.skipif(os.name == "nt" or not ptymod.supported() or not os.path.isdir("/proc"),
                       reason="the process test runs on Linux (Windows/macOS: CI)"),
]

OLD, GOOD, DIES_AT_START, DIES_LATER = "0.1.0", "0.2.0", "0.3.0", "0.4.0"
HELPER = "harness_manager.daemon.update_apply"


@pytest.fixture(scope="module")
def install():
    base = Path(tempfile.mkdtemp(prefix="otad-", dir="/tmp"))
    marker = proc_sweep.track(base)                  # the session guard checks it at the end
    reaper = proc_sweep.start_reaper(base)           # if this process is killed outright
    try:
        yield FakeInstall.build(base / "i", {OLD: "ok", GOOD: "ok", DIES_AT_START: "dies-at-start",
                                             DIES_LATER: "dies-after:2"})
    finally:
        try:
            proc_sweep.sweep(marker, first=(HELPER,))  # anything a world's close missed
        finally:
            shutil.rmtree(base, ignore_errors=True)
            if reaper is not None:
                reaper.wait(timeout=30)              # it sees the base gone and exits


_SEQ = itertools.count(1)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ours(pid: int, marker: str) -> bool:
    try:
        return marker in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False


class World:
    def __init__(self, inst: FakeInstall, name: str) -> None:
        self.inst = inst
        self.work = inst.base.parent / f"w{next(_SEQ)}-{re.sub(r'[^A-Za-z0-9]+', '-', name)[:24]}"
        self.work.mkdir(parents=True)
        self.state = self.work / "state"
        self.env = env(self.work)
        self.port = free_port()
        self.vb = VirtualMps3(self.work / "vb").__enter__()
        self.procs: list[subprocess.Popen] = []

    # -- the daemon the test starts (0.1.0) --

    def start(self) -> dict[str, Any]:
        log = open(self.work / "first-daemon.log", "ab")        # noqa: SIM115 - closed with the proc
        proc = subprocess.Popen(
            [str(self.inst.python(OLD)), "-m", "harness_manager.daemon", "--state-dir",
             str(self.state), "--port", str(self.port), "--pack-overrides",
             json.dumps(pack_overrides(self.vb))],
            env=self.env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=self.work, start_new_session=True)
        self.procs.append(proc)
        h = self.health(want=OLD, timeout=60)
        info = json.loads((self.state / "daemon.json").read_text())
        assert info["pid"] == proc.pid == h["pid"]
        self.token = info["token"]
        return h

    def health(self, want: str | None = None, timeout: float = 60,
               other_than: int | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=2, trust_env=False)
                last = r.json()
                if (want is None or last.get("version") == want) and \
                        (other_than is None or last.get("pid") != other_than):
                    return last
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.2)
        raise AssertionError(f"no /health {want} on {self.port} in {timeout}s; last {last}; "
                             f"log: {self.tail()}")

    def tail(self) -> str:
        out = []
        for name in ("daemon.log", "update/apply.log"):
            p = self.state / name
            if p.exists():
                out.append(f"--- {name}\n" + "\n".join(p.read_text(errors="replace")
                                                        .splitlines()[-25:]))
        return "\n".join(out)

    def api(self, token: str | None = None) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}/api/v1", trust_env=False,
                            timeout=30, headers={"Authorization": f"Bearer {token or self.token}"})

    def open_board(self) -> str:
        with self.api() as c:
            r = c.post("/boards", json={"target": self.vb.shell_endpoint, "note": "otad board"})
            assert r.status_code == 200, r.text
            return r.json()["board_id"]

    def last_apply(self, apply_id: str, timeout: float = 150) -> dict[str, Any]:
        path = self.state / "update" / "last_apply.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                rec = json.loads(path.read_text())
                if rec.get("id") == apply_id:
                    return rec
            except (OSError, ValueError):
                pass
            time.sleep(0.25)
        raise AssertionError(f"no verdict for {apply_id} in {timeout}s\n{self.tail()}")

    def boards_open(self) -> list[str]:
        with self.api() as c:
            return [b["board_id"] for b in c.get("/boards").json()["boards"] if b["open"]]

    # -- clean-up: only what this test started --

    def close(self) -> None:
        """Stop every process this world started, whatever the test left behind. The daemon
        ``daemon.json`` names is asked to stop (it closes its board); then every process that
        still names this world's dir is stopped, the apply helper first (it would start, or
        roll back to, another daemon). That covers a daemon the helper started that has not
        written ``daemon.json`` yet, and each step runs even when one before it raised."""
        marker = str(self.work) + "/"
        try:
            self._ask_the_daemon_to_stop(marker)
        finally:
            try:
                proc_sweep.sweep(marker, first=(HELPER,))
            finally:
                for proc in self.procs:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=10)
                self.vb.__exit__(None, None, None)

    def _ask_the_daemon_to_stop(self, marker: str) -> None:
        info = self.state / "daemon.json"
        token = getattr(self, "token", None)
        if not info.exists() or token is None:
            return
        try:
            pid = json.loads(info.read_text())["pid"]
        except (OSError, ValueError, KeyError):
            return
        if not _ours(pid, marker):
            return
        try:
            with self.api() as c:
                c.post("/daemon/shutdown", json={"force": True}, timeout=5)
        except httpx.HTTPError:
            return
        deadline = time.monotonic() + 10
        while _ours(pid, marker) and time.monotonic() < deadline:
            time.sleep(0.1)


@pytest.fixture
def world(install, request):
    install.reset()
    w = World(install, request.node.name)
    try:
        yield w
    finally:
        w.close()


def events_socket(w: World, token: str) -> Any:
    return ws_connect(f"ws://127.0.0.1:{w.port}/api/v1/events?token={token}&topics=update.*")


# --- the good version ------------------------------------------------------------------------


def test_apply_restarts_on_the_same_port_and_token_and_screen_keeps_its_path(world):
    w = world
    first = w.start()
    old_token = w.token
    bid = w.open_board()
    B = f"/boards/{quote(bid, safe='')}"
    with w.api() as c:
        pty = c.post(f"{B}/consoles/uart0/pty").json()
        assert pty["path"].startswith(str(w.work / "pty"))
        old_device = os.readlink(pty["path"])
        term = PtyClient(pty["path"])
        try:
            deadline = time.monotonic() + 20
            while c.get(f"{B}/consoles/uart0/pty").json()["pty"]["clients"] != 1:
                assert time.monotonic() < deadline, "screen never counted"
                time.sleep(0.1)
            # soft busy: without confirm nothing happens
            r = c.post("/update/app/apply", json={"version": GOOD})
            assert r.status_code == 409 and r.json()["error"]["data"]["reason"] == "SOFT_BUSY"
            r = c.post("/update/app/apply", json={"version": GOOD, "confirm": True,
                                                  "stable_s": 5})
            assert r.status_code == 202, r.text
            apply_id = r.json()["apply"]["id"]
            got = term.read_until(b"re-attach with", timeout=90).decode(errors="replace")
        finally:
            term.close()
    assert f"restarting for an update to {GOOD}" in got and pty["path"] in got
    # the new version answers on the SAME port, with a new pid
    h = w.health(want=GOOD, timeout=90, other_than=first["pid"])
    ws = events_socket(w, old_token)                  # the OLD token opens the events socket
    try:
        frames = recv_json_until(ws, lambda f: f["topic"] in ("update.applied",
                                                              "update.rolled_back"), timeout=60)
    finally:
        ws.close()
    assert frames[-1]["topic"] == "update.applied", frames
    assert frames[-1]["data"]["to"] == GOOD and frames[-1]["data"]["id"] == apply_id
    rec = w.last_apply(apply_id)
    assert (rec["result"], rec["from"], rec["to"]) == ("applied", OLD, GOOD), w.tail()
    # the token is kept: every client of the old daemon still works
    info = json.loads((w.state / "daemon.json").read_text())
    assert (info["token"], info["port"], info["pid"], info["version"]) == \
        (old_token, w.port, h["pid"], GOOD)
    deadline = time.monotonic() + 30
    while bid not in w.boards_open():
        assert time.monotonic() < deadline, "the board was not reopened"
        time.sleep(0.2)
    # screen's path is the same; the device behind it is new, and it serves the console again
    with w.api(old_token) as c:
        again = c.get(f"{B}/consoles/uart0/pty").json()["pty"]
    assert again is not None and again["path"] == pty["path"]
    assert os.readlink(pty["path"]).startswith("/dev/pts/")
    assert os.readlink(pty["path"]) != old_device or again["device"] == old_device
    ptr = w.inst.pointer()
    assert (ptr["current"], ptr["previous"]) == (GOOD, "")
    assert not (w.state / "update" / "resume.json").exists()      # it held the token
    # negative twin: a wrong token is still refused after the restart
    with w.api("not-the-token") as c:
        assert c.get("/boards").status_code == 401


# --- the broken versions ----------------------------------------------------------------------


@pytest.mark.parametrize("version,phase", [(DIES_AT_START, "start"), (DIES_LATER, "stable")])
def test_a_version_that_does_not_stay_up_is_rolled_back_and_never_offered_again(world, version,
                                                                               phase):
    w = world
    first = w.start()
    bid = w.open_board()
    with w.api() as c:
        r = c.post("/update/app/apply", json={"version": version, "stable_s": 5, "health_s": 30})
        assert r.status_code == 202, r.text
        apply_id = r.json()["apply"]["id"]
    rec = w.last_apply(apply_id)
    assert rec["result"] == "rolled-back", w.tail()
    assert rec["phase"] == phase and rec["to"] == version, rec
    if version == DIES_AT_START:        # the reason is the new daemon's own last words
        assert "this version cannot start" in rec["reason"], rec
    log = (w.state / "update" / "apply.log").read_text()
    assert log.count("result rolled-back") == 1, log            # one line per event
    # the OLD version answers on the same port, with the same token, the board open again
    h = w.health(want=OLD, timeout=60, other_than=first["pid"])
    info = json.loads((w.state / "daemon.json").read_text())
    assert (info["token"], info["port"], info["pid"]) == (w.token, w.port, h["pid"])
    deadline = time.monotonic() + 30
    while bid not in w.boards_open():
        assert time.monotonic() < deadline, "the board was not reopened after the rollback"
        time.sleep(0.2)
    # the pointer is back, and the version is marked bad in both places
    ptr = w.inst.pointer()
    assert ptr["current"] == "" and ptr["versions"][version]["state"] == "bad"
    bad = w.inst.bad(w.state)
    assert version in bad and bad[version]["phase"] == phase
    deadline = time.monotonic() + 30
    log = ""
    while "update.rolled_back" not in log and time.monotonic() < deadline:
        time.sleep(0.2)
        log = (w.state / "daemon.log").read_text(errors="replace")
    assert "update.rolled_back" in log                # the restarted daemon said so
    # never again: the status lists it bad, not staged, and an apply of it is refused
    with w.api() as c:
        status = c.get("/update/app").json()
        assert version in status["bad"] and version not in status["staged"]
        r = c.post("/update/app/apply", json={"version": version})
        assert (r.status_code, r.json()["error"]["name"]) == (409, "REFUSED")
        assert "marked bad" in r.json()["error"]["message"]
