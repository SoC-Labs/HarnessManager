"""Spike (SET-CORE): the secret store against a REAL Secret Service, through the ``keyring``
package. Never collected (``tests/spikes/conftest.py``); run it by hand::

    PYTHONPATH=src:. .venv/bin/python -m tests.spikes.settings_keyring_real

The collected tests (``tests/unit/test_settings_secrets.py``) use a stub keyring. This one
proves the real path, safely:

- a private D-Bus session (``dbus-run-session``) with a throwaway GNOME Keyring, whose
  HOME and XDG dirs are under ``/tmp/setcore-kr-*``: nobody's own keyring is touched;
- inside it, ``KeyringBackend("secret-service")`` (keyring -> SecretStorage -> jeepney) is
  reachable, stores a secret, reads it back, and the index names the backend only;
- back in this process (no session bus, like an ssh session), the same secret reads as
  "stored in the Secret Service, which this process cannot reach" (7), not "not set", and
  no file copy exists;
- setting it again here moves it to the 0600 file, deliberately;
- nothing is left running: only processes whose command line names the temp dir are
  stopped, and the dir is removed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from harness_manager.core.errors import UnreachableError
from harness_manager.settings.secrets import FileBackend, KeyringBackend, SecretStore

REPO = Path(__file__).resolve().parents[2]
SECRET = "tok-REAL-KEYRING-SPIKE"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, evidence: object = "") -> None:
    RESULTS.append((name, bool(ok), str(evidence)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {evidence}" if evidence else ""))


def child(root: str) -> int:
    """INSIDE the private session: the real keyring package, the real Secret Service."""
    k = KeyringBackend("secret-service")
    ok, why = k.available()
    store = SecretStore(Path(root), keyrings=[k])
    st = store.set("hubs.lab.token", SECRET)
    back = store.get("hubs.lab.token") == SECRET
    impl = type(k._impl)
    print(json.dumps({"available": ok, "why": why, "status": st.view(), "readback": back,
                      "class": f"{impl.__module__}.{impl.__qualname__}"}))
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        return child(sys.argv[2])
    for tool in ("dbus-run-session", "gnome-keyring-daemon"):
        if not shutil.which(tool):
            print(f"SKIP: {tool} is not installed")
            return 0
    tmp = Path(tempfile.mkdtemp(prefix="setcore-kr-", dir="/tmp"))
    root = tmp / "state"
    (tmp / "run").mkdir(mode=0o700)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "XDG_RUNTIME_DIR": str(tmp / "run"),
           "XDG_DATA_HOME": str(tmp / "data"), "XDG_CONFIG_HOME": str(tmp / "config"),
           "PYTHONPATH": f"{REPO / 'src'}:{REPO}", "LANG": "C.UTF-8"}
    script = ("echo -n throwaway | gnome-keyring-daemon --unlock --components=secrets "
              f">/dev/null 2>&1; exec {sys.executable} -m tests.spikes.settings_keyring_real "
              f"--child {root}")
    left: list[str] = []
    try:
        here = KeyringBackend("secret-service", env={"PATH": "/usr/bin"})
        ok, why = here.available()
        check("K1 this process has no session bus, so no keyring, and it says why",
              not ok and "session bus" in why, why)
        r = subprocess.run(["dbus-run-session", "--", "sh", "-c", script], env=env, cwd=REPO,
                           capture_output=True, text=True, timeout=90)
        line = (r.stdout.strip().splitlines() or ["{}"])[-1]
        got = json.loads(line)
        check("K2 in a desktop-like session the keyring package reaches the Secret Service",
              got.get("available") is True
              and got.get("class") == "keyring.backends.SecretService.Keyring", line)
        check("K3 ...stores the secret there and reads it back; the reply has no value",
              got.get("status", {}).get("backend") == "secret-service"
              and got.get("readback") is True and SECRET not in line)
        idx = (root / "secrets" / "index.json").read_text()
        check("K4 the index names the backend, never the value",
              "secret-service" in idx and SECRET not in idx, idx.replace("\n", " "))
        ssh = SecretStore(root, keyrings=[KeyringBackend("secret-service",
                                                         env={"PATH": "/usr/bin"})])
        st = ssh.status("hubs.lab.token")
        check("K5 back here: 'stored, unreachable here', not 'not set'",
              st.set and st.backend == "secret-service" and not st.reachable, st.view())
        try:
            ssh.get("hubs.lab.token")
            check("K6 reading it here fails with 7 and the way out", False)
        except UnreachableError as exc:
            check("K6 reading it here fails with 7 and the way out",
                  int(exc.code) == 7 and bool(exc.hint), f"{exc.message} | {exc.hint}")
        check("K7 no file copy was written",
              not FileBackend(root / "secrets").path("hubs.lab.token").exists())
        st = ssh.set("hubs.lab.token", "tok-MOVED")
        check("K8 setting it again here moves it to the private file",
              st.backend == "file" and ssh.get("hubs.lab.token") == "tok-MOVED", st.view())
    finally:
        left = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", str(tmp)],
                              capture_output=True, text=True).stdout.split()
        for pid in left:                    # only processes naming OUR temp dir
            subprocess.run(["kill", pid], check=False)
        shutil.rmtree(tmp, ignore_errors=True)
    check("K9 nothing left running from the throwaway session", not left, left or "none")
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} PASS" + (f"; FAIL: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
