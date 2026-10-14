"""An in-memory board lease server (net-protocol v0.21, feature ``board_lease``) for HM tests.

``BoardLeaseServer`` is the board's lease table, held to the FROZEN contract ("Board lease and
panel access"): the acts ``show history acquire heartbeat release request respond cancel revoke
mode`` with relative seconds on the wire; the codes ``leased lease_required bad_token
busy_access`` on gated verbs and ``lease_held queue_full not_holder bad_token local_hold denied
busy invalid busy_access`` on the verb; and the one mutation gate on state-changing verbs:
panel access (``busy_access``) first, then CLAIM (``locked``), then, in single-user mode, the
lease. Reads stay open. A queued ``acquire`` is ``ok:true, granted:false, position, id, code
"lease_held"``; when the holder goes the queue head is RESERVED for 120 s and only its acquire is
granted. ``show`` has ``mode persistent holder queue local_hold_s reclaim_s``; ``history`` events
are ``{ev, who, age_s, info}``.

``attach(fake)`` plugs one into a pyverify ``FakeShell`` (the claimed-lock lab's ``SlotBoard``)
as an op shim: it wraps ``fake.handle_control``, adds the ``board_lease`` feature, and answers
the ``lease`` verb. Time is ``server.now`` (a number the test moves with ``tick(s)``).
"""

from __future__ import annotations

import secrets
from typing import Any

from tests.fakes.claimed_lock import V22_LOCKED_VERBS

FEATURE = "board_lease"
TTL_S = 3600
REQUEST_WINDOW_S = 120
RESERVE_S = 120
LOCAL_HOLD_S = 900
RECLAIM_S = 300
QUEUE_MAX = 4
GATED_VERBS = tuple(V22_LOCKED_VERBS) + ("commit",)
HISTORY_KEEP = 64


def locked_reply(verb: str) -> dict[str, Any]:
    return {"ok": False, "err": f"{verb} locked: board claimed (use ssh)", "code": "locked"}


def _no(err: str, code: str) -> dict[str, Any]:
    return {"ok": False, "err": err, "code": code}


