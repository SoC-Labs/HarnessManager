"""The engine: the one facade the CLI and GUI talk to.

It probes every board pack, opens sessions under the board's ``SessionLock``,
computes the capability view, and publishes lifecycle events on its bus::

    with Engine() as eng:
        cand = eng.candidate_for("192.168.10.101")
        eng.open(cand, note="deploying nanosoc")
        info = eng.info(cand.board_id)          # identity + health + capabilities
        eng.telemetry.readings(eng.session(cand.board_id))
    # leaving the block closes every session and releases every lock

Services:

- ``store`` (T1) and ``telemetry`` (T1) are built in.
- ``deploy`` (T2), ``consoles`` and ``debug`` (T4) are resolved lazily by
  module path and constructed as ``Cls(engine)``. When a module is not
  installed, or fails to load, the attribute is a stub whose every method
  raises ``UnavailableError`` (exit code 12) with the reason.

State lives in ``config.state_dir``, else ``$HARNESS_MANAGER_STATE_DIR``, else
``~/.config/harness-manager``: ``locks/`` for session locks and ``store/`` for the
content store.

One process owns a board. The ``SessionLock`` file excludes other processes.
A process-wide table excludes other ``Engine`` objects in the same process,
which the lock file alone cannot (it trusts its own pid).
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import os
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_manager.core.capabilities import POWER_CYCLE, negotiate
from harness_manager.core.errors import AbsentError, AlreadyError, HeldError, UsageError
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import BoardIdentity, BoardInfo, Candidate, Link, LinkKind
from harness_manager.core.pack import BoardPack, BoardSession, ProbeHints
from harness_manager.core.registry import load_packs
from harness_manager.core.services import EngineConfig
from harness_manager.core.session import LockOwner, SessionLock

from . import naming
from .services._unavailable import UnavailableService, is_unavailable
from .services.store import ContentStore
from .services.telemetry import TelemetryService

log = logging.getLogger(__name__)

STATE_DIR_ENV = "HARNESS_MANAGER_STATE_DIR"

# attribute -> (module, class, service name used in UnavailableError)
LAZY_SERVICES: dict[str, tuple[str, str, str]] = {
    "deploy": ("harness_manager.services.deploy", "DeployService", "deploy"),
    "consoles": ("harness_manager.services.console", "ConsoleBroker", "consoles"),
    "debug": ("harness_manager.services.debug", "DebugService", "debug"),
    "update": ("harness_manager.services.update", "UpdateService", "update"),
}

#: Health.control_channel states in which the harness serves nothing over Ethernet.
HARNESS_DOWN = frozenset({"wedged", "rescue", "offline"})

# Lock files held by any Engine in this process: resolved path -> the holding engine.
# A weak reference, so an engine dropped without close_all() does not hold a board forever.
_PROCESS_HOLDERS: dict[Path, weakref.ref[Engine]] = {}
_PROCESS_HOLDERS_LOCK = threading.Lock()


def resolve_state_dir(config: EngineConfig | None = None) -> Path:
    """``config.state_dir``, else ``$HARNESS_MANAGER_STATE_DIR``, else ``~/.config/harness-manager``."""
    if config is not None and config.state_dir is not None:
        return Path(config.state_dir)
    env = os.environ.get(STATE_DIR_ENV)
    if env:
        return Path(env)
    return Path.home() / ".config" / "harness-manager"


@dataclass
class _Open:
    pack: BoardPack
    candidate: Candidate
    session: BoardSession
    lock: SessionLock
    claim: Path                 # key in _PROCESS_HOLDERS
    note: str


def _link_data(link: Link) -> dict[str, str]:
    return {"kind": link.kind.value, "address": link.address, "detail": link.detail}


def _merge_links(a: tuple[Link, ...], b: tuple[Link, ...]) -> tuple[Link, ...]:
    seen = {(lk.kind, lk.address) for lk in a}
    return a + tuple(lk for lk in b if (lk.kind, lk.address) not in seen)


class Engine:
    def __init__(self, config: EngineConfig | None = None, *,
                 packs: dict[str, BoardPack] | None = None,
                 bus: EventBus | None = None) -> None:
        self.config = config or EngineConfig()
        self.state_dir = resolve_state_dir(self.config)
        self.lock_dir = self.state_dir / "locks"
        self.bus = bus or EventBus()
        self.telemetry = TelemetryService()
        self._packs: dict[str, BoardPack] | None = dict(packs) if packs is not None else None
        self._lock = threading.RLock()
        self._open: dict[str, _Open] = {}
        self._identities: dict[str, BoardIdentity] = {}
        self._services: dict[str, Any] = {}
        self._store: ContentStore | None = None

    # -- services ------------------------------------------------------------------

    @property
    def store(self) -> ContentStore:
        with self._lock:
            if self._store is None:
                self._store = ContentStore(self.state_dir / "store")
            return self._store

    @property
    def deploy(self) -> Any:
        return self._service("deploy")

    @property
    def consoles(self) -> Any:
        return self._service("consoles")

    @property
    def debug(self) -> Any:
        return self._service("debug")

    @property
    def update(self) -> Any:
        """The signed update channel (T7). Optional on an Engine: read it with getattr."""
        return self._service("update")

    def _service(self, attr: str) -> Any:
        with self._lock:
            if attr not in self._services:
                self._services[attr] = self._load_service(attr)
            return self._services[attr]

    def _load_service(self, attr: str) -> Any:
        module, cls_name, service = LAZY_SERVICES[attr]
        try:
            mod = importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name == module:
                return UnavailableService(service)
            log.exception("service module %s failed to load", module)
            return UnavailableService(service, f"failed to load: {exc}")
        except Exception as exc:  # noqa: BLE001 - one broken service must not take the app down
            log.exception("service module %s failed to load", module)
            return UnavailableService(service, f"failed to load: {exc}")
        cls = getattr(mod, cls_name, None)
        if cls is None:
            return UnavailableService(service, f"{module} has no {cls_name}")
        try:
            return cls(self)
        except Exception as exc:  # noqa: BLE001
            log.exception("service %s.%s failed to start", module, cls_name)
            return UnavailableService(service, f"failed to start: {exc}")

    # -- packs and discovery -------------------------------------------------------

    def packs(self) -> dict[str, BoardPack]:
        with self._lock:
            if self._packs is None:
                self._packs = self._load_packs()
            return dict(self._packs)

    def _load_packs(self) -> dict[str, BoardPack]:
        packs = load_packs()
        for name, kwargs in self.config.pack_overrides.items():
            if name not in packs:
                raise UsageError(f"config overrides pack {name!r}, which is not installed",
                                 hint=f"installed packs: {', '.join(sorted(packs)) or 'none'}")
            try:
                packs[name] = type(packs[name])(**kwargs)
            except TypeError as exc:
                raise UsageError(f"bad settings for pack {name!r}: {exc}") from exc
        return packs

    def _pack(self, name: str) -> BoardPack:
        packs = self.packs()
        if name not in packs:
            known = ", ".join(sorted(packs)) or "none installed"
            raise AbsentError(f"no board pack named {name!r}", hint=f"installed packs: {known}")
        return packs[name]

    def probe(self, hints: ProbeHints | None = None) -> list[Candidate]:
        """Ask every pack; one candidate per board_id (links merged); ``board.found`` each."""
        hints = hints or ProbeHints()
        found: dict[str, Candidate] = {}
        for name, pack in sorted(self.packs().items()):
            try:
                cands = pack.probe(hints)
            except UsageError:
                raise                   # the caller's hints are wrong: say so
            except Exception:  # noqa: BLE001 - one failing pack must not hide the others
                log.exception("board pack %r failed to probe", name)
                continue
            for cand in cands:
                prev = found.get(cand.board_id)
                if prev is None:
                    found[cand.board_id] = cand
                else:
                    name, source = naming.stronger(prev, cand)
                    found[cand.board_id] = dataclasses.replace(
                        prev, links=_merge_links(prev.links, cand.links),
                        identity=prev.identity or cand.identity,
                        name=name, name_source=source)
        result = list(found.values())
        for cand in result:
            self.bus.publish(Event("board.found", cand.board_id, {
                "pack": cand.pack, "label": cand.label, "evidence": cand.evidence,
                "name": cand.name, "name_source": cand.name_source,
                "links": [_link_data(lk) for lk in cand.links]}))
        return result

    def candidate_for(self, target: str, pack: str = "mps3", via: str = "") -> Candidate:
        found = self._pack(pack)
        return found.candidate_for_host(target, via) if via else found.candidate_for_host(target)

    # -- sessions ------------------------------------------------------------------

    def open(self, candidate: Candidate, *, note: str = "") -> BoardSession:
        """Take the board's SessionLock and open a session (``HeldError`` if held)."""
        pack = self._pack(candidate.pack)
        board_id = candidate.board_id
        with self._lock:
            if board_id in self._open:
                raise AlreadyError(f"{board_id} is already open in this engine",
                                   hint="use engine.session(board_id), or close it first")
            lock = SessionLock(board_id, lock_dir=self.lock_dir, note=note)
            key = self._claim_in_process(lock)
            try:
                lock.acquire()
                try:
                    session = pack.open(candidate)
                except BaseException:
                    lock.release()
                    raise
            except BaseException:
                self._unclaim_in_process(key)
                raise
            self._open[board_id] = _Open(pack, candidate, session, lock, key, note)
        self.bus.publish(Event("session.opened", board_id, {
            "pack": candidate.pack, "note": note,
            "links": [_link_data(lk) for lk in candidate.links]}))
        return session

    def _claim_in_process(self, lock: SessionLock) -> Path:
        key = lock.path.resolve()
        with _PROCESS_HOLDERS_LOCK:
            ref = _PROCESS_HOLDERS.get(key)
            holder = ref() if ref is not None else None
            if holder is not None and holder is not self:
                owner = lock.owner()
                who = owner.describe() if owner else "another engine in this process"
                raise HeldError(f"{lock.board_id} is in use", holder=who, hint=f"held by {who}")
            _PROCESS_HOLDERS[key] = weakref.ref(self)
        return key

    def _unclaim_in_process(self, key: Path) -> None:
        with _PROCESS_HOLDERS_LOCK:
            ref = _PROCESS_HOLDERS.get(key)
            if ref is not None and ref() in (self, None):
                del _PROCESS_HOLDERS[key]

    def session(self, board_id: str) -> BoardSession:
        return self._get(board_id).session

    def _get(self, board_id: str) -> _Open:
        with self._lock:
            entry = self._open.get(board_id)
        if entry is None:
            raise AbsentError(f"{board_id} is not open in this engine",
                              hint="open it first (engine.open(candidate))")
        return entry

    def lock_owner(self, board_id: str) -> LockOwner | None:
        """Who holds the board's session lock right now (any process), or None."""
        return SessionLock(board_id, lock_dir=self.lock_dir).owner()

    def info(self, board_id: str) -> BoardInfo:
        """Identity + health + the capability view for an open board."""
        entry = self._get(board_id)
        identity = entry.session.identity()
        health = entry.session.health()
        links = [lk.kind for lk in entry.candidate.links]
        available, unavailable = negotiate(entry.pack.capability_specs(), links,
                                           identity.features)
        if health.control_channel in HARNESS_DOWN and LinkKind.ETHERNET in links:
            # T14-3: the harness is not serving, so routes that need its Ethernet
            # services are dead now; routes over other links (USB, SSH, a plug) still work.
            still, _ = negotiate(entry.pack.capability_specs(),
                                 [k for k in links if k != LinkKind.ETHERNET], identity.features)
            lost = available - still
            if lost:
                # Short: it repeats on every lost capability; Health carries the full notes.
                why = f"the harness is {health.control_channel} (see Health)"
                available = available & still
                unavailable = {**unavailable, **dict.fromkeys(lost, why)}
        if POWER_CYCLE in available:   # the link is there; can the device actually cycle?
            power = getattr(entry.session, "power", None)
            reason = "no power adapter" if power is None else power.cycle_reason
            if reason:
                available = available - {POWER_CYCLE}
                unavailable = {**unavailable, POWER_CYCLE: reason}
        candidate = self._named(entry, identity)
        with self._lock:
            changed = self._identities.get(board_id) != identity
            self._identities[board_id] = identity
        if changed:
            self.bus.publish(Event("board.identity", board_id, {
                "shell_id": identity.shell_id, "rm_id": identity.rm_id,
                "rm_name": identity.rm_name, "harness_version": identity.harness_version,
                "features": list(identity.features),
                "name": candidate.name, "name_source": candidate.name_source}))
        return BoardInfo(candidate, identity, health, available, unavailable)

    def _named(self, entry: _Open, identity: BoardIdentity) -> Candidate:
        """N1: the open board's candidate, renamed when its harness or the session (the
        hub) gives a better name than it had (``harness_manager.naming``). Only the name
        fields change, never board_id or links; the session sees the same candidate."""
        cand = naming.with_identity(entry.candidate, identity)
        board_name = getattr(entry.session, "board_name", None)
        if callable(board_name):
            try:
                name, source = board_name(identity)
            except Exception:  # noqa: BLE001 - a name is never worth failing info
                log.exception("naming %s failed", entry.candidate.board_id)
            else:
                cand = naming.offer(cand, name, source)
        if cand is not entry.candidate:
            with self._lock:
                entry.candidate = cand
            if getattr(entry.session, "candidate", None) is not None:
                entry.session.candidate = cand
        return cand

    def close(self, board_id: str) -> None:
        """Close the session and release the lock. Closing a board that is not open is a no-op."""
        with self._lock:
            entry = self._open.pop(board_id, None)
            self._identities.pop(board_id, None)
        if entry is None:
            return
        try:
            self._release_services(board_id, entry.session)
            entry.session.close()
        finally:
            entry.lock.release()
            self._unclaim_in_process(entry.claim)
            self.bus.publish(Event("session.closed", board_id, {"pack": entry.candidate.pack}))

    def _release_services(self, board_id: str, session: BoardSession) -> None:
        """Tell services that hold board ports to let go. Only services already in use."""
        with self._lock:
            in_use = {name: self._services.get(name) for name in ("consoles", "debug")}
        actions = {
            "consoles": lambda svc: svc.close_all(board_id),
            "debug": lambda svc: svc.down(session),
        }
        for name, svc in in_use.items():
            if svc is None or is_unavailable(svc):
                continue
            try:
                actions[name](svc)
            except Exception:  # noqa: BLE001 - closing must always finish
                log.exception("%s service failed while closing %s", name, board_id)

    def close_all(self) -> None:
        """Close every board, even when closing one fails; then raise the first failure.

        Stopping at the first failure left the other boards' SSH tunnels, OpenOCDs and
        locks running after the daemon exited (Q2, 2026-09-24).
        """
        with self._lock:
            ids = list(self._open)
        first: BaseException | None = None
        for board_id in ids:
            try:
                self.close(board_id)
            except Exception as exc:  # noqa: BLE001 - every board must still be closed
                log.exception("closing %s failed", board_id)
                first = first or exc
        if first is not None:
            raise first

    def open_boards(self) -> list[str]:
        with self._lock:
            return list(self._open)

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close_all()
