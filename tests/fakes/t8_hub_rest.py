"""A fake fpgahub 0.3.0 REST + SSE server for team T8 (hub mode).

``FakeFpgahub`` is a real HTTP server on 127.0.0.1 (stdlib ``ThreadingHTTPServer``)
that answers the routes Harness Manager's ``RestHubClient`` calls with fpgahub
0.3.0's own status codes, bodies and events. It was written against the source
(``fpgahub/api/v1.py``, ``api/principal.py``, ``lease.py``, ``daemon.py``) and is
CHECKED against a transcript of the real app (``t8_fpgahub_v030_golden.json``,
recorded by ``t8_record_fpgahub_golden.py`` from fpgahub's own FastAPI app):
``tests/unit/test_t8_contract.py`` replays that transcript here and compares
every status, key and value type.

What it models, with where each rule comes from:

- **Auth** (``principal.py``): a Bearer token is a principal ``owner@<hub hostname>``
  with a role; no/unknown token -> 401 ``read access: present a Bearer token …``;
  too low a role -> 403 ``role='write'; admin required``. ``GET /health`` is anonymous.
- **Leases** (``lease.py`` ``LeaseManager``): the queue is per physical board
  (chassis) and idempotent per holder; re-acquiring as the holder refreshes the
  lease under the SAME token; release and revoke promote the queue head under a
  NEW token; heartbeat/release by anyone but the holder is 403 ``holder or token does
  not match current lease``, with no lease 403 ``no current lease for board``;
  ``DELETE /targets/{t}/queue`` on a multi-target board is 409 ``board_required``.
- **Revoke** (``_do_revoke``): admin only; emits ``lease.revoked``, ``lease.promoted``,
  ``lease.released`` and ``lease.admin_revoked {by, reason, members, prior_holder, …}``
  in the real order; the reason gets `` (by token:<owner>)`` appended.
- **History** (``lease_journal`` + ``LeaseEventRecord``): every ``lease.*`` event is
  journalled; the history routes return only records whose ``board`` matches,
  projected onto the record's ten fields, so ``by``/``reason`` never come back and
  ``lease.admin_revoked`` (no ``board`` key) never appears.
- **Wait** (``GET /lease/wait``): blocks until the caller's principal holds the
  target, then returns the token; 408 on timeout.
- **Shares** (``tty_share``): ``POST`` starts (or returns) a share on a real local
  TCP listener, refused 409 ``lease_conflict`` on a board someone else holds;
  ``DELETE`` is recorded in ``share_stops`` (tests assert nobody calls it).
- **Events** (``events_stream``): ``:connected`` then ``event:``/``data:`` frames,
  chunked like uvicorn, with the ``types`` and ``board`` filters.

Test hooks: ``add_token``, ``expire``, ``drop_streams`` (cut every live SSE
connection), ``refuse_streams`` (answer ``/events`` with an error status),
``fail_next`` (inject an HTTP status or a dropped connection), ``requests`` (every
request line and whether it carried a token; no token value is kept).
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

HUB_HOSTNAME = "mapstone-dev"

#: The lab's shape (fpgahub tests/fixtures/live_shape_config.toml): board mps3_01 with one
#: target, pl; and a two-target board to exercise ``board_required``.
DEFAULT_BOARDS: dict[str, dict[str, dict[str, Any]]] = {
    "mps3_01": {
        "pl": {"board_ip": "192.168.10.101", "host_ip": "192.168.10.1/24",
               "board_mac": "02:00:5e:00:03:03", "hostname": "mps3-01-pl",
               "hub_path": "1-2.3.4.3", "board_type": "mps3",
               "description": "HBI0309C MPS3 #01 (lab)", "gate_ethernet": False,
               "share_tty": False},
    },
    "kr260_01": {
        "ps": {"board_ip": "192.168.20.101", "host_ip": "192.168.20.1/24",
               "board_mac": "02:00:5e:00:20:01", "hostname": "kr260-01-ps",
               "hub_path": "1-1.3", "description": "KR260 PS"},
        "pl": {"board_ip": "192.168.21.101", "host_ip": "192.168.21.1/24",
               "board_mac": "02:00:5e:00:21:01", "hostname": "kr260-01-pl",
               "hub_path": "1-1.4", "description": "KR260 PL"},
    },
}

UNAUTHORIZED = "read access: present a Bearer token (fpgahub login) or sign in to the web UI"
_RANK = {"none": 0, "read": 1, "write": 2, "admin": 3}
LEASE_RECORD_FIELDS = ("ts", "event", "board", "holder", "user", "position", "ttl_s",
                       "expires_at", "source", "error")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pyd(dt: datetime) -> str:
    """A datetime as pydantic serialises it in a response model (``…Z``)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


