"""Lane L2 test rig: the ``uart_baud`` harness verb, a serial port that records its rate, PTY clients.

- ``L2BaudShell``: pyverify's FakeShell plus lane L6's ``uart_baud`` verb, in the wire
  shape frozen on 2026-09-23 (docs: the L2/L6 brief): ``{"op":"uart_baud","stream":S}``
  -> ``{"ok":true,"stream":S,"baud":N,"mode":"fixed|auto|set","settable":B}``;
  ``{"op":...,"baud":N}`` sets it; ``baud: 0`` clears the override. FakeShell does not
  know the ``uart_baud`` feature yet (its VERSION_FEATURES would refuse it), so the
  feature is set after construction, as Team T9 did for ``sysmon``.
- ``install_uart_baud_codec``: gives the installed pyverify a stand-in
  ``ShellClient.uart_baud(stream, baud=None)`` (same request; ``raw`` carries the reply)
  when lane L6's codec is not installed yet. Tests only.
- ``apply_pack_ccr``: wires ``harness_manager_mps3.uart`` into ``Mps3Consoles`` exactly as
  the L2 contract change request asks the lead to (a no-op once it is applied).
- ``LOOP``: the ``l2loop://`` serial scheme. Each open is recorded with its rate; the
  port echoes what it is sent and greets each open with ``serial up at <baud>``.
- ``PtyClient``: a SEPARATE process holding the PTY open (the daemon's own pid is never
  counted as a client), optionally exclusive (TIOCEXCL) the way ``screen`` opens it.
- ``PtyHolder``: a separate process that opens the PTY and never reads it (a probe);
  ``queued(fd)`` is the tty input queue, the bytes waiting for the next reader;
  ``PtyClient(setup_s=...)`` sets its line up with TCSAFLUSH after opening, as screen does.

VirtualMps3 itself is lead-owned and untouched: ``l2_virtual_board`` swaps its shell.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyverify.client import ShellClient
from pyverify.testing.fakeshell import FakeShell

from harness_manager.core.transport import register_serial_scheme
from tests.fakes.virtual_board import FIELDED_3F1A560F, VirtualMps3

NANOSOC_RM_ID = 0x01000001
NANOSOC_UPY_RM_ID = 0x01000005
ETH_SS_RM_ID = 0x01000002


# --- the uart_baud verb ------------------------------------------------------------------------


def _stream_state(baud: int, settable: bool) -> dict[str, Any]:
    return {"baud": baud, "default": baud, "mode": "auto" if settable else "fixed",
            "settable": settable}


class L2BaudShell(FakeShell):
    """FakeShell answering ``uart_baud`` (lane L6's frozen shape)."""

    uart: dict[str, dict[str, Any]]
    ops: list[dict[str, Any]]

    def handle_control(self, request: dict[str, Any]) -> dict[str, Any]:
        self.ops.append(dict(request))
        if request.get("op") == "uart_baud":
            return self._op_uart_baud(request)
        return super().handle_control(request)

    def _op_uart_baud(self, req: dict[str, Any]) -> dict[str, Any]:
        stream = req.get("stream", "uart0")
        st = self.uart.get(stream)
        if st is None:
            return {"ok": False, "err": f"unknown stream {stream!r}"}
        if "baud" in req:
            baud = req["baud"]
            if isinstance(baud, bool) or not isinstance(baud, int) or baud < 0:
                return {"ok": False, "err": "EINVAL: baud must be a whole number >= 0"}
            if not st["settable"]:
                return {"ok": False, "err": "EFIXED: the loaded design has no run-time divisor"}
            if baud == 0:
                st["baud"], st["mode"] = st["default"], "auto"
            elif not 1200 <= baud <= 3_000_000:
                return {"ok": False, "err": f"ERANGE: {baud} is outside 1200..3000000"}
            else:
                st["baud"], st["mode"] = baud, "set"
        return {"ok": True, "stream": stream, "baud": st["baud"], "mode": st["mode"],
                "settable": st["settable"]}


@dataclass(frozen=True)
class _UartBaudResponse:
    ok: bool
    stream: str = ""
    baud: int = 0
    mode: str = ""
    settable: bool = False
    err: str = ""
    raw: dict = field(default_factory=dict)


def _l6_uart_baud(self: ShellClient, stream: str = "uart0",
                  baud: int | None = None) -> _UartBaudResponse:
    """Stand-in for lane L6's ``ShellClient.uart_baud``: the same request, ``raw`` carrier."""
    op: dict[str, Any] = {"op": "uart_baud", "stream": stream}
    if baud is not None:
        op["baud"] = baud
    resp = self._request(op)
    return _UartBaudResponse(ok=bool(resp["ok"]), stream=str(resp.get("stream", "")),
                             baud=int(resp.get("baud") or 0), mode=str(resp.get("mode", "")),
                             settable=bool(resp.get("settable")), err=str(resp.get("err", "")),
                             raw=dict(resp))


def install_uart_baud_codec(monkeypatch: Any) -> bool:
    """Give the installed pyverify a ``uart_baud()`` if it lacks one; True if it was added."""
    if callable(getattr(ShellClient, "uart_baud", None)):
        return False                     # lane L6's real codec is installed: use it
    monkeypatch.setattr(ShellClient, "uart_baud", _l6_uart_baud, raising=False)
    return True


def l2_virtual_board(tmp_path: Path, *, features: tuple[str, ...] = ("uart_baud",),
                     settable: bool = True, rm_id: int = NANOSOC_RM_ID,
                     usb: bool = False) -> VirtualMps3:
    """A VirtualMps3 (fielded profile) with ``rm_id`` loaded and, by default, ``uart_baud``."""
    vb = VirtualMps3(tmp_path, usb=usb, boot_rm_id=rm_id)
    p = FIELDED_3F1A560F
    shell = L2BaudShell.ephemeral(
        static_id=p.static_id, boot_rm_id=rm_id, reset_targets=p.reset_targets,
        harness_version=p.harness_version, harness_sha=p.harness_sha,
        harness_usr_access=p.usr_access, features=p.features)
    shell.features = tuple(p.features) + tuple(features)   # bypasses FakeShell's feature check
    shell.ops = []
    shell.uart = {"uart0": _stream_state(76800, settable), "uart1": _stream_state(76800, settable)}
    vb.shell = shell
    return vb


def apply_pack_ccr(monkeypatch: Any) -> bool:
    """The L2 pack CCR, applied for a test: ``Mps3Consoles`` gets the shell and the two
    optional ``ConsoleAdapter`` methods from ``harness_manager_mps3.uart``. False (and
    nothing patched) once the lead has applied it to ``pack.py``."""
    from harness_manager_mps3 import pack, uart

    if hasattr(pack.Mps3Consoles, "console_baud_info"):
        return False
    original = pack.Mps3Session.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        if self.consoles is not None:
            self.consoles._shell = self.shell

    def info(self: Any) -> dict[str, dict]:
        return uart.console_baud_info(self._endpoints, getattr(self, "_shell", None))

    def set_baud(self: Any, name: str, baud: int) -> dict:
        return uart.console_set_baud(self._endpoints, getattr(self, "_shell", None), name, baud)

    monkeypatch.setattr(pack.Mps3Session, "__init__", init)
    monkeypatch.setattr(pack.Mps3Consoles, "console_baud_info", info, raising=False)
    monkeypatch.setattr(pack.Mps3Consoles, "console_set_baud", set_baud, raising=False)
    return True


# --- a serial port that records its rate ----------------------------------------------------------


class LoopSerial:
    """A host serial port (``core.transport.SerialPort``) that echoes what it is sent."""

    def __init__(self, address: str, baud: int) -> None:
        self.address = address
        self.baud = baud
        self.closed = False
        self._rx = bytearray(f"serial up at {baud}\r\n".encode())
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        if self.closed:
            raise OSError("port closed")
        with self._lock:
            self._rx += data
        return len(data)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            out = bytes(self._rx[:size])
            del self._rx[:size]
            return out

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        return self.read(size or len(self._rx))

    @property
    def in_waiting(self) -> int:
        if self.closed:
            raise OSError("port closed")
        with self._lock:
            return len(self._rx)

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._rx.clear()

    def close(self) -> None:
        self.closed = True


class LoopScheme:
    """The ``l2loop://`` opener: every open is recorded as (address, baud)."""

    scheme = "l2loop"

    def __init__(self) -> None:
        self.opens: list[tuple[str, int]] = []
        self.ports: list[LoopSerial] = []
        self._lock = threading.Lock()

    def open(self, address: str, baud: int) -> LoopSerial:
        port = LoopSerial(address, baud)
        with self._lock:
            self.opens.append((address, baud))
            self.ports.append(port)
        return port

    def reset(self) -> None:
        with self._lock:
            self.opens.clear()
            self.ports.clear()

    def rates(self, address: str) -> list[int]:
        with self._lock:
            return [b for a, b in self.opens if a == address]


LOOP = LoopScheme()
register_serial_scheme(LOOP.scheme, LOOP.open)


# --- PTY clients in another process ----------------------------------------------------------------

_CLIENT = r"""
import fcntl, os, select, sys, termios, time
path, excl, speed, setup = sys.argv[1], sys.argv[2] == "1", int(sys.argv[3]), float(sys.argv[4])
fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
if excl:
    fcntl.ioctl(fd, termios.TIOCEXCL)
if speed:
    a = termios.tcgetattr(fd)
    a[4] = a[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, a)
sys.stdout.write("open\n"); sys.stdout.flush()
if setup:
    # What GNU screen does ~200 ms after it opens a tty: its own modes, with TCSAFLUSH,
    # which throws away whatever was queued on the line before.
    time.sleep(setup)
    a = termios.tcgetattr(fd)
    a[0] |= termios.IGNBRK
    termios.tcsetattr(fd, termios.TCSAFLUSH, a)
while True:
    r, _, _ = select.select([fd, sys.stdin], [], [], 0.1)
    if sys.stdin in r:
        line = sys.stdin.readline()
        if not line or line.strip() == "quit":
            break
        os.write(fd, line.rstrip("\n").encode() + b"\r")
    if fd in r:
        data = os.read(fd, 4096)
        sys.stdout.write(data.hex() + "\n"); sys.stdout.flush()
"""


class PtyClient:
    """A separate process with the PTY open: reads (hex lines on stdout) and types (stdin).

    ``exclusive`` sets TIOCEXCL as ``screen`` does; ``speed`` (a termios constant) sets the
    line speed as ``screen <path> <rate>`` does; ``setup_s`` > 0 sets its own line modes
    with TCSAFLUSH that long after opening, before reading anything, as screen does.
    """

    def __init__(self, path: str, *, exclusive: bool = False, speed: int = 0,
                 setup_s: float = 0.0) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _CLIENT, path, "1" if exclusive else "0", str(speed),
             str(setup_s)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.got = bytearray()
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        first = self._line(10.0)
        if first != "open":
            self.proc.kill()
            err = self.proc.stderr.read()
            raise OSError(f"the PTY client did not open {path}: {first!r} {err}")

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line.strip())
        self._lines.put("")

    def _line(self, timeout: float) -> str:
        try:
            return self._lines.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return ""

    def read_until(self, pattern: bytes, timeout: float = 10.0) -> bytes:
        deadline = time.monotonic() + timeout
        while pattern not in self.got:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"{pattern!r} not seen within {timeout}s; got {bytes(self.got)!r}")
            line = self._line(left)
            if line:
                self.got += bytes.fromhex(line)
        return bytes(self.got)

    def type(self, text: str) -> None:
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for f in (self.proc.stdin, self.proc.stderr):
            try:
                f.close()
            except OSError:
                pass

    def __enter__(self) -> PtyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_HOLDER = r"""
import fcntl, os, sys, termios
path, excl = sys.argv[1], sys.argv[2] == "1"
fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
if excl:
    fcntl.ioctl(fd, termios.TIOCEXCL)
sys.stdout.write("open\n"); sys.stdout.flush()
sys.stdin.readline()
os.close(fd)
sys.stdout.write("closed\n"); sys.stdout.flush()
"""


