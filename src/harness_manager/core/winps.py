"""Read-only questions to Windows PowerShell, answered as JSON (lane WINDOWS).

Harness Manager asks Windows a few things only PowerShell answers well: the disks and
their volumes (the card writer, ``Get-Disk``/``Get-Partition``/``Get-Volume``), the
network adapters and their profiles (the network check, ``Get-NetIPAddress``/
``Get-NetConnectionProfile``). Every script here READS; nothing here changes the system,
and nothing asks for Administrator (HM never escalates: what needs it is printed for the
user to run).

- ``powershell_argv(script)``: ``powershell.exe -NoProfile -NonInteractive
  -ExecutionPolicy Bypass -EncodedCommand <base64 of the UTF-16LE script>`` (no quoting
  rules between Python's command line and PowerShell's can bend the script); the script's
  output is UTF-8 (a volume label with an accent survives) and one JSON document
  (``ConvertTo-Json -Depth 5 -Compress``). ``script_of(argv)`` decodes it back (tests).
- ``run_json(script)``: run it (``core.proc.no_window``: no console window flashes from the
  app), parse the JSON; ``UnavailableError`` when PowerShell is missing, fails or answers
  something that is not JSON.
- ``as_list``: PowerShell's ``ConvertTo-Json`` writes ONE item as an object, not a list;
  this makes either a list.
- ``enum_name``: a CIM enum arrives as a number (``BusType: 7``) from Windows PowerShell
  5.1 and as a name from some hosts; this gives the name either way.

The runner is injectable (``run=``): tests answer with recorded JSON, never PowerShell.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from harness_manager.core.errors import UnavailableError
from harness_manager.core.proc import no_window

#: ``argv -> (exit code, stdout bytes, stderr bytes)``.
Runner = Callable[[Sequence[str]], "tuple[int, bytes, bytes]"]

PREAMBLE = ("$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; ")
TIMEOUT_S = 30.0


def is_windows(platform: str | None = None) -> bool:
    return (platform or sys.platform).startswith("win")


def powershell_exe() -> str:
    """Windows PowerShell 5.1 (every Windows 10/11 has it); ``pwsh`` when only it is there."""
    return shutil.which("powershell.exe") or shutil.which("powershell") or \
        shutil.which("pwsh.exe") or shutil.which("pwsh") or "powershell.exe"


def powershell_argv(script: str, exe: str = "") -> list[str]:
    encoded = base64.b64encode((PREAMBLE + script).encode("utf-16-le")).decode("ascii")
    return [exe or powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
            "Bypass", "-EncodedCommand", encoded]


def script_of(argv: Sequence[str]) -> str:
    """The script a ``powershell_argv`` carries (what a test checks)."""
    return base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")


def _run(argv: Sequence[str], timeout: float = TIMEOUT_S) -> tuple[int, bytes, bytes]:
    try:
        cp = subprocess.run(list(argv), capture_output=True, timeout=timeout, check=False,
                            stdin=subprocess.DEVNULL, **no_window())
    except FileNotFoundError as exc:
        return 127, b"", str(exc).encode()
    except subprocess.TimeoutExpired:
        return 124, b"", f"timed out after {timeout:.0f} s".encode()
    return cp.returncode, cp.stdout or b"", cp.stderr or b""


def run_json(script: str, *, what: str, capability: str = "windows",
             run: Runner | None = None) -> Any:
    """Run ``script`` (it must end in ``ConvertTo-Json``) and return the parsed answer.
    ``what`` names the question in an error ("list the disks")."""
    rc, out, err = (run or _run)(powershell_argv(script))
    if rc != 0:
        text = err.decode("utf-8", errors="replace").strip().splitlines()
        raise UnavailableError(capability, f"PowerShell could not {what}: "
                                           f"{text[-1] if text else f'exit {rc}'}")
    body = out.decode("utf-8-sig", errors="replace").strip()
    if not body:
        return []
    try:
        return json.loads(body)
    except ValueError as exc:
        raise UnavailableError(capability, f"PowerShell answered something that is not JSON "
                                           f"when asked to {what} ({exc})") from exc


def as_list(value: Any) -> list[Any]:
    """``ConvertTo-Json`` of one item is an object: a list either way (None: empty)."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def enum_name(value: Any, names: Mapping[int, str]) -> str:
    """A CIM enum's name, whether it arrives as its number or its name."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return names.get(value, str(value))
    text = str(value or "").strip()
    if text.isdigit():
        return names.get(int(text), text)
    return text


def text(value: Any) -> str:
    """A string field: None and PowerShell's NUL char (no drive letter) become ""."""
    return "" if value is None else str(value).replace("\x00", "").strip()
