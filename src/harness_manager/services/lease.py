"""Hub leases as a service (lane L1): show, acquire (it may queue), heartbeat, release.

A shared lab board sits behind a hub (fpgahub). Its lease is the convention
that keeps two people from driving it at once, and it has a TTL: a lease that
lapses lets the hub reset the board under whoever is still using it. So:

- **acquire** polls through a queue until the hub grants the lease (pyverify's
  ``LeaseClient.acquire``: re-acquiring as the same holder keeps the queue
  place). It can be cancelled; a cancelled wait removes its own queue entry,
  because an abandoned entry can later grant the board to nobody.
- **heartbeat** runs while the board is open in the Harness Manager service:
  every third of the TTL (at least every 10 minutes) the lease is extended.
  A heartbeat that finds the lease gone says whether it ``expired`` (nobody
  holds it) or was ``lost`` (someone else holds it now).
- **release** needs the token AND the holder (the hub answers "no lease to
  release" and keeps the board held otherwise; pyverify.lease).

The token is kept in ``<state_dir>/leases/`` (mode 0600), one file per hub
target, so the CLI and the service share one lease: ``harness-manager lease
acquire`` in a terminal, then the service heartbeats it once the board is open.

The service is board-agnostic. It drives a board's ``hub`` adapter, which
offers ``host``, ``target`` and ``client`` (the MPS3 pack's is
``harness_manager_mps3.hub.Mps3Hub``; the client's verbs are ``lease_show``,
``lease_acquire``, ``lease_heartbeat``, ``lease_release``, ``lease_cancel``).

Events: ``lease.state {target, state: held|queued|released|expired|lost, holder,
expires_at}`` on the engine bus (docs/API.md, week-plan additions).
"""

from __future__ import annotations

import contextlib
import getpass
import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HarnessError,
    HeldError,
    UnavailableError,
)
from harness_manager.core.events import Event, EventBus

log = logging.getLogger(__name__)

TOPIC = "lease.state"
CAPABILITY = "lease"
DEFAULT_TTL_S = 3600
MIN_HEARTBEAT_S = 30.0
MAX_HEARTBEAT_S = 600.0
TICK_S = 5.0
POLL_S = 20.0
ACQUIRE_TIMEOUT_S = 3600.0

_QUEUED = re.compile(r"queued(?: at position (\d+))?")

Progress = Callable[[str, int, int], None]


class HubClientLike(Protocol):
    def lease_show(self) -> Any: ...
    def lease_acquire(self, holder: str, *, ttl: int, poll_s: float = ..., timeout_s: float = ...,
                      sleep: Callable[[float], None] | None = ...,
                      log_fn: Callable[[str], None] | None = ...) -> tuple[Any, str]: ...
    def lease_heartbeat(self, token: str, holder: str) -> str: ...
    def lease_release(self, token: str, holder: str) -> None: ...
    def lease_cancel(self, holder: str) -> bool: ...


class HubAdapter(Protocol):
    """``session.hub`` (CCR L1-3 proposes it for ``core.pack``): the board's hub."""

    host: str
    target: str
    client: HubClientLike


def default_holder() -> str:
    """``harness-manager-<user>@<host>``: says who and where, and stays the same across runs,
    so re-acquiring keeps the queue place (the hub keys its queue by holder)."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in a container
        user = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return f"harness-manager-{user}@{socket.gethostname().split('.')[0]}"


def heartbeat_interval(ttl_s: int) -> float:
    return max(MIN_HEARTBEAT_S, min(MAX_HEARTBEAT_S, ttl_s / 3.0))


# --- the token store -------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredLease:
    hub: str
    target: str
    holder: str
    token: str
    ttl_s: int
    expires_at: str = ""
    acquired_at: float = 0.0

    def public(self, *, mine: bool = True) -> dict[str, Any]:
        """The API's ``lease`` object. The token never leaves this process."""
        return {"target": self.target, "holder": self.holder, "expires_at": self.expires_at,
                "mine": mine}


