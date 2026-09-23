"""Team T13 test rig: harness-manager-daemon in-process (TestClient or real uvicorn), and the CLI harness.

- ``engine_for(vb)``: an ``Engine`` in the test's state dir, pointed at a
  ``VirtualMps3`` with ``EngineConfig.pack_overrides`` (console, push and TFTP ports).
- ``LiveDaemon``: the app under a REAL uvicorn on an ephemeral 127.0.0.1 port,
  in a thread, optionally with ``daemon.json`` written so ``get_engine()`` finds it.
- ``api(url)``: an httpx client with the bearer token and no proxy.
- ``cli``: ``harness-manager`` ``main()`` with ``cmd_daemon.register`` wired in the way
  the lead will wire it into ``cli/main.py``.
- ``spawn``/``stop_all``: real ``python -m harness_manager.daemon`` processes, always
  stopped at the end of a test.

Loopback only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.daemon.server import bind_socket
from harness_manager.daemon.state import DaemonInfo, daemon_json_path, read_info, write_info
from harness_manager.engine import Engine
from tests.fakes.virtual_board import VirtualMps3

TOKEN = "t13-test-token-0123456789"


def state_dir() -> Path:
    """The per-test state dir the autouse fixture set."""
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


def pack_overrides(vb: VirtualMps3, **extra: Any) -> dict[str, dict]:
    return {"mps3": {"console_ports": vb.console_ports, "push_port": vb.shell.raw_tcp_port,
                     "tftp_port": vb.shell.tftp_port, **extra}}


def engine_for(vb: VirtualMps3, **extra: Any) -> Engine:
    return Engine(EngineConfig(state_dir=state_dir(), pack_overrides=pack_overrides(vb, **extra)))


def bid_path(board_id: str) -> str:
    return f"/api/v1/boards/{quote(board_id, safe='')}"


def headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def api(base_url: str, token: str = TOKEN) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers=headers(token), trust_env=False, timeout=30.0)


def wait_for(predicate, timeout: float = 10.0, what: str = "condition") -> Any:
    """Poll ``predicate`` until it returns something truthy (used where no event exists)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class LiveDaemon:
    """``create_app`` under a real uvicorn, in a thread, on an ephemeral port."""

    def __init__(self, engine: Any, *, token: str = TOKEN, write_json: bool = True,
                 **app_kw: Any) -> None:
        import uvicorn

        self.engine = engine
        self.token = token
        self.write_json = write_json
        self.stopped = threading.Event()
        self.app = create_app(engine, token=token, state_dir=state_dir(),
                              shutdown=self.request_stop, **app_kw)
        self.sock = bind_socket("127.0.0.1", 0)
        self.port = self.sock.getsockname()[1]
        config = uvicorn.Config(self.app, log_level="warning", lifespan="on", access_log=False,
                                timeout_graceful_shutdown=2)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._run, daemon=True, name="t13-uvicorn")

    def _run(self) -> None:
        try:
            self.server.run(sockets=[self.sock])
        finally:
            self.stopped.set()

    def request_stop(self) -> None:
        self.server.should_exit = True

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def ws_url(self, path: str, token: str | None = None, **query: str) -> str:
        from urllib.parse import urlencode

        qs = urlencode({"token": self.token if token is None else token, **query})
        return f"ws://127.0.0.1:{self.port}/api/v1{path}?{qs}"

    def client(self, token: str | None = None) -> httpx.Client:
        return api(self.base_url, self.token if token is None else token)

    def __enter__(self) -> LiveDaemon:
        self.thread.start()
        wait_for(lambda: self.server.started or self.stopped.is_set(), what="uvicorn start")
        if not self.server.started:
            raise RuntimeError("uvicorn did not start")
        if self.write_json:
            write_info(state_dir(), DaemonInfo(pid=os.getpid(), port=self.port, token=self.token,
                                               started_at=time.time(), version="test",
                                               hostname=""))
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)
        if self.write_json:
            daemon_json_path(state_dir()).unlink(missing_ok=True)


def ws_connect(url: str, **kw: Any) -> Any:
    """A websockets sync client with no proxy."""
    import inspect

    from websockets.sync.client import connect

    params = inspect.signature(connect).parameters
    if "proxy" in params:
        kw.setdefault("proxy", None)
    if "legacy" in params:
        kw.setdefault("legacy", True)
    kw.setdefault("open_timeout", 10)
    return connect(url, **kw)


def recv_until(ws: Any, pattern: bytes, timeout: float = 10.0) -> bytes:
    """Board bytes from a console socket until ``pattern`` shows up (text frames skipped)."""
    got = bytearray()
    deadline = time.monotonic() + timeout
    while pattern not in got:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"no {pattern!r} in {bytes(got)!r}")
        msg = ws.recv(timeout=left)
        if isinstance(msg, (bytes, bytearray)):
            got += msg
    return bytes(got)


def recv_json_until(ws: Any, predicate, timeout: float = 10.0) -> list[dict]:
    """Text frames (as JSON) until one satisfies ``predicate``; returns all of them."""
    frames: list[dict] = []
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"predicate never held; saw {[f.get('topic') for f in frames]}")
        msg = ws.recv(timeout=left)
        if isinstance(msg, str):
            frames.append(json.loads(msg))
            if predicate(frames[-1]):
                return frames


# --- the CLI with the daemon verbs wired in ---------------------------------------------------


def _with_daemon_verbs(original):
    from harness_manager.cli.cmd_daemon import register

    def make_parser() -> argparse.ArgumentParser:
        parser = original()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        if "daemon" not in sub.choices:        # main.py wires it since the T13 merge
            register(sub)
        return parser

    return make_parser


@contextmanager
def daemon_verbs() -> Iterator[None]:
    """``cli.main.main`` with ``daemon``/``ui`` registered (as the lead will wire them)."""
    from harness_manager.cli import main as cli_main

    original = cli_main.make_parser
    cli_main.make_parser = _with_daemon_verbs(original)
    try:
        yield
    finally:
        cli_main.make_parser = original


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    from harness_manager.cli.main import main

    with daemon_verbs():
        rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


# --- real daemon processes -----------------------------------------------------------------------


def spawn(sdir: Path, *extra: str) -> subprocess.Popen:
    """``python -m harness_manager.daemon`` in the foreground of a child process (the test owns it)."""
    return subprocess.Popen([sys.executable, "-m", "harness_manager.daemon", "--state-dir", str(sdir),
                             "--port", "0", "--log-level", "warning", *extra],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=dict(os.environ))


def wait_info(sdir: Path, pid: int, timeout: float = 30.0) -> DaemonInfo:
    from harness_manager.daemon.control import health

    def ready() -> DaemonInfo | None:
        info = read_info(sdir)
        if info is not None and info.pid == pid and health(info) is not None:
            return info
        return None

    return wait_for(ready, timeout=timeout, what=f"daemon pid {pid}")


def kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def stop_state_dir(sdir: Path) -> None:
    """Stop whatever daemon ``daemon.json`` in ``sdir`` names (test cleanup)."""
    from harness_manager.core.session import pid_alive
    from harness_manager.daemon import control

    info = read_info(sdir)
    if info is None:
        return
    try:
        control.stop(sdir, force=True, timeout=10)
    except Exception:  # noqa: BLE001 - cleanup must go on
        if pid_alive(info.pid) and info.pid != os.getpid():
            try:
                os.kill(info.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
