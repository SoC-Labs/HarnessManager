"""SET-API: a real ``python -m harness_manager.daemon`` process (demo boards, loopback, an
ephemeral port): its Host allow-list, and a secret that never reaches a reply, an event or
the daemon's log (at debug level). Each check has a negative twin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.fakes.t13_daemon import kill, wait_info, ws_connect

SECRET = "tok-LIVE-NEVER-LOGGED-7c31"


@pytest.fixture
def live(tmp_path: Path):
    sdir = tmp_path / "svc"
    log = tmp_path / "daemon.log"
    with log.open("wb") as out:
        proc = subprocess.Popen([sys.executable, "-m", "harness_manager.daemon", "--state-dir",
                                 str(sdir), "--port", "0", "--demo", "--log-level", "debug"],
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                env=dict(os.environ))
    try:
        info = wait_info(sdir, proc.pid)
        yield info, proc, log, sdir
    finally:
        kill(proc)


def client(info, **headers) -> httpx.Client:
    return httpx.Client(base_url=info.base_url, trust_env=False, timeout=30.0,
                        headers={"Authorization": f"Bearer {info.token}", **headers})


def test_a_foreign_host_is_refused_by_the_real_service(live):
    info, _, log, _ = live
    with client(info, Host="evil.example") as c:
        r = c.get("/api/v1/settings")
        assert r.status_code == 403 and r.json()["error"]["name"] == "REFUSED"
        assert c.get("/health").status_code == 403
        assert c.put("/api/v1/settings", json={"general.theme": "dark"}).status_code == 403
    assert upgrade_status(info, "evil.example") == 403        # a WebSocket, refused too


def upgrade_status(info, host: str) -> int:
    """The HTTP status a WebSocket upgrade for ``/events`` gets with this Host."""
    import socket

    with socket.create_connection(("127.0.0.1", info.port), timeout=10) as s:
        s.sendall((f"GET /api/v1/events?token={info.token} HTTP/1.1\r\nHost: {host}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
        head = s.recv(4096).decode("latin-1")
    return int(head.split()[1])


def test_negative_twin_loopback_names_pass_on_the_real_service(live):
    info, _, _, sdir = live
    for host in (f"127.0.0.1:{info.port}", f"localhost:{info.port}"):
        with client(info, Host=host) as c:
            assert c.get("/api/v1/settings/schema").status_code == 200, host
    with client(info) as c:
        assert c.put("/api/v1/settings", json={"general.theme": "dark"}).status_code == 200
    assert "dark" in (sdir / "settings.toml").read_text()      # the refused PUT wrote nothing
    assert upgrade_status(info, f"127.0.0.1:{info.port}") == 101


def test_a_secret_is_in_no_reply_no_event_and_not_the_log(live):
    info, proc, log, sdir = live
    seen: list[str] = []
    with client(info) as c, ws_connect(
            f"ws://127.0.0.1:{info.port}/api/v1/events?token={info.token}&topics=settings.*"
    ) as ws:
        for method, path, body in (
                ("PUT", "/api/v1/settings/secrets/hubs.lab.token", {"value": SECRET}),
                ("GET", "/api/v1/settings?all=1", None),
                ("GET", "/api/v1/settings?key=hubs.lab.token", None),
                ("PUT", "/api/v1/settings", {"hubs.lab.token": SECRET}),         # refused
                ("PUT", "/api/v1/settings/secrets/hubs.lab.token", {"value": SECRET + "\nx"}),
                ("PUT", "/api/v1/settings", {"hubs.lab.host": "hub.example"}),
                ("DELETE", "/api/v1/settings/secrets/hubs.lab.token", None)):
            r = c.request(method, path, json=body)
            seen.append(r.text)
        frames = [ws.recv(timeout=10) for _ in range(3)]
    kill(proc)
    text = log.read_text(encoding="utf-8", errors="replace")
    assert [json.loads(f)["data"]["keys"] for f in frames] == [
        ["hubs.lab.token"], ["hubs.lab.host"], ["hubs.lab.token"]]
    assert SECRET not in "\n".join(seen + frames)
    assert SECRET not in text
    for path in sdir.rglob("*"):
        if path.is_file() and "secrets" not in path.parts:
            assert SECRET not in path.read_text(errors="replace"), path
    # the twin: the log did record these changes, so the grep would have seen a value in them
    assert text.count("settings.changed") >= 3 and "hubs.lab.token" in text
