"""The OpenSSH client, the same on every OS (lane WINDOWS).

Harness Manager runs the system's ``ssh`` for a Linux harness's claim, its board SSH, the
tunnels through a hub and the debug forward (OpenOCD is on the board). On Windows that is
OpenSSH for Windows (``C:\\Windows\\System32\\OpenSSH\\ssh.exe``, an optional feature that
Windows 10 1809+ and 11 install by default), and three things differ:

- **The program.** ``ssh_program()`` is the absolute path of ``ssh.exe`` on Windows (a
  ProxyJump (``-J``) starts a second ssh, which some OpenSSH for Windows builds find only
  when the first was started by its full path); ``ssh`` elsewhere (unchanged).
- **Paths inside ``-o``.** ssh splits an ``-o`` value as it splits a config line: a space
  ends it (``C:\\Users\\Ann Lee\\...`` would become two files) and a backslash can escape.
  ``option_path()`` gives forward slashes on Windows (OpenSSH for Windows takes them) and
  double quotes round a path with a space (every OS).
- **The system config** is ``%ProgramData%\\ssh\\ssh_config`` on Windows
  (``system_config()``), ``/etc/ssh/ssh_config`` elsewhere.

Not different: ``ControlPath=none``/``ControlMaster=no`` parse on Windows (HM never uses
a ControlMaster there: OpenSSH for Windows has none), ``IdentitiesOnly``, ``-i``,
``HostKeyAlias``, ``-J``; ``/dev/null`` is OpenSSH for Windows' own name for ``NUL``.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path


def _windows(platform: str | None) -> bool:
    return (platform or sys.platform).startswith("win")


def ssh_program(platform: str | None = None, *,
                which: Callable[[str], str | None] | None = None,
                environ: Mapping[str, str] | None = None,
                exists: Callable[[str], bool] | None = None) -> str:
    """``ssh`` (POSIX), or the absolute path of ``ssh.exe`` (Windows: PATH first, then
    ``%SystemRoot%\\System32\\OpenSSH``); plain ``ssh`` when neither is there (the caller
    says "install the OpenSSH client")."""
    if not _windows(platform):
        return "ssh"
    found = (which or shutil.which)("ssh")
    if found:
        return found
    env = os.environ if environ is None else environ
    root = env.get("SystemRoot") or env.get("SYSTEMROOT") or "C:\\Windows"
    builtin = f"{root}\\System32\\OpenSSH\\ssh.exe"
    return builtin if (exists or os.path.isfile)(builtin) else "ssh"


def option_path(path: str | os.PathLike[str], platform: str | None = None) -> str:
    """A path for an ``-o Key=VALUE`` or a config line: forward slashes on Windows, and in
    double quotes when it holds a space (ssh would split it)."""
    text = os.fspath(path)
    if _windows(platform):
        text = text.replace("\\", "/")
    if any(c.isspace() for c in text):
        text = f'"{text}"'
    return text


def system_config(platform: str | None = None,
                  environ: Mapping[str, str] | None = None) -> Path:
    """OpenSSH's system-wide client config."""
    if _windows(platform):
        env = os.environ if environ is None else environ
        return Path(env.get("ProgramData") or env.get("PROGRAMDATA") or "C:\\ProgramData") \
            / "ssh" / "ssh_config"
    return Path("/etc/ssh/ssh_config")


INSTALL_HINT_WINDOWS = ("install the OpenSSH client: Settings, System, Optional features, "
                        "View features, OpenSSH Client (or, as Administrator: Add-WindowsCapability "
                        "-Online -Name OpenSSH.Client~~~~0.0.1.0)")


def install_hint(platform: str | None = None) -> str:
    return INSTALL_HINT_WINDOWS if _windows(platform) else "install the OpenSSH client"
