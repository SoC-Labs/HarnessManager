"""Lane Q2 failure injection against the REAL daemon (``tests.soak.q2_soak.Rig``).

::

    nice -n 10 .venv/bin/python -m tests.soak.q2_inject --root /tmp/hm-q2-inject [SCENARIO ...]

Scenarios (each prints what it saw; the report quotes them):

- ``console``: the board vanishes while a console WebSocket and a PTY client are attached,
  then comes back;
- ``deploy``: the board vanishes mid-push;
- ``openocd``: the OpenOCD process dies under a running debug session;
- ``tunnel``: the tunnel's ssh dies, once and then repeatedly (restart and back-off);
- ``concurrent``: two "CLI" clients and the UI hit one board at once, during a deploy;
- ``malformed``: bad requests, looking for a 500;
- ``port``: the daemon's port is taken.

Only processes this script started are ever signalled.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tests.soak.q2_soak import NANOSOC_RM_ID, STATIC_ID, Rig, alive


def say(*parts: Any) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def job(c, method: str, path: str, timeout: float = 120, **kw) -> dict:
    r = c.request(method, path, **kw)
    if r.status_code != 202:
        return {"state": "refused", "status": r.status_code, "body": r.json()}
    jid = r.json()["job"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        j = c.get(f"/api/v1/jobs/{jid}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    return {"state": "timeout", "job": jid}


def child_matching(rig: Rig, needle: str) -> int:
    for pid in rig.children():
        if needle in rig.seen_children.get(pid, ""):
            return pid
    return 0


def open_all(rig: Rig) -> None:
    with rig.client(timeout=60) as c:
        for ip in rig.ips:
            r = c.post("/api/v1/boards", json={"target": ip})
            say("open", ip, r.status_code)


# --- scenarios -------------------------------------------------------------------------------


def s_console(rig: Rig) -> None:
    from websockets.sync.client import connect

    bid = rig.board_ids[0]
    with rig.client() as c:
        path = c.post(f"{rig.bpath(bid)}/consoles/uart0/pty").json()["path"]
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    frames: list[tuple[float, Any]] = []
    t0 = time.monotonic()
    with connect(rig.ws(f"/boards/{quote(bid, safe='')}/consoles/uart0"), open_timeout=10) as ws:
        frames.append((0.0, ws.recv(timeout=10)))
        rig.board_control("stop 0")
        say("board 0 stopped")
        end = time.monotonic() + 8
        while time.monotonic() < end:
            try:
                f = ws.recv(timeout=0.5)
                frames.append((round(time.monotonic() - t0, 2), f))
            except TimeoutError:
                pass
        try:
            ws.send(b"x")
            say("a keystroke while down: accepted by the socket")
        except Exception as exc:  # noqa: BLE001
            say("a keystroke while down:", type(exc).__name__, exc)
        rig.board_control("start 0")
        t1 = time.monotonic()
        say("board 0 started")
        echoed = None
        while time.monotonic() - t1 < 30:
            try:
                ws.send(b"back\r")
                f = ws.recv(timeout=1.0)
                frames.append((round(time.monotonic() - t0, 2), f))
                if isinstance(f, bytes) and b"back" in f:
                    echoed = round(time.monotonic() - t1, 2)
                    break
            except TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                say("the WebSocket ended:", type(exc).__name__, exc)
                break
    say("frames:", [(t, f if isinstance(f, str) else f[:40]) for t, f in frames])
    say("console echo after the board came back:", echoed, "s")
    os.write(fd, b"pty\r")
    time.sleep(1)
    try:
        got = os.read(fd, 4096)
    except BlockingIOError:
        got = b""
    os.close(fd)
    say("PTY after the board came back:", got[-60:])


def _big_overlay(rig: Rig) -> None:
    from tests.fakes.t2_overlays import make_overlay

    if not (rig.overlays / "nanosoc_big").exists():
        make_overlay(rig.overlays, "nanosoc_big", rm_id=NANOSOC_RM_ID | 0x10000,
                     static_id=STATIC_ID, partial=b"\xB1\x75\x00\x0D" * (4 << 20))


def s_deploy(rig: Rig) -> None:
    bid = rig.board_ids[1]
    p = rig.bpath(bid)
    with rig.client(timeout=60) as c:
        r = c.post(f"{p}/deploy", json={"overlay": "nanosoc_big"})
        say("deploy", r.status_code, r.json())
        if r.status_code != 202:
            return
        jid = r.json()["job"]
        deadline = time.monotonic() + 30
        while c.get(f"/api/v1/jobs/{jid}").json()["progress"].get("phase") not in \
                ("push", "partial", "clearing") and time.monotonic() < deadline:
            time.sleep(0.02)
        say("progress at the cut:", c.get(f"/api/v1/jobs/{jid}").json()["progress"])
        rig.board_control("stop 1")
        say("board 1 stopped mid-push")
        while (j := c.get(f"/api/v1/jobs/{jid}").json())["state"] == "running":
            time.sleep(0.2)
        say("job:", j["state"], json.dumps(j.get("error") or j.get("result"))[:400])
        r = c.get(p)
        say("GET board while down:", r.status_code, r.text[:300])
        rig.board_control("start 1")
        time.sleep(1.5)
        r = c.get(p)
        say("GET board after it came back:", r.status_code,
            {k: r.json().get(k) for k in ("identity", "health")} if r.status_code == 200
            else r.text[:300])
        j = job(c, "POST", f"{p}/deploy", json={"overlay": "nanosoc"})
        say("a deploy after it came back:", j["state"], str(j.get("error") or "")[:300])


def s_openocd(rig: Rig) -> None:
    bid = rig.board_ids[0]
    p = rig.bpath(bid)
    with rig.client(timeout=60) as c:
        say("deploy:", job(c, "POST", f"{p}/deploy", json={"overlay": "nanosoc"})["state"])
        j = job(c, "POST", f"{p}/debug/up")
        say("debug up:", j.get("result", {}).get("state"), j.get("error"))
        pid = child_matching(rig, "stub_openocd")
        say("OpenOCD pid", pid)
        os.kill(pid, signal.SIGKILL)
        time.sleep(1)
        say("GET debug after OpenOCD died:", c.get(f"{p}/debug").json())
        say("GET debug again:", c.get(f"{p}/debug").json())
        j = job(c, "POST", f"{p}/debug/up")
        say("debug up again:", j.get("result", {}).get("state"), j.get("error"))
        say("debug down:", c.post(f"{p}/debug/down").json().get("state"))


def _tunnel(c, rig: Rig, i: int) -> dict:
    return c.get(f"{rig.bpath(rig.board_ids[i])}/tunnel").json().get("tunnel") or {}


def s_tunnel(rig: Rig) -> None:
    i = 2
    with rig.client() as c:
        t = _tunnel(c, rig, i)
        say("tunnel:", t.get("state"), "pid", t.get("pid"), "restarts", t.get("restarts"))
        os.kill(int(t["pid"]), signal.SIGKILL)
        t0 = time.monotonic()
        seen = []
        while time.monotonic() - t0 < 10:
            t = _tunnel(c, rig, i)
            if not seen or seen[-1][1] != t.get("state"):
                seen.append((round(time.monotonic() - t0, 2), t.get("state"), t.get("detail")))
            if t.get("state") == "up" and len(seen) > 1:
                break
            time.sleep(0.1)
        say("one drop:", seen)
        r = c.get(rig.bpath(rig.board_ids[i]))
        say("GET board after the restart:", r.status_code)
        # repeatedly: kill it as soon as it is back, 5 times
        t0 = time.monotonic()
        ups = []
        for _ in range(5):
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                t = _tunnel(c, rig, i)
                if t.get("state") == "up" and t.get("pid"):
                    break
                time.sleep(0.05)
            ups.append(round(time.monotonic() - t0, 2))
            os.kill(int(t["pid"]), signal.SIGKILL)
            time.sleep(0.3)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and _tunnel(c, rig, i).get("state") != "up":
            time.sleep(0.1)
        ups.append(round(time.monotonic() - t0, 2))
        t = _tunnel(c, rig, i)
        say("repeated drops: up again at", ups, "restarts", t.get("restarts"), t.get("detail"))
        r = c.get(rig.bpath(rig.board_ids[i]))
        say("GET board after the storm:", r.status_code)
        ssh_now = [p for p in rig.children() if "l1_fake_ssh_bin" in rig.seen_children[p]]
        say("ssh processes now:", len(ssh_now), "(one per open board)")


def s_concurrent(rig: Rig) -> None:
    _big_overlay(rig)
    bid = rig.board_ids[0]
    p = rig.bpath(bid)
    calls = [("POST", f"{p}/deploy", {"overlay": "nanosoc_big"})] + [
        ("GET", p, None), ("POST", f"{p}/reset", {"target": "dut"}), ("GET", f"{p}/telemetry", None),
        ("POST", f"{p}/deploy", {"overlay": "nanosoc"}), ("GET", f"{p}/overlays", None),
        ("POST", f"{p}/debug/detect", None), ("DELETE", p, None), ("GET", f"{p}/consoles", None),
        ("POST", f"{p}/clocks", {"name": "dut", "mhz": 25}), ("GET", "/api/v1/boards", None),
    ] * 3

    def one(call):
        method, path, body = call
        with rig.client(timeout=90) as c:
            r = c.request(method, path, json=body) if body is not None else c.request(method, path)
            err = r.json().get("error") or {}
            return method, path.rsplit("/", 1)[-1][:20], r.status_code, err.get("name", "")

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, calls))
    summary: dict[str, int] = {}
    for _m, _pth, code, name in results:
        key = f"{code} {name}".strip()
        summary[key] = summary.get(key, 0) + 1
    say("concurrent results:", summary)
    say("500s:", [r for r in results if r[2] >= 500])
    with rig.client(timeout=90) as c:
        for j in c.get("/api/v1/jobs").json()["jobs"][-5:]:
            say("job", j["kind"], j["state"], (j.get("error") or {}).get("name"))


def s_malformed(rig: Rig) -> None:
    bid = rig.board_ids[0]
    p = rig.bpath(bid)
    cases = [
        ("POST", "/api/v1/boards", "not json"), ("POST", "/api/v1/boards", {"candidate": {"x": 1}}),
        ("POST", "/api/v1/boards", {"candidate": "abc"}), ("POST", "/api/v1/boards", {"target": 5}),
        ("POST", "/api/v1/boards", {"target": "999.1.1.1:notaport"}),
        ("POST", "/api/v1/probe", {"hosts": [1]}), ("POST", "/api/v1/probe", {"timeout_s": "x"}),
        ("POST", f"{p}/deploy", {"overlay": {"rm_id": "zz"}}), ("POST", f"{p}/deploy", {"overlay": []}),
        ("POST", f"{p}/clocks", {"mhz": "fast"}), ("POST", f"{p}/reset", {"target": "everything"}),
        ("POST", f"{p}/consoles/nope/pty", None), ("POST", f"{p}/consoles/uart0/baud", {"baud": -1}),
        ("POST", f"{p}/consoles/uart0/export", {"port": 70000}), ("POST", f"{p}/lab/nope", {}),
        ("POST", f"{p}/controller/command", {"line": "REBOOT"}),
        ("POST", f"{p}/storage/install", {"files": {"a": "rel"}}), ("POST", f"{p}/lease", {"ttl_s": 1}),
        ("POST", f"{p}/power/cycle", {"off_s": 1}), ("GET", f"{p}/lease", None),
        ("POST", "/api/v1/update/check", {"source": "file:///nonexistent"}),
        ("GET", "/api/v1/jobs/nope", None), ("GET", "/api/v1/boards/x%2Fy", None),
    ]
    with rig.client(timeout=60) as c:
        c.post("/api/v1/boards", json={"target": rig.ips[0]})
        for method, path, body in cases:
            if isinstance(body, str):
                r = c.request(method, path, content=body, headers={"Content-Type": "application/json"})
            elif body is None:
                r = c.request(method, path)
            else:
                r = c.request(method, path, json=body)
            try:
                env = r.json()
            except ValueError:
                env = {"raw": r.text[:100]}
            err = env.get("error") or {}
            flag = "  <-- 500" if r.status_code >= 500 else ""
            say(f"{method} {path.replace(p, '<b>')[-40:]} -> {r.status_code} {err.get('name')}: "
                f"{str(err.get('message'))[:90]} | hint: {str(err.get('hint'))[:60]}{flag}")


def s_port(rig: Rig) -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    other = rig.root / "state-port"
    res = subprocess.run([sys.executable, "-m", "harness_manager.daemon", "--state-dir", str(other),
                          "--port", str(port)], env=rig.env(), capture_output=True, text=True,
                         timeout=60)
    say("daemon on a taken port: exit", res.returncode, res.stderr.strip()[-300:])
    s.close()


SCENARIOS = {"console": s_console, "deploy": s_deploy, "openocd": s_openocd, "tunnel": s_tunnel,
             "concurrent": s_concurrent, "malformed": s_malformed, "port": s_port}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=f"/tmp/hm-q2-inject-{os.getpid()}")
    ap.add_argument("scenarios", nargs="*", default=list(SCENARIOS))
    a = ap.parse_args(argv)
    rig = Rig(Path(a.root), 3)
    try:
        rig.setup()
        _big_overlay(rig)
        rig.start_boards()
        rig.start_daemon()
        say("daemon", rig.pid, rig.base)
        open_all(rig)
        for name in a.scenarios:
            say(f"=== {name}")
            try:
                SCENARIOS[name](rig)
            except Exception as exc:  # noqa: BLE001 - one scenario must not end the run
                say(f"scenario {name} raised {type(exc).__name__}: {exc}")
            open_all(rig)
        log = (rig.state / "daemon.log").read_text(errors="replace")
        say("daemon.log: tracebacks:", len(re.findall(r"^Traceback", log, re.M)),
            "errors:", len(re.findall(r" ERROR ", log)))
        say("stop:", rig.stop_daemon())
        time.sleep(1)
        say("debris:", json.dumps(rig.debris()))
        return 0
    finally:
        rig.teardown()
        if rig.seen_children:
            left = [p for p in rig.seen_children if alive(p)]
            if left:
                say("left alive after teardown:", left)


if __name__ == "__main__":
    sys.exit(main())
