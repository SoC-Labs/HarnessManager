"""Spike (lane OTA): restart the daemon onto the switched version; roll back if it is unhealthy.

    python ota_restart.py LAUNCHER STATE_DIR [HEALTH_S]

A prototype of the missing "restart" step (design: docs/design/HM_SELF_UPDATE.md,
"Switch and restart"). What it does, in order:

1. read the running daemon's port from ``daemon.json`` (so the new daemon keeps it);
2. ``LAUNCHER daemon stop`` (the old daemon; refused by the daemon while a job runs);
3. ``LAUNCHER daemon start --port P``: the launcher follows ``current.json``, so this is
   the NEW version;
4. poll ``/health`` for HEALTH_S seconds and require ``version == current``;
5. otherwise: T7's own ``AppUpdater.rollback()`` (the pointer goes back), then start
   again (the OLD version), and report ``rolled-back``.

Run it with the INSTALLER's venv Python (a stable interpreter that is neither version),
so it can import ``harness_manager.services.update.app`` whatever the pointer says.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe


def health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def run(argv: list[str]) -> tuple[int, str]:
    res = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    return res.returncode, (res.stdout + res.stderr).strip()


def start_and_wait(launcher: str, port: int, want: str, health_s: float) -> tuple[bool, str]:
    rc, out = run([launcher, "daemon", "start", "--port", str(port)])
    if rc != 0:
        return False, f"start exited {rc}: {out.splitlines()[-1] if out else ''}"
    deadline = time.monotonic() + health_s
    while time.monotonic() < deadline:
        h = health(port)
        if h and h.get("ok"):
            if want and h.get("version") != want:
                return False, f"/health says {h.get('version')}, expected {want}"
            return True, f"/health ok, version {h.get('version')}, pid {h.get('pid')}"
        time.sleep(0.25)
    return False, f"no healthy /health within {health_s:g}s"


def main() -> int:
    launcher, state_dir = sys.argv[1], Path(sys.argv[2])
    health_s = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
    info = json.loads((state_dir / "daemon.json").read_text())
    port = int(info["port"])
    up = AppUpdater(AppLayout(state_dir / "update" / "app"), LocalBusyProbe(state_dir))
    want = up.state()["current"]
    t0 = time.monotonic()
    rc, out = run([launcher, "daemon", "stop"])
    print(f"stop      rc={rc} {out.splitlines()[0] if out else ''}")
    if rc not in (0, 8):
        print("result    not-restarted (the daemon refused to stop: a job runs)")
        return 4
    ok, why = start_and_wait(launcher, port, want, health_s)
    print(f"start     {want or 'installer venv'}: {'OK' if ok else 'FAILED'}: {why}")
    if ok:
        print(f"result    restarted on {want} in {time.monotonic() - t0:.1f}s, same port {port}")
        return 0
    st = up.rollback()
    print(f"rollback  pointer back to {st['current']} (previous {st['previous']})")
    run([launcher, "daemon", "stop"])            # clears a stale daemon.json, if any
    ok, why = start_and_wait(launcher, port, st["current"], health_s)
    print(f"start     {st['current']}: {'OK' if ok else 'FAILED'}: {why}")
    print(f"result    {'rolled-back' if ok else 'DOWN'} in {time.monotonic() - t0:.1f}s")
    return 6


if __name__ == "__main__":
    sys.exit(main())