class HttpError(Exception):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(status)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    token_name: str
    host: str = HUB_HOSTNAME

    @property
    def holder(self) -> str:
        return f"{self.name}@{self.host}"

    @property
    def audit_id(self) -> str:
        return f"token:{self.name}"

    def allows(self, role: str) -> bool:
        return _RANK[self.role] >= _RANK[role]


@dataclass
class _Lease:
    target: str
    holder: str
    user: str
    token: str
    expires_at: datetime
    ttl: int
    tier: str = "interactive"

    def info(self) -> dict[str, Any]:
        return {"board": self.target, "holder": self.holder, "user": self.user,
                "expires_at": _pyd(self.expires_at), "tier": self.tier}


@dataclass
class _Waiter:
    holder: str
    user: str
    ttl: int
    tier: str = "interactive"


@dataclass
class _Share:
    target: str
    tty: str
    port: int
    srv: socket.socket
    clients: list[socket.socket] = field(default_factory=list)

    def info(self) -> dict[str, Any]:
        return {"board": self.target, "tty_path": self.tty, "host": "0.0.0.0",
                "port": self.port, "writer": None, "reader_count": len(self.clients),
                "running": True}


class FakeFpgahub:
    """The fake hub. ``with FakeFpgahub() as hub: hub.url``; see the module docstring."""

    def __init__(self, *, boards: dict[str, dict[str, dict[str, Any]]] | None = None,
                 hostname: str = HUB_HOSTNAME) -> None:
        self.hostname = hostname
        self.boards = boards or DEFAULT_BOARDS
        self.targets: dict[str, tuple[str, str, dict[str, Any]]] = {}
        for board, members in self.boards.items():
            for role, spec in members.items():
                self.targets[f"{board}_{role}"] = (board, role, spec)
        self.tokens: dict[str, tuple[str, str, str]] = {}      # secret -> (owner, role, name)
        self.leases: dict[str, _Lease] = {}
        self.queues: dict[str, list[_Waiter]] = {}
        self.journal: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.shares: dict[tuple[str, str], _Share] = {}
        self.share_stops: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self._streams: list[socket.socket] = []
        self._refuse: int | None = None
        self._fail: list[Any] = []
        self._srv: ThreadingHTTPServer | None = None

    # -- lifecycle ----------------------------------------------------------------------------

    def start(self) -> FakeFpgahub:
        hub = self

        class Handler(_Handler):
            fake = hub

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, name="fake-fpgahub",
                         daemon=True).start()
        return self

    def close(self) -> None:
        self.drop_streams()
        with self._cond:
            self._cond.notify_all()
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
        for sh in list(self.shares.values()):
            for c in sh.clients:
                c.close()
            sh.srv.close()

    def __enter__(self) -> FakeFpgahub:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def port(self) -> int:
        assert self._srv is not None
        return self._srv.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- test hooks ---------------------------------------------------------------------------

    def add_token(self, owner: str, role: str = "write", name: str | None = None) -> str:
        secret = secrets.token_urlsafe(24)
        self.tokens[secret] = (owner, role, name or f"{owner}-{role}")
        return secret

    def holder(self, owner: str) -> str:
        return f"{owner}@{self.hostname}"

    def expire(self, target: str) -> None:
        with self._cond:
            lease = self.leases.get(target)
            if lease is not None:
                lease.expires_at = _now() - timedelta(seconds=1)
            self._expire_stale()

    def drop_streams(self) -> int:
        with self._cond:
            socks, self._streams = self._streams, []
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return len(socks)

    def refuse_streams(self, status: int | None) -> None:
        self._refuse = status

    def fail_next(self, *outcomes: Any) -> None:
        """Queue failures for the next requests: an int status, or ``"drop"`` (close unanswered)."""
        self._fail.extend(outcomes)

    def stream_count(self) -> int:
        with self._cond:
            return len(self._streams)

    def emitted(self, prefix: str = "lease.") -> list[str]:
        return [e["type"] for e in self.events if e["type"].startswith(prefix)]

    # -- the model ----------------------------------------------------------------------------

    def principal(self, auth: str | None) -> Principal | None:
        if not auth or not auth.lower().startswith("bearer "):
            return None
        rec = self.tokens.get(auth[7:].strip())
        if rec is None:
            return None
        owner, role, name = rec
        return Principal(owner, role, name, self.hostname)

    def _emit(self, etype: str, **data: Any) -> None:
        ts = _now().isoformat()
        ev = {"type": etype, "ts": ts, "data": data}
        self.events.append(ev)
        if etype.startswith("lease."):
            self.journal.append({"ts": ts, "event": etype, **data})
        self._cond.notify_all()

    def _require_target(self, name: str) -> tuple[str, str, dict[str, Any]]:
        if name not in self.targets:
            configured = ", ".join(sorted(self.targets))
            raise HttpError(404, f"no such board: {name!r}; configured: {configured}")
        return self.targets[name]

    def _require_board(self, board: str) -> list[str]:
        if board not in self.boards:
            raise HttpError(404, f"no such board: {board!r}")
        return [f"{board}_{r}" for r in self.boards[board]]

    def _expire_stale(self) -> None:
        now = _now()
        for target, lease in list(self.leases.items()):
            if lease.expires_at <= now:
                del self.leases[target]
                self._emit("lease.expired", board=target, holder=lease.holder, user=lease.user)
                self._promote(target, lease.ttl)

    def _promote(self, target: str, prior_ttl: int) -> _Lease | None:
        board = self.targets[target][0]
        queue = self.queues.get(board) or []
        if not queue or target in self.leases:
            return None
        w = queue.pop(0)
        ttl = w.ttl or prior_ttl
        lease = _Lease(target, w.holder, w.user, secrets.token_urlsafe(16),
                       _now() + timedelta(seconds=ttl), ttl, w.tier)
        self.leases[target] = lease
        self._emit("lease.promoted", board=target, holder=lease.holder, user=lease.user,
                   tier=lease.tier, expires_at=lease.expires_at.isoformat())
        return lease

    def _queue_entries(self, target: str, key: str) -> list[dict[str, Any]]:
        board = self.targets[target][0] if target in self.targets else target
        return [{"board": key, "holder": w.holder, "user": w.user, "position": i + 1,
                 "tier": w.tier} for i, w in enumerate(self.queues.get(board) or [])]

    # routes ---------------------------------------------------------------------------------

    def whoami(self, p: Principal) -> dict[str, Any]:
        return {"kind": "token", "name": p.name, "role": p.role, "host": p.host,
                "holder": p.holder, "audit_id": p.audit_id, "token_name": p.token_name,
                "groups": [], "limits": {"max_ttl_s": None, "max_boards": None, "tier": None,
                                         "rule": None}}

    def groups(self) -> dict[str, Any]:
        return {"groups": [{"board": b, "size": len(m), "is_paired": len(m) > 1,
                            "members": [{"name": f"{b}_{r}", "role": r} for r in m]}
                           for b, m in self.boards.items()]}

    def target_response(self, name: str) -> dict[str, Any]:
        board, role, spec = self._require_target(name)
        lease = self.leases.get(name)
        queue = self.queues.get(board) or []
        return {
            "name": name, "server": self.hostname, "attach": "usbip",
            "hub_path": spec.get("hub_path"), "effective_hub_path": None,
            "discovered_mac": None, "description": spec.get("description", ""),
            "naming": {"tty_symlink_dir": name, "net_name": name},
            "network": {"host_ip": spec.get("host_ip"), "board_ip": spec.get("board_ip"),
                        "board_mac": spec.get("board_mac"), "pl_mac": None,
                        "hostname": spec.get("hostname", name), "dns_search": "fpga"},
            "access": {"default_client": None, "lease_timeout_s": 3600,
                       "share_tty": bool(spec.get("share_tty", False)), "share_port_base": 12000,
                       "gate_tty": False, "gate_ethernet": bool(spec.get("gate_ethernet", False))},
            "capabilities": [], "board_type": spec.get("board_type"), "pinout_url": None,
            "lease_state": "held" if lease else ("queued" if queue else "none"),
            "lease_holder": lease.holder if lease else None,
            "lease_queue_length": len(queue),
        }

    def lease_get(self, name: str) -> dict[str, Any]:
        self._require_target(name)
        self._expire_stale()
        lease = self.leases.get(name)
        return {"current": lease.info() if lease else None,
                "queue": self._queue_entries(name, name), "background_queue": []}

    def board_lease_get(self, board: str) -> dict[str, Any]:
        members = self._require_board(board)
        self._expire_stale()
        per = [{"board": m, "current": self.leases[m].info() if m in self.leases else None}
               for m in members]
        held = [e for e in per if e["current"]]
        state = "free" if not held else ("held" if len(held) == len(per) and
                                         len({e["current"]["holder"] for e in held}) == 1
                                         else "partial")
        queue = self._queue_entries(board, board)
        return {"board": board, "state": state, "description": None, "capabilities": [],
                "tags": [], "members": per, "queue": queue, "queue_length": len(queue),
                "background_queue": [], "background_queue_length": 0,
                "current_tier": held[0]["current"]["tier"] if held else None}

    def acquire(self, name: str, p: Principal, body: dict[str, Any]) -> dict[str, Any]:
        board, _, _ = self._require_target(name)
        ttl = body.get("ttl_seconds", 3600)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise HttpError(422, [{"type": "greater_than", "loc": ["body", "ttl_seconds"],
                                   "msg": "Input should be greater than 0", "input": ttl,
                                   "ctx": {"gt": 0}}])
        tier = body.get("tier", "interactive")
        self._expire_stale()
        cur = self.leases.get(name)
        if cur is None or cur.holder == p.holder:
            if cur is None:
                cur = self.leases[name] = _Lease(name, p.holder, p.name,
                                                 secrets.token_urlsafe(16),
                                                 _now() + timedelta(seconds=ttl), ttl, tier)
            else:
                cur.expires_at, cur.ttl = _now() + timedelta(seconds=ttl), ttl
            self._emit("lease.acquired", board=name, holder=cur.holder, user=cur.user, tier=tier)
            return {"kind": "granted", "lease": cur.info(), "token": cur.token, "tier": tier,
                    "requeue_on_revoke": False}
        queue = self.queues.setdefault(board, [])
        pos = next((i + 1 for i, w in enumerate(queue) if w.holder == p.holder), 0)
        if not pos:
            queue.append(_Waiter(p.holder, p.name, ttl, tier))
            pos = len(queue)
        user = queue[pos - 1].user
        self._emit("lease.queued", board=name, holder=p.holder, user=user, position=pos, tier=tier)
        return {"kind": "queued", "board": name, "holder": p.holder, "user": user,
                "position": pos, "tier": tier, "queue": "interactive"}

    def release(self, name: str, p: Principal, body: dict[str, Any]) -> dict[str, Any]:
        self._require_target(name)
        token = _need_token(body)
        self._expire_stale()
        cur = self.leases.get(name)
        if cur is None:
            return {"released": False}
        if cur.holder != p.holder or cur.token != token:
            raise HttpError(403, "holder or token does not match current lease")
        del self.leases[name]
        self._promote(name, cur.ttl)
        self._emit("lease.released", board=name, holder=p.holder, actor=p.audit_id)
        return {"released": True}

    def heartbeat(self, name: str, p: Principal, body: dict[str, Any]) -> dict[str, Any]:
        self._require_target(name)
        token = _need_token(body)
        self._expire_stale()
        cur = self.leases.get(name)
        if cur is None:
            raise HttpError(403, "no current lease for board")
        if cur.holder != p.holder or cur.token != token:
            raise HttpError(403, "holder or token does not match current lease")
        ttl = body.get("ttl_seconds") or cur.ttl
        cur.expires_at = _now() + timedelta(seconds=ttl)
        self._emit("lease.heartbeat", board=name, holder=cur.holder, user=cur.user,
                   expires_at=cur.expires_at.isoformat(), ttl_s=body.get("ttl_seconds"))
        return cur.info()

    def wait(self, name: str, p: Principal, timeout: float) -> dict[str, Any]:
        self._require_target(name)
        deadline = _now() + timedelta(seconds=timeout)
        while True:
            self._expire_stale()
            cur = self.leases.get(name)
            if cur is not None and cur.holder == p.holder:
                return {"kind": "granted", "token": cur.token, "tier": cur.tier,
                        "leases": [cur.info()]}
            left = (deadline - _now()).total_seconds()
            if left <= 0:
                raise HttpError(408, f"lease wait timed out after {timeout:g}s for board {name!r}")
            self._cond.wait(min(left, 0.2))

    def cancel_target_queue(self, name: str, p: Principal) -> dict[str, Any]:
        board, _, _ = self._require_target(name)
        members = [f"{board}_{r}" for r in self.boards[board]]
        if len(members) > 1:
            raise HttpError(409, {"kind": "board_required", "board": board, "members": members,
                                  "hint": f"target {name!r} is part of board {board!r}; "
                                          f"cancel via DELETE /boards/{board}/queue."})
        removed = self._remove_waiter(board, p.holder)
        if removed:
            self._emit("lease.queue.cancelled", board=name, holder=p.holder, actor=p.audit_id)
        return {"board": name, "removed": removed}

    def cancel_board_queue(self, board: str, p: Principal) -> dict[str, Any]:
        self._require_board(board)
        removed = self._remove_waiter(board, p.holder)
        if removed:
            self._emit("lease.queue.cancelled", chassis=board, board=None, holder=p.holder,
                       actor=p.audit_id)
        return {"board": board, "removed": removed}

    def _remove_waiter(self, board: str, holder: str) -> bool:
        queue = self.queues.get(board) or []
        for i, w in enumerate(queue):
            if w.holder == holder:
                del queue[i]
                return True
        return False

    def revoke(self, board: str, p: Principal, reason: str | None) -> dict[str, Any]:
        members = self._require_board(board)
        if not p.allows("admin"):
            raise HttpError(403, f"role={p.role!r}; admin required to revoke the lease on "
                                 f"{board!r}")
        full = (reason or "admin force-release") + f" (by {p.audit_id})"
        kicked = []
        for m in members:
            cur = self.leases.pop(m, None)
            if cur is None:
                continue
            self._emit("lease.revoked", board=m, holder=cur.holder, user=cur.user, tier=cur.tier,
                       reason=full)
            self._promote(m, cur.ttl)
            kicked.append(cur)
        for cur in kicked:
            self._emit("lease.released", board=cur.target, holder=cur.holder, chassis=board)
        if kicked:
            self._emit("lease.admin_revoked", by=p.audit_id, reason=full,
                       members=[c.target for c in kicked], prior_holder=kicked[0].holder,
                       prior_user=kicked[0].user, chassis=board)
        return {"board": board, "revoked": [c.target for c in kicked], "by": p.audit_id}

    def history(self, names: list[str], limit: int) -> list[dict[str, Any]]:
        out = []
        for name in names:
            rows = [r for r in self.journal if r.get("board") == name]
            out.extend(rows[-limit:] if limit else rows)
        out.sort(key=lambda r: r.get("ts") or "")
        return [{k: r.get(k) for k in LEASE_RECORD_FIELDS} for r in out]

    def share_start(self, name: str, p: Principal, body: dict[str, Any]) -> list[dict[str, Any]]:
        self._require_target(name)
        extra = sorted(set(body) - {"tty_paths", "baud", "bytesize", "parity", "stopbits"})
        if extra:
            raise HttpError(422, [{"type": "extra_forbidden", "loc": ["body", k],
                                   "msg": "Extra inputs are not permitted", "input": body[k]}
                                  for k in extra])
        cur = self.leases.get(name)
        if cur is not None and cur.holder != p.holder and not p.allows("admin"):
            raise HttpError(409, {"kind": "lease_conflict", "board": name, "op": "share start",
                                  "holder": cur.holder, "user": cur.user,
                                  "message": f"share start refused: board {name!r} is leased "
                                             f"by {cur.holder} ({cur.user}). Wait for the lease "
                                             f"to be released, or ask an admin to "
                                             f"force-release it."})
        paths = body.get("tty_paths") or []
        if not paths:
            raise HttpError(400, "tty_paths is required (at least one)")
        out = []
        for tty in paths:
            sh = self.shares.get((name, tty))
            if sh is None:
                srv = socket.socket()
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("127.0.0.1", 0))
                srv.listen(4)
                sh = self.shares[(name, tty)] = _Share(name, tty, srv.getsockname()[1], srv)
                threading.Thread(target=_share_accept, args=(sh,), daemon=True).start()
            out.append(sh.info())
        self._emit("share.started", board=name, count=len(out))
        return out


