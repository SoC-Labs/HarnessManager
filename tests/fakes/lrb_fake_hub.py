"""LR-B: fpgahub 0.3.0 and its request-note directory in miniature, behind the frozen
HubClient interface (docs/LEASE_REQUESTS.md). Nothing here touches a network or a hub.

``LrbHub`` is the hub's state, shared by every session; ``LrbHub.client(principal)``
is one Harness Manager session's ``HubClient``. The semantics the service relies on:

- a lease's holder is the CALLER's principal; ``--holder`` is ignored (0.3.0);
- an acquire grants a free board, re-grants the same token to the current holder,
  and otherwise queues (FCFS, 1-based positions, re-acquiring keeps the place);
- a release or a revoke promotes the head of the queue (a new token, which the
  promoted principal's next acquire hands back);
- ``lease_revoke`` writes ``lease.released`` and, unless ``history_has_revoke`` is
  False (what fpgahub 0.3.0's per-target history really returns: the admin_revoked
  event carries ``chassis=``, not ``board=``), ``lease.admin_revoked {by, reason,
  prior_holder}`` with fpgahub's `` (by unix:<user>)`` suffix on the reason;
- notes are kept as the objects written (``req``/``ans`` by id).

``LrbHubRef`` is the ``session.hub`` adapter: ``host``, ``target``, ``client``.
``calls`` records ``(principal, verb)`` for every client call, so a test can say
"no revoke happened" or "one ssh call per poll".
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pyverify.lease import Lease

from harness_manager.core.errors import RefusedError, UnreachableError, UsageError
from harness_manager_mps3.hub import LeaseLostError

HOST = "mapstone-dev.ecs.soton.ac.uk"
TARGET = "mps3_01_pl"
BOARD = "mps3_01"
EXPIRES = "2026-09-25T12:00:00+00:00"
_ID = re.compile(r"[A-Za-z0-9_.\-]+")


@dataclass(frozen=True)
class QueueEntry:
    position: int
    holder: str
    user: str


@dataclass(frozen=True)
class LeaseStatus:
    held: bool
    holder: str = ""
    user: str = ""
    expires_at: str = ""
    queue: tuple[QueueEntry, ...] = ()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


class LrbHub:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock                          # the hub's own clock (history timestamps)
        self.current: dict[str, Any] | None = None
        self.queue: list[tuple[str, str]] = []      # (principal, user), head first
        self.notes: dict[str, Any] = {}
        self.answers: dict[str, Any] = {}
        self.history: list[dict[str, Any]] = []
        self.revokes: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.history_has_revoke = True
        self.fail_next: dict[str, Exception] = {}  # verb -> raised once
        self._tokens = 0

    def client(self, principal: str, user: str = "") -> LrbClient:
        return LrbClient(self, principal, user or principal.split("@")[0])

    # -- the hub's side -----------------------------------------------------------------------

    def _token(self) -> str:
        self._tokens += 1
        return f"tok-{self._tokens:04d}-lrb"

    def _log(self, event: str, **kw: Any) -> None:
        self.history.append({"ts": iso(self.clock()), "event": event, "board": TARGET, **kw})

    def grant_to(self, principal: str, user: str = "") -> str:
        """Test helper: ``principal`` holds the board now (e.g. a lease taken elsewhere)."""
        self.current = {"holder": principal, "user": user or principal.split("@")[0],
                        "token": self._token(), "expires_at": EXPIRES}
        self._log("lease.granted", holder=principal)
        return self.current["token"]

    def _promote(self) -> None:
        if self.current is None and self.queue:
            principal, user = self.queue.pop(0)
            self.current = {"holder": principal, "user": user, "token": self._token(),
                            "expires_at": EXPIRES}
            self._log("lease.promoted", holder=principal)

    def position(self, principal: str) -> int:
        for i, (p, _u) in enumerate(self.queue):
            if p == principal:
                return i + 1
        return 0

    def status(self) -> LeaseStatus:
        q = tuple(QueueEntry(i + 1, p, u) for i, (p, u) in enumerate(self.queue))
        c = self.current
        if c is None:
            return LeaseStatus(False, queue=q)
        return LeaseStatus(True, c["holder"], c["user"], c["expires_at"], q)

    def expire(self) -> None:
        self.current = None
        self._log("lease.expired")
        self._promote()


class LrbClient:
    """One session's ``HubClient`` (the frozen interface plus L1's lease verbs)."""

    def __init__(self, hub: LrbHub, principal: str, user: str) -> None:
        self.hub = hub
        self._principal = principal
        self.user = user
        self.host = HOST
        self.target = TARGET

    def _call(self, verb: str) -> None:
        self.hub.calls.append((self._principal, verb))
        err = self.hub.fail_next.pop(verb, None)
        if err is not None:
            raise err

    # -- identity and status --------------------------------------------------------------------

    def principal(self) -> str:
        self._call("principal")
        return self._principal

    def lease_status(self) -> LeaseStatus:
        self._call("lease_status")
        return self.hub.status()

    def lease_show(self) -> LeaseStatus:
        self._call("lease_show")
        return self.hub.status()

    def board_id(self) -> str:
        self._call("board_id")
        return BOARD

    # -- L1's lease verbs (the holder is the caller's principal) ----------------------------------

    def lease_acquire(self, holder: str, *, ttl: int, poll_s: float = 20.0, timeout_s: float = 3600.0,
                      sleep: Callable[[float], None] | None = None,
                      log_fn: Callable[[str], None] | None = None) -> tuple[Any, str]:
        say = log_fn or (lambda _m: None)
        waited = 0.0
        while True:
            self._call("lease_acquire")
            h, me = self.hub, self._principal
            if h.current is None or h.current["holder"] == me:
                if h.current is None:
                    if me in [p for p, _ in h.queue]:
                        h.queue = [(p, u) for p, u in h.queue if p != me]
                    h.current = {"holder": me, "user": self.user, "token": h._token(),
                                 "expires_at": EXPIRES}
                    h._log("lease.granted", holder=me)
                say(f"lease granted: {holder} holds {TARGET}")
                return Lease(token=h.current["token"], holder=holder, target=TARGET), EXPIRES
            if me not in [p for p, _ in h.queue]:
                h.queue.append((me, self.user))
                h._log("lease.queued", holder=me, position=len(h.queue))
            say(f"queued at position {h.position(me)} for {TARGET} as {holder}; "
                f"re-acquiring in {poll_s}s")
            if waited >= timeout_s:
                self.lease_cancel(holder)
                raise UnreachableError(f"gave up waiting for {TARGET} after {timeout_s}s")
            (sleep or time.sleep)(poll_s)
            waited += poll_s

    def lease_heartbeat(self, token: str, holder: str) -> str:
        self._call("lease_heartbeat")
        c = self.hub.current
        if c is None:
            raise LeaseLostError("lease heartbeat: no current lease for board", state="expired")
        if c["holder"] != self._principal or c["token"] != token:
            raise LeaseLostError("lease heartbeat: holder or token does not match current lease",
                                 state="lost")
        return EXPIRES

    def lease_release(self, token: str, holder: str) -> None:
        self._call("lease_release")
        c = self.hub.current
        if c is None or c["holder"] != self._principal or c["token"] != token:
            raise RefusedError("no lease to release")
        self.hub.current = None
        self.hub._log("lease.released", holder=self._principal)
        self.hub._promote()

    def lease_cancel(self, holder: str) -> bool:
        self._call("lease_cancel")
        before = len(self.hub.queue)
        self.hub.queue = [(p, u) for p, u in self.hub.queue if p != self._principal]
        return len(self.hub.queue) != before

    # -- the frozen request verbs -------------------------------------------------------------------

    def lease_revoke(self, reason: str) -> dict[str, Any]:
        self._call("lease_revoke")
        h = self.hub
        by = f"unix:{self.user}"
        full = f"{reason} (by {by})"
        prior = h.current
        h.revokes.append({"reason": reason, "by": self._principal,
                          "prior_holder": prior["holder"] if prior else ""})
        if prior is None:
            return {"revoked": [], "by": by}
        h.current = None
        h._log("lease.released", holder=prior["holder"])
        if h.history_has_revoke:
            h._log("lease.admin_revoked", by=by, reason=full, prior_holder=prior["holder"])
        h._promote()
        return {"revoked": [TARGET], "by": by}

    def lease_history(self, limit: int = 50) -> list[dict[str, Any]]:
        self._call("lease_history")
        return [dict(e) for e in self.hub.history[-limit:]]

    def put_request(self, note: Any) -> None:
        self._call("put_request")
        if not _ID.fullmatch(note.id):
            raise UsageError(f"bad note id {note.id!r}")
        self.hub.notes[note.id] = note

    def list_requests(self) -> list[Any]:
        self._call("list_requests")
        return list(self.hub.notes.values())

    def delete_request(self, request_id: str) -> None:
        self._call("delete_request")
        self.hub.notes.pop(request_id, None)

    def put_answer(self, note: Any) -> None:
        self._call("put_answer")
        self.hub.answers[note.id] = note

    def get_answer(self, request_id: str) -> Any:
        self._call("get_answer")
        return self.hub.answers.get(request_id)


class LrbHubRef:
    """``session.hub`` for one session."""

    def __init__(self, client: LrbClient) -> None:
        self.host = HOST
        self.target = TARGET
        self.client = client


class FakeClock:
    """Wall and monotonic time for every session, advanced by the services' sleeps.

    ``after(s, fn)`` runs ``fn`` once the clock has moved ``s`` seconds on: a
    single-threaded script of "what the other side does meanwhile".
    """

    def __init__(self, t0: float = 1_790_000_000.0) -> None:
        self.t = t0
        self.mono = 1000.0
        self._due: list[tuple[float, int, Callable[[], None]]] = []
        self._n = 0

    def wall(self, skew: float = 0.0) -> Callable[[], float]:
        return lambda: self.t + skew

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.mono += seconds
        while True:
            due = sorted(d for d in self._due if d[0] <= self.t)
            if not due:
                return
            item = due[0]
            self._due.remove(item)
            item[2]()

    sleep = advance

    def after(self, seconds: float, fn: Callable[[], None]) -> None:
        self._n += 1
        self._due.append((self.t + seconds, self._n, fn))