class BoardLeaseServer:
    """The lease table. ``claimed``: a callable giving whether the board is claimed (the
    owner's key is the one that claimed it, so a claimed board is an owner-writable one)."""

    def __init__(self, *, name: str = "mps3-01", claimed: Any = lambda: True,
                 mode: str = "open", persistent: bool = True) -> None:
        self.name = name
        self._claimed = claimed
        self.mode = mode
        self.persistent = persistent
        self.now = 1000.0
        self.holder: dict[str, Any] | None = None     # who, app, token, at, expires
        self.queue: list[dict[str, Any]] = []         # waiters: id, who, msg, at, req_at
        self.history: list[dict[str, Any]] = []       # at, ev, who, info
        self.panel_events: list[dict[str, Any]] = []  # k: access_arm / lease_break
        self.reserve_start = 0.0                      # when the holder went (queue reserved from here)
        self.local_hold_until = 0.0
        self.access_busy = False                      # an override attempt runs at the panel
        self.acts: list[tuple[str, dict[str, Any]]] = []     # every lease act served
        self.seen: list[dict[str, Any]] = []                 # every other request, as received
        self._n = 0

    # -- time -----------------------------------------------------------------------------------

    def tick(self, seconds: float) -> None:
        self.now += seconds
        self._expire()

    def _expire(self) -> None:
        if self.holder and self.holder["expires"] <= self.now:
            self._event("expire", self.holder["who"])
            self._holder_goes()

    def _event(self, ev: str, who: str = "", info: str = "") -> None:
        self.history.append({"at": self.now, "ev": ev, "who": who, "info": info})
        del self.history[:-HISTORY_KEEP]

    def _holder_goes(self) -> None:
        """The holder is gone: the queue head is reserved for ``RESERVE_S``."""
        self.holder = None
        if self.queue:
            self.reserve_start = self.now
            self._event("reserve", self.queue[0]["who"])

    def _fresh_id(self) -> str:
        self._n += 1
        return f"{self._n:08x}"

    # -- the verb -------------------------------------------------------------------------------

    def op(self, request: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        self._expire()
        act = str(request.get("act") or "")
        self.acts.append((act, dict(request)))
        fn = getattr(self, f"_act_{act}", None)
        if fn is None:
            return _no("invalid act (show history acquire heartbeat release request respond "
                       "cancel revoke mode)", "invalid")
        if "lease" in request and not isinstance(request["lease"], str):
            return _no("bad args: lease must be a string", "bad_args")
        if self.access_busy and act not in ("show", "history"):
            return _no("lease refused: board access in progress at the panel", "busy_access")
        return fn(request)

    def gate(self, verb: str, request: dict[str, Any]) -> dict[str, Any] | None:
        """None to allow a state-changing ``verb``; else the refusal (panel access, then the lease
        in single-user mode; the claim gate is the caller's)."""
        if self.access_busy:
            return _no(f"{verb} refused: board access in progress at the panel", "busy_access")
        if self.mode != "single_user":
            return None
        token = str(request.get("lease") or "")
        held = self.holder
        if held is not None and token == held["token"]:
            held["idle_at"] = self.now
            return None
        if held is None:
            return _no(f"{verb} leased: single-user mode (acquire the board lease first)"
                       if not token else f"{verb} leased: lease token not current",
                       "lease_required" if not token else "bad_token")
        if not token:
            return _no(f"{verb} leased: held by {held['who']} "
                       f"({int(held['expires'] - self.now)} s left)", "leased")
        return _no(f"{verb} leased: lease token not current", "bad_token")

    def gated(self, request: dict[str, Any]) -> str:
        """The verb name when ``request`` is state-changing (the claim lock's list), else ""."""
        op = str(request.get("op"))
        if op in GATED_VERBS:
            return op
        if op == "display" and request.get("owner") != "query" and request.get("act") != "query":
            return op
        if op == "touch_cal" and request.get("act") in ("set", "default"):
            return op
        return ""

    def local_break(self) -> dict[str, Any]:
        """The touchscreen override's commit: the lease ends, mode ``open``, 900 s hold on
        switching it on; the queue is kept (not promoted). Panel event ``lease_break``."""
        self.mode = "open"
        self.holder = None
        self.reserve_start = 0.0
        self.local_hold_until = self.now + LOCAL_HOLD_S
        self.access_busy = False
        self._event("break", "", "")
        self.panel_events.append({"k": "lease_break", "at": self.now})
        return {"ok": True}

    def access_arm(self) -> None:
        """An override attempt starts at the panel (gated verbs get ``busy_access``)."""
        self.access_busy = True
        self.panel_events.append({"k": "access_arm", "at": self.now})

    # -- reads ----------------------------------------------------------------------------------

    def _is_holder(self, request: dict[str, Any]) -> bool:
        h = self.holder
        return h is not None and bool(request.get("lease")) and request.get("lease") == h["token"]

    def _holder_view(self, request: dict[str, Any]) -> dict[str, Any] | None:
        h = self.holder
        if h is None:
            return None
        return {"who": h["who"], "app": h["app"], "held_s": int(self.now - h["at"]),
                "expires_in_s": int(h["expires"] - self.now), "you": self._is_holder(request)}

    def _show(self, request: dict[str, Any]) -> dict[str, Any]:
        queue = [{"id": w["id"], "who": w["who"], "msg": w["msg"], "age_s": int(self.now - w["at"])}
                 for w in self.queue]
        return {"ok": True, "op": "lease", "act": "show", "mode": self.mode,
                "persistent": self.persistent, "owner": bool(self._claimed()), "holder": self._holder_view(request),
                "queue": queue, "local_hold_s": max(0, int(self.local_hold_until - self.now)),
                "reclaim_s": 0}

    def _act_show(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._show(request)

    def _act_history(self, request: dict[str, Any]) -> dict[str, Any]:
        n = max(1, min(8, int(request.get("n") or 8)))
        events = [{"ev": e["ev"], "who": e["who"], "age_s": int(self.now - e["at"]),
                   "info": e["info"]} for e in self.history[-n:]]
        return {"ok": True, "op": "lease", "act": "history", "events": events}

    # -- the lease ------------------------------------------------------------------------------

    def _grant(self, who: str, app: str, ttl: int) -> dict[str, Any]:
        token = secrets.token_hex(16)
        self.holder = {"who": who, "app": app, "token": token, "at": self.now,
                       "expires": self.now + ttl, "idle_at": self.now}
        self.reserve_start = 0.0
        self._event("grant", who)
        return {"ok": True, "op": "lease", "act": "acquire", "granted": True, "token": token,
                "expires_in_s": ttl}

    def _queued(self, w: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "op": "lease", "act": "acquire", "granted": False,
                "position": self.queue.index(w) + 1, "id": w["id"], "code": "lease_held"}

    def _act_acquire(self, request: dict[str, Any]) -> dict[str, Any]:
        who = str(request.get("who") or "")
        if not who:
            return _no("invalid who argument", "invalid")
        ttl = int(request.get("ttl_s") or TTL_S)
        if not 300 <= ttl <= 14400:
            return _no("invalid ttl_s argument", "invalid")
        if self.holder is not None and self.holder["who"] == who:
            return _no("lease already held by you", "lease_held")
        mine = next((w for w in self.queue if w["who"] == who), None)
        if self.holder is None and self._may_take(mine):
            if mine is not None:
                self.queue.remove(mine)
            return self._grant(who, str(request.get("app") or ""), ttl)
        if mine is None:
            if len(self.queue) >= QUEUE_MAX:
                return _no("lease queue full (4 waiting)", "queue_full")
            mine = {"id": self._fresh_id(), "who": who, "msg": "", "at": self.now, "req_at": 0.0}
            self.queue.append(mine)
            self._event("queue", who)
        return self._queued(mine)

    def _may_take(self, mine: dict[str, Any] | None) -> bool:
        """The board is free: the queue head holds the reservation for ``RESERVE_S``, then it passes
        to the next waiter; once every waiter has had its 120 s anybody may take it."""
        if not self.queue or not self.reserve_start:
            return True
        idx = int((self.now - self.reserve_start) // RESERVE_S)
        return idx >= len(self.queue) or mine is self.queue[idx]

    def _holds(self, request: dict[str, Any], act: str) -> dict[str, Any] | None:
        """None when the request's top-level token is the holder's, else the refusal."""
        token = request.get("lease")
        if not isinstance(token, str) or len(token) != 32:
            return _no(f"lease {act}: not the holder", "not_holder")
        if self.holder is None or token != self.holder["token"]:
            return _no(f"lease {act}: lease token not current", "bad_token")
        return None

    def _act_heartbeat(self, request: dict[str, Any]) -> dict[str, Any]:
        bad = self._holds(request, "heartbeat")
        if bad:
            return bad
        ttl = int(request.get("ttl_s") or TTL_S)
        if not 300 <= ttl <= 14400:
            return _no("invalid ttl_s argument", "invalid")
        self.holder["expires"] = self.now + ttl            # type: ignore[index]
        self.holder["idle_at"] = self.now                  # type: ignore[index]
        self._event("renew", self.holder["who"])           # type: ignore[index]
        return self._show(request)

    def _act_release(self, request: dict[str, Any]) -> dict[str, Any]:
        bad = self._holds(request, "release")
        if bad:
            return bad
        self._event("release", self.holder["who"])         # type: ignore[index]
        self._holder_goes()
        return {"ok": True, "op": "lease", "act": "release"}

    def _act_request(self, request: dict[str, Any]) -> dict[str, Any]:
        who = str(request.get("who") or "")
        msg = str(request.get("msg") or "")
        if not who or len(msg) > 48:
            return _no("invalid msg argument" if who else "invalid who argument", "invalid")
        w = next((x for x in self.queue if x["who"] == who), None)
        if w is None:
            if len(self.queue) >= QUEUE_MAX:
                return _no("lease queue full (4 waiting)", "queue_full")
            w = {"id": self._fresh_id(), "who": who, "msg": "", "at": self.now, "req_at": 0.0}
            self.queue.append(w)
        w["msg"], w["req_at"] = msg or "(asking)", self.now
        self._event("request", who)
        return {"ok": True, "op": "lease", "act": "request", "id": w["id"]}

    def _act_respond(self, request: dict[str, Any]) -> dict[str, Any]:
        bad = self._holds(request, "respond")
        if bad:
            return bad
        w = next((x for x in self.queue if x["id"] == request.get("id")), None)
        if w is None:
            return _no("invalid id: no such request", "invalid")
        who = self.holder["who"]                            # type: ignore[index]
        if request.get("accept"):
            self._event("give", who)
            self._holder_goes()
            return {"ok": True, "op": "lease", "act": "respond"}
        minutes = int(request.get("minutes") or 0)
        if not 5 <= minutes <= 60:
            return _no("invalid minutes argument", "invalid")
        self._event("keep", who, f"keep {minutes}")
        w["msg"], w["req_at"] = "", 0.0
        self.holder["expires"] = self.now + minutes * 60    # type: ignore[index]
        return {"ok": True, "op": "lease", "act": "respond", "expires_in_s": minutes * 60}

    def _act_cancel(self, request: dict[str, Any]) -> dict[str, Any]:
        w = next((x for x in self.queue if x["id"] == request.get("id")), None)
        if w is None:
            return _no("invalid id: no such request", "invalid")
        self.queue.remove(w)
        self._event("cancel", w["who"])
        return {"ok": True, "op": "lease", "act": "cancel"}

    def _act_revoke(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("confirm") != self.name:
            return _no("lease revoke: confirm must be the board's label", "invalid")
        head = self.queue[0] if self.queue else None
        if head is None or not head.get("req_at"):
            return _no("lease revoke: no unanswered request", "busy")
        left = int(REQUEST_WINDOW_S - (self.now - head["req_at"]))
        if left > 0:
            return _no(f"lease revoke: the holder has {left} s to answer", "busy")
        if self.holder is not None:
            self._event("revoke", head["who"], f"from {self.holder['who']}")   # actor, then info
            self._holder_goes()
        return {"ok": True, "op": "lease", "act": "revoke"}

    def _act_mode(self, request: dict[str, Any]) -> dict[str, Any]:
        want = str(request.get("mode") or "")
        owner = bool(self._claimed())
        if want == "single_user":
            if not owner:
                return _no("lease mode: only the owner key can turn single-user on", "denied")
            if self.local_hold_until > self.now:
                return _no(f"lease mode: ended at the panel "
                           f"{int(LOCAL_HOLD_S - (self.local_hold_until - self.now))} s ago "
                           f"(wait {int(self.local_hold_until - self.now)} s)", "local_hold")
            ttl = int(request.get("ttl_s") or TTL_S)
            if not 300 <= ttl <= 14400:
                return _no("invalid ttl_s argument", "invalid")
            self.mode = "single_user"
            self._event("mode_on", "owner#0000")
            who = "owner#0000"
            reply = {**self._grant(who, "", ttl), "act": "mode"}
            return {**self._show({"lease": reply["token"]}), "act": "mode",
                    "token": reply["token"], "expires_in_s": ttl}
        if want == "open":
            if not (owner or self._is_holder(request)):
                return _no("lease mode: not the holder", "not_holder")
            if self.holder is not None:
                self._event("release", self.holder["who"])
            self.mode = "open"
            self._event("mode_off", "owner" if owner else "")
            self.holder = None
            self.queue.clear()
            self.reserve_start = 0.0
            return {**self._show(request), "act": "mode"}
        return _no("invalid mode argument", "invalid")


def attach(fake: Any, server: BoardLeaseServer | None = None, *, on_lease: Any = None,
           **kw: Any) -> BoardLeaseServer:
    """Make ``fake`` (a started pyverify ``FakeShell`` subclass) a board with the lease verb.

    ``fake.handle_control`` is wrapped: the ``lease`` verb is the server's; any other request
    meets the claim gate first (a non-local peer on a ``claim_gate_all`` claimed board gets
    ``locked`` for the lease writes), then the single-user gate on state-changing verbs.
    ``on_lease(request, peer)`` hears each ``lease`` request (the other verbs reach the wrapped
    handler, which a test may already be recording)."""
    srv = server or BoardLeaseServer(claimed=lambda: bool(getattr(fake, "ssh_claimed", False)), **kw)
    fake.board_lease = srv
    features = tuple(getattr(fake, "features", ()) or ())
    if FEATURE not in features:
        fake.features = (*features, FEATURE)
    inner = fake.handle_control
    if not hasattr(fake, "gated"):
        fake.gated = []
    reads = ("show", "history")

    def locked(peer: str | None) -> bool:
        return bool(getattr(fake, "claim_gate_all", False) and getattr(fake, "ssh_claimed", False)
                    and not fake._store_local(peer))

    def served(request: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        if request.get("op") == "lease":
            act = str(request.get("act") or "")
            if on_lease is not None:
                on_lease(request, peer)
            if act not in reads and locked(peer):
                fake.gated.append(("lease", str(peer)))
                return locked_reply("lease")
            return srv.op(request, peer)
        srv.seen.append(dict(request))
        verb = srv.gated(request)
        if verb and not locked(peer):
            refusal = srv.gate(verb, request)
            if refusal is not None:
                fake.gated.append((verb, str(peer)))
                return refusal
        return inner(request, peer=peer)

    fake.handle_control = served
    return srv
