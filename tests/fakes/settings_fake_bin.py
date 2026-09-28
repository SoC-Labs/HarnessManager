"""Fake ``ssh``, ``sg``, ``id`` and ``fpgahub`` EXECUTABLES for the hubs' Test connection
(lane SET-HUBS; promoted from the SETTINGS spike, ``tests/spikes/settings_fake_bin.py``).

``install(bindir)`` writes four ``/bin/sh`` shims that run this module under their own
name. ``ssh`` plays the scenario in ``$SETTINGS_FAKE_SCENARIO`` for the connection, then
runs the remote command with ``sh -c`` (so the quoting crosses two real shells, as on the
hub), where ``sg``, ``id`` and ``fpgahub`` are these fakes too. Nothing reaches a network:
the fake ``ssh`` never connects anywhere, whatever host it is given.

Scenarios: ``ok``, ``dns``, ``timeout``, ``hostkey``, ``auth`` (exit 255, "Permission
denied (publickey)"), ``nogroup`` (the account is not in ``fpga``), ``socket`` (in the group
by ``id``, but the socket refuses: the membership came after the login), ``nofpgahub``,
``badjson``; FIX-PACK-2: ``stalecache`` (the lab hub: ``id -Gn`` misses ``fpga`` from a stale
sssd/nscd cache, but ``sg fpga`` works and so does everything after it) and ``sgrefuses``
(``id -Gn`` lists ``fpga`` but ``sg`` refuses it).

``fpgahub`` answers ``board list --json`` and ``target show T`` (fpgahub 0.3.0's
``console.print_json`` of ``GET /targets/{t}``) from ``t8_hub_rest.FakeFpgahub``'s lab
shape; ``$SETTINGS_FAKE_LOG`` records every call (argv, COLUMNS), so a test can prove
that no lease verb ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCENARIO_ENV = "SETTINGS_FAKE_SCENARIO"
LOG_ENV = "SETTINGS_FAKE_LOG"
REPO = Path(__file__).resolve().parents[2]


def install(bindir: Path) -> Path:
    bindir.mkdir(parents=True, exist_ok=True)
    for name in ("ssh", "sg", "id", "fpgahub"):
        shim = bindir / name
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m tests.fakes.settings_fake_bin '
                        f'{name} "$@"\n')
        shim.chmod(0o755)
    return bindir


def env_for(bindir: Path, scenario: str, log: Path | None = None) -> dict[str, str]:
    """An environment that finds the fakes first (and never a real ssh first)."""
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
           "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{REPO}", SCENARIO_ENV: scenario}
    if log is not None:
        env[LOG_ENV] = str(log)
    return env


def _log(record: dict) -> None:
    path = os.environ.get(LOG_ENV)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def _ssh(args: list[str]) -> int:
    scenario = os.environ.get(SCENARIO_ENV, "ok")
    words = list(args)
    host, remote, i = "", "", 0
    while i < len(words):
        w = words[i]
        if w in ("-o", "-J", "-F", "-i", "-p", "-l"):
            i += 2
            continue
        if w.startswith("-"):
            i += 1
            continue
        host, remote = w, " ".join(words[i + 1:])
        break
    _log({"argv": ["ssh", *args]})
    fail = {
        "dns": f"ssh: Could not resolve hostname {host}: Name or service not known",
        "timeout": f"ssh: connect to host {host} port 22: Connection timed out",
        "hostkey": "Host key verification failed.",
        "auth": f"{os.environ.get('USER', 'me')}@{host}: Permission denied "
                "(publickey,gssapi-with-mic).",
    }.get(scenario)
    if fail:
        print(fail, file=sys.stderr)
        return 255
    return subprocess.run(["sh", "-c", remote], check=False).returncode


def _sg(args: list[str]) -> int:
    scenario = os.environ.get(SCENARIO_ENV, "ok")
    if len(args) != 3 or args[1] != "-c":
        print("usage: sg group [-c command]", file=sys.stderr)
        return 2
    _log({"sg": args})
    if scenario in ("nogroup", "sgrefuses"):
        print("Password: Invalid password.", file=sys.stderr)
        return 1
    return subprocess.run(["sh", "-c", args[2]], check=False).returncode


def _id(args: list[str]) -> int:
    missing = os.environ.get(SCENARIO_ENV) in ("nogroup", "stalecache")
    groups = ["dam1n19", "fp"] + ([] if missing else ["fpga"])
    print(" ".join(groups))
    return 0


def _fpgahub(args: list[str]) -> int:
    scenario = os.environ.get(SCENARIO_ENV, "ok")
    _log({"fpgahub": args, "COLUMNS": os.environ.get("COLUMNS")})
    if scenario == "nofpgahub":
        print("sh: fpgahub: command not found", file=sys.stderr)
        return 127
    if scenario == "socket":
        print("Error: cannot connect to /run/fpgahub/fpgahub.sock: [Errno 13] Permission denied",
              file=sys.stderr)
        return 1
    from tests.fakes.t8_hub_rest import FakeFpgahub

    hub = FakeFpgahub()                   # the lab's shape; never started (no socket)
    if args[:2] == ["board", "list"]:
        if scenario == "badjson":
            print("┏━━━━━━━━┓ a table, not JSON")
            return 0
        print(json.dumps(hub.groups(), indent=2))
        return 0
    if args[:2] == ["target", "show"] and len(args) == 3:
        try:
            print(json.dumps(hub.target_response(args[2]), indent=2))
        except Exception:  # noqa: BLE001 - the fake's HttpError: fpgahub's _die
            print(f"Error: HTTP 404: no such board: {args[2]!r}", file=sys.stderr)
            return 1
        return 0
    print(f"the settings fake answers only `board list` and `target show` (got {args})",
          file=sys.stderr)
    return 2


def main() -> int:
    name, args = sys.argv[1], sys.argv[2:]
    return {"ssh": _ssh, "sg": _sg, "id": _id, "fpgahub": _fpgahub}[name](args)


if __name__ == "__main__":
    sys.exit(main())
