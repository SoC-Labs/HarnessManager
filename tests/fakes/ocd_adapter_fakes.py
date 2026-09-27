#!/usr/bin/env python3
"""Fake ``openocd`` binaries that only list their adapters (lane DEBUG-OCD).

``make_fake(dir, mode)`` writes an ``openocd`` launcher (POSIX: a ``sh`` script that
``exec``s Python, so a kill reaches it; Windows: ``openocd.cmd``) that runs this file with a
fixed ``mode``. Each mode prints what that kind of build prints for
``-c "adapter list" -c shutdown`` (and ``-c interface_list -c shutdown``), measured on
srv03335 2026-09-27 where a real one exists:

- ``v012``: the 0.12.0 release ("The following debug adapters are available:", "N: name"),
  with remote_bitbang;
- ``soclabs``: the same words, the SoC Labs build's list (jlink, buspirate, hostio4);
- ``xpack``: xPack 0.12.0-7 (0.12.0+dev), "name { transports }" and no header;
- ``v011``: an older build: ``adapter list`` is an invalid command (exit 1);
  ``interface_list`` lists "debug interfaces", with remote_bitbang;
- ``hang``: prints the banner and sleeps (a probe must time out, not hang);
- ``fail``: exits 2 with an error and no list.

Any other command line (a real session) prints an error and exits 9, so a test sees at
once if anything but the probe ran. ``$FAKE_OPENOCD_LOG``: one JSON line per run
(``mode``, ``argv``, ``pid``).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

MODES = ("v012", "soclabs", "xpack", "v011", "hang", "fail")
V012 = ("ftdi", "jlink", "remote_bitbang", "cmsis-dap")
SOCLABS = ("jlink", "buspirate", "hostio4")
XPACK = (("cmsis-dap", "jtag swd"), ("ftdi", "jtag swd"), ("jlink", "jtag swd"),
         ("remote_bitbang", "jtag swd"), ("st-link", "jtag swd swim"))
V011 = ("parport", "ftdi", "remote_bitbang")
HANG_S = 30.0


def make_fake(directory: Path, mode: str, *, name: str = "openocd",
              python: str | None = None) -> Path:
    """Write the launcher for ``mode`` into ``directory``; return its path."""
    assert mode in MODES, mode
    directory.mkdir(parents=True, exist_ok=True)
    python = python or sys.executable
    me = Path(__file__).resolve()
    if os.name == "nt":
        path = directory / f"{name}.cmd"
        path.write_text(f'@"{python}" "{me}" {mode} %*\r\n')
    else:
        path = directory / name
        path.write_text(f'#!/bin/sh\nexec "{python}" "{me}" {mode} "$@"\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def runs(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


def _say(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def main(mode: str, argv: list[str]) -> int:
    log = os.environ.get("FAKE_OPENOCD_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps({"mode": mode, "argv": argv, "pid": os.getpid()}) + "\n")
    banner = {"xpack": "xPack Open On-Chip Debugger 0.12.0+dev-02228-ge5888bda3-dirty",
              "soclabs": "Open On-Chip Debugger 0.12.0-g9ea7f3d-dirty",
              "v011": "Open On-Chip Debugger 0.10.0"}.get(mode, "Open On-Chip Debugger 0.12.0")
    _say(f"{banner} (fake)")
    _say("Licensed under GNU GPL v2")
    _say("For bug reports, read")
    _say("\thttp://openocd.org/doc/doxygen/bugs.html")
    if mode == "hang":
        time.sleep(HANG_S)
        return 0
    if mode == "fail":
        _say("Error: fake: this build cannot start (exit 2)")
        return 2
    cmds = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c"]
    if argv and argv[0] != "-c" or "-f" in argv or cmds[-1:] != ["shutdown"]:
        _say(f"Error: fake: only the adapter probe is served, not {argv!r}")
        return 9
    ask = cmds[0] if cmds else ""
    if ask == "adapter list":
        if mode == "v011":
            _say('invalid command name "adapter"')
            return 1
        if mode == "xpack":
            for name, transports in XPACK:
                _say(f"{name:<14} {{ {transports} }}")
        else:
            _say("The following debug adapters are available:")
            for i, name in enumerate(SOCLABS if mode == "soclabs" else V012, 1):
                _say(f"{i}: {name}")
            _say("")
    elif ask == "interface_list" and mode == "v011":
        _say("The following debug interfaces are available:")
        for i, name in enumerate(V011, 1):
            _say(f"{i}: {name}")
    else:
        _say(f'invalid command name "{ask.split()[0] if ask else ""}"')
        return 1
    _say("shutdown command invoked")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
