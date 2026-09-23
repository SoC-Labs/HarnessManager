"""Tiny TCP "shells" with one wire behaviour each, for the 6900 failure mapping.

Each models one thing a real harness does on the control port (plan §10; the
fielded shell's coordinator_net.c; harness handover A3):

- ``"eof"``: accept, then close with no reply (the single-client refusal, FIN);
- ``"rst"``: accept, then RESET (a POSIX close with unread request bytes);
- ``"silent"``: accept and never reply (a hung harnessd: the kernel's backlog);
- ``"ebusy"``: answer every request ``{"ok":false,"err":"EBUSY",...}`` (A3);
- ``"script"``: answer each ``op`` from ``replies`` (a dict op -> reply, or a
  callable(request) -> reply); an op not in it gets "unknown op".
"""

from __future__ import annotations

import json
import socket
import struct
import threading
from collections.abc import Callable
from typing import Any


class RawShell:
    def __init__(self, behaviour: str = "script",
                 replies: dict[str, Any] | Callable[[dict], dict] | None = None) -> None:
        self.behaviour = behaviour
        self.replies = replies or {}
        self.requests: list[dict] = []
        self.accepted = 0
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"127.0.0.1:{self.port}"

    def __enter__(self) -> RawShell:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
        except OSError:
            pass
        self._srv.close()
        self._thread.join(timeout=2.0)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            if self._stop.is_set():
                conn.close()
                return
            self.accepted += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _reply_for(self, request: dict) -> dict:
        if self.behaviour == "ebusy":
            return {"ok": False, "err": "EBUSY", "since_ms": 900, "holder": "10.1.2.3:40000"}
        if callable(self.replies):
            return self.replies(request)
        reply = self.replies.get(request.get("op"))
        return reply if reply is not None else {"ok": False, "err": "unknown op"}

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            if self.behaviour == "eof":
                return
            if self.behaviour == "rst":
                try:
                    conn.recv(4096)          # let the request arrive, then abort
                except OSError:
                    pass
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return
            if self.behaviour == "silent":
                conn.settimeout(0.05)
                while not self._stop.is_set():
                    try:
                        if conn.recv(4096) == b"":
                            return
                    except TimeoutError:
                        continue
                    except OSError:
                        return
                return
            buf = b""
            conn.settimeout(5.0)
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    request = json.loads(line)
                    self.requests.append(request)
                    reply = self._reply_for(request)
                    try:
                        conn.sendall(json.dumps(reply).encode() + b"\n")
                    except OSError:
                        return


#: A v0.11 bare-metal shell's ping/version/diag, for scripted servers.
PING = {"ok": True, "shell_id": "0x1a102610", "rm_id": "0x00000000"}
VERSION_BARE = {"ok": True, "harness": "1.0.0", "ver32": "0x01000000", "sha": "d68dd0ed",
                "dirty": 0, "lmb_kb": 1024,
                "features": ["clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed", "dut_egress",
                             "jtag_server", "xvc_dbgbr", "stats", "log", "reboot", "touch_cal"],
                "usr_access": "0x01000000", "skew": False}
VERSION_LINUX = {**VERSION_BARE, "lmb_kb": 128,
                 "features": [f for f in VERSION_BARE["features"] if f != "windowed"],
                 "impl": "linux"}
