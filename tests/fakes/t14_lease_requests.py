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
  ``answer_incoming(bid, id, "keep", minutes=15)`` is our answer given elsewhere (the CLI,
  another page): D5 shows it as ``incoming[].answer``;
- ``set_board(bid, "mps3_01")``: the physical board ``GET /lease`` names (D4); by default
  the target without its ``_pl``;
- hub mode over fpgahub REST (T8, docs/HUB_MODE.md): ``notes_reason = "why"`` turns request
  notes off (no message reaches the holder, no Keep answer: ``notes_supported`` false), and
  ``revoke_reason = "why"`` is a token that cannot revoke (``can_revoke`` false: force is
  REFUSED whatever the clock says);
- victim: ``taken(bid, by=..., reason=...)`` is another session force-releasing it;
  ``DELETE .../lease/taken`` forgets it (D11);
- D9: ``new_holder(bid, principal)``: the lease passes to someone who was never asked, so
  our note is re-sent to them with a fresh 120 s deadline.
- D12: ``lease.holder_kind`` is ``"hm"`` once the holder answered our request (``answer``),
  else ``"unknown"`` (it may be a script): force then needs ``confirm_board``, the board's
  name (400 USAGE without it, 409 REFUSED with another), by the real service's rules
  (``harness_manager.services.lease.holder_kind`` / ``confirm_board_error``).
