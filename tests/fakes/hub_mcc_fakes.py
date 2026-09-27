"""MCC-FIX fakes: the MCC ON the hub, with no hub and no board.

- ``PtyMcc``: a ``FakeMcc`` behind a real pseudo-terminal, so the hub-side reader
  (``hub_mcc.HUB_MCC_READ_PY``) opens a real tty path, sets it raw and types at its own pace,
  exactly as it would on the hub. The parent keeps only the master side (the second-reader
  scan must not see the test itself holding the tty).
- ``HubTool``: a hub runner. It runs the hub-side reader FOR REAL against a ``PtyMcc`` (the tty
  path Harness Manager names is swapped for the pty's), answers the Python 3.10+ probe, and
  plays pyverify's hub-side writer (``HUB_MCC_REBOOT_PY``) against the same ``FakeMcc``:
  ``others`` for a second reader, ``mcc.bare_crlf_crs`` for the post-SD-write quirk. Any other
  argv goes to ``inner`` (the L1 fake hub), and every argv is recorded in ``calls``.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
from typing import Any

from pyverify.bootrate import HUB_MCC_REBOOT_PY
from pyverify.lease import RunResult

from harness_manager_mps3.hub_mcc import HUB_MCC_READ_PY
from tests.fakes.fake_mcc import BOOT_BANNER, FakeMcc

MCC_TTY = "/dev/mps3_01_pl/tty_00"


class PtyMcc:
    def __init__(self, mcc: FakeMcc) -> None:
        self.mcc = mcc
        mcc.read(4096)                   # FakeMcc starts with a prompt queued; a real idle MCC is silent
        master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)                  # only the script holds the slave (the reader scan)
        self.master = master
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._pump, name="pty-mcc", daemon=True)
        self._t.start()

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self.master], [], [], 0.005)
                if r:
                    for b in os.read(self.master, 4096):
                        self.mcc.write(bytes([b]))
                out = self.mcc.read(4096)
                if out:
                    os.write(self.master, out)
            except OSError:                  # EIO while no one has the slave open
                time.sleep(0.005)

    def close(self) -> None:
        self._stop.set()
        self._t.join(timeout=2)
        os.close(self.master)

    def __enter__(self) -> PtyMcc:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class HubTool:
    def __init__(self, inner: Any = None, *, mcc: FakeMcc | None = None,
                 pty: PtyMcc | None = None, tty: str = MCC_TTY, python: str | None = None,
                 hub_python: str = "/usr/bin/python3.11") -> None:
        self.inner = inner
        self.mcc = mcc if mcc is not None else (pty.mcc if pty is not None else None)
        self.pty = pty
        self.tty = tty
        self.python = python                 # None: run the argv as given (sh + the picker)
        self.hub_python = hub_python
        self.others: list[list[Any]] = []
        self.calls: list[list[str]] = []
        self.writer_runs: list[dict[str, Any]] = []
        self.reader_runs: list[dict[str, Any]] = []
        self.files: dict[str, str] = {}

    def __call__(self, argv: Any, timeout: float | None = None) -> RunResult:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["sh", "-c"] and "sys.version_info < (3, 10)" in argv[2]:
            return RunResult(0 if self.hub_python else 127,
                             f"{self.hub_python}\n" if self.hub_python else "", "")
        if len(argv) == 5 and argv[:2] == ["sh", "-c"] and argv[3] == HUB_MCC_READ_PY:
            return self._reader(argv, timeout)
        if len(argv) == 4 and argv[1:3] == ["-c", HUB_MCC_REBOOT_PY]:
            return self._writer(argv[0], json.loads(argv[3]))
        if argv[:2] == ["sh", "-c"] and argv[2].startswith("cat --") and len(argv) == 5:
            return RunResult(0, self.files.pop(argv[4], ""), "")
        if self.inner is not None:
            return self.inner(argv, timeout=timeout)
        return RunResult(2, "", f"unexpected hub command {argv}\n")

    def _reader(self, argv: list[str], timeout: float | None) -> RunResult:
        args = json.loads(argv[4])
        self.reader_runs.append(dict(args))
        if self.readers():                  # the script's /proc scan would find them on a hub
            return RunResult(3, json.dumps({"tty": args["tty"], "others": self.readers(),
                                            "rc": 3, "reason": "another process reads "
                                            + args["tty"]}) + "\n", "")
        if self.pty is not None and args["tty"] == self.tty:
            args["tty"] = self.pty.path
        run = [self.python, "-c", HUB_MCC_READ_PY] if self.python else argv[:4]
        proc = subprocess.run([*run, json.dumps(args)], capture_output=True, text=True,
                              timeout=timeout or 60, check=False)
        out = proc.stdout.replace(self.pty.path, self.tty) if self.pty is not None else proc.stdout
        return RunResult(proc.returncode, out, proc.stderr)

    def _writer(self, python: str, args: dict[str, Any]) -> RunResult:
        """pyverify's HUB_MCC_REBOOT_PY, played against the FakeMcc's state."""
        self.writer_runs.append({**args, "python": python})
        out: dict[str, Any] = {"tty": args["tty"], "mode": args["mode"], "sent": False,
                               "ack": False, "others": [], "prompt": "", "echo": ""}

        def done(rc: int, reason: str | None = None) -> RunResult:
            out.update(rc=rc, reason=reason)
            return RunResult(rc, json.dumps(out) + "\n", "")

        if args["tty"] != self.tty or self.mcc is None:
            return done(2, f"no such tty {args['tty']} on this host")
        if self.readers():
            out["others"] = self.readers()
            return done(3, f"another process reads {args['tty']}")
        if args["mode"] == "scan":
            return done(0)
        if self.mcc.bare_crlf_crs > 0:
            self.mcc.bare_crlf_crs -= 1
            out["prompt"] = "\r\n"
            return done(4, "no intact Cmd> after a bare CR")
        if self.mcc.menu != "main":
            out["prompt"] = "\r\nDebug> "
            return done(4, "Debug> submenu, not Cmd>")
        out["prompt"] = "\r\nCmd> "
        out.update(sent=True, ack=True, echo="REBOOT\r\nRebooting...")
        self.mcc.reboots += 1
        if self.mcc.on_reboot:
            self.mcc.on_reboot()
        if self.mcc.on_boot:
            self.mcc.on_boot()
        if float(args.get("capture_s") or 0) > 0:
            text = "\r\n".join(BOOT_BANNER) + "\r\nCmd> "
            if args.get("log"):
                self.files[args["log"]] = text
            out.update(configuring=True, complete=True, failed=False, log=args.get("log"),
                       tail=text[-400:])
        return done(0)

    def readers(self) -> list[list[Any]]:
        """Other readers of the MCC tty: ``others``, plus an fpgahub share on it (its broker
        holds the tty open: a second reader, exactly what the real scan finds)."""
        out = [list(o) for o in self.others]
        shares = getattr(self.inner, "shares", {}) or {}
        if self.tty in shares:
            out.append([4100, f"/opt/fpgahub/bin/python3.11 -m fpgahub.tty_share {self.tty}"])
        return out

    def share_starts(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["fpgahub", "share", "start"]]