class LeaseStore:
    """One JSON file per hub target, mode 0600: the token releases the board, so it is a secret."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, hub: str, target: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", f"{hub}__{target}")
        return self.root / f"{safe}.json"

    def get(self, hub: str, target: str) -> StoredLease | None:
        try:
            data = json.loads(self._path(hub, target).read_text(encoding="utf-8"))
            return StoredLease(**data)
        except (OSError, ValueError, TypeError):
            return None

    def put(self, lease: StoredLease) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        path = self._path(lease.hub, lease.target)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(lease), fh)
        os.replace(tmp, path)

    def drop(self, hub: str, target: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(hub, target).unlink()


# --- the service -----------------------------------------------------------------------------


class _Cancelled(Exception):
    pass


@dataclass
class _Tracked:
    hub: Any
    last_beat: float
    announced: bool = False


class LeaseService:
    """Leases for boards behind a hub. ``bus`` gets ``lease.state`` events; None is fine (CLI)."""

    def __init__(self, state_dir: Path, bus: EventBus | None = None, *,
                 clock: Callable[[], float] = time.monotonic, tick_s: float = TICK_S,
                 heartbeat_s: float | None = None) -> None:
        self.store = LeaseStore(Path(state_dir) / "leases")
        self.bus = bus
        self._clock = clock
        self._tick_s = tick_s
        self._heartbeat_s = heartbeat_s          # None: a third of the lease's TTL
        self._mu = threading.Lock()
        self._tracked: dict[str, _Tracked] = {}
        self._acquiring: dict[str, threading.Event] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- views ----------------------------------------------------------------------------------

    @staticmethod
    def require_hub(hub: Any, board_id: str = "") -> Any:
        if hub is None:
            raise UnavailableError(CAPABILITY, f"{board_id or 'this board'} is not behind a hub; "
                                               "add a hub table to boards.toml (docs/HIL_B0.md)")
        return hub

    def view(self, hub: Any) -> dict[str, Any]:
        """``{lease: {target, holder, expires_at, mine, user} or None, hub: HOST or None}``."""
        if hub is None:
            return {"lease": None, "hub": None}
        shown = hub.client.lease_show()
        if not shown.held:
            return {"lease": None, "hub": hub.host}
        stored = self.store.get(hub.host, hub.target)
        mine = stored is not None and stored.holder == shown.holder
        return {"lease": {"target": hub.target, "holder": shown.holder,
                          "expires_at": shown.expires_at or (stored.expires_at if mine and stored else ""),
                          "mine": mine, "user": shown.user},
                "hub": hub.host}

    def _emit(self, board_id: str, hub: Any, state: str, holder: str = "", expires_at: str = "") -> None:
        if self.bus is None:
            return
        self.bus.publish(Event(TOPIC, board_id, {"target": hub.target, "state": state,
                                                 "holder": holder, "expires_at": expires_at}))

    # -- acquire / release ------------------------------------------------------------------------

    def acquire(self, hub: Any, *, board_id: str = "", ttl_s: int = DEFAULT_TTL_S,
                holder: str | None = None, progress: Progress | None = None,
                cancel: threading.Event | None = None, poll_s: float = POLL_S,
                timeout_s: float = ACQUIRE_TIMEOUT_S, heartbeat: bool = True) -> dict[str, Any]:
        """Block until the lease is held (it may queue); store it; heartbeat it while tracked."""
        hub = self.require_hub(hub, board_id)
        if not isinstance(ttl_s, int) or ttl_s <= 0:
            from harness_manager.core.errors import UsageError

            raise UsageError(f"ttl_s must be a positive whole number of seconds, not {ttl_s!r}")
        holder = holder or default_holder()
        stored = self.store.get(hub.host, hub.target)
        if stored is not None and stored.holder == holder:
            shown = hub.client.lease_show()
            if shown.held and shown.holder == holder:
                # Already ours: say so rather than queue behind ourselves.
                if heartbeat:
                    self.track(board_id, hub)
                return {"lease": stored.public(), "already": True}
        cancel = cancel or threading.Event()
        key = board_id or f"{hub.host}/{hub.target}"
        with self._mu:
            self._acquiring[key] = cancel
        report = progress or (lambda *_: None)

        def sleep(seconds: float) -> None:
            if cancel.wait(seconds):
                raise _Cancelled()

        def on_log(message: str) -> None:
            m = _QUEUED.search(message)
            if m:
                pos = int(m.group(1)) if m.group(1) else 0
                report("queued", pos, 0)
                self._emit(board_id, hub, "queued", holder)

        report("acquire", 0, 1)
        try:
            lease, expires_at = hub.client.lease_acquire(holder, ttl=ttl_s, poll_s=poll_s,
                                                          timeout_s=timeout_s, sleep=sleep,
                                                          log_fn=on_log)
        except _Cancelled:
            removed = False
            with contextlib.suppress(HarnessError):
                removed = hub.client.lease_cancel(holder)
            raise ActionFailedError(
                f"the lease request for {hub.target} was cancelled"
                + ("; its queue entry was removed" if removed else ""),
                hint="acquire again when you want the board") from None
        finally:
            with self._mu:
                self._acquiring.pop(key, None)
        record = StoredLease(hub=hub.host, target=hub.target, holder=holder, token=lease.token,
                             ttl_s=ttl_s, expires_at=expires_at, acquired_at=time.time())
        self.store.put(record)
        report("held", 1, 1)
        self._emit(board_id, hub, "held", holder, expires_at)
        if heartbeat:
            self.track(board_id, hub, announced=True)
        return {"lease": record.public()}

    def cancel_acquire(self, board_id: str) -> bool:
        """Stop a queued acquire for this board (its job then removes the queue entry)."""
        with self._mu:
            ev = self._acquiring.get(board_id)
        if ev is None:
            return False
        ev.set()
        return True

    def release(self, hub: Any, *, board_id: str = "") -> dict[str, Any]:
        hub = self.require_hub(hub, board_id)
        if self.cancel_acquire(board_id):
            return {"ok": True, "cancelled": True}
        stored = self.store.get(hub.host, hub.target)
        if stored is None:
            shown = hub.client.lease_show()
            who = f"held by {shown.holder}" if shown.held else "not leased"
            raise AbsentError(f"this Harness Manager holds no lease on {hub.target} ({who})",
                              hint="a lease taken outside Harness Manager is released where it was "
                                   "taken (fpgahub lease release --token …)")
        hub.client.lease_release(stored.token, stored.holder)
        self.store.drop(hub.host, hub.target)
        self.untrack(board_id)
        self._emit(board_id, hub, "released", stored.holder)
        return {"ok": True, "released": stored.public(mine=True)}

    # -- heartbeat --------------------------------------------------------------------------------

    def track(self, board_id: str, hub: Any, *, announced: bool = False) -> None:
        """Heartbeat this board's stored lease (if any, now or later) while it stays tracked."""
        if hub is None:
            return
        with self._mu:
            self._tracked[board_id] = _Tracked(hub, self._clock(), announced=announced)
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="lease-heartbeat",
                                                daemon=True)
                self._thread.start()

    def untrack(self, board_id: str) -> None:
        with self._mu:
            self._tracked.pop(board_id, None)

    def tracked(self) -> list[str]:
        with self._mu:
            return list(self._tracked)

    def _run(self) -> None:
        while not self._stop.wait(self._tick_s):
            self.beat_due()

    def beat_due(self, *, force: bool = False) -> None:
        """One heartbeat round: every tracked board whose stored lease is due. Never raises."""
        with self._mu:
            items = list(self._tracked.items())
        for board_id, tr in items:
            stored = self.store.get(tr.hub.host, tr.hub.target)
            if stored is None:
                continue                           # nothing of ours to keep alive (yet)
            every = self._heartbeat_s or heartbeat_interval(stored.ttl_s)
            now = self._clock()
            if not force and now - tr.last_beat < every:
                continue
            tr.last_beat = now
            try:
                expires_at = tr.hub.client.lease_heartbeat(stored.token, stored.holder)
            except HeldError as exc:
                state = getattr(exc, "state", "lost")
                self.store.drop(tr.hub.host, tr.hub.target)
                self.untrack(board_id)
                log.warning("lease heartbeat for %s: %s", board_id, exc.message)
                self._emit(board_id, tr.hub, state if state in ("expired", "lost") else "lost",
                           stored.holder)
                continue
            except HarnessError as exc:
                # The hub did not answer this time: the TTL still covers us; try next tick.
                log.warning("lease heartbeat for %s failed (will retry): %s", board_id, exc.message)
                tr.last_beat = now - every + min(every, 60.0)
                continue
            if expires_at:
                self.store.put(StoredLease(**{**asdict(stored), "expires_at": expires_at}))
            if not tr.announced or expires_at != stored.expires_at:
                tr.announced = True
                self._emit(board_id, tr.hub, "held", stored.holder, expires_at or stored.expires_at)

    def close(self) -> None:
        self._stop.set()
        with self._mu:
            waits = list(self._acquiring.values())
            self._tracked.clear()
        for ev in waits:
            ev.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
