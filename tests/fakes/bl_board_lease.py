"""An in-memory board lease server (net-protocol v0.21, feature ``board_lease``) for HM tests.

``BoardLeaseServer`` is the board's lease table: the acts ``show history acquire heartbeat
release request respond cancel revoke mode`` with relative seconds on the wire, the codes
``leased lease_required lease_held bad_token queue_full local_hold not_holder busy_access``, and
the single-user gate on state-changing verbs: CLAIM FIRST (``locked``), then, in single-user
mode, the lease (``leased`` when someone holds it and no token came, ``lease_required`` when
nobody does, ``bad_token`` for a wrong one). Reads stay open.

``attach(fake)`` plugs one into a pyverify ``FakeShell`` (the claimed-lock lab's ``SlotBoard``)
as an op shim: it wraps ``fake.handle_control``, adds the ``board_lease`` feature, and answers
the ``lease`` verb. Time is ``server.now`` (a number the test moves with ``tick(s)``).

The request/reply field names are HM's reading of pyverify ``board_lease.py`` (F0 stubs): the
contract v0.21 text is not frozen yet, so the shapes are in ONE place, ``BoardLeaseServer._act_*``.
"""

from __future__ import annotations

import secrets
from typing import Any

from tests.fakes.claimed_lock import V22_LOCKED_VERBS

FEATURE = "board_lease"
TTL_S = 3600
REQUEST_WINDOW_S = 120
LOCAL_HOLD_S = 900
QUEUE_MAX = 4
GATED_VERBS = tuple(V22_LOCKED_VERBS) + ("commit",)


def locked_reply(verb: str) -> dict[str, Any]:
    return {"ok": False, "err": f"{verb} locked: board claimed (use ssh)", "code": "locked"}