def _need_token(body: dict[str, Any]) -> str:
    token = body.get("token")
    if not isinstance(token, str):
        raise HttpError(422, [{"type": "missing", "loc": ["body", "token"],
                               "msg": "Field required", "input": body}])
    return token


def _share_accept(sh: _Share) -> None:
    while True:
        try:
            c, _ = sh.srv.accept()
        except OSError:
            return
        sh.clients.append(c)


# --- HTTP ------------------------------------------------------------------------------------------

_ROUTES: list[tuple[str, re.Pattern[str], str, str]] = [
    # (method, path regex, handler, minimum role)
    ("GET", re.compile(r"/api/v1/whoami"), "whoami", "read"),
    ("GET", re.compile(r"/api/v1/groups"), "groups", "read"),
    ("GET", re.compile(r"/api/v1/events"), "events", "read"),
    ("GET", re.compile(r"/api/v1/targets/(?P<n>[^/]+)"), "target", "read"),
    ("GET", re.compile(r"/api/v1/targets/(?P<n>[^/]+)/lease"), "lease_get", "read"),
    ("POST", re.compile(r"/api/v1/targets/(?P<n>[^/]+)/lease"), "acquire", "write"),
    ("DELETE", re.compile(r"/api/v1/targets/(?P<n>[^/]+)/lease"), "release", "write"),
    ("POST", re.compile(r"/api/v1/targets/(?P<n>[^/]+)/lease/heartbeat"), "heartbeat", "write"),
    ("GET", re.compile(r"/api/v1/targets/(?P<n>[^/]+)/lease/wait"), "wait", "read"),
    ("GET", re.compile(r"/api/v1/targets/(?P<n>[^/]+)/lease/history"), "history", "read"),
    ("DELETE", re.compile(r"/api/v1/targets/(?P<n>[^/]+)/queue"), "cancel_target", "write"),
    ("GET", re.compile(r"/api/v1/targets/(?P<n>[^/]+)/shares"), "shares", "read"),
    ("POST", re.compile(r"/api/v1/targets/(?P<n>[^/]+)/shares"), "share_start", "write"),
    ("DELETE", re.compile(r"/api/v1/targets/(?P<n>[^/]+)/shares"), "share_stop", "write"),
    ("GET", re.compile(r"/api/v1/boards/(?P<n>[^/]+)/lease"), "board_lease", "read"),
    ("POST", re.compile(r"/api/v1/boards/(?P<n>[^/]+)/lease/revoke"), "revoke", "admin"),
    ("GET", re.compile(r"/api/v1/boards/(?P<n>[^/]+)/lease/history"), "board_history", "read"),
    ("DELETE", re.compile(r"/api/v1/boards/(?P<n>[^/]+)/queue"), "cancel_board", "write"),
]

