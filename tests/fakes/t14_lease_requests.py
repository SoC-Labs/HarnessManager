"""Lease requests, force release and leaving the queue (docs/LEASE_REQUESTS.md, frozen),
simulated for the T14 mock daemon (lane LR-D, the web UI).

Lanes LR-A (hub client), LR-B (lease service) and LR-C (daemon routes) build the real
thing in parallel. Until they land, the UI is built and tested against this: the four
frozen routes, the extended ``GET /boards/{bid}/lease``, the ``lease_request`` and
``lease_force`` jobs and the ``lease.*`` events, over the mock's ``WeekPlanSim`` hubs.
Nothing here reaches a hub: no ssh, no fpgahub, no revoke.

The scripted scenarios are knobs on ``LeaseRequestSim`` (``sim.requests`` on the mock):

- requester: ``answer(bid, "keep", minutes=15, message=...)`` or ``answer(bid, "release")``
  plays the holder's answer; ``advance(bid, seconds)`` moves the request's clock on (the
  fake clock: no test waits two minutes); ``queue_ahead(bid, principal)`` puts someone
  before us; ``refuse_force = "why"`` makes the next force a 409 the page did not foresee
  (the daemon caches ``GET /lease`` for 10 s, so the page can be behind the hub);
  ``announce_force = False`` drops the ``lease.force_available`` event;
- holder: ``incoming(bid, by=..., message=...)`` is another session asking for our lease;
- victim: ``taken(bid, by=..., reason=...)`` is another session force-releasing it.
"""

from __future__ import annotations

import getpass
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)

API = "/api/v1"

#: The four routes docs/LEASE_REQUESTS.md adds (daemon module hub_api, lane LR-C).
LEASE_REQUEST_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/boards/{bid}/lease/request"),
    ("POST", "/boards/{bid}/lease/respond"),
    ("POST", "/boards/{bid}/lease/force"),
    ("DELETE", "/boards/{bid}/lease/queue"),
)

WINDOW_S = 120                      # a request's deadline: created_at + 120 s
KEEP_MINUTES = (5, 15, 30, 60)
NOTE_MAX = 4096                     # a note on the hub is at most 4 KiB


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat()


def me() -> str:
    """This client's principal, as the week-plan sim names our lease holder."""
    return f"{getpass.getuser()}@harness-manager"


def _user_host(principal: str) -> tuple[str, str]:
    user, _, host = principal.partition("@")
    return user, host


