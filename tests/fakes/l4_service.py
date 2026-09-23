"""Lane L4 test rig: harness-manager-daemon's power and update routes over the real Engine.

- ``write_boards(text)``: ``boards.toml`` in the per-test state dir (the pack reads it
  when a board opens, so write it first).
- ``share_clock(session, clock)``: point the session's power driver at the fake
  device's clock, so a power cycle costs no wall time (T9's pattern).
- ``with_update(engine, ...)``: seed ``engine.update`` with T7's ``UpdateService``
  trusting the test keys (the build pins none yet), and an ``AppUpdater`` on a
  ``FakeUv`` (T7's pattern in ``test_t7_cli.py``).
- ``hold_board(daemon, bid)``: a job that runs until released, for the HELD checks.
- ``wait_job``/``events_until``: a job's end, by polling or on the events socket.

Loopback only: the plugs, the channel server and the board are all on 127.0.0.1.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from harness_manager.services.update import UpdateService
from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe
from tests.fakes.fake_channel import TestKeys
from tests.fakes.t7_board import FakeUv
from tests.fakes.t13_daemon import headers, state_dir

KEYS = TestKeys()
H = headers()
APP_VERSION = "0.1.0"


def write_boards(text: str) -> Path:
    sdir = state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / "boards.toml"
    path.write_text(text)
    path.chmod(0o600)
    return path


def share_clock(session: Any, clock: Any) -> None:
    """The session's power driver sleeps and reads time on ``clock`` (the fake plug's)."""
    driver = session.power.driver
    driver._clock = clock
    driver._sleep = clock.sleep


def with_update(engine: Any, *, uv: FakeUv | None = None) -> UpdateService:
    sdir = Path(engine.state_dir)
    app = AppUpdater(AppLayout(sdir / "update" / "app"), LocalBusyProbe(sdir), uv="/opt/uv",
                     runner=uv or FakeUv(), python_version="3.11", running_version=APP_VERSION,
                     windows=False)
    svc = UpdateService(engine, trust=KEYS.trust(), token="", app_version=APP_VERSION,
                        app_updater=app)
    engine._services["update"] = svc         # engine.update is lazy: seed it (T7's pattern)
    return svc


def hold_board(daemon: Any, board_id: str, kind: str = "deploy") -> tuple[Any, threading.Event]:
    """Start a job on ``board_id`` that runs until the returned event is set."""
    release = threading.Event()
    job = daemon.jobs.submit(kind, board_id, lambda progress: release.wait(30))
    return job, release


def wait_job(client: Any, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/jobs/{job_id}", headers=H).json()
        if body["state"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} still running after {timeout} s")


def events_until(ws: Any, topic: str, job: str | None = None, limit: int = 2000) -> list[dict]:
    """Event frames until ``topic`` (for ``job``, when given); every frame seen, in order."""
    frames: list[dict] = []
    for _ in range(limit):
        frame = json.loads(ws.receive_text())
        frames.append(frame)
        if frame["topic"] == topic and (job is None or frame["data"].get("job") == job):
            return frames
    raise AssertionError(f"no {topic} in {limit} frames: {[f['topic'] for f in frames]}")
