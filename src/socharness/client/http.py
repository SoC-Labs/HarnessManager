"""A small synchronous client for the socharnessd API. Stdlib ``http.client`` only.

Every call carries the bearer token. A success returns the JSON object; a
failure raises the ``HarnessError`` its error envelope describes (see
``codec.error_from_json``), so callers handle daemon errors exactly as they
handle engine errors. No proxy is ever used: the daemon is on this machine.
"""

from __future__ import annotations

import http.client
import json
from typing import Any
from urllib.parse import quote, urlsplit

from socharness.cli.output import jsonable
from socharness.core.errors import HarnessError, UnreachableError

from .codec import error_from_json

API = "/api/v1"
DEFAULT_TIMEOUT_S = 300.0


def q(part: str) -> str:
    """One URL path segment (a board id may hold '@', ':' and '/')."""
    return quote(part, safe="")


class Http:
    def __init__(self, base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "http" or not parts.hostname or not parts.port:
            raise ValueError(f"not a daemon URL: {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.host = parts.hostname
        self.port = parts.port
        self.token = token
        self.timeout = timeout

    def ws_url(self, path: str, **query: str) -> str:
        from urllib.parse import urlencode

        host = f"[{self.host}]" if ":" in self.host else self.host
        qs = urlencode({"token": self.token, **query})
        return f"ws://{host}:{self.port}{API}{path}?{qs}"

    def request(self, method: str, path: str, body: Any = None, *,
                timeout: float | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(jsonable(body)).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(self.host, self.port,
                                          timeout=timeout or self.timeout)
        try:
            conn.request(method, f"{API}{path}", body=data, headers=headers)
            resp = conn.getresponse()
            status, raw = resp.status, resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise UnreachableError(
                f"socharnessd at {self.base_url} did not answer ({exc})",
                hint="`socharness daemon status` checks it; SOCHARNESS_NO_DAEMON=1 bypasses it") \
                from exc
        finally:
            conn.close()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            raise HarnessError(f"socharnessd sent a reply that is not JSON (HTTP {status})") \
                from None
        if not isinstance(payload, dict):
            raise HarnessError(f"socharnessd sent a reply that is not an object (HTTP {status})")
        if 200 <= status < 300 and payload.get("ok", True):
            return payload
        err = payload.get("error")
        if isinstance(err, dict):
            raise error_from_json(err)
        raise HarnessError(f"socharnessd answered HTTP {status} with no error object")

    def get(self, path: str, **kw: Any) -> dict[str, Any]:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> dict[str, Any]:
        return self.request("POST", path, {} if body is None else body, **kw)

    def delete(self, path: str, **kw: Any) -> dict[str, Any]:
        return self.request("DELETE", path, **kw)