class BoardLeaseServer:
    """The lease table. ``claimed``: a callable giving whether the board is claimed."""

    def __init__(self, *, name: str = "mps3-01", claimed: Any = lambda: True,
                 mode: str = "open") -> None:
        self.name = name
        self._claimed = claimed
        self.mode = mode
        self.now = 1000.0
        self.holder: dict[str, Any] | None = None     # who, app, token, expires
        self.queue: list[dict[str, Any]] = []         # waiters: id, who, msg, token, at, ...
        self.history: list[dict[str, Any]] = []       # at, event, who, reason, by, prior
        self.local_hold_until = 0.0
        self.mode_since = self.now
        self.mode_by = ""
        self.acts: list[tuple[str, dict[str, Any]]] = []     # every lease act served
        self.seen: list[dict[str, Any]] = []                 # every other request, as received
        self._n = 0

    # -- time -----------------------------------------------------------------------------------

    def tick(self, seconds: float) -> None:
        self.now += seconds
        self._expire()

    def _expire(self) -> None:
        if self.holder and self.holder["expires"] <= self.now:
            self._event("expired", self.holder["who"])
            self.holder = None

    def _event(self, event: str, who: str = "", **extra: Any) -> None:
        self.history.append({"at": self.now, "event": event, "who": who, **extra})

    def _fresh_id(self) -> str:
        self._n += 1
        return f"r{self._n}"

    # -- the verb -------------------------------------------------------------------------------

    def op(self, request: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        self._expire()
        act = str(request.get("act") or "")
        self.acts.append((act, dict(request)))
        fn = getattr(self, f"_act_{act}", None)
        if fn is None:
            return {"ok": False, "err": f"unknown lease act {act!r}", "code": "bad_request"}
        return fn(request)

    def gate(self, verb: str, request: dict[str, Any]) -> dict[str, Any] | None:
        """None to allow a state-changing ``verb`` in single-user mode; else the refusal."""
        if self.mode != "single_user":
            return None
        token = str(request.get("lease") or "")
        held = self.holder
        if held is not None and token == held["token"]:
            return None
        if held is None:
            return {"ok": False, "err": f"{verb} needs the board lease: single-user mode is on "
                                        "and nobody holds it", "code": "lease_required"}
        if not token:
            return {"ok": False, "err": f"{verb} leased: {held['who']} has the board",
                    "code": "leased", "holder": held["who"]}
        return {"ok": False, "err": f"{verb}: that lease token is not valid", "code": "bad_token"}

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
        """The touchscreen override's commit: mode off, the holder ended, a 15 min hold."""
        self.mode = "open"
        who = self.holder["who"] if self.holder else ""
        self.holder = None
        self.queue.clear()
        self.local_hold_until = self.now + LOCAL_HOLD_S
        self._event("lease_break", who, reason="single-user mode was switched off on the touchscreen")
        return {"ok": True}

    # -- reads ----------------------------------------------------------------------------------

    def _holder_view(self, request: dict[str, Any]) -> dict[str, Any] | None:
        h = self.holder
        if h is None:
            return None
        return {"who": h["who"], "app": h["app"], "held_s": int(self.now - h["at"]),
                "expires_in_s": int(h["expires"] - self.now),
                "you": bool(request.get("lease")) and request.get("lease") == h["token"]}

    def _act_show(self, request: dict[str, Any]) -> dict[str, Any]:
        queue = []
        for w in self.queue:
            row = {"id": w["id"], "who": w["who"], "msg": w["msg"], "age_s": int(self.now - w["at"])}
            if w.get("answer"):
                row.update(answer=w["answer"], minutes=w.get("minutes", 0),
                           answer_age_s=int(self.now - w["answer_at"]))
            queue.append(row)
        return {"ok": True, "mode": self.mode, "holder": self._holder_view(request),
                "queue": queue, "local_hold_s": max(0, int(self.local_hold_until - self.now)),
                "reclaim_s": 0, "can_change": bool(self._claimed()), "since_s":
                int(self.now - self.mode_since), "by": self.mode_by, "persisted": True,
                "you": str(request.get("who") or "")}

    def _act_history(self, request: dict[str, Any]) -> dict[str, Any]:
        n = int(request.get("n") or 8)
        events = [{**{k: v for k, v in e.items() if k != "at"}, "age_s": int(self.now - e["at"])}
                  for e in self.history[-n:]]
        return {"ok": True, "events": events}

    # -- the lease ------------------------------------------------------------------------------

    def _act_acquire(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.local_hold_until > self.now:
            return {"ok": False, "err": "someone switched single-user mode off at the board",
                    "code": "local_hold"}
        if self.mode != "single_user":
            return {"ok": False, "err": "single-user mode is off: the board is shared",
                    "code": "lease_required"}
        who = str(request.get("who") or "")
        ttl = int(request.get("ttl_s") or TTL_S)
        ticket = str(request.get("token") or "")
        mine = next((w for w in self.queue if w["who"] == who), None)
        if self.holder is not None:
            if self.holder["who"] == who:
                return self._granted(self.holder["token"], self.holder["expires"] - self.now)
            if mine is None:
                if len(self.queue) >= QUEUE_MAX:
                    return {"ok": False, "err": "the queue is full", "code": "queue_full"}
                mine = {"id": self._fresh_id(), "who": who, "msg": "", "at": self.now,
                        "token": secrets.token_hex(16)}
                self.queue.append(mine)
            return self._queued(mine)
        if mine is not None and self.queue[0] is not mine:
            return self._queued(mine)
        token = mine["token"] if mine is not None else (ticket or secrets.token_hex(16))
        if mine is not None:
            self.queue.remove(mine)
        self.holder = {"who": who, "app": str(request.get("app") or ""), "token": token,
                       "at": self.now, "expires": self.now + ttl}
        self._event("acquired", who)
        return self._granted(token, ttl)

    def _queued(self, w: dict[str, Any]) -> dict[str, Any]:
        pos = self.queue.index(w) + 1
        return {"ok": False, "err": f"queued at position {pos}", "code": "lease_held",
                "granted": False, "position": pos, "token": w["token"],
                "holder": self.holder["who"] if self.holder else ""}

    @staticmethod
    def _granted(token: str, left: float) -> dict[str, Any]:
        return {"ok": True, "granted": True, "token": token, "expires_in_s": int(left)}

    def _holds(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """None when the request's top-level token is the holder's, else the refusal."""
        if self.holder is None:
            return {"ok": False, "err": "nobody holds the board lease", "code": "lease_required"}
        if request.get("lease") != self.holder["token"]:
            return {"ok": False, "err": "that lease token is not valid", "code": "bad_token"}
        return None

    def _act_heartbeat(self, request: dict[str, Any]) -> dict[str, Any]:
        bad = self._holds(request)
        if bad:
            return bad
        ttl = int(request.get("ttl_s") or TTL_S)
        self.holder["expires"] = self.now + ttl            # type: ignore[index]
        return {"ok": True, "expires_in_s": ttl}

    def _act_release(self, request: dict[str, Any]) -> dict[str, Any]:
        bad = self._holds(request)
        if bad:
            return bad
        self._event("released", self.holder["who"])         # type: ignore[index]
        self.holder = None
        return {"ok": True}

    def _act_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.holder is None:
            return {"ok": False, "err": "nobody holds the board lease", "code": "lease_required"}
        who = str(request.get("who") or "")
        w = next((x for x in self.queue if x["who"] == who), None)
        if w is None:
            if len(self.queue) >= QUEUE_MAX:
                return {"ok": False, "err": "the queue is full", "code": "queue_full"}
            w = {"id": self._fresh_id(), "who": who, "msg": "", "at": self.now,
                 "token": secrets.token_hex(16)}
            self.queue.append(w)
        w["msg"] = str(request.get("msg") or "")
        return {"ok": True, "id": w["id"], "window_s": REQUEST_WINDOW_S}

    def _act_respond(self, request: dict[str, Any]) -> dict[str, Any]:
        bad = self._holds(request)
        if bad:
            return {**bad, "code": "not_holder"} if bad["code"] == "lease_required" else bad
        w = next((x for x in self.queue if x["id"] == request.get("id")), None)
        if w is None:
            return {"ok": False, "err": "no such request", "code": "bad_request"}
        if request.get("accept"):
            w["answer"], w["answer_at"] = "release", self.now
            self._event("released", self.holder["who"])      # type: ignore[index]
            self.holder = None
        else:
            w.update(answer="keep", answer_at=self.now, minutes=int(request.get("minutes") or 0))
        return {"ok": True}

    def _act_cancel(self, request: dict[str, Any]) -> dict[str, Any]:
        rid, who = request.get("id"), request.get("who")
        gone = [w for w in self.queue if (rid and w["id"] == rid) or (who and w["who"] == who)]
        for w in gone:
            self.queue.remove(w)
        return {"ok": True, "removed": bool(gone)}

    def _act_revoke(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("confirm_board") != self.name:
            return {"ok": False, "err": f"type the board's name ({self.name}) to force it",
                    "code": "not_holder"}
        prior = self.holder["who"] if self.holder else ""
        if prior:
            self._event("revoked", prior, reason=str(request.get("reason") or ""), by="owner",
                        prior=prior)
        self.holder = None
        return {"ok": True, "revoked": bool(prior), "by": "owner"}

    def _act_mode(self, request: dict[str, Any]) -> dict[str, Any]:
        want = str(request.get("mode") or "")
        if want == "single_user":
            if not self._claimed():
                return {"ok": False, "err": "claim the board first: the lease needs to know "
                                            "whose key is whose", "code": "not_holder"}
            if self.local_hold_until > self.now:
                return {"ok": False, "err": "someone switched single-user mode off at the board",
                        "code": "local_hold"}
            if self.holder is not None:
                return {"ok": False, "err": f"{self.holder['who']} has the board", "code": "leased"}
            self.mode, self.mode_since, self.mode_by = "single_user", self.now, str(request.get("who") or "")
            reply: dict[str, Any] = {"ok": True, "mode": self.mode}
            if request.get("take"):
                ttl = int(request.get("ttl_s") or TTL_S)
                token = secrets.token_hex(16)
                self.holder = {"who": str(request.get("who") or ""), "app": str(request.get("app") or ""),
                               "token": token, "at": self.now, "expires": self.now + ttl}
                self._event("acquired", self.holder["who"])
                reply.update(token=token, expires_in_s=ttl)
            return reply
        if want == "open":
            if self.holder is not None and request.get("lease") != self.holder["token"] \
                    and request.get("confirm_board") != self.name:
                return {"ok": False, "err": f"{self.holder['who']} has the board: type its name "
                                            "to switch it off", "code": "not_holder"}
            if self.holder is not None:
                self._event("revoked", self.holder["who"], reason="single-user mode switched off")
            self.mode, self.mode_since = "open", self.now
            self.holder = None
            self.queue.clear()
            return {"ok": True, "mode": self.mode}
        return {"ok": False, "err": "mode is open or single_user", "code": "bad_request"}


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
