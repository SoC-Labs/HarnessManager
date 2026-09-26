"""The OS-slot route (docs/API.md "User microSD and OS slots", ``card_api.py``) in the T14 mock.

A simulated card per board (``CardSim``), rendered with the real ``services.slots`` JSON
helper so the mock and the daemon cannot disagree on the shape: by default the board has
no card, so no OS slots. ``insert(bid, linux=True)`` puts a card with the OS slots A/B on
it; ``bare_metal(bid)`` makes the harness one without them (``available: false``). The
card itself is the mock's ``GET /card`` (L1-CARD, over the DemoEngine).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from harness_manager.core.pack import CardStatus, SlotInfo, SlotStatus
from harness_manager.services.slots import slot_status_json

API = "/api/v1"
NO_SLOTS = "the bare-metal harness has no OS slots (the slot verbs are the Linux harness's)"


class CardSim:
    def __init__(self) -> None:
        self.cards: dict[str, CardStatus] = {}
        self.no_store: set[str] = set()

    def card(self, bid: str) -> CardStatus:
        return self.cards.get(bid) or CardStatus(
            store=True, present=False, state="none", text="none", boot="none",
            notes=("no card: the board boots exactly as it always has",))

    def insert(self, bid: str, *, linux: bool = True) -> None:
        slots = SlotStatus(running="A", default="A", target="B", fabric_sid="0x72bb0a36",
                           slots={"A": SlotInfo("A", state="valid", hdr_crc="0x3e5e9c2c",
                                                length=24354312, verified="boot",
                                                version="1.1.0"),
                                  "B": SlotInfo("B")}) if linux else None
        self.cards[bid] = CardStatus(
            store=True, present=True, state="valid", text="nanosoc [A]", card_mb=15193,
            default={"rm_id": "0x01000001", "rm_name": "nanosoc", "static_id": "0x72bb0a36",
                     "slot": "A"},
            boot="loaded", committable=True, os_slots=slots)

    def bare_metal(self, bid: str) -> None:
        self.no_store.add(bid)


def register(app: FastAPI, state: Any, sim: CardSim, ok: Any) -> None:
    @app.get(f"{API}/boards/{{bid}}/slots")
    def slots_read(bid: str) -> dict[str, Any]:
        state.session(bid)
        state.jobs.gate(bid)
        st = sim.card(bid).os_slots
        if bid in sim.no_store or st is None:
            return ok(board_id=bid, available=False, reason=NO_SLOTS, slots=None)
        return ok(board_id=bid, available=True, reason="", slots=slot_status_json(st))
