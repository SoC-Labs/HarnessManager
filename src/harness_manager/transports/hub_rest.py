"""Hub mode: fpgahub's own REST API as a drop-in for the SSH/CLI hub client (team T8).

Today ``harness_manager_mps3.hub.HubClient`` reaches the lab hub (fpgahub on
mapstone-dev) with ``ssh HUB 'sg fpga -c "fpgahub …"'``. External users have no
SSH account on the hub; they have an fpgahub **Bearer token**. ``RestHubClient``
speaks fpgahub 0.3.0's ``/api/v1`` directly and offers every verb of the SSH client
plus the frozen ``docs/LEASE_REQUESTS.md`` additions, returning the same dataclasses
and raising the same errors, so ``LeaseService`` and the pack cannot tell them apart.

Configured in boards.toml, beside (or instead of) ``host``::

    hub = { url = "https://mapstone-dev.ecs.soton.ac.uk:7246", target = "mps3_01_pl",
            token_file = "~/.config/harness-manager/hub.token", ca_file = "~/lab-ca.pem" }

``url`` set means REST; ``host`` alone means SSH; both means REST, with ``host``
kept for the SSH tunnel fallback of the data plane (``hub_reach``). The token comes
from ``token_file``, else ``$FPGAHUB_TOKEN``, else the fpgahub CLI's login store
(``fpgahub login --addr … --token …`` writes ``~/.config/fpgahub/config.toml``),
whose credentials are used only for the hub the store names (fpgahub's own F6 rule).
The token is sent only as an ``Authorization`` header: never in a URL, never logged,
never in an error message.

Which listener. fpgahub serves ``/api/v1`` on three listeners: the unix socket
(admin, on the hub only), the mTLS peer port 7245 (a client certificate is required
at the TLS handshake, so a token alone cannot get in), and the web port (7246 on the
lab hub: ``listen_web = "0.0.0.0:7246"``, ``web_tls = true``), which takes a Bearer
token. A token user points ``url`` at the web port; a user with a peer certificate
may point it at 7245 with ``cert_file``/``key_file``.

Differences from the SSH client, all from fpgahub itself:

- **The holder is the token's principal** (``owner@hub-hostname``), whatever holder
  the caller asks for; ``lease_acquire`` returns it in ``Lease.holder``.
- **A queued acquire waits on ``GET /lease/wait``**, the hub's own long poll, in
  short slices (a cancel is seen within ``wait_slice_s``), and re-asserts its queue
  place between slices (re-acquiring as the same principal is idempotent).
- **Force-release needs an admin credential** (``POST /boards/{b}/lease/revoke`` is
  ``require_admin``); over SSH the unix socket is admin by construction. A write
  token gets ``RefusedError`` naming the role (``can_revoke()`` says so up front).
- **There is no file store, so there are no request notes** (degraded mode):
  a request is its queue entry alone. ``put_request`` keeps the note locally,
  ``list_requests`` shows every waiter as a request with an empty message,
  ``put_answer`` raises ``UnavailableError`` (a "keep" answer cannot reach the
  requester) and ``get_answer`` is always None. ``notes_supported`` is False.
  ``docs/HUB_MODE.md`` proposes the fpgahub feature that would lift this.
- **Lease history carries no by/reason for a revoke.** fpgahub's history route
  filters its audit records through ``LeaseEventRecord``, which has no ``by`` or
  ``reason`` field, and the ``lease.admin_revoked`` record has no ``board`` key, so
  neither route returns it (measured on the real v0.3.0 app,
  ``tests/fakes/t8_fpgahub_v030_golden.json``). The SSE stream carries both;
  ``lease_history`` merges what the stream saw (``observe``).

Retries respect the lease semantics: reads, acquire (idempotent per principal),
heartbeat and share start retry a transport failure; release retries and reads a
second "nothing to release" as done; a revoke is retried only when the request
provably never left (connection refused), because a repeated revoke would kick the
waiter it just promoted.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import re
import socket
import ssl
import stat
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from harness_manager.core.errors import (
    AbsentError,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10
    import tomli as tomllib

log = logging.getLogger(__name__)

TRANSPORT_REST = "rest"
TRANSPORT_SSH = "ssh"
API_PREFIX = "/api/v1"
#: fpgahub's default TCP port (the mTLS peer listener) when an address names none.
FPGAHUB_DEFAULT_PORT = 7245
DEFAULT_TARGET = "mps3_01_pl"
DEFAULT_TIMEOUT_S = 30.0
#: One slice of ``GET /lease/wait`` while queued: a cancel is seen within this.
WAIT_SLICE_S = 5.0
#: fpgahub caps a wait at 600 s (``_WAIT_MAX_TIMEOUT_S``).
WAIT_MAX_S = 600.0
RETRY_BACKOFF_S = (0.5, 1.5)
REQUEST_DEADLINE_S = 120.0      # docs/LEASE_REQUESTS.md: a request's timer
DIRECT_MODES = ("auto", "never", "always")

#: The boards.toml ``hub`` keys this module owns (``hub.py`` accepts them, CCR T8-1).
REST_KEYS = frozenset({"url", "token_file", "ca_file", "cert_file", "key_file", "insecure",
                       "events", "direct", "board", "timeout_s"})

#: Every fpgahub route this client calls: name -> (method, OpenAPI path template).
#: ``tests/unit/test_t8_contract.py`` checks each against the v0.3.0 schema.
ROUTES: dict[str, tuple[str, str]] = {
    "health": ("GET", "/api/v1/health"),
    "whoami": ("GET", "/api/v1/whoami"),
    "groups": ("GET", "/api/v1/groups"),
    "target": ("GET", "/api/v1/targets/{name}"),
    "lease_show": ("GET", "/api/v1/targets/{name}/lease"),
    "lease_acquire": ("POST", "/api/v1/targets/{name}/lease"),
    "lease_release": ("DELETE", "/api/v1/targets/{name}/lease"),
    "lease_heartbeat": ("POST", "/api/v1/targets/{name}/lease/heartbeat"),
    "lease_wait": ("GET", "/api/v1/targets/{name}/lease/wait"),
    "lease_history": ("GET", "/api/v1/targets/{name}/lease/history"),
    "queue_cancel": ("DELETE", "/api/v1/targets/{name}/queue"),
    "board_queue_cancel": ("DELETE", "/api/v1/boards/{name}/queue"),
    "lease_revoke": ("POST", "/api/v1/boards/{name}/lease/revoke"),
    "share_list": ("GET", "/api/v1/targets/{name}/shares"),
    "share_start": ("POST", "/api/v1/targets/{name}/shares"),
    "events": ("GET", "/api/v1/events"),
}

_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_NOTE_ID_RE = re.compile(r"[^A-Za-z0-9_.\-]+")


# --- the frozen shapes (docs/LEASE_REQUESTS.md) ------------------------------------------------
#
# ``harness_manager_mps3.hub`` owns them (lane LR-A). A drop-in returns the SAME classes, so
# they are taken from the pack when it defines them; until LR-A lands, these twins (same
# fields, same order, frozen) stand in. Resolved per call, so import order never matters.


@dataclass(frozen=True)
class _QueueEntry:
    position: int
    holder: str
    user: str


@dataclass(frozen=True)
class _LeaseStatus:
    held: bool
    holder: str
    user: str
    expires_at: str
    queue: tuple = ()


@dataclass(frozen=True)
class _RequestNote:
    id: str
    by: str
    user: str
    host: str
    message: str
    created_at: str
    deadline_at: str


@dataclass(frozen=True)
class _AnswerNote:
    id: str
    answer: str
    minutes: int
    message: str
    at: str


@dataclass(frozen=True)
class _LeaseView:
    target: str
    held: bool
    holder: str = ""
    user: str = ""
    expires_at: str = ""
    raw: str = ""


@dataclass(frozen=True)
class _ShareInfo:
    tty: str
    host: str
    port: int
    writer: str = ""
    readers: int = 0
    running: bool = True


class _LeaseLostError(HeldError):
    def __init__(self, message: str, *, state: str, hint: str = "") -> None:
        super().__init__(message, hint=hint)
        self.state = state


_FALLBACKS: dict[str, Any] = {
    "QueueEntry": _QueueEntry, "LeaseStatus": _LeaseStatus, "RequestNote": _RequestNote,
    "AnswerNote": _AnswerNote, "LeaseView": _LeaseView, "ShareInfo": _ShareInfo,
    "LeaseLostError": _LeaseLostError,
}


def shape(name: str) -> Any:
    """The pack's class ``name`` (``harness_manager_mps3.hub``), else this module's twin."""
    mod = sys.modules.get("harness_manager_mps3.hub")
    if mod is None:
        with contextlib.suppress(ImportError):
            import harness_manager_mps3.hub as mod  # noqa: PLC0415 - optional, lazy
    found = getattr(mod, name, None) if mod is not None else None
    return found if found is not None else _FALLBACKS[name]


def _lease_cls() -> Any:
    from pyverify.lease import Lease

    return Lease


# --- configuration -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RestHubConfig:
    """The REST half of a boards.toml ``hub`` table."""

    url: str                       # scheme://host[:port], no path
    target: str = DEFAULT_TARGET
    token_file: str = ""
    ca_file: str = ""
    cert_file: str = ""
    key_file: str = ""
    insecure: bool = False
    events: bool = True
    direct: str = "auto"
    board: str = ""                # the physical board (lease unit); "" = ask the hub
    timeout_s: float = DEFAULT_TIMEOUT_S
    ssh_host: str = ""             # hub.host, when both are set: the data plane's fallback
    #: CCR SET-HUB-2: a named hub (``[hubs.<name>]``, boards.toml ``hub.use``) takes its
    #: token from the settings (``settings.hubs.hub_credential``), never from ``token_file``;
    #: ``settings_root`` is the config dir its secret store is in ("" = the default one).
    hub_name: str = ""
    settings_root: str = ""

    @property
    def scheme(self) -> str:
        return urlsplit(self.url).scheme

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def port(self) -> int:
        parts = urlsplit(self.url)
        return parts.port or (443 if parts.scheme == "https" else 80)

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"


def _loopback(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def _normalise_url(url: Any, where: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise UsageError(f"{where}.url must be the hub's API address, e.g. "
                         "\"https://mapstone-dev.ecs.soton.ac.uk:7246\"")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise UsageError(f"{where}.url {url!r} is not an http(s) URL",
                         hint="e.g. https://mapstone-dev.ecs.soton.ac.uk:7246 (the hub's web port)")
    if parts.username or parts.password:
        raise UsageError(f"{where}.url must not carry credentials; put the token in token_file",
                         hint="a URL ends up in logs; a token file does not")
    if parts.query or parts.fragment:
        raise UsageError(f"{where}.url must not carry a query or fragment")
    if parts.scheme == "http" and not _loopback(parts.hostname):
        raise UsageError(f"{where}.url is plain http to {parts.hostname}: the Bearer token "
                         "would cross the network readable",
                         hint="use https (the lab hub's web port 7246 is TLS)")
    path = parts.path.rstrip("/")
    if path not in ("", API_PREFIX):
        raise UsageError(f"{where}.url path must be empty or {API_PREFIX}, not {parts.path!r}")
    try:
        port = parts.port
    except ValueError as exc:
        raise UsageError(f"{where}.url has a bad port: {exc}") from exc
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def _path_value(table: Mapping[str, Any], key: str, where: str) -> str:
    value = table.get(key, "")
    if not isinstance(value, str):
        raise UsageError(f"{where}.{key} must be a file path")
    return value


def parse_rest_table(table: Mapping[str, Any], *, where: str = "hub",
                     target: str | None = None) -> RestHubConfig | None:
    """The REST keys of one boards.toml ``hub`` table; None when it has no ``url``.

    Validates only ``REST_KEYS`` (``hub.py`` validates the rest). ``UsageError``
    names the bad key.
    """
    if not isinstance(table, Mapping) or "url" not in table:
        return None
    url = _normalise_url(table.get("url"), where)
    insecure = table.get("insecure", False)
    if not isinstance(insecure, bool):
        raise UsageError(f"{where}.insecure must be true or false")
    events = table.get("events", True)
    if not isinstance(events, bool):
        raise UsageError(f"{where}.events must be true or false")
    direct = table.get("direct", "auto")
    if direct not in DIRECT_MODES:
        raise UsageError(f"{where}.direct must be one of {', '.join(DIRECT_MODES)}")
    board = table.get("board", "")
    if not isinstance(board, str) or (board and not _NAME_RE.fullmatch(board)):
        raise UsageError(f"{where}.board must be an fpgahub board name, e.g. mps3_01")
    timeout = table.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise UsageError(f"{where}.timeout_s must be a positive number of seconds")
    tgt = target if target is not None else table.get("target", DEFAULT_TARGET)
    if not isinstance(tgt, str) or not _NAME_RE.fullmatch(tgt):
        raise UsageError(f"{where}.target must be an fpgahub target name, e.g. mps3_01_pl")
    cert, key = _path_value(table, "cert_file", where), _path_value(table, "key_file", where)
    if bool(cert) != bool(key):
        raise UsageError(f"{where}: cert_file and key_file go together (an mTLS client pair)")
    host = table.get("host", "")
    return RestHubConfig(url=url, target=tgt, token_file=_path_value(table, "token_file", where),
                         ca_file=_path_value(table, "ca_file", where), cert_file=cert,
                         key_file=key, insecure=insecure, events=events, direct=direct,
                         board=board, timeout_s=float(timeout),
                         ssh_host=host if isinstance(host, str) else "")


def transport_for(table: Mapping[str, Any]) -> str:
    """``"rest"`` when the table has ``url`` (even beside ``host``), ``"ssh"`` for ``host``."""
    if isinstance(table, Mapping) and table.get("url"):
        return TRANSPORT_REST
    if isinstance(table, Mapping) and table.get("host"):
        return TRANSPORT_SSH
    return ""


# --- the credential ------------------------------------------------------------------------------


class Credential:
    """A Bearer token and where it came from. ``repr``/``str`` never show the token."""

    __slots__ = ("_token", "source", "tls_dir", "insecure")

    def __init__(self, token: str | None, source: str, *, tls_dir: str = "",
                 insecure: bool = False) -> None:
        self._token = token or None
        self.source = source
        self.tls_dir = tls_dir
        self.insecure = insecure

    @property
    def present(self) -> bool:
        return self._token is not None

    def header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def __repr__(self) -> str:
        return f"Credential(source={self.source!r}, token={'<set>' if self._token else None})"

    __str__ = __repr__


def fpgahub_login_store() -> Path:
    """Where ``fpgahub login`` writes (fpgahub ``cli.client_config_path``)."""
    override = os.environ.get("FPGAHUB_CLIENT_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "fpgahub" / "config.toml"


def _addr_key(addr: str) -> tuple[str, int]:
    host, sep, port = addr.strip().rpartition(":")
    if not sep or not port.isdigit() or "]" in port:
        return addr.strip().strip("[]").lower(), FPGAHUB_DEFAULT_PORT
    return host.strip("[]").lower(), int(port)


def _read_login_store(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    section = data.get("client")
    return section if isinstance(section, dict) else {}


def _read_token_file(path_text: str) -> str:
    path = Path(path_text).expanduser()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise UsageError(f"the hub token file {path} cannot be read: {exc.strerror or exc}",
                         hint="write the token there (one line, chmod 600)") from None
    if not text or any(c.isspace() for c in text):
        raise UsageError(f"the hub token file {path} must hold one token on one line")
    if os.name == "posix":
        with contextlib.suppress(OSError):
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                log.warning("the hub token file %s is readable by others; chmod 600 it", path)
    return text


def resolve_credential(cfg: RestHubConfig, *, env: Mapping[str, str] | None = None,
                       store: Path | None = None) -> Credential:
    """An inline hub table: ``token_file``, else ``$FPGAHUB_TOKEN``, else the fpgahub login
    store for THIS hub (T8's order, kept exactly for the tables that exist today).

    A named hub (``cfg.hub_name``, CCR SET-HUB-2) goes by the settings design instead
    (``settings.hubs.hub_credential``): ``$FPGAHUB_TOKEN`` for this hub, then the user's
    ``hubs.<name>.token`` (the secret store, or a ``file:`` read strictly: a file others can
    read is refused), then the fpgahub login store for this hub.
    """
    env = os.environ if env is None else env
    if cfg.hub_name:
        from harness_manager.settings.hubs import hub_credential

        return hub_credential(cfg.hub_name, cfg.host, cfg.port,
                              root=cfg.settings_root or None, env=env, login_store=store)
    if cfg.token_file:
        return Credential(_read_token_file(cfg.token_file), f"token_file {cfg.token_file}")
    token = env.get("FPGAHUB_TOKEN", "")
    env_addr = env.get("FPGAHUB_ADDR", "")
    if token and (not env_addr or _addr_key(env_addr) == (cfg.host.lower(), cfg.port)):
        return Credential(token, "$FPGAHUB_TOKEN")
    path = store or fpgahub_login_store()
    saved = _read_login_store(path)
    addr = saved.get("addr")
    if isinstance(addr, str) and _addr_key(addr) == (cfg.host.lower(), cfg.port):
        tok = saved.get("token") if isinstance(saved.get("token"), str) else None
        tls = saved.get("tls_dir") if isinstance(saved.get("tls_dir"), str) else ""
        return Credential(tok, f"fpgahub login ({path})", tls_dir=tls,
                          insecure=saved.get("insecure") is True)
    return Credential(None, "none")


# --- errors ----------------------------------------------------------------------------------------


def _detail(body: Any) -> Any:
    return body.get("detail", body) if isinstance(body, dict) else body


def _detail_text(detail: Any) -> str:
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("hint") or detail.get("kind") or detail)
    if isinstance(detail, list):
        return "; ".join(f"{'.'.join(str(p) for p in d.get('loc', ()))}: {d.get('msg', '')}"
                         if isinstance(d, dict) else str(d) for d in detail)
    return str(detail)


def hub_error(what: str, status: int, body: Any, cfg: RestHubConfig) -> HarnessError:
    """An fpgahub HTTP error as a ``HarnessError`` with the right exit code and next step."""
    detail = _detail(body)
    text = _detail_text(detail)
    low = text.lower()
    where = cfg.addr
    lost = shape("LeaseLostError")
    if status == 401:
        if cfg.hub_name:                       # SET-HUB-2: a named hub's token is a setting
            return UnreachableError(
                f"{what}: the hub {cfg.hub_name} ({where}) did not accept a credential "
                f"(HTTP 401: {text})",
                hint=f"store the right token: `harness-manager hub token {cfg.hub_name} "
                     f"--stdin` (or `harness-manager config set-secret hubs.{cfg.hub_name}"
                     ".token`); an admin mints tokens (`fpgahub token create`)")
        return UnreachableError(
            f"{what}: the hub {where} did not accept a credential (HTTP 401: {text})",
            hint="set hub.token_file in boards.toml, or `fpgahub login --addr "
                 f"{where} --token …`; an admin mints tokens (`fpgahub token create`); "
                 "or name a hub: `harness-manager hub token NAME --stdin`")
    if status == 403:
        if "no current lease" in low:
            return lost(f"{what}: the lease on {cfg.target} has expired ({text})",
                        state="expired", hint="acquire it again before touching the board")
        if "does not match" in low:
            return lost(f"{what}: the lease on {cfg.target} is no longer ours ({text})",
                        state="lost", hint="see who holds it: harness-manager lease show")
        m = re.search(r"role='?(\w+)'?; (\w+) required", text)
        if m:
            return RefusedError(
                f"{what} needs the {m.group(2)} role on the hub {where}; "
                f"this credential is {m.group(1)}",
                hint="ask the hub admin for a token with that role (or ask the holder)")
        return RefusedError(f"{what}: the hub {where} refused it ({text})")
    if status == 404:
        if "no such board" in low:
            return AbsentError(f"{what}: the hub {where} has no target {cfg.target!r} ({text})",
                               hint="set hub.target in boards.toml (mps3_01_pl on the lab hub)")
        return AbsentError(f"{what}: the hub {where} has no such route (HTTP 404: {text})",
                           hint="the hub must run fpgahub 0.3.0 or later")
    if status == 409:
        holder = detail.get("holder", "") if isinstance(detail, dict) else ""
        return HeldError(f"{what} on {where}: {text}", holder=holder or "")
    if status == 422:
        return UsageError(f"{what}: the hub rejected the request ({text})")
    if status == 408:
        return UnreachableError(f"{what}: the hub {where} timed out ({text})")
    return UnreachableError(f"{what}: the hub {where} failed (HTTP {status}: {text})")


class _Sent(Exception):
    """A transport failure after the request may have reached the hub."""


class _NotSent(Exception):
    """A transport failure before any byte of the request left (DNS, refused)."""


# --- HTTP ------------------------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)


class HttpTransport:
    """One request per connection over ``http.client``; the Bearer token rides a header only."""

    def __init__(self, cfg: RestHubConfig, credential: Credential) -> None:
        self.cfg = cfg
        self.credential = credential
        self._ssl = self._ssl_context() if cfg.scheme == "https" else None

    def _ssl_context(self) -> ssl.SSLContext:
        cfg, cred = self.cfg, self.credential
        ca = cfg.ca_file or (str(Path(cred.tls_dir) / "ca.crt") if cred.tls_dir else "")
        ctx = ssl.create_default_context(cafile=os.path.expanduser(ca) if ca else None)
        cert, key = cfg.cert_file, cfg.key_file
        if not cert and cred.tls_dir:
            c, k = Path(cred.tls_dir) / "client.crt", Path(cred.tls_dir) / "client.key"
            if c.is_file() and k.is_file():
                cert, key = str(c), str(k)
        if cert:
            ctx.load_cert_chain(os.path.expanduser(cert), os.path.expanduser(key))
        if cfg.insecure or cred.insecure:
            ctx.check_hostname = False           # hostname only: the chain is still verified
        return ctx

    def connection(self, timeout: float) -> http.client.HTTPConnection:
        host, port = self.cfg.host, self.cfg.port
        if self._ssl is not None:
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._ssl)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": "harness-manager-hub-rest",
             **self.credential.header()}
        if extra:
            h.update(extra)
        return h

    def request(self, method: str, path: str, *, body: Any = None,
                params: Mapping[str, Any] | None = None, timeout: float) -> Response:
        url = API_PREFIX + path + (f"?{urlencode(params)}" if params else "")
        payload = None if body is None else json.dumps(body).encode()
        headers = self.headers({"Content-Type": "application/json"} if payload is not None
                               else None)
        conn = self.connection(timeout)
        try:
            try:
                conn.connect()
            except (OSError, ssl.SSLError) as exc:
                raise _NotSent(exc) from exc
            try:
                conn.request(method, url, body=payload, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
            except (OSError, http.client.HTTPException) as exc:
                raise _Sent(exc) from exc
        finally:
            conn.close()
        text = raw.decode("utf-8", "replace")
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = text
        return Response(resp.status, data, {k.lower(): v for k, v in resp.getheaders()})

    def open_stream(self, path: str, params: Mapping[str, Any] | None, *,
                    timeout: float | None) -> tuple[http.client.HTTPConnection,
                                                     http.client.HTTPResponse]:
        """A streaming GET (SSE). The caller owns and closes the connection."""
        url = API_PREFIX + path + (f"?{urlencode(params)}" if params else "")
        conn = self.connection(timeout or DEFAULT_TIMEOUT_S)
        conn.connect()
        sock = getattr(conn, "sock", None)
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.settimeout(timeout)
        conn.request("GET", url, headers=self.headers({"Accept": "text/event-stream",
                                                       "Cache-Control": "no-cache"}))
        return conn, conn.getresponse()


# --- helpers ---------------------------------------------------------------------------------------


def _iso(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _now_iso(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat(timespec="seconds")


def _plus_s(iso: str, seconds: float) -> str:
    try:
        at = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (at + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def note_id_for(holder: str) -> str:
    """A request id for a waiter the hub knows only by its holder (degraded mode)."""
    return "q-" + (_NOTE_ID_RE.sub("_", holder).strip("_") or "anon")


# --- the client ------------------------------------------------------------------------------------


class RestHubClient:
    """fpgahub over its REST API for one target: the ``HubClient`` verbs and the frozen additions."""

    transport = TRANSPORT_REST
    #: Degraded mode: no note store over REST (module docstring; docs/HUB_MODE.md).
    notes_supported = False
    notes_reason = ("this hub is reached over its REST API, which has no note store: a "
                    "request is your place in the queue, without a message, and the holder "
                    "can release but cannot answer 'keep'")

    def __init__(self, cfg: RestHubConfig, *, credential: Credential | None = None,
                 http: HttpTransport | None = None, clock: Callable[[], float] = time.time,
                 retry_backoff_s: tuple[float, ...] = RETRY_BACKOFF_S,
                 wait_slice_s: float = WAIT_SLICE_S) -> None:
        self.config = cfg
        self.target = cfg.target
        self.host = cfg.host
        self.url = cfg.url
        self.timeout_s = cfg.timeout_s
        self.credential = credential if credential is not None else resolve_credential(cfg)
        self._http = http or HttpTransport(cfg, self.credential)
        self._clock = clock
        self._backoff = tuple(retry_backoff_s)
        self.wait_slice_s = wait_slice_s
        self._mu = threading.Lock()
        self._whoami: dict[str, Any] | None = None
        self._board: str = cfg.board
        self._wait_supported = True
        self._requests: dict[str, Any] = {}          # our own RequestNotes (local only)
        self._seen_waiters: dict[str, str] = {}      # holder -> first seen (ISO)
        self._queued_at: dict[str, str] = {}         # holder -> the hub's lease.queued ts
        self._observed: deque[dict[str, Any]] = deque(maxlen=200)
        self._warned_notes = False

    def __repr__(self) -> str:
        return f"RestHubClient({self.url!r}, target={self.target!r}, {self.credential!r})"

    # -- plumbing ---------------------------------------------------------------------------

    def _call(self, what: str, method: str, path: str, *, body: Any = None,
              params: Mapping[str, Any] | None = None, timeout: float | None = None,
              retry: str = "safe", ok: tuple[int, ...] = (200, 201, 204),
              passthrough: tuple[int, ...] = ()) -> Response:
        """One API call with retries by ``retry`` (``safe``/``connect``/``never``).

        ``passthrough`` statuses are returned for the caller to interpret.
        """
        attempts = 1 + (len(self._backoff) if retry != "never" else 0)
        last: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                time.sleep(self._backoff[attempt - 1])
            try:
                resp = self._http.request(method, path, body=body, params=params,
                                          timeout=timeout or self.timeout_s)
            except _NotSent as exc:
                last = exc.__cause__ or exc
                if retry == "never":
                    break
                continue
            except _Sent as exc:
                last = exc.__cause__ or exc
                if retry != "safe":
                    break
                continue
            if resp.status in ok or resp.status in passthrough:
                resp.headers["x-attempts"] = str(attempt + 1)
                return resp
            if resp.status >= 500 and retry == "safe" and attempt + 1 < attempts:
                last = UnreachableError(f"HTTP {resp.status}")
                continue
            raise hub_error(what, resp.status, resp.body, self.config)
        raise self._unreachable(what, last)

    def _unreachable(self, what: str, exc: Exception | None) -> UnreachableError:
        why = str(exc) if exc is not None else "no answer"
        if isinstance(exc, ssl.SSLCertVerificationError):
            return UnreachableError(
                f"{what}: the hub {self.config.addr}'s TLS certificate did not verify ({why})",
                hint="set hub.ca_file to the hub's CA certificate (ask the hub admin)")
        if isinstance(exc, ssl.SSLError):
            return UnreachableError(
                f"{what}: TLS with the hub {self.config.addr} failed ({why})",
                hint="port 7245 needs a client certificate; a token uses the web port (7246)")
        return UnreachableError(f"{what}: cannot reach the hub {self.config.addr} ({why})",
                                hint="check hub.url, and that you are on the campus network "
                                     "or VPN")

    def _t(self, suffix: str = "") -> str:
        return f"/targets/{self.target}{suffix}"

    # -- identity -----------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """``GET /health`` (anonymous): ``{status, version, schema_version}``."""
        return self._call("health", "GET", "/health").body or {}

    def whoami(self, *, fresh: bool = False) -> dict[str, Any]:
        with self._mu:
            cached = self._whoami
        if cached is not None and not fresh:
            return cached
        who = self._call("whoami", "GET", "/whoami").body or {}
        with self._mu:
            self._whoami = who
        return who

    def principal(self) -> str:
        """The holder string the hub records for this credential (``owner@hub-host``)."""
        return str(self.whoami().get("holder", ""))

    def role(self) -> str:
        return str(self.whoami().get("role", ""))

    def can_revoke(self) -> tuple[bool, str]:
        """Whether this credential may force-release (fpgahub: admin only), and why not."""
        try:
            role = self.role()
        except HarnessError as exc:
            return False, exc.message
        if role == "admin":
            return True, ""
        return False, (f"force-release needs an admin credential on {self.config.addr}; "
                       f"this token is {role or 'unknown'}")

    def board_id(self) -> str:
        """The physical board (the lease unit) that owns the target: ``mps3_01`` for ``mps3_01_pl``."""
        with self._mu:
            if self._board:
                return self._board
        groups = (self._call("groups", "GET", "/groups").body or {}).get("groups", [])
        board = next((g.get("board", "") for g in groups
                      if any(m.get("name") == self.target for m in g.get("members", []))), "")
        with self._mu:
            self._board = board or self.target
            return self._board

    def target_info(self) -> dict[str, Any]:
        """``GET /targets/{t}`` (fpgahub ``BoardResponse``): network, access (the gates), ..."""
        return self._call("target", "GET", self._t()).body or {}

    def groups(self) -> list[dict[str, Any]]:
        """``GET /groups``: the physical boards and their targets (SET-HUBS: Test connection
        and discovery; a read)."""
        body = self._call("groups", "GET", "/groups").body or {}
        groups = body.get("groups", []) if isinstance(body, dict) else []
        return [g for g in groups if isinstance(g, dict)]

    # -- leases (the SSH client's verbs) ---------------------------------------------------------

    def _lease_body(self) -> dict[str, Any]:
        return self._call("lease show", "GET", self._t("/lease")).body or {}

    def lease_show(self) -> Any:
        data = self._lease_body()
        cur = data.get("current")
        view = shape("LeaseView")
        if not cur:
            raw = "not leased"
            return view(target=self.target, held=False, raw=raw)
        raw = f"held by {cur.get('holder', '')} (user {cur.get('user', '')}, " \
              f"expires {cur.get('expires_at', '')})"
        return view(target=self.target, held=True, holder=cur.get("holder", ""),
                    user=cur.get("user", ""), expires_at=_iso(cur.get("expires_at")), raw=raw)

    def lease_acquire(self, holder: str, *, ttl: int, poll_s: float = 20.0,
                      timeout_s: float = 3600.0, sleep: Callable[[float], None] | None = None,
                      log_fn: Callable[[str], None] | None = None,
                      now: Callable[[], float] = time.monotonic) -> tuple[Any, str]:
        """Acquire, waiting while queued. Returns ``(Lease, expires_at)``.

        ``holder`` is advisory: the hub records this credential's principal, and the
        returned ``Lease.holder`` is that. ``sleep`` is the caller's cancel point
        (``LeaseService`` raises from it); it is called between wait slices.
        """
        sleep = sleep or time.sleep
        say = log_fn or (lambda _m: None)
        deadline = now() + timeout_s
        body = {"ttl_seconds": int(ttl), "tier": "interactive"}
        last_pos: Any = None
        data: dict[str, Any] | None = None
        reassert_at = 0.0
        while True:
            if data is None or now() >= reassert_at:
                # (Re-)assert the queue place: idempotent per principal, and it recovers an
                # entry the hub lost. Every POST makes the hub emit lease.queued, so it is
                # done once per poll_s, not once per wait slice.
                data = self._call("lease acquire", "POST", self._t("/lease"), body=body).body or {}
                reassert_at = now() + poll_s
            if data.get("kind") == "granted":
                return self._granted(data, say)
            pos = data.get("position")
            if pos != last_pos:
                say(f"queued at position {pos} for {self.target} as {data.get('holder', holder)}"
                    f"; waiting for the hub to promote it")
                last_pos = pos
            left = deadline - now()
            if left <= 0:
                removed = False
                with contextlib.suppress(HarnessError):
                    removed = self.lease_cancel(holder)
                cur = self.lease_show()
                raise HeldError(f"gave up waiting for {self.target} after {timeout_s:.0f}s"
                                + ("; the queue entry was removed" if removed else ""),
                                holder=getattr(cur, "holder", ""),
                                hint="try again later, or ask the holder to release it")
            granted = self._wait(min(poll_s, self.wait_slice_s, left)) \
                if self._wait_supported else None
            if granted is not None:
                return self._granted(granted, say)
            sleep(0.0 if self._wait_supported else min(poll_s, max(left, 0.0)))

    def _granted(self, data: dict[str, Any], say: Callable[[str], None]) -> tuple[Any, str]:
        lease = data.get("lease") or (data.get("leases") or [{}])[0]
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise UnreachableError(f"the hub granted {self.target} but sent no token",
                                   hint="release it from the hub's web UI")
        who = lease.get("holder", "")
        if who:
            with self._mu:
                if self._whoami is not None and self._whoami.get("holder") != who:
                    self._whoami = None
        say(f"lease granted: {who} holds {self.target}")
        lease_cls = _lease_cls()
        return lease_cls(token=token, holder=who, target=self.target), _iso(lease.get("expires_at"))

    def _wait(self, seconds: float) -> dict[str, Any] | None:
        """One slice of ``GET /lease/wait``: the grant, or None on the hub's 408."""
        seconds = max(0.1, min(seconds, WAIT_MAX_S))
        try:
            resp = self._call("lease wait", "GET", self._t("/lease/wait"),
                              params={"timeout": f"{seconds:g}"}, timeout=seconds + 15.0,
                              passthrough=(408,))
        except AbsentError as exc:
            if "no such route" in exc.message:
                self._wait_supported = False      # an older hub: poll the acquire instead
                return None
            raise
        return None if resp.status == 408 else (resp.body or None)

    def lease_heartbeat(self, token: str, holder: str) -> str:
        """Extend the lease by its own TTL. Returns the new ``expires_at``."""
        data = self._call("lease heartbeat", "POST", self._t("/lease/heartbeat"),
                          body={"token": token}).body or {}
        return _iso(data.get("expires_at"))

    def lease_release(self, token: str, holder: str) -> None:
        resp = self._call("lease release", "DELETE", self._t("/lease"), body={"token": token})
        released = bool((resp.body or {}).get("released"))
        if not released and resp.headers.get("x-attempts", "1") == "1":
            raise shape("LeaseLostError")(
                f"no lease to release on {self.target}: the hub holds none under this token",
                state="expired", hint="it may have expired; see harness-manager lease show")

    def lease_cancel(self, holder: str = "") -> bool:
        """Leave the queue (our own entry: fpgahub keys it on the credential)."""
        resp = self._call("lease cancel", "DELETE", self._t("/queue"), body={},
                          passthrough=(409,))
        if resp.status == 409:
            detail = _detail(resp.body)
            board = detail.get("board") if isinstance(detail, dict) else None
            if not (isinstance(detail, dict) and detail.get("kind") == "board_required"
                    and isinstance(board, str) and _NAME_RE.fullmatch(board)):
                raise hub_error("lease cancel", 409, resp.body, self.config)
            with self._mu:
                self._board = self._board or board
            resp = self._call("lease cancel", "DELETE", f"/boards/{board}/queue", body={})
        return bool((resp.body or {}).get("removed"))

    # -- shares (never stop) -----------------------------------------------------------------

    def share_list(self) -> list[Any]:
        info = shape("ShareInfo")
        rows = self._call("share list", "GET", self._t("/shares")).body or []
        return [info(tty=r.get("tty_path", ""), host=r.get("host", ""), port=int(r.get("port", 0)),
                     writer=r.get("writer") or "", readers=int(r.get("reader_count", 0)),
                     running=bool(r.get("running", True))) for r in rows]

    def share_for(self, tty: str) -> Any:
        return next((s for s in self.share_list() if s.tty == tty), None)

    def share_start(self, tty: str, baud: int = 115200) -> Any:
        info = shape("ShareInfo")
        rows = self._call("share start", "POST", self._t("/shares"),
                          body={"tty_paths": [tty], "baud": int(baud)}).body or []
        for r in rows:
            if r.get("tty_path") == tty:
                return info(tty=tty, host=r.get("host", ""), port=int(r.get("port", 0)),
                            writer=r.get("writer") or "", readers=int(r.get("reader_count", 0)),
                            running=bool(r.get("running", True)))
        raise UnreachableError(f"share start on {self.config.addr} returned no share for {tty}")

    def share_stop(self, *_a: Any, **_k: Any) -> None:
        raise RefusedError("fpgahub share stop stops EVERY share on the board; "
                           "Harness Manager never runs it",
                           hint="close only your own client; david stops shares at close-out")

    # -- the frozen additions (docs/LEASE_REQUESTS.md) -----------------------------------------

    def lease_status(self) -> Any:
        data = self._lease_body()
        cur = data.get("current") or {}
        entry, status = shape("QueueEntry"), shape("LeaseStatus")
        queue = tuple(entry(position=int(q.get("position", 0)), holder=q.get("holder", ""),
                            user=q.get("user", "")) for q in data.get("queue") or [])
        return status(held=bool(cur), holder=cur.get("holder", ""), user=cur.get("user", ""),
                      expires_at=_iso(cur.get("expires_at")), queue=queue)

    def lease_revoke(self, reason: str) -> dict[str, Any]:
        """Admin force-release of the board; fpgahub promotes the head of the queue.

        Never retried once the request may have left: a second revoke would kick the
        waiter the first one promoted.
        """
        board = self.board_id()
        data = self._call("lease revoke", "POST", f"/boards/{board}/lease/revoke",
                          params={"reason": reason} if reason else None, retry="connect").body
        data = data or {}
        return {"revoked": list(data.get("revoked") or []), "by": data.get("by", ""),
                "board": data.get("board", board)}

    def lease_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """The hub's lease history for the target, plus the revokes the event stream saw.

        fpgahub's history drops ``by``/``reason`` and never returns
        ``lease.admin_revoked`` (module docstring). Events ``observe``d from
        ``/events`` fill that in: each carries ``by`` and ``reason``.
        """
        body = self._call("lease history", "GET", self._t("/lease/history"),
                          params={"limit": int(limit)}).body or {}
        rows = [dict(r) for r in body.get("events") or []]
        by_key = {(r.get("ts"), r.get("event")): r for r in rows}
        with self._mu:
            observed = [dict(r) for r in self._observed]
        for obs in observed:
            row = by_key.get((obs.get("ts"), obs.get("event")))
            if row is None:
                rows.append(obs)
            else:                               # the same audit record: add what REST dropped
                row.update({k: v for k, v in obs.items() if v and not row.get(k)})
        rows.sort(key=lambda r: str(r.get("ts") or ""))
        return rows[-limit:] if limit else rows

    # degraded mode: request notes -----------------------------------------------------------

    def _warn_notes(self) -> None:
        if not self._warned_notes:
            self._warned_notes = True
            log.info("hub %s: %s", self.config.addr, self.notes_reason)

    def put_request(self, note: Any) -> None:
        """Kept locally only: over REST the request is the queue entry (degraded mode)."""
        if not _NAME_RE.fullmatch(str(getattr(note, "id", ""))):
            raise UsageError("a request id is [A-Za-z0-9_.-] only")
        self._warn_notes()
        with self._mu:
            self._requests[note.id] = note

    def list_requests(self) -> list[Any]:
        """Our own requests, then every other waiter in the queue as a message-less request."""
        note_cls = shape("RequestNote")
        status = self.lease_status()
        try:
            me = self.principal()
        except HarnessError:
            me = ""
        now = _now_iso(self._clock)
        out: list[Any] = []
        with self._mu:
            waiting = {q.holder for q in status.queue}
            self._seen_waiters = {h: at for h, at in self._seen_waiters.items() if h in waiting}
            mine = list(self._requests.values())
            for q in status.queue:
                if q.holder == me:
                    continue
                created = self._queued_at.get(q.holder) or self._seen_waiters.setdefault(
                    q.holder, now)
                by, _, host = q.holder.partition("@")
                out.append(note_cls(id=note_id_for(q.holder), by=q.holder, user=q.user or by,
                                    host=host, message="", created_at=created,
                                    deadline_at=_plus_s(created, REQUEST_DEADLINE_S)))
        return mine + out

    def delete_request(self, request_id: str) -> None:
        with self._mu:
            self._requests.pop(request_id, None)

    def put_answer(self, note: Any) -> None:
        raise UnavailableError("lease_answer", self.notes_reason)

    def get_answer(self, request_id: str) -> Any:
        return None

    # -- the event stream feeds these ----------------------------------------------------------

    def relevant(self, event: Mapping[str, Any]) -> bool:
        """Is this fpgahub event about our target or its board?"""
        data = event.get("data") or {}
        if data.get("board") == self.target:
            return True
        with self._mu:
            board = self._board
        chassis = data.get("chassis")
        if chassis is not None and chassis in (board, self.target):
            return True
        return self.target in (data.get("members") or ())

    def observe(self, event: Mapping[str, Any]) -> None:
        """Remember what history cannot give back: revokes with by/reason, queue times."""
        etype, data = str(event.get("type", "")), dict(event.get("data") or {})
        ts = str(event.get("ts", ""))
        if etype == "lease.queued" and data.get("holder"):
            with self._mu:
                self._queued_at.setdefault(str(data["holder"]), ts)
        elif etype in ("lease.promoted", "lease.acquired", "lease.queue.cancelled") and \
                data.get("holder"):
            with self._mu:
                self._queued_at.pop(str(data["holder"]), None)
        if etype in ("lease.admin_revoked", "lease.revoked"):
            row = {"ts": ts, "event": etype, "board": data.get("board") or self.target,
                   "holder": data.get("prior_holder") or data.get("holder"),
                   "user": data.get("prior_user") or data.get("user"),
                   "by": data.get("by", ""), "reason": data.get("reason", ""),
                   "source": "events"}
            with self._mu:
                self._observed.append(row)


# --- choosing a transport ----------------------------------------------------------------------


def make_rest_client(cfg: RestHubConfig) -> RestHubClient:
    return RestHubClient(cfg)


#: What ``client_for`` builds a REST client with; tests swap it for one aimed at the fake hub.
DEFAULT_REST_FACTORY: Callable[[RestHubConfig], Any] = make_rest_client


def client_for(hub_config: Any, *, ssh_factory: Callable[[], Any] | None = None) -> Any:
    """The hub client a ``hub.HubConfig`` asks for: REST when it has ``rest``, else SSH.

    ``hub.py`` calls this (CCR T8-1); ``ssh_factory`` builds today's SSH client.
    """
    rest = getattr(hub_config, "rest", None)
    if rest is not None:
        return DEFAULT_REST_FACTORY(rest)
    if ssh_factory is None:
        raise UsageError("this hub table has neither url (REST) nor host (SSH)")
    return ssh_factory()


def with_target(cfg: RestHubConfig, target: str) -> RestHubConfig:
    return replace(cfg, target=target)