#: (method, OpenAPI template) of every route the fake serves: the contract test checks them.
SERVED = [("GET", "/api/v1/health")] + [
    (m, rx.pattern.replace("(?P<n>[^/]+)", "{name}")) for m, rx, _, _ in _ROUTES]


class _Handler(BaseHTTPRequestHandler):
    fake: FakeFpgahub
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    # -- plumbing ---------------------------------------------------------------------------

    def _send(self, status: int, body: Any) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise HttpError(422, [{"type": "json_invalid", "loc": ["body", 0],
                                   "msg": "JSON decode error", "input": {}}]) from exc

    def _dispatch(self, method: str) -> None:
        fake = self.fake
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        auth = self.headers.get("Authorization")
        fake.requests.append({"method": method, "path": path, "query": parts.query,
                              "authorized": bool(auth)})
        if fake._fail:
            outcome = fake._fail.pop(0)
            if outcome == "drop":
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self._send(int(outcome), {"detail": f"injected HTTP {outcome}"})
            return
        try:
            body = self._body()
            if method == "GET" and path == "/api/v1/health":
                self._send(200, {"status": "ok", "version": "0.3.0", "schema_version": 1})
                return
            route = next(((h, m.groupdict(), role) for mm, rx, h, role in _ROUTES
                          if mm == method and (m := rx.fullmatch(path))), None)
            if route is None:
                known = any(rx.fullmatch(path) for _, rx, _, _ in _ROUTES)
                raise HttpError(405 if known else 404,
                                "Method Not Allowed" if known else "Not Found")
            handler, args, need = route
            p = fake.principal(auth)
            if p is None:
                raise HttpError(401, UNAUTHORIZED)
            if not p.allows(need if need != "admin" else "admin"):
                if handler == "revoke":
                    raise HttpError(403, f"role={p.role!r}; admin required")
                raise HttpError(403, f"role={p.role!r}; {need} required")
            if handler == "events":
                self._events(query)
                return
            name = args.get("n", "")
            status, out = 200, None
            with fake._cond:
                if handler == "whoami":
                    out = fake.whoami(p)
                elif handler == "groups":
                    out = fake.groups()
                elif handler == "target":
                    out = fake.target_response(name)
                elif handler == "lease_get":
                    out = fake.lease_get(name)
                elif handler == "board_lease":
                    out = fake.board_lease_get(name)
                elif handler == "acquire":
                    out = fake.acquire(name, p, body or {})
                elif handler == "release":
                    out = fake.release(name, p, body or {})
                elif handler == "heartbeat":
                    out = fake.heartbeat(name, p, body or {})
                elif handler == "wait":
                    timeout = float((query.get("timeout") or ["60"])[0])
                    if not 0 < timeout <= 600:
                        raise HttpError(422, [{"type": "less_than_equal",
                                               "loc": ["query", "timeout"],
                                               "msg": "timeout out of range", "input": timeout}])
                    out = fake.wait(name, p, timeout)
                elif handler == "history":
                    fake._require_target(name)
                    limit = int((query.get("limit") or ["50"])[0])
                    out = {"board": name, "events": fake.history([name], limit)}
                elif handler == "board_history":
                    members = fake._require_board(name)
                    limit = int((query.get("limit") or ["50"])[0])
                    out = {"board": name, "events": fake.history(members, limit)}
                elif handler == "cancel_target":
                    out = fake.cancel_target_queue(name, p)
                elif handler == "cancel_board":
                    out = fake.cancel_board_queue(name, p)
                elif handler == "revoke":
                    out = fake.revoke(name, p, (query.get("reason") or [None])[0])
                elif handler == "shares":
                    fake._require_target(name)
                    out = [s.info() for (t, _), s in fake.shares.items() if t == name]
                elif handler == "share_start":
                    status, out = 201, fake.share_start(name, p, body or {})
                elif handler == "share_stop":
                    fake.share_stops.append(name)
                    status = 204
            self._send(status, out)
        except HttpError as exc:
            self._send(exc.status, {"detail": exc.detail})

    # -- SSE --------------------------------------------------------------------------------

    def _chunk(self, text: str) -> None:
        data = text.encode()
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _events(self, query: dict[str, list[str]]) -> None:
        fake = self.fake
        if fake._refuse is not None:
            self._send(fake._refuse, {"detail": f"injected HTTP {fake._refuse}"})
            return
        types = set((query.get("types") or [""])[0].split(",")) - {""}
        board = (query.get("board") or [None])[0]

        def matches(ev: dict[str, Any]) -> bool:
            if types and ev["type"] not in types:
                return False
            return board is None or ev["data"].get("board") == board

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        with fake._cond:
            fake._streams.append(self.connection)
            cursor = len(fake.events)
        try:
            self._chunk(":connected\n\n")
            while True:
                with fake._cond:
                    while cursor >= len(fake.events) and self.connection in fake._streams:
                        fake._cond.wait(0.2)
                    if self.connection not in fake._streams:
                        return
                    batch, cursor = fake.events[cursor:], len(fake.events)
                for ev in batch:
                    if matches(ev):
                        self._chunk(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n")
        except OSError:
            return
        finally:
            with fake._cond:
                if self.connection in fake._streams:
                    fake._streams.remove(self.connection)
            self.close_connection = True


def rest_config(hub: FakeFpgahub, **kw: Any) -> Any:
    """A ``RestHubConfig`` aimed at ``hub`` (plain http is allowed to 127.0.0.1 only)."""
    from harness_manager.transports.hub_rest import RestHubConfig

    kw.setdefault("target", "mps3_01_pl")
    return RestHubConfig(url=hub.url, **kw)


def client_for(hub: FakeFpgahub, token: str | None, **kw: Any) -> Any:
    """A ``RestHubClient`` on ``hub`` with ``token`` (None: anonymous), fast retries."""
    from harness_manager.transports.hub_rest import Credential, RestHubClient

    opts = {"retry_backoff_s": (0.01, 0.01), "wait_slice_s": 0.3}
    opts.update(kw.pop("client_opts", {}))
    return RestHubClient(rest_config(hub, **kw), credential=Credential(token, "test"), **opts)


def wait_until(pred: Callable[[], Any], timeout: float = 5.0, step: float = 0.02) -> Any:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(step)
    raise AssertionError("condition not met in time")
