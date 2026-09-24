"""Lane Q2 soak: the REAL daemon for a while, against virtual boards through the fake hub.

::

    nice -n 10 .venv/bin/python -m tests.soak.q2_soak --minutes 40 --root /tmp/hm-q2-soak

What it runs:

- ``tests.soak.q2_boards``: 3 ``VirtualMps3`` boards in their own process;
- ``python -m harness_manager.daemon`` (unmodified), with a private state dir and
  PTY dir, ``boards.toml`` routing each board ``via = "ssh:fakehub.invalid"``, the
  fake ``ssh`` EXECUTABLE (``tests/fakes/l1_fake_ssh_bin.py``) first on PATH, and
  ``stub_openocd`` as OpenOCD. Nothing leaves 127.0.0.1; no real host is named.

The churn (light: a few requests a second in total):

- boards: one board is closed and reopened every ``--reopen-s``;
- consoles: a WebSocket on uart0 per open board: type, read the echo, leave;
- PTYs: open uart0's PTY, a client opens the path, reads, leaves; sometimes the PTY is closed;
- the UI's polling: ``GET /boards/{bid}`` and ``/telemetry`` per board;
- event WebSockets connecting and disconnecting;
- every ``--deploy-s``: deploy nanosoc to board 0, debug up, status, debug down, restore.

Every ``--sample-s`` it samples the daemon: fds (and of them sockets, PTY masters,
inotify), threads, RSS, child processes (the ssh and OpenOCD stubs), and the sizes
of the state dir and daemon.log. At the end it stops the daemon the way
``harness-manager daemon stop`` does and lists any debris.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

REPO = Path(__file__).resolve().parents[2]
HUB = "fakehub.invalid"
NANOSOC_RM_ID = 0x01000001
STATIC_ID = 0x72BB0A36


# --- /proc sampling ------------------------------------------------------------------------


def _ppid_map() -> dict[int, int]:
    out: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat_line = Path(f"/proc/{entry}/stat").read_text()
            out[int(entry)] = int(stat_line.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
    return out


def descendants(pid: int) -> list[int]:
    ppids = _ppid_map()
    kids: dict[int, list[int]] = {}
    for child, parent in ppids.items():
        kids.setdefault(parent, []).append(child)
    out, todo = [], list(kids.get(pid, []))
    while todo:
        p = todo.pop()
        out.append(p)
        todo += kids.get(p, [])
    return sorted(out)


def cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
    except OSError:
        return ""


def alive(pid: int) -> bool:
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
        return st.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def fd_kinds(pid: int) -> dict[str, int]:
    kinds = {"fds": 0, "sockets": 0, "ptmx": 0, "pts": 0, "inotify": 0, "pipes": 0, "files": 0}
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return kinds
    for name in names:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{name}")
        except OSError:
            continue
        kinds["fds"] += 1
        if target.startswith("socket:"):
            kinds["sockets"] += 1
        elif target == "/dev/ptmx":
            kinds["ptmx"] += 1
        elif target.startswith("/dev/pts/"):
            kinds["pts"] += 1
        elif target == "anon_inode:inotify":
            kinds["inotify"] += 1
        elif target.startswith("pipe:"):
            kinds["pipes"] += 1
        elif target.startswith("/"):
            kinds["files"] += 1
    return kinds


def proc_status(pid: int) -> dict[str, int]:
    out = {"threads": 0, "rss_kb": 0}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Threads:"):
                out["threads"] = int(line.split()[1])
            elif line.startswith("VmRSS:"):
                out["rss_kb"] = int(line.split()[1])
    except OSError:
        pass
    return out


def tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total


# --- the rig -------------------------------------------------------------------------------


def write_exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def free_block(n: int = 4) -> int:
    """A base port with ``n`` free consecutive ports, from the ephemeral range."""
    for _ in range(200):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            base = s.getsockname()[1]
        if base + n >= 65535:
            continue
        ok = True
        for p in range(base, base + n):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", p))
                except OSError:
                    ok = False
                    break
        if ok:
            return base
    raise RuntimeError("no free port block")


class Rig:
    """Boards process + daemon process, with every path private under ``root``."""

    def __init__(self, root: Path, n_boards: int = 3) -> None:
        self.root = root
        self.n = n_boards
        self.state = root / "state"
        self.pty = root / "pty"
        self.bin = root / "bin"
        self.boards_dir = root / "boards"
        self.overlays = root / "overlays"
        self.cfg = root / "cfg"
        self.boards_proc: subprocess.Popen | None = None
        self.daemon: subprocess.Popen | None = None
        self.info: dict[str, Any] = {}
        self.board_ids: list[str] = []
        self.ips: list[str] = []
        self.seen_children: dict[int, str] = {}

    # -- setup ---------------------------------------------------------------------------

    def setup(self) -> None:
        from tests.fakes.stub_openocd import make_wrapper
        from tests.fakes.t2_overlays import make_overlay
        from tests.fakes.t4_debug_rig import make_cfg_dir

        for d in (self.state, self.bin, self.boards_dir, self.overlays):
            d.mkdir(parents=True, exist_ok=True)
        self.pty.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_exe(self.bin / "ssh",
                  f'#!/bin/sh\nexec "{sys.executable}" -m tests.fakes.l1_fake_ssh_bin "$@"\n')
        make_wrapper(self.bin)
        make_cfg_dir(self.cfg)
        make_overlay(self.overlays, "nanosoc", rm_id=NANOSOC_RM_ID, static_id=STATIC_ID)
        make_overlay(self.overlays, "greybox", rm_id=0, static_id=STATIC_ID)

    def start_boards(self) -> None:
        log = open(self.root / "boards.log", "ab")
        self.boards_proc = subprocess.Popen(
            ["nice", "-n", "10", sys.executable, "-m", "tests.soak.q2_boards", "--n", str(self.n),
             "--out", str(self.boards_dir)], cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONPATH=f"{REPO / 'src'}{os.pathsep}{REPO}"))
        deadline = time.monotonic() + 30
        while not (self.boards_dir / "ready").exists():
            if time.monotonic() > deadline or self.boards_proc.poll() is not None:
                raise RuntimeError("the boards did not start (boards.log)")
            time.sleep(0.1)
        boards = json.loads((self.boards_dir / "boards.json").read_text())
        self.board_ids = [b["board_id"] for b in boards]
        self.ips = [b["ip"] for b in boards]
        toml = "".join(f'[boards.b{i}]\nmatch = ["{ip}"]\nvia = "ssh:{HUB}"\n\n'
                       for i, ip in enumerate(self.ips))
        (self.state / "boards.toml").write_text(toml)

    def board_control(self, line: str) -> None:
        with open(self.boards_dir / "control", "a") as fh:
            fh.write(line + "\n")

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        for var in list(env):
            if var.startswith(("HARNESS_MANAGER_", "STUB_OPENOCD_", "L1_FAKE_SSH_")):
                del env[var]
        env.update({
            "HARNESS_MANAGER_STATE_DIR": str(self.state),
            "HARNESS_MANAGER_PTY_DIR": str(self.pty),
            "PATH": f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
            # the tree this file is in: a snapshot run never mixes in edited sources
            "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{REPO}",
            "L1_FAKE_SSH_ROUTES": (self.boards_dir / "routes.json").read_text(),
            "L1_FAKE_SSH_G": f"hostname {HUB}\n",
            "HARNESS_MANAGER_OPENOCD": str(self.bin / "openocd"),
            "HARNESS_MANAGER_MPS3_OPENOCD_DIR": str(self.cfg),
            "HARNESS_MANAGER_MPS3_OVERLAY_DIRS": str(self.overlays),
            "HARNESS_MANAGER_DEBUG_PORT_BASE": str(free_block()),
            "STUB_OPENOCD_NO_ADAPTER": "1",
        })
        return env

    def start_daemon(self, log_level: str = "info") -> None:
        log = open(self.state / "daemon.log", "ab")
        self.daemon = subprocess.Popen(
            ["nice", "-n", "10", sys.executable, "-m", "harness_manager.daemon", "--state-dir",
             str(self.state), "--port", "0", "--log-level", log_level],
            cwd=str(self.state), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=self.env(), start_new_session=True)
        # `nice` execs, so the pid is the daemon's
        deadline = time.monotonic() + 60
        path = self.state / "daemon.json"
        while True:
            if self.daemon.poll() is not None:
                raise RuntimeError(f"the daemon exited ({self.daemon.returncode}); see daemon.log")
            try:
                info = json.loads(path.read_text())
                if info.get("pid") == self.daemon.pid:
                    self.info = info
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("the daemon did not write daemon.json")
            time.sleep(0.1)
        with self.client() as c:
            deadline = time.monotonic() + 30
            while c.get("/api/v1/health").status_code != 200:
                if time.monotonic() > deadline:
                    raise RuntimeError("no health")
                time.sleep(0.1)

    @property
    def pid(self) -> int:
        return int(self.info["pid"])

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.info['port']}"

    def client(self, timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(base_url=self.base, trust_env=False, timeout=timeout,
                            headers={"Authorization": f"Bearer {self.info['token']}"})

    def ws(self, path: str, **query: str) -> str:
        from urllib.parse import urlencode

        return (f"ws://127.0.0.1:{self.info['port']}/api/v1{path}?"
                f"{urlencode({'token': self.info['token'], **query})}")

    @staticmethod
    def bpath(board_id: str) -> str:
        return f"/api/v1/boards/{quote(board_id, safe='')}"

    # -- observation ----------------------------------------------------------------------

    def children(self) -> list[int]:
        kids = descendants(self.pid) if self.daemon and self.daemon.poll() is None else []
        for k in kids:
            if k not in self.seen_children:
                self.seen_children[k] = cmdline(k)
        return kids

    def sample(self) -> dict[str, Any]:
        row: dict[str, Any] = {"t": round(time.time(), 1)}
        row.update(fd_kinds(self.pid))
        row.update(proc_status(self.pid))
        kids = self.children()
        row["children"] = len(kids)
        row["ssh"] = sum(1 for k in kids if "l1_fake_ssh_bin" in self.seen_children.get(k, ""))
        row["openocd"] = sum(1 for k in kids if "stub_openocd" in self.seen_children.get(k, ""))
        row["state_bytes"] = tree_bytes(self.state)
        try:
            row["log_bytes"] = (self.state / "daemon.log").stat().st_size
        except OSError:
            row["log_bytes"] = 0
        row["pty_links"] = sum(1 for _ in self.pty.glob("*/*"))
        return row

    def debris(self) -> dict[str, Any]:
        """What is left once the daemon is gone."""
        left = {pid: cmd for pid, cmd in self.seen_children.items() if alive(pid)}
        return {
            "children_alive": left,
            "daemon_json": (self.state / "daemon.json").exists(),
            "daemon_lock": (self.state / "harness-manager-daemon.lock").exists(),
            "board_locks": sorted(p.name for p in (self.state / "locks").glob("*.lock")),
            "debug_records": sorted(p.name for p in (self.state / "debug").glob("*.json")),
            "pty_entries": sorted(str(p.relative_to(self.pty)) for p in self.pty.rglob("*")),
        }

    def stop_daemon(self) -> str:
        from harness_manager.daemon import control

        t0 = time.monotonic()
        try:
            out = control.stop(self.state, force=True, timeout=control.STOP_WAIT_S)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            out = f"stop failed: {exc}"
        if self.daemon is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.daemon.wait(10)
        return f"{out} in {time.monotonic() - t0:.1f}s"

    def teardown(self) -> None:
        if self.daemon is not None and self.daemon.poll() is None:
            self.daemon.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.daemon.wait(15)
        # our own children only: every pid here was a descendant of OUR daemon
        for pid, cmd in list(self.seen_children.items()):
            if alive(pid) and ("l1_fake_ssh_bin" in cmd or "stub_openocd" in cmd):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
        if self.boards_proc is not None and self.boards_proc.poll() is None:
            self.boards_proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.boards_proc.wait(10)


# --- the churn -------------------------------------------------------------------------------


class Churn:
    def __init__(self, rig: Rig, stop: threading.Event, args: argparse.Namespace) -> None:
        self.rig = rig
        self.stop = stop
        self.args = args
        self.counts: dict[str, int] = {}
        self.errors: dict[str, list[str]] = {}
        self._mu = threading.Lock()
        self.open: set[str] = set()

    def count(self, key: str, error: str = "") -> None:
        with self._mu:
            self.counts[key] = self.counts.get(key, 0) + 1
            if error:
                errs = self.errors.setdefault(key, [])
                if len(errs) < 20:
                    errs.append(f"{time.strftime('%H:%M:%S')} {error}")

    def _sleep(self, s: float) -> bool:
        return self.stop.wait(s * random.uniform(0.7, 1.3))

    def open_board(self, c: httpx.Client, i: int) -> None:
        r = c.post("/api/v1/boards", json={"target": self.rig.ips[i], "note": "q2 soak"})
        body = r.json()
        if r.status_code == 200 or body.get("error", {}).get("name") == "ALREADY":
            with self._mu:
                self.open.add(self.rig.board_ids[i])
            self.count("open")
        else:
            self.count("open.fail", f"{r.status_code} {body.get('error')}")

    def boards(self) -> None:
        with self.rig.client() as c:
            for i in range(self.rig.n):
                self.open_board(c, i)
            while not self._sleep(self.args.reopen_s):
                i = self.rig.n - 1
                bid = self.rig.board_ids[i]
                with self._mu:
                    self.open.discard(bid)
                time.sleep(0.5)
                r = c.delete(self.rig.bpath(bid))
                self.count("close" if r.status_code == 200 else "close.fail",
                           "" if r.status_code == 200 else f"{r.status_code} {r.text[:200]}")
                if self._sleep(2):
                    break
                self.open_board(c, i)

    def opened(self) -> list[str]:
        with self._mu:
            return sorted(self.open)

    def consoles(self) -> None:
        from websockets.sync.client import connect

        while not self._sleep(self.args.console_s):
            for bid in self.opened():
                token = f"q2-{random.randrange(1 << 30):x}"
                try:
                    with connect(self.rig.ws(f"/boards/{quote(bid, safe='')}/consoles/uart0"),
                                 open_timeout=10, close_timeout=5) as ws:
                        ws.recv(timeout=10)             # the state frame
                        ws.send(f"{token}\r".encode())
                        got = b""
                        deadline = time.monotonic() + 10
                        while token.encode() not in got and time.monotonic() < deadline:
                            frame = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
                            if isinstance(frame, bytes):
                                got += frame
                        if token.encode() in got:
                            self.count("console.echo")
                        else:
                            self.count("console.noecho", f"{bid}: {got[-80:]!r}")
                except Exception as exc:  # noqa: BLE001 - counted
                    self.count("console.fail", f"{bid}: {type(exc).__name__}: {exc}")

    def ptys(self) -> None:
        with self.rig.client() as c:
            while not self._sleep(self.args.pty_s):
                for bid in self.opened():
                    try:
                        r = c.post(f"{self.rig.bpath(bid)}/consoles/uart0/pty")
                        if r.status_code != 200:
                            self.count("pty.fail", f"{r.status_code} {r.text[:200]}")
                            continue
                        path = r.json()["path"]
                        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                        try:
                            os.write(fd, b"pty\r")
                            time.sleep(0.5)
                            with contextlib.suppress(BlockingIOError):
                                os.read(fd, 4096)
                        finally:
                            os.close(fd)
                        self.count("pty.attach")
                        if random.random() < 0.3:
                            r = c.delete(f"{self.rig.bpath(bid)}/consoles/uart0/pty")
                            self.count("pty.close" if r.status_code == 200 else "pty.close.fail",
                                       "" if r.status_code == 200 else r.text[:200])
                    except Exception as exc:  # noqa: BLE001
                        self.count("pty.fail", f"{bid}: {type(exc).__name__}: {exc}")

    def poll(self) -> None:
        with self.rig.client() as c:
            while not self._sleep(self.args.poll_s):
                for bid in self.opened():
                    for suffix in ("", "/telemetry", "/debug", "/consoles"):
                        try:
                            r = c.get(f"{self.rig.bpath(bid)}{suffix}")
                            ok = r.status_code == 200 or (r.status_code == 409 and
                                                          r.json()["error"]["name"] == "HELD")
                            self.count("poll" if ok else "poll.fail",
                                       "" if ok else f"{suffix} {r.status_code} {r.text[:200]}")
                        except Exception as exc:  # noqa: BLE001
                            self.count("poll.fail", f"{type(exc).__name__}: {exc}")
                with contextlib.suppress(Exception):
                    c.get("/api/v1/boards")
                    c.get("/api/v1/jobs")

    def events(self) -> None:
        from websockets.sync.client import connect

        while not self._sleep(self.args.events_s):
            try:
                with connect(self.rig.ws("/events"), open_timeout=10, close_timeout=5) as ws:
                    end = time.monotonic() + random.uniform(1, 8)
                    n = 0
                    while time.monotonic() < end:
                        with contextlib.suppress(TimeoutError):
                            ws.recv(timeout=0.5)
                            n += 1
                    self.count("events.ws")
            except Exception as exc:  # noqa: BLE001
                self.count("events.fail", f"{type(exc).__name__}: {exc}")

    def _job(self, c: httpx.Client, method: str, path: str, **kw: Any) -> dict[str, Any]:
        r = c.request(method, path, **kw)
        if r.status_code != 202:
            return {"state": "refused", "status": r.status_code, "body": r.text[:300]}
        job = r.json()["job"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            j = c.get(f"/api/v1/jobs/{job}").json()
            if j["state"] != "running":
                return j
            time.sleep(0.2)
        return {"state": "timeout"}

    def deploys(self) -> None:
        bid = self.rig.board_ids[0]
        p = self.rig.bpath(bid)
        with self.rig.client(timeout=60) as c:
            while not self._sleep(self.args.deploy_s):
                j = self._job(c, "POST", f"{p}/deploy", json={"overlay": "nanosoc"})
                self.count(f"deploy.{j['state']}", "" if j["state"] == "done" else str(j)[:300])
                j = self._job(c, "POST", f"{p}/debug/up")
                state = (j.get("result") or {}).get("state", j["state"])
                self.count(f"debug.up.{state}", "" if state == "up" else str(j)[:300])
                time.sleep(2)
                r = c.get(f"{p}/debug")
                self.count(f"debug.status.{r.json().get('state')}")
                r = c.post(f"{p}/debug/down")
                self.count("debug.down" if r.status_code == 200 else "debug.down.fail",
                           "" if r.status_code == 200 else r.text[:300])
                j = self._job(c, "POST", f"{p}/restore")
                self.count(f"restore.{j['state']}", "" if j["state"] == "done" else str(j)[:300])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=f"/tmp/hm-q2-soak-{os.getpid()}")
    ap.add_argument("--minutes", type=float, default=40)
    ap.add_argument("--boards", type=int, default=3)
    ap.add_argument("--sample-s", type=float, default=60)
    ap.add_argument("--reopen-s", type=float, default=90)
    ap.add_argument("--console-s", type=float, default=5)
    ap.add_argument("--pty-s", type=float, default=10)
    ap.add_argument("--poll-s", type=float, default=2)
    ap.add_argument("--events-s", type=float, default=3)
    ap.add_argument("--deploy-s", type=float, default=240)
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args(argv)
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    rig = Rig(root, args.boards)
    samples: list[dict[str, Any]] = []
    out = root / "soak.json"
    stop = threading.Event()
    churn = Churn(rig, stop, args)
    try:
        rig.setup()
        rig.start_boards()
        rig.start_daemon(args.log_level)
        print(f"daemon pid {rig.pid} on {rig.base}; root {root}", flush=True)
        threads = [threading.Thread(target=getattr(churn, n), name=n, daemon=True)
                   for n in ("boards", "consoles", "ptys", "poll", "events", "deploys")]
        time.sleep(1)
        samples.append(rig.sample())
        for t in threads:
            t.start()
        end = time.monotonic() + args.minutes * 60
        while time.monotonic() < end:
            stop.wait(min(args.sample_s, max(0.0, end - time.monotonic())))
            if rig.daemon.poll() is not None:
                print("the daemon died", flush=True)
                break
            row = rig.sample()
            row["counts"] = dict(churn.counts)
            samples.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != "counts"}), flush=True)
            out.write_text(json.dumps({"samples": samples, "counts": churn.counts,
                                       "errors": churn.errors}, indent=1))
        stop.set()
        for t in threads:
            t.join(timeout=30)
        # the churn stopped: let the daemon settle, then the last idle sample
        time.sleep(5)
        idle = rig.sample()
        idle["counts"] = dict(churn.counts)
        idle["idle"] = True
        samples.append(idle)
        with rig.client() as c:
            open_now = [b["board_id"] for b in c.get("/api/v1/boards").json()["boards"]
                        if b["open"]]
        stopped = rig.stop_daemon()
        time.sleep(1)
        result = {"samples": samples, "counts": churn.counts, "errors": churn.errors,
                  "open_at_stop": open_now, "stop": stopped, "debris": rig.debris(),
                  "children_seen": rig.seen_children}
        out.write_text(json.dumps(result, indent=1))
        print(json.dumps({k: result[k] for k in ("counts", "stop", "debris")}, indent=1))
        return 0
    finally:
        stop.set()
        rig.teardown()


if __name__ == "__main__":
    sys.exit(main())
