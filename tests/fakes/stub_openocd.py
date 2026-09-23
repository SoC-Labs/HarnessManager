#!/usr/bin/env python3
"""stub_openocd: a stand-in for the ``openocd`` binary, for Team T4's tests.

It reads its command line the way OpenOCD 0.12 does, and fails the way OpenOCD
fails, so the debug service's ordering, port and error handling can be tested
with no OpenOCD installed. Every message it prints is the real one (measured
against OpenOCD 0.12.0+dev, 2026-09-23, with tests/fakes/t4_rbb_jtag.py).

What it checks, and how it fails (a non-zero exit, so a test cannot miss it):

- ``-f``/``-c`` run in argv order; ``-s`` adds a search directory.
- **Probe overrides after a ``-f`` (exit 2).** The MPS3 target halves read
  ``RBB_HOST``/``RBB_PORT``/``TRANSPORT_MODE`` with ``info exists``; a ``set``
  after the ``-f`` is silently ignored by the real OpenOCD. The stub refuses it.
- **A port command after ``init`` (exit 1)**, with OpenOCD's own message.
- **A config it cannot find (exit 1)**, like OpenOCD.
- **Never leaves 127.0.0.1 (exit 3).** If the probe half would dial anything
  else (the configs' default is the lab board, 192.168.10.101:6921), it
  refuses instead of connecting.

What it does:

- ``init`` (explicit, or implied after the command line) connects to
  ``RBB_HOST:RBB_PORT`` like the remote_bitbang adapter: refused ->
  ``Failed to connect``; accepted then closed at once (the board's
  single-client refusal) -> ``remote_bitbang_fill_buf ... reset by peer``. It
  then reports the IDCODE from ``$STUB_OPENOCD_IDCODE`` (default 0x6ba00477;
  ``none`` -> ``all zeroes``). ``$STUB_OPENOCD_NO_ADAPTER=1`` skips the dial.
- Binds telnet and tcl before ``init`` and gdb inside it, printing OpenOCD's
  ``Listening on port N for <svc> connections`` lines. A taken telnet/tcl port
  exits; a taken gdb port is logged and the stub keeps running, as OpenOCD does.
- tcl RPC (0x1a-terminated): ``version``, ``scan_chain``,
  ``<target> cget -event gdb-attach``, ``shutdown``.
- A gdb connection records a ``gdb-attach`` event (with the hook it would run).
- SIGTERM: prints ``shutdown command invoked`` and exits 0.

``$STUB_OPENOCD_LOG``: one JSON line per run (``argv``, ``pid``) and per event.
``$STUB_OPENOCD_INIT_DELAY``: seconds to wait before ``init`` (a slow board).
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import socket
import stat
import sys
import time
from pathlib import Path

PROBE_VARS = ("RBB_HOST", "RBB_PORT", "TRANSPORT_MODE", "XVC_HOST", "XVC_PORT")
PORT_CMDS = ("gdb_port", "telnet_port", "tcl_port", "bindto")
LOOPBACK = ("127.0.0.1", "localhost")


# --- helpers usable from the tests ---------------------------------------------------


def make_wrapper(directory: Path, python: str | None = None) -> Path:
    """Write an ``openocd`` launcher for this stub into ``directory``; return its path.

    Point ``$HARNESS_MANAGER_OPENOCD`` at it. POSIX: a ``sh`` script that ``exec``s
    Python (so the pid is the stub's). Windows: a ``.cmd``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    python = python or sys.executable
    stub = Path(__file__).resolve()
    if os.name == "nt":
        path = directory / "openocd.cmd"
        path.write_text(f'@"{python}" "{stub}" %*\r\n')
    else:
        path = directory / "openocd"
        path.write_text(f'#!/bin/sh\nexec "{python}" "{stub}" "$@"\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def read_log(path: Path) -> list[dict]:
    """Every JSON line the stub wrote (runs and events)."""
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# --- the stub itself ------------------------------------------------------------------


def _log(entry: dict) -> None:
    target = os.environ.get("STUB_OPENOCD_LOG")
    if target:
        with open(target, "a") as fh:
            fh.write(json.dumps(entry) + "\n")


def _say(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _split_commands(script: str) -> list[str]:
    """Split a ``-c`` script on top-level ``;`` (braces protect a Tcl body)."""
    out, depth, cur = [], 0, []
    for ch in script:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == ";" and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [c for c in out if c]


class Stub:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.search: list[Path] = []
        self.vars: dict[str, str] = {}
        self.seen_f = False
        self.inited = False
        self.noinit = False
        self.shutdown_requested = False
        self.ports = {"gdb": "3333", "telnet": "4444", "tcl": "6666"}
        self.bindto = "127.0.0.1"
        self.hooks: dict[str, str] = {}
        self.idcode = os.environ.get("STUB_OPENOCD_IDCODE", "0x6ba00477").lower()
        self.sel = selectors.DefaultSelector()
        self.adapter_sock: socket.socket | None = None
        self.gdb_listener: socket.socket | None = None

    # -- command line -----------------------------------------------------------------

    def run(self) -> int:
        _log({"argv": self.argv, "pid": os.getpid()})
        _say("Open On-Chip Debugger 0.12.0 (stub_openocd)")
        items: list[tuple[str, str]] = []
        it = iter(self.argv)
        for arg in it:
            if arg in ("-s", "--search"):
                self.search.append(Path(next(it)))
            elif arg in ("-f", "--file"):
                items.append(("f", next(it)))
            elif arg in ("-c", "--command"):
                items.append(("c", next(it)))
            elif arg in ("-d", "-l", "--log_output"):
                if arg != "-d":
                    next(it)
            else:
                _say(f"Error: stub_openocd: unexpected argument {arg!r}")
                return 1
        for kind, value in items:
            rc = self._file(value) if kind == "f" else self._script(value)
            if rc is not None:
                return rc
            if self.shutdown_requested:
                _say("shutdown command invoked")
                return 0
        return self._serve()

    def _file(self, name: str) -> int | None:
        self.seen_f = True
        path = Path(name)
        found = path if path.is_absolute() and path.is_file() else next(
            (d / name for d in self.search if (d / name).is_file()), None)
        if found is None:
            _say(f"Error: Can't find {name}")
            return 1
        return None

    def _script(self, script: str) -> int | None:
        for cmd in _split_commands(script):
            rc = self._command(cmd)
            if rc is not None:
                return rc
        return None

    def _command(self, cmd: str) -> int | None:
        words = cmd.split()
        head = words[0]
        if head == "set" and len(words) >= 3:
            if words[1] in PROBE_VARS and self.seen_f:
                _say(f"Error: stub_openocd: ORDER: '{cmd}' comes after a -f; the target "
                     "half has already read it, so the real OpenOCD would silently ignore it")
                return 2
            self.vars[words[1]] = " ".join(words[2:])
            return None
        if head in PORT_CMDS:
            if self.inited:
                _say(f"Error: The '{head}' command must be used before 'init'.")
                return 1
            if head == "bindto":
                self.bindto = words[1]
            else:
                self.ports[head.split("_")[0]] = words[1]
            return None
        if head == "noinit":
            self.noinit = True
            return None
        if head == "init":
            return self._init(servers=False)
        if head == "scan_chain":
            _say(self._scan_chain())
            return None
        if head == "shutdown":
            self.shutdown_requested = True
            return None
        if len(words) >= 5 and words[1:4] == ["configure", "-event", "gdb-attach"]:
            self.hooks[head] = " ".join(words[4:]).strip("{} ")
            return None
        return None                                 # anything else: accepted silently

    # -- init: the adapter and the chain ------------------------------------------------------

    def _init(self, *, servers: bool) -> int | None:
        if self.inited:
            return None
        self.inited = True
        delay = float(os.environ.get("STUB_OPENOCD_INIT_DELAY", "0") or 0)
        if delay:
            time.sleep(delay)
        if os.environ.get("STUB_OPENOCD_NO_ADAPTER") != "1":
            rc = self._adapter()
            if rc is not None:
                return rc
        if self.idcode in ("none", "0x00000000"):
            _say("Error: JTAG scan chain interrogation failed: all zeroes")
            _say("Error: Check JTAG interface, timings, target power, etc.")
            return 1
        _say(f"Info : JTAG tap: nanosoc.cpu tap/device found: {self.idcode} "
             "(mfg: 0x23b (ARM Ltd), part: 0xba00, ver: 0x6)")
        return self._bind_gdb()

    def _adapter(self) -> int | None:
        host = self.vars.get("RBB_HOST", "192.168.10.101")
        port = int(self.vars.get("RBB_PORT", "6921"))
        _say("Info : Initializing remote_bitbang driver")
        _say(f"Info : Connecting to {host}:{port}")
        if host not in LOOPBACK:
            _say(f"Error: stub_openocd: refusing to dial {host}:{port}; tests stay on 127.0.0.1")
            return 3
        try:
            sock = socket.create_connection((host, port), timeout=2.0)
        except OSError as exc:
            _say(f"Error: Error on socket 'Failed to connect': errno=={exc.errno}, "
                 f"message: {exc.strerror}.")
            return 1
        sock.settimeout(0.15)          # the board's refusal is an immediate close
        try:
            data = sock.recv(1)
            closed = data == b""
        except TimeoutError:
            closed = False
        except OSError:
            closed = True
        if closed:
            _say("Error: Error on socket 'remote_bitbang_fill_buf': errno==104, "
                 "message: Connection reset by peer.")
            sock.close()
            return 1
        _say("Info : remote_bitbang driver initialized")
        self.adapter_sock = sock
        return None

    def _scan_chain(self) -> str:
        return ("   TapName             Enabled  IdCode     Expected   IrLen IrCap IrMask\n"
                "-- ------------------- -------- ---------- ---------- ----- ----- ------\n"
                f" 0 nanosoc.cpu            Y     {self.idcode} 0x6ba00477     4 0x01  0x03")

    # -- servers --------------------------------------------------------------------------------

    def _listen(self, svc: str) -> socket.socket | None | int:
        port = self.ports[svc]
        if port == "disabled":
            _say(f"Info : {svc} port disabled")
            return None
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.bindto, int(port)))
        except OSError:
            _say(f"Error: couldn't bind {svc} to socket on port {port}: Address already in use")
            s.close()
            return 1
        s.listen(4)
        s.setblocking(False)
        self.sel.register(s, selectors.EVENT_READ, ("listen", svc))
        _say(f"Info : Listening on port {port} for {svc} connections")
        return s

    def _bind_gdb(self) -> int | None:
        _say(f"Info : starting gdb server for nanosoc.cpu0 on {self.ports['gdb']}")
        res = self._listen("gdb")
        if isinstance(res, socket.socket):
            self.gdb_listener = res
        # A taken gdb port is logged and OpenOCD KEEPS RUNNING (measured); so does the stub.
        return None

    def _serve(self) -> int:
        signal.signal(signal.SIGTERM, lambda *_: self._quit())
        for svc in ("tcl", "telnet"):                  # server_init, before init
            res = self._listen(svc)
            if res == 1:
                return 1
        if not self.noinit:
            rc = self._init(servers=True)
            if rc is not None:
                return rc
        while True:
            for key, _ in self.sel.select(timeout=0.5):
                kind, info = key.data
                if kind == "listen":
                    self._accept(key.fileobj, info)
                else:
                    rc = self._client(key.fileobj, info)
                    if rc is not None:
                        return rc

    def _accept(self, listener: socket.socket, svc: str) -> None:
        conn, _ = listener.accept()
        conn.setblocking(True)
        if svc == "gdb":
            target = "nanosoc.cpu0"
            _log({"event": "gdb-attach", "target": target, "hook": self.hooks.get(target, "")})
            conn.close()
            return
        if svc == "telnet":
            conn.sendall(b"Open On-Chip Debugger\r\n> ")
        self.sel.register(conn, selectors.EVENT_READ, ("client", [svc, bytearray()]))

    def _client(self, conn: socket.socket, info: list) -> int | None:
        svc, buf = info
        try:
            data = conn.recv(4096)
        except OSError:
            data = b""
        if not data:
            self.sel.unregister(conn)
            conn.close()
            return None
        if svc != "tcl":
            return None
        buf += data
        while b"\x1a" in buf:
            raw, _, rest = bytes(buf).partition(b"\x1a")
            buf[:] = rest
            cmd = raw.decode(errors="replace").strip()
            if cmd == "shutdown":
                conn.sendall(b"shutdown command invoked\x1a")
                self._quit()
            conn.sendall(self._rpc(cmd).encode() + b"\x1a")
        return None

    def _rpc(self, cmd: str) -> str:
        words = cmd.split()
        if cmd == "version":
            return "Open On-Chip Debugger 0.12.0 (stub_openocd)"
        if cmd == "scan_chain":
            return self._scan_chain()
        if len(words) == 4 and words[1:] == ["cget", "-event", "gdb-attach"]:
            return self.hooks.get(words[0], "")
        return f'invalid command name "{words[0] if words else ""}"'

    def _quit(self) -> None:
        _say("shutdown command invoked")
        if self.adapter_sock is not None:
            self.adapter_sock.close()
        sys.exit(0)


if __name__ == "__main__":
    sys.exit(Stub(sys.argv[1:]).run())
