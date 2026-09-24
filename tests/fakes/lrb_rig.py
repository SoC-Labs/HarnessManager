"""LR-B: Harness Manager sessions around one fake hub (``lrb_fake_hub``), on one fake clock.

``World`` is the lab: one ``LrbHub``, one ``FakeClock``, and a session per person.
Each session is its own ``LeaseService`` (its own state dir, bus and, for the clock
skew tests, its own wall-clock offset), talking to the hub as its own principal.
The services never sleep for real: ``sleep`` advances the shared clock, which runs
whatever the test scheduled with ``clock.after`` (the other side's actions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.events import EventBus
from harness_manager.services.lease import LeaseService, RequestNote

from .lrb_fake_hub import FakeClock, LrbHub, LrbHubRef, iso

BID = "mps3@192.168.10.101:6900"
DAVID = "david@mapstone-dev"          # the holder
BOB = "bob@lab-pc"                     # a requester
CAROL = "carol@lab-pc2"                # another requester


@dataclass
class Sess:
    name: str
    principal: str
    svc: LeaseService
    hub: LrbHubRef
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def of(self, topic: str) -> list[dict[str, Any]]:
        return [data for t, _b, data in self.events if t == topic]

    def states(self) -> list[str]:
        return [d["state"] for d in self.of("lease.state")]


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.clock = FakeClock()
        self.hub = LrbHub(clock=self.clock.wall())
        self.sessions: list[Sess] = []

    def session(self, principal: str, *, name: str = "", skew: float = 0.0,
                state: str = "") -> Sess:
        name = name or principal.split("@")[0]
        bus = EventBus()
        svc = LeaseService(self.tmp / (state or name), bus, clock=self.clock.monotonic,
                           tick_s=3600.0, wall_clock=self.clock.wall(skew),
                           sleep=self.clock.advance)
        s = Sess(name, principal, svc, LrbHubRef(self.hub.client(principal)))
        bus.subscribe("lease.*", lambda ev: s.events.append((ev.topic, ev.board_id, dict(ev.data))))
        self.sessions.append(s)
        return s

    def holding(self, principal: str = DAVID, **kw: Any) -> Sess:
        """A session that holds the board (acquired as ``david-hm``) and has it open."""
        s = self.session(principal, **kw)
        s.svc.acquire(s.hub, board_id=BID, holder="david-hm", heartbeat=False)
        s.svc.track(BID, s.hub, announced=True)
        return s

    def queued_by_hand(self, principal: str, age_s: float, *, message: str = "") -> RequestNote:
        """``principal`` queued and wrote its note ``age_s`` ago (as another process did)."""
        user = principal.split("@")[0]
        self.hub.queue.append((principal, user))
        created = self.clock.t - age_s
        note = RequestNote(id=f"{int(created)}-{user}", by=principal, user=user,
                           host=principal.split("@")[1], message=message, created_at=iso(created),
                           deadline_at=iso(created + 120))
        self.hub.notes[note.id] = note
        return note

    def calls(self, principal: str, verb: str | None = None) -> list[str]:
        return [v for p, v in self.hub.calls if p == principal and (verb is None or v == verb)]

    def close(self) -> None:
        for s in self.sessions:
            s.svc.close()
