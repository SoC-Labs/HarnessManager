"""MPS3 board names: boards.toml, the harness, and the lab hub's name for the board (lane N1).

The order is ``harness_manager.naming`` (config > harness > hub > hub-target >
address). This module supplies the MPS3 pack's two hooks and the hub lookup.

The hub's name. fpgahub keeps two names for the lab board: the lease TARGET
(``mps3_01_pl``, what leases and shares use) and the physical BOARD that owns it
(``mps3_01``). ``fpgahub board list --json`` prints the ownership (fpgahub 0.3.0
``cli.py`` ``chassis_list`` over ``GET /groups``, ``api/schemas.py``
``GroupsResponse``)::

    {"groups": [{"board": "mps3_01", "size": 1, "is_paired": false,
                 "members": [{"name": "mps3_01_pl", "role": "pl"}]}]}

fpgahub derives the board from the target (``grouping.chassis_of``): an explicit
``chassis`` in the hub's config.toml, else the target minus a trailing ``_ps``,
``_pl`` or ``_mcc``. So the board id is known three ways, best first:

1. boards.toml ``hub.board`` (the key docs/LEASE_REQUESTS.md adds for LR-A):
   stated by the user, no hub call;
2. the hub's answer: ``HubClient.board_id()`` once lane LR-A merges it (CCR N1-4),
   until then this module's own ``fpgahub board list --json`` through
   ``hub.DEFAULT_RUNNER_FACTORY`` (read-only, no lease, no share). Asked at most
   once per (hub, target) per process, and only for an OPEN board, at ``info``;
3. fpgahub's suffix rule applied here to boards.toml ``hub.target``, with no hub
   call (source ``hub-target``). This is what a board shows before it is opened.

The name shown is the id with ``_`` as ``-`` (``mps3_01`` -> ``mps3-01``).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from harness_manager import naming as _n
from harness_manager.core.errors import HarnessError, UsageError
from harness_manager.core.model import BoardIdentity, Candidate

log = logging.getLogger(__name__)

#: fpgahub ``grouping._CHASSIS_SUFFIXES``: a target's role suffix inside its board.
TARGET_SUFFIXES = ("_ps", "_pl", "_mcc")

#: How long a hub answer is trusted, and how long a failed ask is not repeated.
HUB_NAME_TTL_S = 3600.0
HUB_FAIL_TTL_S = 300.0
HUB_NAME_TIMEOUT_S = 15.0

_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")

# (hub host, target) -> (board id or "", monotonic time asked)
_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_CACHE_MU = threading.Lock()


def clear_cache() -> None:
    """Forget every hub answer (tests; a hub whose config changed)."""
    with _CACHE_MU:
        _CACHE.clear()


# --- the hub table (read raw: hub.py is lane LR-A's) ----------------------------------------


def hub_table(candidate: Candidate) -> dict[str, Any]:
    """The board's boards.toml ``hub`` table, raw; ``{}`` when it has none or it is broken."""
    from .hub import board_tables

    try:
        table = board_tables(candidate).get("hub")
    except (UsageError, OSError):
        return {}
    return dict(table) if isinstance(table, dict) else {}


def _hub_id(value: Any) -> str:
    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else ""


def derive_board_id(target: str) -> str:
    """fpgahub's rule without an explicit chassis: ``mps3_01_pl`` -> ``mps3_01``."""
    for suffix in TARGET_SUFFIXES:
        if target.endswith(suffix) and len(target) > len(suffix):
            return target[: -len(suffix)]
    return target


def parse_board_list(text: str, target: str) -> str:
    """The board that owns ``target`` in ``fpgahub board list --json``; ``""`` if none does.

    A reply that is not that JSON raises ``ValueError`` (the caller keeps the derived name).
    """
    data = json.loads(text)
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise ValueError("no 'groups' list in fpgahub board list --json")
    for group in groups:
        if not isinstance(group, dict):
            continue
        members = group.get("members") or []
        if any(isinstance(m, dict) and m.get("name") == target for m in members):
            return _hub_id(group.get("board"))
    return ""


