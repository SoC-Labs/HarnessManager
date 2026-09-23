"""A small JSON-over-HTTP client for plugs and PDUs (standard library only).

Why not ``urllib``'s auth handlers: Shelly Gen2 uses HTTP Digest with
SHA-256, which ``urllib.request`` does not implement (it knows MD5 and SHA-1).

Failures become ``HttpFailure`` with a ``reason`` that is safe to show: it
never contains a password, and never a URL with a query string (Tasmota puts
its password in the query). Callers turn it into ``Reading.unavailable`` or an
exit-coded ``HarnessError``.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit

from socharness.core.errors import ActionFailedError, HarnessError, UnreachableError

from .config import Auth, safe_url

log = logging.getLogger(__name__)

AUTH_HINT = "check power.auth in boards.toml"


class HttpFailure(Exception):
    """A request that did not produce a usable reply. ``reason`` is safe to show."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind          # "auth" | "timeout" | "unreachable" | "http" | "reply"
        self.reason = reason

    def as_error(self) -> HarnessError:
        if self.kind in ("timeout", "unreachable"):
            return UnreachableError(self.reason, hint="check the plug's address and that it is powered")
        if self.kind == "auth":
            return ActionFailedError(self.reason, hint=AUTH_HINT)
        return ActionFailedError(self.reason)


def encode_query(params: Mapping[str, str]) -> str:
    """``a=1&b=x%20y``: spaces as ``%20`` (Tasmota does not decode ``+``)."""
    return "&".join(f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in params.items())


class JsonHttp:
    """GET/POST JSON against one device. ``scheme`` is "basic", "digest" or "none"."""

    def __init__(self, base_url: str, *, timeout_s: float, auth: Auth | None = None,
                 scheme: str = "basic", label: str = "") -> None:
        parts = urlsplit(base_url)
        self._https = parts.scheme == "https"
        self._host = parts.hostname or ""
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        self.timeout_s = timeout_s
        self._auth = auth
        self._scheme = scheme
        self.label = label or safe_url(base_url)
        self._nc = 0

    # -- public ------------------------------------------------------------------------

    def get(self, path: str, query: Mapping[str, str] | None = None, *,
            secret_query: bool = False) -> Any:
        """GET ``path?query``. ``secret_query``: the query holds a secret; never log it."""
        target = self._prefix + path + (("?" + encode_query(query)) if query else "")
        shown = self._prefix + path + (" (query hidden)" if secret_query and query else
                                       ("?" + encode_query(query) if query else ""))
        return self._request("GET", target, None, shown)

    def post(self, path: str, body: Any) -> Any:
        target = self._prefix + path
        return self._request("POST", target, json.dumps(body).encode(), target)

    # -- internals -----------------------------------------------------------------------

    def _secrets(self) -> list[str]:
        return [self._auth.password.reveal()] if self._auth and self._auth.password else []

    def _scrub(self, text: str) -> str:
        for s in self._secrets():
            text = text.replace(s, "***")
        return text

    def _request(self, method: str, target: str, body: bytes | None, shown: str) -> Any:
        log.debug("%s %s %s", method, self.label, shown)
        headers = {"Accept": "application/json", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self._auth is not None and self._scheme == "basic":
            token = f"{self._auth.user}:{self._auth.password.reveal()}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(token).decode()
        status, reason, rheaders, data = self._send(method, target, body, headers)
        if status == 401 and self._auth is not None and self._scheme == "digest":
            challenge = rheaders.get("www-authenticate", "")
            if challenge.lower().startswith("digest"):
                headers["Authorization"] = self._digest(method, target, challenge)
                status, reason, rheaders, data = self._send(method, target, body, headers)
        if status == 401:
            if self._auth is None:
                raise HttpFailure("auth", f"{self.label} needs a password (HTTP 401): "
                                          "add power.auth in boards.toml")
            raise HttpFailure("auth", f"{self.label} rejected the credentials (HTTP 401)")
        if not 200 <= status < 300:
            raise HttpFailure("http", f"{self.label} answered HTTP {status} {reason}".rstrip())
        try:
            return json.loads(data.decode("utf-8")) if data.strip() else None
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpFailure("reply", f"{self.label} replied with something that is not JSON") from exc

    def _send(self, method: str, target: str, body: bytes | None,
              headers: dict[str, str]) -> tuple[int, str, dict[str, str], bytes]:
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=self.timeout_s)
        try:
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            rheaders = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp.reason or "", rheaders, data
        except TimeoutError as exc:
            raise HttpFailure("timeout", f"{self.label} did not answer within {self.timeout_s:g} s") from exc
        except ConnectionRefusedError as exc:
            raise HttpFailure("unreachable", f"{self.label} refused the connection") from exc
        except (OSError, http.client.HTTPException) as exc:
            text = self._scrub(getattr(exc, "strerror", None) or str(exc) or type(exc).__name__)
            raise HttpFailure("unreachable", f"cannot reach {self.label}: {text}") from exc
        finally:
            conn.close()

    def _digest(self, method: str, uri: str, challenge: str) -> str:
        assert self._auth is not None
        params = parse_challenge(challenge)
        self._nc += 1
        try:
            return digest_authorization(
                params, method=method, uri=uri, user=self._auth.user,
                password=self._auth.password.reveal(), nc=self._nc,
                cnonce=os.urandom(12).hex())
        except ValueError as exc:
            raise HttpFailure("auth", f"{self.label}: {exc}") from exc


# --- HTTP Digest (RFC 7616) ------------------------------------------------------------

_PARAM_RE = re.compile(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')
_ALGORITHMS = {"MD5": "md5", "SHA-256": "sha256"}


def parse_challenge(header: str) -> dict[str, str]:
    """``Digest realm="x", nonce="y", qop="auth", algorithm=SHA-256`` -> a dict."""
    body = header.split(None, 1)[1] if " " in header else ""
    return {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
            for m in _PARAM_RE.finditer(body)}


def digest_authorization(challenge: Mapping[str, str], *, method: str, uri: str, user: str,
                         password: str, nc: int, cnonce: str) -> str:
    """The ``Authorization`` header value for a Digest challenge (qop=auth or none)."""
    algorithm = challenge.get("algorithm", "MD5").upper()
    if algorithm not in _ALGORITHMS:
        raise ValueError(f"unsupported digest algorithm {algorithm}")
    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")

    def h(text: str) -> str:
        return hashlib.new(_ALGORITHMS[algorithm], text.encode("utf-8")).hexdigest()

    ha1 = h(f"{user}:{realm}:{password}")
    ha2 = h(f"{method}:{uri}")
    qops = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
    parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"',
             f"algorithm={algorithm}"]
    if "auth" in qops:
        response = h(f"{ha1}:{nonce}:{nc:08x}:{cnonce}:auth:{ha2}")
        parts += [f'response="{response}"', "qop=auth", f"nc={nc:08x}", f'cnonce="{cnonce}"']
    elif not qops:
        parts.append(f'response="{h(f"{ha1}:{nonce}:{ha2}")}"')
    else:
        raise ValueError(f"unsupported digest qop {challenge.get('qop')!r}")
    if "opaque" in challenge:
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)
