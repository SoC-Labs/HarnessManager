"""The boards ``boards.toml`` configures, listed without contacting them (lane SIDEBAR-UX).

``GET /boards`` lists every board this service has probed or opened. After a restart that
was none of them, so a board configured in ``boards.toml`` (the lab's ``mps3-01``) left the
sidebar until it was added again. Now every board ``boards.toml`` has a table for is listed
too, marked ``source: "config"`` and not open:

- **No contact.** Its candidate is what the pack builds from the table's address
  (``engine.candidate_for``): the MPS3 pack reads files only there (the route from ``via``,
  the hub's share links, the name), never the board or the hub. QUIET-POLL holds: nothing
  here touches a board, and the page reads nothing more until the user opens it.
- **Its own route.** Opening it (``POST /boards`` with that candidate) goes through the
  table's ``via``/``hub``, as ``harness-manager info ADDRESS`` does.
- **Which address.** The table's key when it is a board id (``mps3@192.168.10.101:6900``),
  else its first ``match`` entry that is one, else its first ``match`` address with the
  default pack. A table with neither (a hub target with no address) is not listed.

``configured`` on every row whose board has a table: ``{key, via, hub, target, match,
name}``, all from the table and secret-free (a hub is named by ``hub.use``, its ``host``,
or its URL without credentials).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_manager import naming
from harness_manager.core.errors import HarnessError
from harness_manager.core.model import Candidate
from harness_manager.power.config import (
    BOARDS_FILE,
    BoardConfig,
    BoardsConfig,
    ConfigError,
    load_boards,
    safe_url,
)

log = logging.getLogger(__name__)

#: The pack a bare ``match`` address means when the table does not say (the engine's own
#: ``candidate_for`` default).
DEFAULT_PACK = "mps3"
#: A configured board's candidate evidence: listed from the file, the board not asked.
EVIDENCE = "in boards.toml (not contacted)"
#: ... and once ``POST /boards`` opened it (the row keeps the candidate after a close).
OPENED = "opened from boards.toml"

_warned: set[str] = set()
_warned_mu = threading.Lock()


@dataclass(frozen=True)
class ConfiguredBoard:
    key: str                          # the boards.toml table key
    candidate: Candidate              # built offline by the pack; evidence = EVIDENCE
    summary: dict[str, Any]           # the row's ``configured``


def _warn_once(text: str) -> None:
    with _warned_mu:
        if text in _warned:
            return
        _warned.add(text)
    log.warning("%s", text)


def boards_path(engine: Any) -> Path | None:
    """The engine's own boards.toml when it has a state dir; else None (the default path,
    which the pack's routing reads too: ``power.config.default_path``)."""
    state = getattr(engine, "state_dir", None)
    return Path(state) / BOARDS_FILE if state is not None else None


def load(engine: Any) -> BoardsConfig:
    """boards.toml as the pack reads it; empty (and said once in the log) when it is broken."""
    try:
        return load_boards(boards_path(engine))
    except ConfigError as exc:
        _warn_once(f"boards.toml is not listed in the sidebar: {exc.message}")
        return BoardsConfig()


def summary(board: BoardConfig) -> dict[str, Any]:
    """The row's ``configured``: what the table says about reaching the board, secret-free."""
    tables = dict(board.tables)
    via = tables.get("via", "")
    hub = tables.get("hub")
    name = target = ""
    if isinstance(hub, Mapping):
        use, host, url = hub.get("use"), hub.get("host"), hub.get("url")
        if isinstance(use, str) and use:
            name = use
        elif isinstance(host, str) and host:
            name = host
        elif isinstance(url, str) and url:
            name = safe_url(url)
        target = hub.get("target") if isinstance(hub.get("target"), str) else ""
    return {"key": board.key, "via": via if isinstance(via, str) else "", "hub": name,
            "target": target, "match": list(board.match), "name": board.name}


def _packs(engine: Any) -> set[str]:
    try:
        return set(engine.packs())
    except Exception:  # noqa: BLE001 - a pack that cannot load lists nothing here
        return set()


def target_of(board: BoardConfig, packs: set[str]) -> tuple[str, str] | None:
    """``(pack, host[:port])`` for a table, or None when it names no address."""
    for ref in (board.key, *board.match):
        pack, sep, addr = ref.partition("@")
        if sep and pack in packs and addr:
            return pack, addr
    default = DEFAULT_PACK if DEFAULT_PACK in packs else (next(iter(packs)) if len(packs) == 1
                                                           else "")
    if not default:
        return None
    for ref in board.match:
        if "@" not in ref and ref and not any(c.isspace() for c in ref) and not ref.startswith("-"):
            return default, ref
    return None


def configured_boards(engine: Any, boards: BoardsConfig | None = None) -> list[ConfiguredBoard]:
    """Every board boards.toml has a table for, as a candidate built without any contact.
    A table the pack refuses (a bad ``via``, an unknown hub) is left out and logged once."""
    boards = load(engine) if boards is None else boards
    if not boards.boards:
        return []
    packs = _packs(engine)
    out: list[ConfiguredBoard] = []
    seen: set[str] = set()
    for board in boards.boards:
        where = target_of(board, packs)
        if where is None:
            continue
        pack, addr = where
        try:
            cand = engine.candidate_for(addr, pack)
        except (HarnessError, ValueError) as exc:
            _warn_once(f"boards.toml {board.key!r} is not listed in the sidebar: "
                       f"{getattr(exc, 'message', exc)}")
            continue
        if cand.board_id in seen:
            continue
        seen.add(cand.board_id)
        # The table's own name (N1: config outranks every other source); the MPS3 pack has
        # applied it already, an engine that reads no boards.toml (the demo's) has not.
        cand = naming.offer(cand, board.name, naming.CONFIG) if board.name else cand
        out.append(ConfiguredBoard(board.key, dataclasses.replace(cand, evidence=EVIDENCE),
                                   summary(board)))
    return out


def board_rows(engine: Any, known: Mapping[str, Candidate],
               open_ids: set[str]) -> list[tuple[str, Candidate, str, dict[str, Any] | None]]:
    """``(board_id, candidate, source, configured)`` for GET /boards, sorted by board id:
    the boards the service knows (``source`` ``open`` or ``probe``), then those only
    boards.toml names (``config``). Reads files only."""
    boards = load(engine)
    conf = {c.candidate.board_id: c for c in configured_boards(engine, boards)}
    rows = []
    for board_id, cand in known.items():
        table = boards.for_board(board_id, cand.links) if boards.boards else None
        rows.append((board_id, cand, "open" if board_id in open_ids else "probe",
                     summary(table) if table is not None else None))
    for board_id, c in conf.items():
        if board_id not in known:
            rows.append((board_id, c.candidate, "config", c.summary))
    return sorted(rows, key=lambda r: r[0])
