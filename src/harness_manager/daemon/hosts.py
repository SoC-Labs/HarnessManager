"""The service's Host allow-list: defence in depth against DNS rebinding (lane SET-API).

``docs/design/SETTINGS.md`` §12.8. The Bearer token already stops a hostile web page: it
travels in a header, never a cookie, so a page that rebinds its own name to 127.0.0.1 can
neither forge a request nor read a reply. But ``PUT /settings/secrets`` makes the token worth
more, so the service also refuses any request, HTTP or WebSocket, whose ``Host`` is not a
name it answers to:

- the loopback names: ``localhost``, ``127.0.0.1`` (any ``127.x.y.z``) and ``::1``;
- the address it listens on (``--listen``), unless that is a wildcard (``0.0.0.0``, ``::``);
- off loopback, this machine's own names (``socket.gethostname()`` and its FQDN), and any
  IP address: a rebinding page's ``Host`` is always ITS name, never an address, so an address
  lets nothing in that the token does not already stop (a browser on another machine that
  opens the service by its IP keeps working);
- the names in ``advanced.allowed_hosts`` (settings; the admin policy may set or lock it).

A refused request gets **403** with the error envelope (REFUSED, 15) and no route runs, so
nothing is read or changed; a WebSocket is refused before it is accepted. The port in the
header is not checked (an ``ssh -L`` forward may use another one).

``create_app(..., allowed_hosts=...)`` turns it on. Every service ``harness-manager daemon``
starts has it (``server.run_daemon``); an app a test builds has it only when asked.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from collections.abc import Iterable
from typing import Any

from harness_manager.cli.output import error_json
from harness_manager.core.errors import RefusedError

log = logging.getLogger(__name__)

LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
#: In an allow-list: any IP address passes (a service listening off loopback).
ANY_IP = "<any-ip>"
WILDCARDS = frozenset({"", "0.0.0.0", "::"})
#: The HTTP status of a refused Host (as 401 is fixed for a missing token).
FORBIDDEN = 403
_LOGGED_MAX = 64


def normalise(name: str) -> str:
    return str(name).strip().lower().rstrip(".").strip("[]")


def host_of(header: str) -> str:
    """``"127.0.0.1:8080"`` -> ``"127.0.0.1"``; ``"[::1]:8080"`` -> ``"::1"``; lower case."""
    h = str(header or "").strip()
    if h.startswith("["):
        return normalise(h[1:h.find("]")] if "]" in h else h[1:])
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return normalise(h)


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _loopback(name: str) -> bool:
    if name in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def allowed_hosts(listen: str = "127.0.0.1", extra: Iterable[str] = ()) -> frozenset[str]:
    """The names a service listening on ``listen`` answers to (the loopback names always)."""
    names = set(LOOPBACK_NAMES)
    bind = normalise(listen)
    if bind not in WILDCARDS:
        names.add(bind)
    if not _loopback(bind) or bind in WILDCARDS:
        names.add(ANY_IP)
        for fn in (socket.gethostname, socket.getfqdn):
            try:
                name = normalise(fn())
            except OSError:
                continue
            if name:
                names.add(name)
    names |= {normalise(h) for h in extra if normalise(h)}
    return frozenset(names)


def host_allowed(header: str, allowed: Iterable[str]) -> bool:
    name = host_of(header)
    if not name or name == ANY_IP:
        return False
    return name in allowed or _loopback(name) or (ANY_IP in allowed and _is_ip(name))


def refusal(header: str) -> RefusedError:
    shown = host_of(header)[:100] or "(none)"
    return RefusedError(f"this Harness Manager service does not answer to the host name "
                        f"{shown!r}",
                        hint="open it at the address `harness-manager ui` prints "
                             "(http://127.0.0.1:PORT/); to use another name, add it with "
                             "`harness-manager config set advanced.allowed_hosts NAME` and "
                             "restart the service")


class HostGuard:
    """ASGI middleware: HTTP and WebSocket requests with a foreign ``Host`` are refused."""

    def __init__(self, app: Any, allowed: Iterable[str]) -> None:
        self.app = app
        self.allowed = frozenset(normalise(h) for h in allowed)
        self._logged: set[str] = set()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        header = ""
        for name, value in scope.get("headers") or ():
            if name == b"host":
                header = value.decode("latin-1")
                break
        if host_allowed(header, self.allowed):
            await self.app(scope, receive, send)
            return
        shown = host_of(header)[:100]
        if shown not in self._logged and len(self._logged) < _LOGGED_MAX:
            self._logged.add(shown)
            log.warning("refused a request for the host name %r (not one this service answers "
                        "to: advanced.allowed_hosts)", shown or "(none)")
        body = json.dumps(error_json(refusal(header))).encode("utf-8")
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(body)).encode("ascii")),
                   (b"cache-control", b"no-store")]
        if kind == "http":
            await send({"type": "http.response.start", "status": FORBIDDEN, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        if "websocket.http.response" in (scope.get("extensions") or {}):
            await send({"type": "websocket.http.response.start", "status": FORBIDDEN,
                        "headers": headers})
            await send({"type": "websocket.http.response.body", "body": body})
        else:                                   # a server without the denial extension
            await send({"type": "websocket.close", "code": 4000 + int(RefusedError.code)})