class PtyHolder:
    """A separate process that opens the PTY and NEVER reads it, until ``release()``.

    It is what a program that probes a terminal and leaves looks like (``stty -F``, a
    terminal emulator checking the device, a screen started and quit at once): an
    open and a close, with the board output still queued for the next client.
    ``exclusive`` sets TIOCEXCL as ``screen`` does (and leaves it set on close).
    """

    def __init__(self, path: str, *, exclusive: bool = False) -> None:
        self.proc = subprocess.Popen([sys.executable, "-c", _HOLDER, path,
                                      "1" if exclusive else "0"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline().strip()
        if line != "open":
            self.proc.kill()
            raise OSError(f"the PTY holder did not open {path}: {line!r}")

    def release(self) -> None:
        """Close the device (the process then exits)."""
        if self.proc.poll() is None:
            self.proc.stdin.write("\n")
            self.proc.stdin.flush()
            self.proc.stdout.readline()
            self.proc.wait(timeout=10)
        for f in (self.proc.stdin, self.proc.stdout):
            f.close()


def queued(fd: int) -> int:
    """Bytes waiting in a tty's input queue (TIOCINQ), i.e. queued for the next reader."""
    import fcntl
    import struct
    import termios

    return struct.unpack("i", fcntl.ioctl(fd, termios.TIOCINQ, struct.pack("i", 0)))[0]


def open_fails_busy(path: str) -> bool:
    """True when opening ``path`` from ANOTHER process fails with EBUSY (TIOCEXCL is set)."""
    code = ("import os,sys\ntry:\n    os.close(os.open(sys.argv[1], os.O_RDWR|os.O_NOCTTY))\n"
            "except OSError as e:\n    sys.exit(16 if e.errno == 16 else 1)\n")
    return subprocess.run([sys.executable, "-c", code, path], timeout=10).returncode == 16


def fast_pty_options(**extra: Any) -> dict[str, Any]:
    """PtyManager timings for tests: poll 50 ms, scan 100 ms, stall 0.3 s, settle 0.3 s."""
    return {"poll_s": 0.05, "scan_s": 0.1, "stall_s": 0.3, "scan_budget_s": 5.0,
            "settle_s": 0.3, **extra}


def in_ring(port: Any, data: bytes) -> bool:
    """``data`` is in the PTY's replay ring (the recent output a new client is shown)."""
    return data in bytes(port._ring)


def wait_for(predicate: Any, timeout: float = 10.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


_PTY_DIRS: list[Path] = []


def _remove_pty_dirs() -> None:
    import shutil

    for d in _PTY_DIRS:
        shutil.rmtree(d, ignore_errors=True)


def pty_dir(tmp_path: Path) -> Path:
    """A private PTY directory for one test (``HARNESS_MANAGER_PTY_DIR``).

    Under /tmp where there is one, like the product's own default. On srv03335 something
    outside our processes opens new tty devices whose links appear under $TMPDIR
    (/tmpdir), which upsets the exact client counts these tests check. It never touches
    /tmp (lane L2's measurements).
    """
    import atexit
    import tempfile

    base = Path("/tmp")
    if os.name == "posix" and base.is_dir():
        d = Path(tempfile.mkdtemp(prefix="hm-pty-", dir=base))
        if not _PTY_DIRS:
            atexit.register(_remove_pty_dirs)
        _PTY_DIRS.append(d)
        return d / "ptys"
    return tmp_path / "ptys"


def is_link_to(path: str | Path, device: str) -> bool:
    try:
        return os.readlink(path) == device
    except OSError:
        return False


# --- the CLI with the L2 verbs wired in ---------------------------------------------------------


def _with_l2_verbs(original: Any) -> Any:
    import argparse

    from harness_manager.cli.cmd_io import register

    def make_parser() -> argparse.ArgumentParser:
        parser = original()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        if "pty" not in sub.choices:           # main.py wires them once the CCR is applied
            register(sub)
        return parser

    return make_parser


def run_cli(capsys: Any, *argv: str) -> tuple[int, str, str]:
    """``harness-manager`` ``main()`` with ``pty``/``baud`` registered as the lead will wire them."""
    from harness_manager.cli import main as cli_main

    original = cli_main.make_parser
    cli_main.make_parser = _with_l2_verbs(original)
    try:
        rc = cli_main.main(list(argv))
    finally:
        cli_main.make_parser = original
    out, err = capsys.readouterr() if capsys is not None else ("", "")
    return rc, out, err