class LeaseRequestSim:
    """The request notes, answers, queue and revokes of every simulated hub."""

    def __init__(self, week: Any) -> None:
        self.week = week                         # l3_week_plan.WeekPlanSim (its ``hubs``)
        self._lock = threading.RLock()
        self.window_s = WINDOW_S
        self.outgoing: dict[str, dict[str, Any]] = {}      # bid -> our request
        self.ahead: dict[str, list[str]] = {}               # bid -> principals queued before us
        self.inbox: dict[str, list[dict[str, Any]]] = {}   # bid -> requests for our lease
        self.answers: dict[str, dict[str, dict[str, Any]]] = {}   # bid -> id -> our answer
        self.last_taken: dict[str, dict[str, Any]] = {}    # bid -> {by, reason, at}
        self.revokes: list[dict[str, Any]] = []            # what `lease revoke` would have run
        self.refuse_force = ""
        #: False: no lease.force_available event (the page must find out by reading at zero)
        self.announce_force = True
        self._watch: threading.Thread | None = None

    # -- helpers -----------------------------------------------------------------------------

    def publish(self, topic: str, bid: str, data: dict[str, Any]) -> None:
        self.week.publish(topic, bid, data)

    def hub(self, bid: str) -> dict[str, Any]:
        hub = self.week.hubs.get(bid)
        if hub is None:
            raise UnavailableError("lease", f"{bid} is not behind a hub: there is no lease to "
                                            "request")
        return hub

    @staticmethod
    def _deadline(req: dict[str, Any]) -> float:
        return req["created"] + req["window"]

    def _keep_until(self, req: dict[str, Any]) -> float | None:
        a = req.get("answer")
        if not a or a["answer"] != "keep":
            return None
        return a["at_epoch"] + 60 * a["minutes"]

    def _position(self, bid: str) -> int:
        return len(self.ahead.get(bid, [])) + 1

    def force_state(self, bid: str, now: float | None = None) -> tuple[bool, str]:
        """(available, reason) by the frozen rule: the deadline passed; no answer, or a keep
        that ran out; and we are at the head of the queue."""
        now = time.time() if now is None else now
        req = self.outgoing.get(bid)
        if req is None:
            return False, "there is no request of yours to force"
        hub = self.week.hubs.get(bid) or {}
        lease = hub.get("lease")
        if not lease or lease.get("mine"):
            return False, "nobody else holds the lease"
        holder = lease["holder"]
        deadline = self._deadline(req)
        if now < deadline:
            left = int(deadline - now + 0.999)
            return False, (f"{holder} has {left} s left to answer (until "
                           f"{iso(deadline)[11:19]} UTC)")
        until = self._keep_until(req)
        if until is not None and now < until:
            mins = max(1, round((until - now) / 60))
            return False, (f"{holder} answered: keep for {req['answer']['minutes']} min; that "
                           f"runs out in {mins} min")
        ahead = self.ahead.get(bid) or []
        if ahead:
            return False, (f"you are position {len(ahead) + 1} in the queue: a revoke promotes "
                           f"the head, so {ahead[0]} would get the board")
        return True, ""

    # -- knobs: the requester's side -------------------------------------------------------------

    def answer(self, bid: str, answer: str, *, minutes: int = 0, message: str = "") -> None:
        """The holder answers our request (their session writes ans-<id>.json)."""
        with self._lock:
            req = self.outgoing[bid]
            now = time.time()
            req["answer"] = {"answer": answer, "minutes": minutes if answer == "keep" else 0,
                             "message": message, "at": iso(now), "at_epoch": now}
            req["force_published"] = False
            hub = self.hub(bid)
        self.publish("lease.answered", bid, {"id": req["id"], "answer": answer,
                                             "minutes": req["answer"]["minutes"],
                                             "message": message})
        if answer == "release":
            with self._lock:
                hub["lease"] = None
            self._promote(bid)

    def advance(self, bid: str, seconds: float) -> None:
        """The fake clock: our request (and any answer) is ``seconds`` older."""
        with self._lock:
            req = self.outgoing[bid]
            req["created"] -= seconds
            if req.get("answer"):
                req["answer"]["at_epoch"] -= seconds
                req["answer"]["at"] = iso(req["answer"]["at_epoch"])

    def queue_ahead(self, bid: str, principal: str = "carol@lab-pc-09") -> None:
        with self._lock:
            self.ahead.setdefault(bid, []).append(principal)

    def clear_ahead(self, bid: str) -> None:
        with self._lock:
            self.ahead.pop(bid, None)

    # -- knobs: the holder's and the victim's side -------------------------------------------------

    def incoming(self, bid: str, *, by: str = "bob@lab-pc-02",
                 message: str = "need it for the 15:00 demo", age_s: float = 0) -> str:
        """Another session asks for our lease: it queues and writes req-<id>.json."""
        now = time.time() - age_s
        user, host = _user_host(by)
        note = {"id": f"r{uuid.uuid4().hex[:8]}", "by": by, "user": user, "host": host,
                "message": message, "created_at": iso(now), "deadline_at": iso(now + self.window_s)}
        with self._lock:
            self.inbox.setdefault(bid, []).append(note)
        self.publish("lease.wanted", bid, {k: note[k] for k in (
            "id", "by", "user", "host", "message", "deadline_at")})
        return note["id"]

    def withdraw_incoming(self, bid: str, request_id: str) -> None:
        """The requester left the queue: their note is gone."""
        with self._lock:
            self.inbox[bid] = [n for n in self.inbox.get(bid, []) if n["id"] != request_id]

    def taken(self, bid: str, *, by: str = "bob@lab-pc-02",
              reason: str | None = None) -> dict[str, Any]:
        """Another session force-released our lease (fpgahub audit: lease.admin_revoked).
        ``reason=""``: no revoke note was found (LR-A: ``by`` is then the next holder)."""
        now = time.time()
        with self._lock:
            hub = self.hub(bid)
            prior = hub["lease"]
            user, _ = _user_host(by)
            hub["lease"] = {"target": hub["target"], "holder": by, "user": user,
                            "expires_at": iso(now + 3600), "mine": False}
            made = iso(now - self.window_s - 5)
            record = {"by": by, "at": iso(now), "reason": reason if reason is not None else (
                f"force-released by {by} via Harness Manager: no answer to a request made at "
                f"{made}")}
            self.last_taken[bid] = record
            self.inbox.pop(bid, None)
        self.publish("lease.state", bid, {"target": hub["target"], "state": "lost",
                                          "holder": (prior or {}).get("holder", me()),
                                          "expires_at": ""})
        self.publish("lease.taken", bid, dict(record))
        return record

    # -- the view ----------------------------------------------------------------------------------

    def view(self, bid: str) -> dict[str, Any]:
        """The keys docs/LEASE_REQUESTS.md adds to ``GET /boards/{bid}/lease``."""
        with self._lock:
            hub = self.week.hubs.get(bid)
            out: dict[str, Any] = {"queue": [], "request": None, "incoming": [],
                                   "taken": dict(self.last_taken[bid])
                                   if bid in self.last_taken else None}
            if hub is None:
                return out
            queue: list[tuple[str, bool]] = []
            req = self.outgoing.get(bid)
            if req is not None:
                queue = [(p, False) for p in self.ahead.get(bid, [])] + [(me(), True)]
            lease = hub.get("lease")
            if lease and lease.get("mine"):
                queue = [(n["by"], False) for n in self.inbox.get(bid, [])]
                out["incoming"] = [dict(n) for n in self.inbox.get(bid, [])]
            out["queue"] = [{"position": i, "holder": p, "user": _user_host(p)[0], "mine": mine}
                            for i, (p, mine) in enumerate(queue, start=1)]
            if req is not None:
                available, why = self.force_state(bid)
                a = req.get("answer")
                out["request"] = {
                    "id": req["id"], "message": req["message"],
                    "created_at": iso(req["created"]),
                    "deadline_at": iso(self._deadline(req)),
                    "position": self._position(bid),
                    "answer": {k: a[k] for k in ("answer", "minutes", "message", "at")}
                    if a else None,
                    "force_available": available, "force_reason": why,
                }
            return out

    # -- the service ---------------------------------------------------------------------------------

    def _promote(self, bid: str) -> None:
        """The lease went free and we head the queue: our queued acquire is promoted."""
        with self._lock:
            hub = self.hub(bid)
            req = self.outgoing.pop(bid, None)
            self.ahead.pop(bid, None)
            hub["lease"] = {"target": hub["target"], "holder": me(), "user": getpass.getuser(),
                            "expires_at": iso(time.time() + (req or {}).get("ttl", 3600)),
                            "mine": True}
            if req is not None:
                req["held"].set()
        self.publish("lease.state", bid, {"target": hub["target"], "state": "held",
                                          "holder": me(), "expires_at": hub["lease"]["expires_at"]})

    def _watcher(self) -> None:
        """lease.force_available at the deadline (or when a keep runs out), once per turn."""
        while True:
            time.sleep(0.1)
            with self._lock:
                due = []
                for bid, req in self.outgoing.items():
                    available, _ = self.force_state(bid)
                    if available and not req.get("force_published"):
                        req["force_published"] = True
                        due.append((bid, req["id"]))
            for bid, rid in due:
                if self.announce_force:
                    self.publish("lease.force_available", bid, {"id": rid})

    def start_watch(self) -> None:
        with self._lock:
            if self._watch is None:
                self._watch = threading.Thread(target=self._watcher, daemon=True,
                                               name="t14-lease-watch")
                self._watch.start()

    def request(self, bid: str, message: str, ttl: int, progress: Any) -> dict[str, Any]:
        hub = self.hub(bid)
        with self._lock:
            if hub["lease"] and hub["lease"]["mine"]:
                return {"lease": {k: hub["lease"][k] for k in ("target", "holder", "expires_at")}}
            held, cancel = threading.Event(), threading.Event()
            req = {"id": f"q{uuid.uuid4().hex[:8]}", "message": message, "created": time.time(),
                   "window": self.window_s, "ttl": ttl, "answer": None, "held": held,
                   "cancel": cancel, "force_published": False}
            self.outgoing[bid] = req
        progress("queued", self._position(bid), 0)
        self.publish("lease.state", bid, {"target": hub["target"], "state": "queued",
                                          "holder": me(), "expires_at": ""})
        if hub["lease"] is None:                 # nobody holds it: promoted at once
            self._promote(bid)
        else:
            progress("notified", 0, 0)
        self.start_watch()
        stop = time.monotonic() + 600
        answered = force = False
        while time.monotonic() < stop:
            with self._lock:
                free = hub["lease"] is None and not self.ahead.get(bid) and bid in self.outgoing
            if free:                             # the holder released (or it lapsed)
                self._promote(bid)
            if held.wait(0.05):
                progress("held", 1, 1)
                lease = hub["lease"]
                return {"lease": {k: lease[k] for k in ("target", "holder", "expires_at")}}
            if cancel.is_set():
                raise ActionFailedError(f"the request for {hub['target']} was withdrawn: this "
                                        "client left the queue", hint="request it again when "
                                                                      "you want the board")
            a = req["answer"]
            if a and a["answer"] == "keep" and not answered:
                answered = True
                progress("answered", 0, 0)
                return {"answered": {k: a[k] for k in ("answer", "minutes", "message", "at")}}
            if not force and req.get("force_published"):
                force = True
                progress("force-available", 0, 0)
        raise HeldError(f"{hub['target']} is leased to {hub['lease']['holder']}",
                        holder=hub["lease"]["holder"], hint="the mock's queue timed out")

    def leave(self, bid: str) -> bool:
        with self._lock:
            req = self.outgoing.pop(bid, None)
            self.ahead.pop(bid, None)
        if req is None:
            return False
        req["cancel"].set()
        self.publish("lease.left", bid, {})
        return True

    def respond(self, bid: str, body: dict[str, Any]) -> None:
        rid, answer = body.get("id"), body.get("answer")
        if not isinstance(rid, str) or not rid:
            raise UsageError("the request needs 'id'", hint="GET .../lease lists incoming "
                                                              "requests with their id")
        if answer not in ("release", "keep"):
            raise UsageError(f"answer must be 'release' or 'keep', not {answer!r}")
        minutes = body.get("minutes", 0)
        if answer == "keep" and (isinstance(minutes, bool) or minutes not in KEEP_MINUTES):
            raise UsageError(f"keep needs minutes of 5, 15, 30 or 60, not {minutes!r}")
        message = body.get("message") or ""
        if not isinstance(message, str) or len(message.encode()) > NOTE_MAX // 2:
            raise UsageError("message must be text of at most 2 KiB (a note is at most 4 KiB)")
        hub = self.hub(bid)
        with self._lock:
            note = next((n for n in self.inbox.get(bid, []) if n["id"] == rid), None)
            if note is None:
                raise AbsentError(f"no request {rid!r} for {hub['target']}",
                                  hint="it was withdrawn, or the requester has the board")
            if not hub["lease"] or not hub["lease"]["mine"]:
                raise RefusedError(f"this client does not hold {hub['target']}: only the holder "
                                   "answers")
            self.answers.setdefault(bid, {})[rid] = {"answer": answer, "minutes": minutes
                                                     if answer == "keep" else 0,
                                                     "message": message, "at": iso(time.time())}
            if answer == "release":
                released = hub["lease"]
                hub["lease"] = {"target": hub["target"], "holder": note["by"],
                                "user": note["user"], "expires_at": iso(time.time() + 3600),
                                "mine": False}
                self.inbox.pop(bid, None)
        if answer == "release":
            self.publish("lease.state", bid, {"target": hub["target"], "state": "released",
                                              "holder": released["holder"], "expires_at": ""})

    def check_force(self, bid: str, body: dict[str, Any]) -> None:
        """Before any revoke: USAGE without confirm, then the frozen availability rule."""
        if body.get("confirm") is not True:
            raise UsageError("force needs confirm: true",
                             hint="it kicks the holder off the board now; the UI asks first")
        req = self.outgoing.get(bid)
        if req is None:
            raise RefusedError("there is no request of yours to force: request the board first")
        if self.refuse_force:
            why, self.refuse_force = self.refuse_force, ""
            raise RefusedError(why, hint="GET .../lease shows the hub's view now")
        available, why = self.force_state(bid)
        if not available:
            if time.time() < self._deadline(req):
                raise UnavailableError("lease_force", why)
            raise RefusedError(f"force is not available: {why}")

    def force(self, bid: str, progress: Any) -> dict[str, Any]:
        hub = self.hub(bid)
        with self._lock:
            req = self.outgoing[bid]
            prior = hub["lease"]["holder"]
            reason = (f"force-released by {me()} via Harness Manager: no answer to a request "
                      f"made at {iso(req['created'])}")
            self.revokes.append({"board": hub.get("board") or hub["target"].rsplit("_", 1)[0],
                                 "target": hub["target"], "reason": reason,
                                 "prior_holder": prior})
            hub["lease"] = None
        progress("revoke", 0, 1)
        self._promote(bid)
        progress("held", 1, 1)
        lease = hub["lease"]
        return {"lease": {k: lease[k] for k in ("target", "holder", "expires_at")},
                "revoked": [prior], "by": me()}