def query_board_id(host: str, target: str, group: str | None = "fpga", *,
                   runner_factory: Callable[[str, str | None], Callable[..., Any]] | None = None,
                   timeout_s: float = HUB_NAME_TIMEOUT_S) -> str:
    """Ask the hub which board owns ``target`` (``fpgahub board list --json``, read-only).

    Returns ``""`` when the hub lists no board for it. Raises ``HarnessError`` when the
    hub cannot be asked, or ``ValueError`` when its reply is not the expected JSON.
    """
    from . import hub as _hub

    factory = runner_factory or _hub.DEFAULT_RUNNER_FACTORY
    res = factory(host, group)(["fpgahub", "board", "list", "--json"], timeout=timeout_s)
    if res.returncode != 0:
        raise _hub.classify_hub_error("board list", host, target,
                                      (res.stderr or res.stdout or "").strip() or "(no output)")
    return parse_board_list(res.stdout, target)


def cached_board_id(host: str, target: str) -> str | None:
    """The hub's last answer for (host, target) while it is fresh; None when not asked or stale."""
    with _CACHE_MU:
        hit = _CACHE.get((host, target))
    if hit is None:
        return None
    board, at = hit
    ttl = HUB_NAME_TTL_S if board else HUB_FAIL_TTL_S
    return board if time.monotonic() - at < ttl else None


def _remember(host: str, target: str, board: str) -> None:
    with _CACHE_MU:
        _CACHE[(host, target)] = (board, time.monotonic())


def hub_board_id(host: str, target: str, group: str | None = "fpga", *,
                 client: Any = None) -> str:
    """The hub's board for ``target``, cached; ``""`` when the hub gives none or cannot be asked.

    ``client`` is the session's ``HubClient``: its ``board_id()`` (lane LR-A) is used
    when it exists; until then this module asks with its own ``board list``.
    """
    cached = cached_board_id(host, target)
    if cached is not None:
        return cached
    board = ""
    try:
        ask = getattr(client, "board_id", None)
        board = _hub_id(ask()) if callable(ask) else query_board_id(host, target, group)
    except (HarnessError, ValueError, OSError, TimeoutError) as exc:
        log.info("hub %s: no board name for %s (%s)", host, target, exc)
    _remember(host, target, board)
    return board


# --- the pack hooks ----------------------------------------------------------------------------


def _offline_hub_name(candidate: Candidate) -> tuple[str, str]:
    """(board id, source) from boards.toml and the cache: never asks the hub."""
    table = hub_table(candidate)
    if not table:
        return "", ""
    stated = _hub_id(table.get("board"))
    if stated:
        return stated, _n.HUB
    host, target = table.get("host"), _hub_id(table.get("target"))
    if isinstance(host, str) and host and target:
        cached = cached_board_id(host, target)
        if cached:
            return cached, _n.HUB
    if target:
        return derive_board_id(target), _n.HUB_TARGET
    return "", ""


def name_candidate(candidate: Candidate) -> Candidate:
    """Pack hook: name the candidate from boards.toml, its probe identity, and the hub table.

    Offline: it reads files and the cache, never the hub or the board.
    """
    out = _n.with_config_name(candidate)
    out = _n.with_identity(out, candidate.identity)
    board, source = _offline_hub_name(out)
    if board:
        out = _n.offer(out, _n.hub_display(board), source)
    return out


def session_board_name(session: Any, identity: BoardIdentity | None = None) -> tuple[str, str]:
    """Session hook (``BoardSession.board_name``, CCR N1-3): the hub's confirmed name for an
    open board. Asks the hub at most once per (hub, target) per process; ``("", "")``
    when the board has no hub, or the hub gives no name."""
    del identity                      # the engine applies the harness name itself
    candidate = getattr(session, "candidate", None)
    hub = getattr(session, "hub", None)
    if candidate is None or hub is None:
        return "", ""
    table = hub_table(candidate)
    stated = _hub_id(table.get("board")) if table else ""
    if stated:
        return _n.hub_display(stated), _n.HUB
    host, target = getattr(hub, "host", ""), getattr(hub, "target", "")
    if not host or not target:
        return "", ""
    group = getattr(getattr(hub, "config", None), "group", "fpga")
    board = hub_board_id(host, target, group, client=getattr(hub, "client", None))
    return (_n.hub_display(board), _n.HUB) if board else ("", "")
