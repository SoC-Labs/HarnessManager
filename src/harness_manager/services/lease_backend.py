"""One lease model for hub leases and board leases (Release 3, single-user mode). F0 INTERFACE.

``services/lease.py`` (LeaseService) drives a board's lease server through the narrow
``HubAdapter`` shape (``host``, ``target``, ``client``: a ``HubClientLike``). A hub board's
server is its fpgahub hub (``session.hub``); a desk board in single-user mode is its own
lease server (``harness_manager_mps3.board_lease``, feature ``board_lease``). This module is
the seam both sides code against; it is frozen at F0 (lanes HM-A..HM-D,
docs/planning/BOARD_LEASE_HM_PLAN.md).

- ``lease_of(session)``: the session's lease server: ``session.lease`` when the pack sets it
  (HM-A), else ``session.hub`` (old packs keep working), else ``None`` (no lease at all).
- ``LeaseServer``: ``HubAdapter`` plus ``kind``, ``name``, ``words`` and ``mode()``.
- ``server_word(adapter)``: "the hub" / "the board", the one word source for UI and errors.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

KIND_HUB = "hub"
KIND_BOARD = "board"

#: Plain words per server kind (UI, CLI and error text take them from here).
WORDS: dict[str, dict[str, str]] = {
    KIND_HUB: {"server": "the hub", "lease": "hub lease", "mode_off": ""},
    KIND_BOARD: {"server": "the board", "lease": "board lease",
                 "mode_off": "single-user mode is off: the board is shared"},
}

#: The label HM shows while board leases are a courtesy lock (Phase 1, Release 3).
COURTESY_LABEL = ("Courtesy lock: it keeps people out of each other's way and keeps out anyone "
                  "without a key, but anyone whose key is on the board can still get round it.")


@dataclass(frozen=True)
class LeaseMode:
    """A board server's mode (``None`` from a hub server: the hub has no mode)."""

    single_user: bool
    can_change: bool = False
    change_reason: str = ""
    since_s: int = 0            # relative seconds, as the board reports them
    by: str = ""
    persisted: bool = True      # False on a card-less board (/persist on tmpfs)
    local_hold_s: int = 0       # >0: switched off at the touchscreen; remote re-enable waits


class LeaseServer(Protocol):
    """``HubAdapter`` plus what a board server adds (HM-A implements the board kind)."""

    host: str
    target: str
    client: Any                 # HubClientLike (services/lease.py)
    kind: str                   # KIND_HUB | KIND_BOARD
    name: str                   # the hub's name, or the board's label
    words: dict[str, str]

    def mode(self) -> LeaseMode | None: ...


def lease_of(session: Any) -> Any:
    """The session's lease server, or ``None``. ``session.lease`` wins over ``session.hub``."""
    lease = getattr(session, "lease", None)
    if lease is not None:
        return lease
    return getattr(session, "hub", None)


def kind_of(adapter: Any) -> str:
    """``hub`` for anything without a ``kind`` (a pre-Release-3 hub adapter)."""
    return str(getattr(adapter, "kind", "") or KIND_HUB)


def server_word(adapter: Any) -> str:
    """"the hub" or "the board"."""
    return WORDS.get(kind_of(adapter), WORDS[KIND_HUB])["server"]


def mode_of(adapter: Any) -> LeaseMode | None:
    """The board's mode, or ``None`` for a hub (or a server that cannot say)."""
    fn = getattr(adapter, "mode", None)
    return fn() if callable(fn) else None


def lease_required(adapter: Any) -> bool:
    """Must a mutation hold the lease? Always behind a hub; on a board, only in single-user
    mode. An unknown mode answers False: the board stays the authority and refuses itself."""
    if adapter is None:
        return False
    if kind_of(adapter) == KIND_HUB:
        return True
    mode = mode_of(adapter)
    return bool(mode and mode.single_user)