def register(app: FastAPI, state: Any, sim: LeaseRequestSim, ok: Any, accepted: Any) -> None:
    """The four frozen routes (``GET /lease`` gains ``sim.view`` in l3_week_plan)."""
    jobs = state.jobs

    @app.post(f"{API}/boards/{{bid}}/lease/request", status_code=202)
    def lease_request(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        message = body.get("message") or ""
        if not isinstance(message, str) or len(message.encode()) > NOTE_MAX // 2:
            raise UsageError("message must be text of at most 2 KiB (a note is at most 4 KiB)")
        ttl = body.get("ttl_s", 3600)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= 86400:
            raise UsageError(f"ttl_s must be whole seconds from 60 to 86400, not {ttl!r}")
        state.session(bid)
        sim.hub(bid)
        if bid in sim.outgoing:
            raise RefusedError(f"{bid} already has a request of yours waiting",
                               hint="leave the queue first (DELETE .../lease/queue)")
        return accepted(jobs.start(bid, "lease_request",
                                   lambda progress: sim.request(bid, message, ttl, progress)))

    @app.post(f"{API}/boards/{{bid}}/lease/respond")
    def lease_respond(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:  # noqa: B008
        state.session(bid)
        sim.respond(bid, body)
        return ok()

    @app.post(f"{API}/boards/{{bid}}/lease/force", status_code=202)
    def lease_force(bid: str, body: dict[str, Any] = Body(default_factory=dict)) -> JSONResponse:  # noqa: B008
        state.session(bid)
        sim.hub(bid)
        sim.check_force(bid, body)
        # Our own queued request job still runs (it is promoted by the revoke): force runs
        # beside it, never beside anything else.
        return accepted(jobs.start(bid, "lease_force", lambda progress: sim.force(bid, progress),
                                   beside=("lease_request",)))

    @app.delete(f"{API}/boards/{{bid}}/lease/queue")
    def lease_leave(bid: str) -> dict[str, Any]:
        state.session(bid)
        sim.hub(bid)
        return ok(left=sim.leave(bid))