- CCR PANEL-1: ``notify_holder(bid, seq=, at=)`` is a tap on the front panel's request
  banner (the mock's ``PanelSim.tap(bid, "request")`` calls it, as presence calls the lease
  service's): ``tapped_at`` on the open request and ``lease.tapped``; never a release.
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

from harness_manager import naming
from harness_manager.cli.output import with_data
from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.services.lease import confirm_board_error, holder_kind, typed_names

API = "/api/v1"

#: The routes docs/LEASE_REQUESTS.md adds (daemon module hub_api, lane LR-C): the four of
#: its API table, and D11's dismiss of the victim's banner.
LEASE_REQUEST_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/boards/{bid}/lease/request"),
    ("POST", "/boards/{bid}/lease/respond"),
    ("POST", "/boards/{bid}/lease/force"),
    ("DELETE", "/boards/{bid}/lease/queue"),
    ("DELETE", "/boards/{bid}/lease/taken"),
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
        #: CCR PANEL-1: taps on the front panel's request banner, bid -> id -> (seq, tapped_at)
        self.taps: dict[str, dict[str, tuple[int, str]]] = {}
        self.revokes: list[dict[str, Any]] = []            # what `lease revoke` would have run
        self.refuse_force = ""
        #: False: no lease.force_available event (the page must find out by reading at zero)
        self.announce_force = True
        #: hub mode over REST (T8): non-empty = no request notes / no revoke, with the reason
        self.notes_reason = ""
        self.revoke_reason = ""
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
        if self.revoke_reason:                   # T8: the token's role, whatever the clock says
            return False, self.revoke_reason
        hub = self.week.hubs.get(bid) or {}
        lease = hub.get("lease")
        if not lease or lease.get("mine"):
            return False, "nobody else holds the lease"
        holder = lease["holder"]
        deadline = self._deadline(req)
        if now < deadline:
            left = int(deadline - now + 0.999)
            if req.get("reasked"):               # D9
                return False, (f"{holder} was not asked until {iso(req['created'])[11:19]} UTC: "
                               f"they have {left} s left to answer")
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

    def set_board(self, bid: str, board: str) -> None:
        with self._lock:
            self.hub(bid)["board"] = board

    def new_holder(self, bid: str, principal: str = "carol@lab-pc-09") -> None:
        """D9: the lease passes to ``principal`` while we wait; they were never asked, so the
        note goes to them with a fresh deadline (and force waits for it)."""
        now = time.time()
        with self._lock:
            hub = self.hub(bid)
            user, _ = _user_host(principal)
            hub["lease"] = {"target": hub["target"], "holder": principal, "user": user,
                            "expires_at": iso(now + 3600), "mine": False}
            req = self.outgoing.get(bid)
            if req is not None:
                req.update(created=now, answer=None, force_published=False, reasked=True)
        self.publish("lease.state", bid, {"target": hub["target"], "state": "queued",
                                          "holder": me(), "expires_at": ""})

    def dismiss_taken(self, bid: str) -> bool:
        with self._lock:
            return self.last_taken.pop(bid, None) is not None

    def clear_ahead(self, bid: str) -> None:
        with self._lock:
            self.ahead.pop(bid, None)

    # -- knobs: the holder's and the victim's side -------------------------------------------------

    def incoming(self, bid: str, *, by: str = "bob@lab-pc-02",
                 message: str = "need it for the 15:00 demo", age_s: float = 0) -> str:
        """Another session asks for our lease: it queues and writes req-<id>.json."""
        now = time.time() - age_s
        user, host = _user_host(by)
        if self.notes_reason:                    # T8: every waiter, never its message
            message = ""
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

    # -- the front panel (CCR PANEL-1, as LeaseService.notify_holder plays it) ------------------------

    def notify_holder(self, bid: str, *, seq: int, at: float) -> dict[str, Any]:
        """Someone at the board tapped the panel's lease-request banner (decision P2): the
        open request (the oldest unanswered one: ours, or one for our lease) gets
        ``tapped_at`` and ``lease.tapped {id, by, at}`` is published once per ``seq``.
        ``notified`` is True where our lease is held. It never releases, answers, forces or
        leaves: nothing here touches the lease, the queue or an answer."""
        with self._lock:
            hub = self.week.hubs.get(bid)
            if hub is None:
                return {"notified": False, "request": None}
            answers = self.answers.get(bid, {})
            open_ = [(n["created_at"], n["id"], n["by"]) for n in self.inbox.get(bid, [])
                     if n["id"] not in answers]
            req = self.outgoing.get(bid)
            if req is not None and not req.get("answer"):
                open_.append((iso(req["created"]), req["id"], me()))
            if not open_:
                return {"notified": False, "request": None}      # the banner was stale
            _, rid, by = min(open_)
            when = iso(at) if at else iso(time.time())
            taps = self.taps.setdefault(bid, {})
            fresh = taps.get(rid, (None, ""))[0] != seq
            if fresh:
                taps[rid] = (seq, when)
            notified = bool((hub.get("lease") or {}).get("mine"))
        request = {"id": rid, "by": by}
        if fresh:
            self.publish("lease.tapped", bid, {**request, "at": when})
        return {"notified": notified, "request": request}

    def _tapped_at(self, bid: str, rid: str) -> str | None:
        return self.taps.get(bid, {}).get(rid, (0, None))[1]

    # -- the view ----------------------------------------------------------------------------------

    def view(self, bid: str) -> dict[str, Any]:
        """The keys docs/LEASE_REQUESTS.md adds to ``GET /boards/{bid}/lease``."""
        with self._lock:
            hub = self.week.hubs.get(bid)
            out: dict[str, Any] = {"queue": [], "request": None, "incoming": [],
                                   "taken": dict(self.last_taken[bid])
                                   if bid in self.last_taken else None}
            out["board"] = None
            # T8 hub mode over REST: what this client's hub connection can do. The names follow
            # the REST client (notes_supported, notes_reason, can_revoke()); LR-B may rename.
            out.update(notes_supported=not self.notes_reason, notes_reason=self.notes_reason,
                       can_revoke=not self.revoke_reason, revoke_reason=self.revoke_reason)
            if hub is None:
                return out
            out["board"] = hub.get("board") or hub["target"].rsplit("_", 1)[0]     # D4
            queue: list[tuple[str, bool]] = []
            req = self.outgoing.get(bid)
            if req is not None:
                queue = [(p, False) for p in self.ahead.get(bid, [])] + [(me(), True)]
            lease = hub.get("lease")
            if lease and lease.get("mine"):
                queue = [(n["by"], False) for n in self.inbox.get(bid, [])]
                answers = self.answers.get(bid, {})
                out["incoming"] = [{**n, "answer": dict(answers[n["id"]])      # D5
                                    if n["id"] in answers else None,
                                    "tapped_at": self._tapped_at(bid, n["id"])}  # PANEL-1
                                   for n in self.inbox.get(bid, [])]
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
                    "tapped_at": self._tapped_at(bid, req["id"]),              # PANEL-1
                }
            return out

    def lease_keys(self, bid: str) -> dict[str, Any]:
        """D12: what ``GET /lease`` adds to ``lease``: is a Harness Manager session known to
        hold it (it answered our request), or may it be a script?"""
        with self._lock:
            hub = self.week.hubs.get(bid) or {}
            lease = hub.get("lease")
            if not lease:
                return {}
            req = self.outgoing.get(bid)
            kind, why = holder_kind(req.get("answer") if req else None, asked=req is not None,
                                    here=bool(lease.get("mine")), notes_ok=not self.notes_reason)
            return {"holder_kind": kind, "holder_kind_reason": why}

    def board_of(self, bid: str) -> str:
        hub = self.hub(bid)
        return hub.get("board") or hub["target"].rsplit("_", 1)[0]

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
        seen_answer: Any = None
        force_seen = False
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
                return {"left": True}            # D7: leaving is not a failure
            # D1: a keep answer is a phase, not the end: we stay queued, and force can open
            # again once its minutes run out.
            a = req["answer"]
            if a and a is not seen_answer:
                seen_answer = a
                progress("answered", 0, 0)
            now_force = bool(req.get("force_published"))
            if now_force and not force_seen:
                progress("force-available", 0, 0)
            force_seen = now_force
        raise ActionFailedError(f"{hub['target']} is leased to {hub['lease']['holder']}",
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
        if self.notes_reason and answer == "keep":
            raise UnavailableError("lease_keep", self.notes_reason)
        self.answer_incoming(bid, rid, answer, minutes=minutes, message=message)

    def answer_incoming(self, bid: str, rid: str, answer: str, *, minutes: int = 0,
                        message: str = "") -> None:
        """Our answer to an incoming request (POST .../respond, or the CLI elsewhere)."""
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

    def check_force(self, bid: str, body: dict[str, Any], names: tuple[str, ...] = ()) -> None:
        """Before any revoke: USAGE without confirm, then the frozen availability rule, then
        D12's typed board name (``names``: what the page calls the board, then its address)."""
        if body.get("confirm") is not True:
            raise UsageError("force needs confirm: true",
                             hint="it kicks the holder off the board now; the UI asks first")
        req = self.outgoing.get(bid)
        if req is None:
            raise RefusedError("there is no request of yours to force: request the board first")
        if self.refuse_force:
            why, self.refuse_force = self.refuse_force, ""
            raise RefusedError(why, hint="GET .../lease shows the hub's view now")
        if self.revoke_reason:
            raise RefusedError(f"force is not available: {self.revoke_reason}",
                               hint="ask the hub admin, or wait for the holder")
        available, why = self.force_state(bid)
        if not available:
            deadline = self._deadline(req)
            if time.time() < deadline and not req.get("answer"):
                err = UnavailableError("lease_force", why)
                err.hint = ("force-release opens at the deadline if there is still no answer "
                            "and you are at the head of the queue")
                raise with_data(err, time_left_s=max(1, int(deadline - time.time() + 0.999)),
                                deadline_at=iso(deadline))           # D3
            raise RefusedError(f"force is not available: {why}")
        keys = self.lease_keys(bid)
        hub = self.hub(bid)
        err = confirm_board_error(
            keys.get("holder_kind", "unknown"), keys.get("holder_kind_reason", ""),
            body.get("confirm_board"),
            typed_names(names[0] if names else "", self.board_of(bid), hub["target"], *names[1:]),
            hub["target"])
        if err is not None:
            raise err

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
        cand = state.session(bid).candidate
        sim.hub(bid)
        sim.check_force(bid, body, (cand.name or "", naming.address_of(cand)))
        # Our own queued request job still runs (it is promoted by the revoke): force runs
        # beside it, never beside anything else.
        return accepted(jobs.start(bid, "lease_force", lambda progress: sim.force(bid, progress),
                                   beside=("lease_request",)))

    @app.delete(f"{API}/boards/{{bid}}/lease/queue")
    def lease_leave(bid: str) -> dict[str, Any]:
        state.session(bid)
        sim.hub(bid)
        return ok(left=sim.leave(bid))

    @app.delete(f"{API}/boards/{{bid}}/lease/taken")
    def lease_taken_dismiss(bid: str) -> dict[str, Any]:
        state.session(bid)                       # D11: GET /lease then says taken: null
        return ok(dismissed=sim.dismiss_taken(bid))
