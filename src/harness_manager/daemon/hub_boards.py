"""Which hub each board is behind, with no contact (lane UI2-API-HUB, UI v2 gap G3).

The UI v2 sidebar groups the boards by hub and shows a lease badge on every hub board, open or
not. So the service must say, for every board it lists (``GET /boards``), which hub it is
behind, without asking the board or the hub:

- an **open** board: its session's ``hub`` adapter;
- any **other** board (probed, or only named in ``boards.toml``): the engine's ``hub_for``
  (the demo's scripted hub) or the board pack's ``hub_for(candidate)`` (the MPS3 pack reads
  ``boards.toml`` and the candidate's ``hub://`` links: files only). Adapters are kept per
  board (a REST client keeps state: our request notes), dropped when the files change
  (``settings.changed``) or the board opens or closes.

``hub_row`` is the row's ``hub`` object: ``{name, host, target, transport}``. ``name`` is the
hub's name in Settings (``[hubs.<name>]``, ``hub.use``) when it has one, else its host: the
``{name}`` of ``GET /hubs/{name}/leases``.

The lease routes of a board that is not open (UI v2 "Request without opening": view, request,
leave the queue, cancel) take their hub from here too (``hub_api.hub_of``).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from harness_manager.core.errors import HarnessError

log = logging.getLogger(__name__)


class BoardHubs:
    """The hub adapter of every board the daemon lists, found without contact."""

    def __init__(self, daemon: Any) -> None:
        self.d = daemon
        self._mu = threading.Lock()
        self._adapters: dict[str, tuple[Any, Any]] = {}      # bid -> (candidate, adapter)
        self._names: dict[str, tuple[str, str]] = {}         # hub name -> (host, transport)
        for topic in ("settings.changed", "session.opened", "session.closed"):
            daemon.bus.subscribe(topic, self._drop)

    def _drop(self, ev: Any) -> None:
        with self._mu:
            if ev.topic == "settings.changed" or not ev.board_id:
                self._adapters.clear()
                self._names.clear()
            else:
                self._adapters.pop(ev.board_id, None)

    def candidate(self, bid: str) -> Any:
        """The board's candidate: the open session's, a probed one, or its ``boards.toml``
        row's (built from the file). None when the service does not know the board."""
        try:
            return self.d.engine.session(bid).candidate
        except HarnessError:
            pass
        known = self.d.known().get(bid)
        if known is not None:
            return known
        from . import configured

        for c in configured.configured_boards(self.d.engine):
            if c.candidate.board_id == bid:
                return c.candidate
        return None

    def adapter(self, bid: str, candidate: Any = None) -> Any:
        """The board's hub adapter (``host``, ``target``, ``client``), or None (no hub)."""
        try:
            session = self.d.engine.session(bid)
        except HarnessError:
            session = None
        if session is not None:
            return getattr(session, "hub", None)
        cand = candidate if candidate is not None else self.candidate(bid)
        if cand is None:
            return None
        with self._mu:
            hit = self._adapters.get(bid)
        if hit is not None and hit[0] == cand:
            return hit[1]
        adapter = self._build(bid, cand)
        with self._mu:
            self._adapters[bid] = (cand, adapter)
        return adapter

    def _build(self, bid: str, cand: Any) -> Any:
        engine_hub = getattr(self.d.engine, "hub_for", None)     # the demo's scripted hub
        try:
            if callable(engine_hub):
                return engine_hub(bid)
            pack = self.d.engine.packs().get(getattr(cand, "pack", ""))
            fn = getattr(pack, "hub_for", None)
            return fn(cand) if callable(fn) else None
        except Exception as exc:  # noqa: BLE001 - a bad table only costs this board its hub
            log.info("no hub for %s: %s", bid, getattr(exc, "message", exc))
            return None


    # -- the GET /boards row, without building a hub client ----------------------------------

    def row(self, bid: str, cand: Any, conf: dict[str, Any] | None) -> dict[str, Any] | None:
        """The row's ``hub``: from the open session's adapter, the engine's scripted hub (the
        demo), or the ``boards.toml`` row's ``configured`` summary (its ``hub`` and ``target``;
        a named hub's host and transport from Settings). Never builds a hub client (listing
        touches nothing, not even an ssh runner); None for a board with no hub."""
        try:
            return hub_row(getattr(self.d.engine.session(bid), "hub", None))
        except HarnessError:
            pass
        if callable(getattr(self.d.engine, "hub_for", None)):
            return hub_row(self.adapter(bid, cand))
        if not conf or not conf.get("hub"):
            return None
        name = str(conf["hub"])
        host, transport = self._named(name)
        return {"name": name, "host": host or name, "target": str(conf.get("target") or ""),
                "transport": transport}

    def _named(self, name: str) -> tuple[str, str]:
        """``(host, transport)`` of the Settings hub ``name`` (``("", "")`` for an inline
        table's host, or a hub Settings does not know). Cached until settings change."""
        with self._mu:
            hit = self._names.get(name)
        if hit is not None:
            return hit
        out = ("", "")
        try:
            from harness_manager.settings import hubs

            from .settings_api import settings_context

            r = settings_context(self.d).resolver()
            if name in hubs.hub_names(r):
                hub = hubs.resolve_hub(name, r)
                out = (str(getattr(hub, "host", "") or ""), str(hub.transport or ""))
        except Exception as exc:  # noqa: BLE001 - the row is only informative
            log.debug("hub %s not resolved: %s", name, exc)
        with self._mu:
            self._names[name] = out
        return out


def hub_name(adapter: Any) -> str:
    """The hub's name: ``[hubs.<name>]``'s name when the board names one, else its host."""
    name = getattr(getattr(adapter, "config", None), "name", "")
    return name if isinstance(name, str) and name else str(getattr(adapter, "host", "") or "")


def hub_row(adapter: Any) -> dict[str, Any] | None:
    """``GET /boards`` rows' ``hub`` (ui2 api-hub, G3): ``{name, host, target, transport}``."""
    if adapter is None:
        return None
    client = getattr(adapter, "client", None)
    return {"name": hub_name(adapter), "host": str(getattr(adapter, "host", "") or ""),
            "target": str(getattr(adapter, "target", "") or ""),
            "transport": str(getattr(adapter, "transport", "")
                             or getattr(client, "transport", "") or "ssh")}


def boards_of(d: Any, name: str) -> tuple[Any, dict[str, list[str]]]:
    """The hub named ``name`` (a hub name, or a host): an adapter to read it through (an
    open board's first) and ``{target: [board ids]}`` for every board the service lists on
    it. ``(None, {})`` when no board it lists is behind that hub."""
    from . import configured

    hubs: BoardHubs = d.board_hubs
    open_ids = set(d.engine.open_boards())
    by_target: dict[str, list[str]] = {}
    chosen, chosen_open = None, False
    for bid, cand, _source, conf in configured.board_rows(d.engine, d.known(), open_ids):
        listed = hubs.row(bid, cand, conf)
        if listed is None or name not in (listed["name"], listed["host"]):
            continue
        adapter = hubs.adapter(bid, cand)                  # built now: this is a hub read
        if adapter is None:
            continue
        by_target.setdefault(str(adapter.target), []).append(bid)
        if chosen is None or (bid in open_ids and not chosen_open):
            chosen, chosen_open = adapter, bid in open_ids
    return chosen, by_target
