"""Board display names: one resolution order for the CLI, the daemon and the web UI (lane N1).

A board's name ("mps3-01") is what a person calls it. It is display only: it
never keys a board. ``Candidate.board_id`` does that (``mps3@<unit>`` once the
harness reports a unit id, else ``mps3@<address>``), so a renamed board keeps
its boards.toml table, its session lock and its hub lease, and two boards may
share a name without being confused.

Resolution order (``SOURCES``; the first source that gives a name wins):

1. ``config``: boards.toml ``name = "mps3-01"`` in the board's table. The one
   name the user sets on purpose, so nothing overrides it.
2. ``harness``: the name the board reports for itself (``BoardIdentity.name``;
   the proposed ``name`` key of ``version`` and identify, stored on the board).
   It travels with the board from hub to hub.
3. ``hub``: the fpgahub board that owns the board's lease target, as the hub
   reports it (``fpgahub board list --json``: ``mps3_01`` owns ``mps3_01_pl``)
   or as boards.toml ``hub.board`` states it. It names the hub's SLOT: a board
   swapped into the slot inherits it, which is why the harness name outranks it.
4. ``hub-target``: the same, derived without asking the hub by fpgahub's own
   rule (a trailing ``_pl``/``_ps``/``_mcc`` is stripped from the target).
5. ``""``: no name; front-ends show the address (``192.168.10.101:6900``).

A hub id is shown with ``_`` as ``-`` (``mps3_01`` -> ``mps3-01``): the name
people use for the board. The raw id stays in the evidence and in ``--json``.

This module is board-agnostic; a board pack supplies the hub part
(``harness_manager_mps3.naming``).
"""

from __future__ import annotations

import dataclasses
import unicodedata

from harness_manager.core.model import BoardIdentity, Candidate

CONFIG = "config"
HARNESS = "harness"
HUB = "hub"
HUB_TARGET = "hub-target"

#: Precedence, highest first. A source not listed here never names a board.
SOURCES: tuple[str, ...] = (CONFIG, HARNESS, HUB, HUB_TARGET)

#: What each source means, for a human line ("name  mps3-01 (from boards.toml)").
SOURCE_TEXT: dict[str, str] = {
    CONFIG: "boards.toml",
    HARNESS: "the harness",
    HUB: "the hub",
    HUB_TARGET: "the hub target's name",
}

MAX_NAME_LEN = 64


def clean_name(value: object) -> str:
    """A usable display name, or ``""``.

    Accepts a string of 1-64 printable characters after trimming. Anything else
    (not a string, empty, too long, a control or format character that could
    rewrite a terminal line or a TSV row) gives ``""``: a bad name from the board
    or the hub falls back to the next source instead of failing the read.
    """
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or len(name) > MAX_NAME_LEN:
        return ""
    if any(unicodedata.category(ch)[0] == "C" for ch in name):
        return ""
    return name


def rank(source: str) -> int:
    """Lower is stronger; an unknown or empty source ranks below every known one."""
    try:
        return SOURCES.index(source)
    except ValueError:
        return len(SOURCES)


def offer(candidate: Candidate, name: object, source: str) -> Candidate:
    """The candidate named ``name`` from ``source`` when that outranks its current name.

    An invalid name, or a source weaker than the one that named it already,
    leaves the candidate unchanged. The same source may rename it (a harness
    whose name changed).
    """
    clean = clean_name(name)
    if not clean or source not in SOURCES:
        return candidate
    current = candidate.name_source if candidate.name else ""
    if current and rank(source) > rank(current):
        return candidate
    if candidate.name == clean and current == source:
        return candidate
    return dataclasses.replace(candidate, name=clean, name_source=source)


def with_identity(candidate: Candidate, identity: BoardIdentity | None) -> Candidate:
    """The candidate named by its harness, when the identity carries a name."""
    if identity is None or not identity.name:
        return candidate
    return offer(candidate, identity.name, HARNESS)


def stronger(a: Candidate, b: Candidate) -> tuple[str, str]:
    """``(name, source)``: the better-ranked name of two candidates for one board."""
    pick = a
    if b.name and (not a.name or rank(b.name_source) < rank(a.name_source)):
        pick = b
    return (pick.name, pick.name_source) if pick.name else ("", "")


def address_of(candidate: Candidate) -> str:
    """The part of ``board_id`` after ``@`` (``192.168.10.101:6900``), else the id."""
    return candidate.board_id.split("@", 1)[1] if "@" in candidate.board_id else candidate.board_id


def display_name(candidate: Candidate) -> str:
    """What a front-end calls the board: its name, else its address."""
    return candidate.name or address_of(candidate)


def describe_source(candidate: Candidate) -> str:
    """``"from boards.toml"``, ``"from the hub"``...; ``""`` when the board has no name."""
    if not candidate.name:
        return ""
    return f"from {SOURCE_TEXT.get(candidate.name_source, candidate.name_source)}"


def hub_display(board_id: str) -> str:
    """A hub board id as people write it: ``mps3_01`` -> ``mps3-01``."""
    return board_id.replace("_", "-")


def config_name(candidate: Candidate) -> str:
    """boards.toml ``name`` for this board (by key or ``match``), or ``""``. Never raises:
    a broken boards.toml is reported by whatever reads it for real work."""
    from harness_manager.core.errors import UsageError
    from harness_manager.power.config import load_boards

    try:
        board = load_boards().for_board(candidate.board_id, candidate.links)
    except (UsageError, OSError):
        return ""
    return board.name if board is not None else ""


def with_config_name(candidate: Candidate) -> Candidate:
    """The candidate with its boards.toml name, when it has one."""
    return offer(candidate, config_name(candidate), CONFIG)
