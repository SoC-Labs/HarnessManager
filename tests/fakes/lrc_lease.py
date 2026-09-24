"""Lane LR-C: a fake lease service for lease requests, force release and leaving the queue.

``FakeLeaseService`` subclasses the real ``LeaseService`` (L1's acquire, release,
heartbeat and track stay real) and replaces the frozen LR-B additions with an
in-memory hub (``LeaseWorld``). Nothing here reaches a hub, ssh or fpgahub: a
"revoke" is an entry in ``world.revoked``.

The additions follow docs/LEASE_REQUESTS.md "Interfaces" exactly:

- ``request(board_id, hub, *, message, ttl_s, progress, cancel)``: refused (ALREADY)
  when our principal already holds it (CCR-A2); else queue + note;
  phases ``queued``, ``notified``, ``answered``, ``force-available``, ``held``; blocks
  until held (``{lease}``) or we leave (``{left: true}``, D7; ``cancel_raises`` makes a
  cancel raise instead, as an older service did). A "keep" answer does not end it (D1);
- ``respond(board_id, hub, request_id, answer, *, minutes, message)``;
- ``force(board_id, hub, *, confirm, confirm_board, board_names)``: refused
  (REFUSED/UNAVAILABLE) unless available; D12: a holder that did not answer our request
  may be a script, so the board's name is required (the real ``confirm_board_error``);
  then the revoke, and the head of the queue (us) is promoted;
- ``leave(board_id, hub)``: ``{left}``;
- ``view(hub)``: the extended view.

Events go on the service's bus as LR-B's would: ``lease.answered`` and
``lease.force_available`` from ``request``, ``lease.left`` from ``leave``. The holder
side's 10 s poll is ``poll_holder_side(board_id, hub)``: ``lease.wanted`` for each new
incoming request, ``lease.taken`` once after a forced release of our lease.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    AlreadyError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event
from harness_manager.services.lease import (
    LeaseService,
    confirm_board_error,
    holder_kind,
    typed_names,
)

ME = "david@mapstone-dev"
ALICE = "alice@lab-pc"
BOB = "bob@lab-pc"
WINDOW_S = 120
EXPIRES = "2026-09-25T12:00:00+00:00"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


@dataclass
class LeaseWorld:
    """The hub as both sessions see it. Times are real UTC (the routes compare with now)."""

    me: str = ME
    holder: str | None = ALICE
    queue: list[str] = field(default_factory=list)         # principals, head first
    notes: dict[str, dict[str, Any]] = field(default_factory=dict)     # req-<id>.json
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)   # ans-<id>.json
    revoked: list[dict[str, Any]] = field(default_factory=list)        # never a real revoke
    taken: dict[str, Any] | None = None
    board: str = "mps3_01"               # the physical board a revoke names (D4); "" = unknown
    cancel_raises: bool = False
    tick_s: float = 0.01
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    forced_with: list[dict[str, Any]] = field(default_factory=list)    # force()'s D12 arguments
    mu: threading.RLock = field(default_factory=threading.RLock)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    # -- the other side, as a test drives it ------------------------------------------------

    def add_request(self, by: str = ALICE, message: str = "", *, age_s: float = 0.0) -> str:
        """Someone else's request for the lease (``me`` should hold it)."""
        with self.mu:
            if by not in self.queue:
                self.queue.append(by)
            rid = f"r{next(self._ids)}"
            created = time.time() - age_s
            self.notes[rid] = {"id": rid, "by": by, "user": by.split("@")[0],
                               "host": by.split("@")[-1], "message": message,
                               "created_at": iso(created), "deadline_at": iso(created + WINDOW_S)}
            return rid

    def my_request(self) -> dict[str, Any] | None:
        with self.mu:
            return next((n for n in self.notes.values() if n["by"] == self.me), None)

    def expire_deadline(self, rid: str | None = None) -> None:
        """Move a request's deadline into the past (the 2:00 has run out)."""
        with self.mu:
            note = self.notes[rid] if rid else self.my_request()
            assert note is not None
            created = time.time() - WINDOW_S - 1
            note["created_at"], note["deadline_at"] = iso(created), iso(created + WINDOW_S)

    def answer(self, rid: str, answer: str, minutes: int = 0, message: str = "",
               *, age_s: float = 0.0) -> None:
        """The holder's session answers (a ``keep``, or ``release``: the head is promoted)."""
        with self.mu:
            self.answers[rid] = {"id": rid, "answer": answer, "minutes": minutes,
                                 "message": message, "at": iso(time.time() - age_s)}
            if answer == "release":
                self.holder = None
                self._promote()

    def release(self) -> None:
        """The holder gives the lease back with a plain ``lease release``."""
        with self.mu:
            self.holder = None
            self._promote()

    def force_me_off(self, by: str = ALICE, reason: str = "") -> None:
        """Someone force-released OUR lease (the victim side)."""
        with self.mu:
            self.holder = by
            if by in self.queue:
                self.queue.remove(by)
            self.taken = {"by": by, "at": iso(time.time()),
                          "reason": reason or f"force-released by {by} via Harness Manager: "
                                              "no answer to a request made at 12:00:00"}

    def _promote(self) -> None:
        """fpgahub promotes the head of the queue when the lease is free."""
        if self.holder is None and self.queue:
            self.holder = self.queue.pop(0)
            for rid, note in list(self.notes.items()):
                if note["by"] == self.holder:
                    del self.notes[rid]

    # -- derived -------------------------------------------------------------------------------

    def force_state(self, rid: str) -> tuple[bool, str]:
        note = self.notes.get(rid)
        if note is None:
            return False, "no such request"
        now = time.time()
        if now < _epoch(note["deadline_at"]):
            return False, f"the holder has until {note['deadline_at']} to answer"
        ans = self.answers.get(rid)
        if ans is not None:
            if ans["answer"] == "release":
                return False, "the holder released it"
            if now < _epoch(ans["at"]) + 60 * ans["minutes"]:
                return False, f"the holder keeps it for {ans['minutes']} min"
        if not self.queue or self.queue[0] != self.me:
            return False, "you are not at the head of the queue"
        return True, ""


