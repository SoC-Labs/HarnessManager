"""A fake UDP ``identify`` responder (Linux harness plan §10, agreed 2026-09-23).

Models the parts of the wire the pack relies on:

- one datagram in, ONE reply out, to the SENDER's addr:port;
- a malformed request (bad JSON, wrong op/v, nonce not 8-32 hex) is SILENT,
  never an error reply (HARNESSD_CONTRACT §9.2);
- replies are rate-limited (~10/s, token bucket);
- the reply is built at request time by ``reply_fn(nonce)``; ``None`` means
  silent (a hung harnessd answers identify no more than it answers 6900).

``canonical_reply(**overrides)`` is the agreed reply with every key, for tests
that need a board without a whole FakeShell behind it.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

_HEX = frozenset("0123456789abcdefABCDEF")

ReplyFn = Callable[[str], "dict[str, Any] | None"]


def valid_request(obj: Any) -> bool:
    return (isinstance(obj, dict) and obj.get("op") == "identify" and obj.get("v") == 1
            and isinstance(obj.get("nonce"), str) and 8 <= len(obj["nonce"]) <= 32
            and set(obj["nonce"]) <= _HEX)


def canonical_reply(nonce: str = "", **overrides: Any) -> dict[str, Any]:
    """The agreed run-mode reply (plan §10) with every key; ``None`` values are dropped."""
    reply: dict[str, Any] = {
        "ok": True, "op": "identify", "v": 1, "nonce": nonce, "board": "mps3",
        "mac": "02004d505300", "ip": "127.0.0.1", "dhcp": False,
        "shell_id": "0x3f1a560f", "rm_id": "0x00000000", "harness": "1.0.0", "proto": "0.11",
        "impl": "linux", "unit": None, "up_ms": 1234, "os_up_ms": 60000, "mode": "run",
        "ssh": {"claimed": False, "host_key_sha256": "SHA256:fake"},
        "ports": {"ctrl": 6900, "push": 6910, "tftp": 69, "jtag": 6921, "xvc": 2542,
                  "uart0": 6930, "uart1": 6931, "swo": 6932},
    }
    reply.update(overrides)
    return {k: v for k, v in reply.items() if v is not None}


class FakeIdentifyResponder:
    """``with FakeIdentifyResponder(reply_fn) as r:``; ``r.port`` is the bound port."""

    def __init__(self, reply_fn: ReplyFn, host: str = "127.0.0.1", port: int = 0,
                 rate_per_s: float = 10.0, requests: list | None = None) -> None:
        self.reply_fn = reply_fn
        self.host = host
        self.port = port
        self.rate_per_s = rate_per_s
        #: every datagram seen: (sender, parsed request or None, replied?). Pass a list
        #: to share it across restarts (the owning fake keeps one log).
        self.requests: list[tuple[tuple[str, int], dict | None, bool]] = (
            requests if requests is not None else [])
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> FakeIdentifyResponder:
        if self._sock is not None:
            return self
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.settimeout(0.05)
        self.port = sock.getsockname()[1]
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> FakeIdentifyResponder:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def replies_sent(self) -> int:
        return sum(1 for _, _, replied in self.requests if replied)

    def _serve(self) -> None:
        tokens, last = self.rate_per_s, time.monotonic()
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                data, addr = sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                obj = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                obj = None
            req = obj if valid_request(obj) else None
            now = time.monotonic()
            tokens = min(self.rate_per_s, tokens + (now - last) * self.rate_per_s)
            last = now
            replied = False
            if req is not None and tokens >= 1.0:
                reply = self.reply_fn(req["nonce"])
                if reply is not None:
                    tokens -= 1.0
                    payload = json.dumps(reply, separators=(",", ":")).encode("ascii")
                    try:
                        sock.sendto(payload[:1200], addr)
                        replied = True
                    except OSError:
                        pass
            self.requests.append((addr, req, replied))