class FakeLeaseService(LeaseService):
    def __init__(self, state_dir: Path, bus: Any = None, *, world: LeaseWorld, **kw: Any) -> None:
        super().__init__(state_dir, bus, **kw)
        self.world = world
        self._announced: set[str] = set()
        self._taken_told = False

    # -- helpers -----------------------------------------------------------------------------

    def _publish(self, topic: str, board_id: str, data: dict[str, Any]) -> None:
        if self.bus is not None:
            self.bus.publish(Event(topic, board_id, data))

    def _lease(self, hub: Any) -> dict[str, Any]:
        return {"target": hub.target, "holder": self.world.me, "user": self.world.me.split("@")[0],
                "expires_at": EXPIRES, "mine": True}

    # -- the frozen additions -------------------------------------------------------------------

    def view(self, hub: Any) -> dict[str, Any]:
        w = self.world
        w.calls.append(("view",))
        if hub is None:
            return {"lease": None, "hub": None}
        with w.mu:
            lease = None if w.holder is None else {
                "target": hub.target, "holder": w.holder, "user": w.holder.split("@")[0],
                "expires_at": EXPIRES, "mine": w.holder == w.me}
            queue = [{"position": i + 1, "holder": p, "user": p.split("@")[0], "mine": p == w.me}
                     for i, p in enumerate(w.queue)]
            request = None
            mine = w.my_request()
            if mine is not None:
                ok, reason = w.force_state(mine["id"])
                ans = w.answers.get(mine["id"])
                request = {"id": mine["id"], "message": mine["message"],
                           "created_at": mine["created_at"], "deadline_at": mine["deadline_at"],
                           "position": w.queue.index(w.me) + 1 if w.me in w.queue else 0,
                           "answer": None if ans is None else {k: ans[k] for k in (
                               "answer", "minutes", "message", "at")},
                           "force_available": ok, "force_reason": reason}
            incoming = ([{**{k: n[k] for k in ("id", "by", "user", "host", "message",
                                               "created_at", "deadline_at")},
                          "answer": None if n["id"] not in w.answers else {   # D5
                              k: w.answers[n["id"]][k] for k in ("answer", "minutes", "message",
                                                                  "at")}}
                         for n in w.notes.values() if n["by"] != w.me]
                        if w.holder == w.me else [])
            if lease is not None:                    # D12, by the real service's rule
                kind, why = holder_kind(request["answer"] if request else None,
                                        asked=request is not None, here=lease["mine"])
                lease.update(holder_kind=kind, holder_kind_reason=why)
            out = {"lease": lease, "hub": hub.host, "queue": queue, "request": request,
                   "incoming": incoming, "taken": w.taken}
            if w.board:
                out["board"] = w.board
            return out

    def request(self, board_id: str, hub: Any, *, message: str = "", ttl_s: int = 7200,
                progress: Any = None, cancel: threading.Event | None = None) -> dict[str, Any]:
        w = self.world
        w.calls.append(("request", board_id, message, ttl_s))
        report = progress or (lambda *_: None)
        cancel = cancel or threading.Event()
        with w.mu:
            if w.holder == w.me:            # CCR-A2: same principal, maybe another session
                raise AlreadyError(f"you already hold {hub.target} (another session)")
            if w.holder is None:
                w.holder = w.me
                return {"lease": self._lease(hub)}
            if w.me not in w.queue:
                w.queue.append(w.me)
            position = w.queue.index(w.me) + 1
            rid = f"r{next(w._ids)}"
            now = time.time()
            w.notes[rid] = {"id": rid, "by": w.me, "user": w.me.split("@")[0],
                            "host": w.me.split("@")[-1], "message": message,
                            "created_at": iso(now), "deadline_at": iso(now + WINDOW_S)}
        report("queued", position, 0)
        report("notified", 0, 0)
        told_answer = told_force = False
        while True:
            if cancel.wait(w.tick_s):
                with w.mu:
                    if w.me in w.queue:
                        w.queue.remove(w.me)
                    w.notes.pop(rid, None)
                if w.cancel_raises:
                    raise ActionFailedError(f"the lease request for {hub.target} was cancelled; "
                                            "its queue entry was removed")
                return {"left": True}                                  # D7
            with w.mu:
                held = w.holder == w.me
                gone = not held and rid not in w.notes and w.me not in w.queue
                ans = w.answers.get(rid)
                force_ok, _ = w.force_state(rid) if rid in w.notes else (False, "")
            if gone:                                  # left from elsewhere (lease leave)
                return {"left": True}
            if ans is not None and not told_answer:
                told_answer = True
                report("answered", 0, 0)
                self._publish("lease.answered", board_id, {k: ans[k] for k in (
                    "id", "answer", "minutes", "message")})
                # D1: a keep does not end the request; we stay queued and keep polling.
            if held:
                report("held", 1, 1)
                return {"lease": self._lease(hub)}
            if force_ok and not told_force:
                told_force = True
                report("force-available", 0, 0)
                self._publish("lease.force_available", board_id, {"id": rid})

    def respond(self, board_id: str, hub: Any, request_id: str, answer: str, *,
                minutes: int = 0, message: str = "") -> dict[str, Any]:
        w = self.world
        w.calls.append(("respond", board_id, request_id, answer, minutes, message))
        if answer not in ("release", "keep"):
            raise UsageError(f"answer must be release or keep, not {answer!r}")
        with w.mu:
            if w.holder != w.me:
                raise RefusedError(f"you do not hold {hub.target}; only the holder answers")
            note = w.notes.get(request_id)
            if note is None or note["by"] == w.me:
                raise AbsentError(f"no request {request_id!r} for {hub.target}")
            w.answer(request_id, answer, minutes, message)
        return {"ok": True}

    def force(self, board_id: str, hub: Any, *, confirm: bool, confirm_board: str | None = None,
              board_names: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
        w = self.world
        w.calls.append(("force", board_id, confirm))
        w.forced_with.append({"confirm_board": confirm_board, "board_names": tuple(board_names)})
        if confirm is not True:
            raise UsageError("force needs confirm")
        with w.mu:
            mine = w.my_request()
            if mine is None:
                raise RefusedError("you have no request")
            ok, reason = w.force_state(mine["id"])
            if not ok:
                if "until" in reason:
                    raise UnavailableError("lease_force", reason)
                raise RefusedError(f"force-release is not available: {reason}")
            kind, why = holder_kind(w.answers.get(mine["id"]))
            names = list(board_names)
            err = confirm_board_error(kind, why, confirm_board,
                                      typed_names(names[0] if names else "", w.board, hub.target,
                                                  *names[1:]), hub.target)
            if err is not None:
                raise err
            victim = w.holder
            w.revoked.append({"target": hub.target, "prior_holder": victim, "by": w.me,
                              "reason": f"force-released by {w.me} via Harness Manager: no answer "
                                        f"to a request made at {mine['created_at']}"})
            w.holder = None
            w._promote()                  # the head (us) gets it
            assert w.holder == w.me
        return {"lease": self._lease(hub)}

    def dismiss_taken(self, hub: Any) -> bool:
        """D11: forget the last forced release (the victim closed the banner)."""
        w = self.world
        w.calls.append(("dismiss_taken",))
        if hub is None:
            return False
        with w.mu:
            had, w.taken = w.taken is not None, None
        return had

    def leave(self, board_id: str, hub: Any) -> dict[str, Any]:
        w = self.world
        w.calls.append(("leave", board_id))
        with w.mu:
            left = w.me in w.queue
            if left:
                w.queue.remove(w.me)
            for rid in [r for r, n in w.notes.items() if n["by"] == w.me]:
                del w.notes[rid]
        if left:
            self._publish("lease.left", board_id, {})
        return {"left": left}

    # -- the holder side's 10 s poll (LR-B's tracker, faked) ---------------------------------

    def poll_holder_side(self, board_id: str, hub: Any) -> None:
        view = self.view(hub)
        for inc in view["incoming"]:
            if inc["id"] not in self._announced:
                self._announced.add(inc["id"])
                self._publish("lease.wanted", board_id, {k: inc[k] for k in (
                    "id", "by", "user", "host", "message", "deadline_at")})
        if view["taken"] and not self._taken_told:
            self._taken_told = True
            self._publish("lease.taken", board_id, dict(view["taken"]))


def factory(world: LeaseWorld):
    """``hub_api.LeaseService`` / ``cmd_hub.LeaseService`` replacement bound to ``world``."""

    def make(state_dir: Path, bus: Any = None, **kw: Any) -> FakeLeaseService:
        return FakeLeaseService(state_dir, bus, world=world, **kw)

    return make
