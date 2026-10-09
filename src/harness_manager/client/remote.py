"""``RemoteEngine``: the ``core.services.Engine`` protocol over harness-manager-daemon's API.

The CLI's verbs run against it unchanged (``cli.engine.get_engine`` returns
one when a daemon is running), so the CLI and the web UI share one engine and
one board session.

How it behaves where the in-process engine would differ:

- **Sharing a board.** ``open`` asks the daemon to open the board. If the
  daemon already has it open (the web UI, another CLI verb), the daemon says
  ALREADY and this client SHARES that session instead: it is marked
  ``owned=False`` and ``close`` leaves it open in the daemon. A board this
  client opened itself is closed in the daemon by ``close``.
- **Events.** ``bus`` is a local ``EventBus`` fed by the daemon's events
  WebSocket, which is connected on the first ``subscribe`` (or job). So
  ``deploy.*`` progress reaches ``bus`` subscribers as it does in-process.
- **Long operations** (deploy, restore, reboot, SD backup/install/restore,
  debug up) are daemon jobs. The call blocks until the job ends, forwarding
  ``job.progress`` to the ``progress`` callable, and returns the job's result
  or raises its error. It waits for ``job.done`` on the events socket, so
  every progress event has been delivered before it returns; without the
  socket it polls ``GET /jobs/{id}`` and replays the phases the job recorded.
- **Adapters** on a ``RemoteSession`` are proxies, present exactly when the
  daemon's session has the adapter (``GET /boards/{bid}/session``). A storage
  proxy has no ``load_backup``: the CLI then builds the record from the archive
  itself and the daemon re-verifies it with the real adapter. Paths sent to the
  daemon are made absolute first (its working directory is not yours).
- **Lab verbs** go through ``session.shell.call(fn)``: ``fn`` receives a proxy
  with the five shell verbs ``cli/cmd_lab.py`` uses (``link``,
  ``display_owner``, ``display_settled``, ``macgen``, ``read_dut_frame``), each
  one ``POST /boards/{bid}/lab/{verb}``. Anything else raises UNAVAILABLE.
- **The front panel** (``session.panel``, CCR PANEL-5) is a proxy of the daemon's
  ``/panel`` routes: ``support``/``state``/``frame``/``locate`` as the adapter protocol
  says, plus ``presence()`` and ``identify_until()`` from the daemon's presence service.
  The daemon owns presence, so ``hello`` is UNAVAILABLE and nothing is ``offer``ed.
- **The Live display** (``engine.display``, ``client/display.py``): the daemon's display
  routes, the status, a still (PNG or raw) and a live view over its WebSocket.
- **Not proxied**: the debug adapter's OpenOCD command-line pieces (OpenOCD runs
  in the daemon) and the storage adapter's ``locate``.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harness_manager.core import capabilities as C
from harness_manager.core.errors import (
    AbsentError,
    AlreadyError,
    HarnessError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import BoardInfo, Candidate, Reading
from harness_manager.core.pack import (
    BackupRecord,
    BoardSession,
    CardStatus,
    DeployResult,
    OverlayRef,
    PreflightItem,
    ProbeHints,
    Progress,
    detail_of,
    report_progress,
)
from harness_manager.core.panel import PanelFrame, PanelState, PanelSupport
from harness_manager.core.services import DebugStatus, EngineConfig
from harness_manager.core.session import LockOwner

from .codec import error_from_json, from_json
from .http import Http, q

log = logging.getLogger(__name__)

PUMP_CONNECT_S = 5.0
JOB_POLL_S = 1.0
JOB_EVENT_GRACE_S = 2.0
CONSOLE_BUFFER = 4 * 1024 * 1024


# --- WebSockets -------------------------------------------------------------------------------


def ws_connect(url: str, *, open_timeout: float = 10.0) -> Any:
    """A sync websockets connection with no proxy; a refused handshake raises its error."""
    from websockets.exceptions import InvalidHandshake, InvalidStatus
    from websockets.sync.client import connect

    kwargs: dict[str, Any] = {"open_timeout": open_timeout, "max_size": None}
    params = inspect.signature(connect).parameters
    if "proxy" in params:
        kwargs["proxy"] = None          # websockets >= 15 honours *_PROXY; never for loopback
    if "legacy" in params:
        kwargs["legacy"] = True         # websockets >= 17: return the connection itself
    try:
        return connect(url, **kwargs)
    except InvalidStatus as exc:
        response = exc.response
        try:
            payload = json.loads(response.body or b"{}")
        except ValueError:
            payload = {}
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            raise error_from_json(err) from None
        raise UnreachableError(f"harness-manager-daemon refused the WebSocket (HTTP {response.status_code})") \
            from None
    except (OSError, TimeoutError, InvalidHandshake) as exc:
        raise UnreachableError(f"cannot open a WebSocket to harness-manager-daemon ({exc})",
                               hint="`harness-manager daemon status` checks it") from exc


class _EventPump:
    """The events WebSocket, read on a thread; each frame becomes an ``Event``."""

    def __init__(self, http: Http, on_event: Callable[[Event], None]) -> None:
        self._http = http
        self._on_event = on_event
        self._ws: Any = None
        self._thread: threading.Thread | None = None
        self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive

    def start(self, timeout: float = PUMP_CONNECT_S) -> None:
        self._ws = ws_connect(self._http.ws_url("/events"), open_timeout=timeout)
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="harness-manager-events")
        self._thread.start()

    def _run(self) -> None:
        try:
            for message in self._ws:
                if not isinstance(message, str):
                    continue
                try:
                    frame = json.loads(message)
                except ValueError:
                    continue
                ev = Event(topic=str(frame.get("topic", "")),
                           board_id=str(frame.get("board_id") or ""),
                           data=frame.get("data") or {},
                           at=float(frame.get("at") or time.time()))
                try:
                    self._on_event(ev)
                except Exception:  # noqa: BLE001 - one bad handler must not end the stream
                    log.exception("event handler failed for %s", ev.topic)
        except Exception as exc:  # noqa: BLE001 - the socket closed; waiters fall back to polling
            log.debug("events socket ended: %r", exc)
        finally:
            self._alive = False

    def stop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._alive = False


class _RemoteBus(EventBus):
    """A local bus; the first subscription connects the daemon's events socket."""

    def __init__(self, engine: RemoteEngine) -> None:
        super().__init__()
        self._engine = engine

    def subscribe(self, topic: str, handler: Callable[[Event], None]) -> Callable[[], None]:
        self._engine._ensure_pump()
        return super().subscribe(topic, handler)


# --- the engine --------------------------------------------------------------------------------


class RemotePack:
    """A pack as the daemon lists it; local-only questions go to the installed pack."""

    def __init__(self, name: str, title: str, local: Any = None) -> None:
        self.name = name
        self.title = title
        self._local = local

    def capability_specs(self) -> Sequence[Any]:
        return tuple(self._local.capability_specs()) if self._local is not None else ()

    def candidate_for_host(self, spec: str, via: str = "") -> Candidate:
        if self._local is None:
            raise UsageError(f"pack {self.name!r} is not installed here, only in the daemon")
        return self._local.candidate_for_host(spec, via) if via else self._local.candidate_for_host(spec)

    def probe(self, hints: ProbeHints) -> list[Candidate]:
        raise UsageError("the daemon probes: use engine.probe()")

    def identity_policy(self) -> Any:
        """Lane IDENTITY: the installed pack's rules for naming a board (None without it)."""
        hook = getattr(self._local, "identity_policy", None)
        return hook() if callable(hook) else None

    def open(self, candidate: Candidate) -> BoardSession:
        raise UsageError("the daemon opens boards: use engine.open()")


class RemoteEngine:
    """Implements ``harness_manager.core.services.Engine`` over the harness-manager-daemon API."""

    def __init__(self, base_url: str, token: str, *, state_dir: Path | None = None,
                 info: Any = None, timeout: float | None = None) -> None:
        from harness_manager.daemon.state import default_state_dir

        self._http = Http(base_url, token) if timeout is None else Http(base_url, token,
                                                                          timeout=timeout)
        self.base_url = self._http.base_url
        self.daemon = info
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.lock_dir = self.state_dir / "locks"
        self.config = EngineConfig(state_dir=self.state_dir)
        self.bus: EventBus = _RemoteBus(self)
        self.deploy = RemoteDeploy(self)
        self.consoles = RemoteConsoles(self)
        self.debug = RemoteDebug(self)
        self.xvc = RemoteXvc(self)             # lane XVC-CORE: fabric debug over XVC
        self.board_claim = RemoteClaim(self)   # lane LINUX-CLAIM: the Linux harness's SSH claim
        self.slot_service = RemoteSlots(self)  # FIX-PACK-6: slot/card changes as service jobs
        self.board_identity = RemoteIdentity(self)   # lane BOARD-ID: label/IP/MAC and the fix
        self.telemetry = RemoteTelemetry(self)
        from .display import RemoteDisplay

        self.display = RemoteDisplay(self)     # the Live display (LCD mirror) routes
        self._mu = threading.RLock()
        self._sessions: dict[str, RemoteSession] = {}
        self._packs: dict[str, RemotePack] | None = None
        self._local: dict[str, Any] | None = None
        self._store: Any = None
        self._pump: _EventPump | None = None
        self._pump_failed = False
        self._job_cv = threading.Condition()
        self._job_events: OrderedDict[str, list[Event]] = OrderedDict()

    # -- finding the daemon -------------------------------------------------------------------

    @classmethod
    def discover(cls, state_dir: Path | None = None, *,
                 timeout: float = 1.0) -> RemoteEngine | None:
        """The engine of the daemon running for ``state_dir``, if it is alive and answers."""
        from harness_manager.daemon.control import health
        from harness_manager.daemon.state import default_state_dir, discover

        state = Path(state_dir) if state_dir is not None else default_state_dir()
        info = discover(state)
        if info is None or health(info, timeout) is None:
            return None
        return cls(info.base_url, info.token, state_dir=state, info=info)

    def describe(self) -> str:
        pid = getattr(self.daemon, "pid", None)
        return f"harness-manager-daemon at {self.base_url}" + (f" (pid {pid})" if pid else "")

    @property
    def http(self) -> Http:
        return self._http

    # -- services the protocol names -----------------------------------------------------------

    @property
    def store(self) -> Any:
        """The content store is files in the shared state dir (safe across processes)."""
        with self._mu:
            if self._store is None:
                from harness_manager.services.store import ContentStore

                self._store = ContentStore(self.state_dir / "store")
            return self._store

    # -- events and jobs -----------------------------------------------------------------------

    def _ensure_pump(self) -> bool:
        with self._mu:
            if self._pump is not None and self._pump.alive:
                return True
            if self._pump_failed:
                return False
            pump = _EventPump(self._http, self._on_event)
            try:
                pump.start()
            except HarnessError as exc:
                log.debug("no events socket (%s); jobs will be polled", exc)
                self._pump_failed = True
                return False
            self._pump = pump
            return True

    def _on_event(self, ev: Event) -> None:
        if ev.topic.startswith("job."):
            job_id = str(ev.data.get("job", ""))
            with self._job_cv:
                self._job_events.setdefault(job_id, []).append(ev)
                while len(self._job_events) > 64:
                    self._job_events.popitem(last=False)
                self._job_cv.notify_all()
        EventBus.publish(self.bus, ev)

    def run_job(self, path: str, body: Any = None, *, progress: Progress | None = None) -> Any:
        """POST a long operation, wait for its job, return its result (or raise its error)."""
        streaming = self._ensure_pump()
        job_id = self._http.post(path, body)["job"]
        delivered: list[str] = []
        seen = 0
        finished = False
        grace_until: float | None = None
        next_poll = time.monotonic() + (JOB_POLL_S if streaming else 0.0)
        while not finished:
            with self._job_cv:
                events = list(self._job_events.get(job_id, ()))
            for ev in events[seen:]:
                if ev.topic == "job.progress" and progress is not None:
                    phase = str(ev.data.get("phase", ""))
                    if not delivered or delivered[-1] != phase:
                        delivered.append(phase)
                    # with its detail (SLOT-TIMING's text, FIX-PACK-6's estimated) when the
                    # callable takes it, as the in-process service calls it
                    report_progress(progress, phase, int(ev.data.get("done", 0) or 0),
                                    int(ev.data.get("total", 0) or 0), detail_of(ev.data))
                elif ev.topic in ("job.done", "job.failed"):
                    finished = True
            seen = len(events)
            if finished:
                break
            now = time.monotonic()
            if grace_until is not None and now >= grace_until:
                break
            if now >= next_poll:
                next_poll = now + JOB_POLL_S
                state = self._http.get(f"/jobs/{q(job_id)}")
                pump_alive = self._pump is not None and self._pump.alive
                if state.get("state") != "running" and grace_until is None:
                    grace_until = now + (JOB_EVENT_GRACE_S if pump_alive else 0.0)
                    if not pump_alive:
                        break
            self._wait_job_events(job_id, seen, 0.25)
        record = self._http.get(f"/jobs/{q(job_id)}")
        if progress is not None:
            # Phases the events did not deliver (no socket, or it dropped): replay them.
            for phase in record.get("phases", []):
                if phase not in delivered:
                    delivered.append(phase)
                    progress(phase, 0, 0)
        with self._job_cv:
            self._job_events.pop(job_id, None)
        if record.get("state") == "failed":
            raise error_from_json(record.get("error") or {})
        return record.get("result")

    def _wait_job_events(self, job_id: str, seen: int, timeout: float) -> None:
        with self._job_cv:
            self._job_cv.wait_for(lambda: len(self._job_events.get(job_id, ())) > seen,
                                  timeout=timeout)

    # -- packs and discovery -------------------------------------------------------------------

    def _local_packs(self) -> dict[str, Any]:
        with self._mu:
            if self._local is None:
                from harness_manager.core.registry import load_packs

                try:
                    self._local = load_packs()
                except Exception:  # noqa: BLE001 - only local conveniences depend on it
                    log.exception("loading the local board packs failed")
                    self._local = {}
            return self._local

    def packs(self) -> dict[str, RemotePack]:
        with self._mu:
            if self._packs is None:
                titles = self._http.get("/packs").get("packs", {})
                local = self._local_packs()
                self._packs = {name: RemotePack(name, str(title), local.get(name))
                               for name, title in titles.items()}
            return dict(self._packs)

    def probe(self, hints: ProbeHints | None = None) -> list[Candidate]:
        hints = hints or ProbeHints()
        body = {"hosts": list(hints.hosts), "serial_ports": list(hints.serial_ports),
                "volumes": list(hints.volumes), "scan_usb": hints.scan_usb,
                "scan_network": hints.scan_network, "timeout_s": hints.timeout_s}
        if getattr(hints, "via", ""):
            body["via"] = hints.via
        found = self._http.post("/probe", body).get("candidates", [])
        return [from_json(Candidate, c) for c in found]

    def candidate_for(self, target: str, pack: str = "mps3", via: str = "") -> Candidate:
        local = self._local_packs().get(pack)
        if local is None:
            known = ", ".join(sorted(self._local_packs())) or "none installed"
            raise AbsentError(f"no board pack named {pack!r}", hint=f"installed packs: {known}")
        return local.candidate_for_host(target, via) if via else local.candidate_for_host(target)

    # -- sessions ------------------------------------------------------------------------------

    def open(self, candidate: Candidate, *, note: str = "") -> BoardSession:
        board_id = candidate.board_id
        with self._mu:
            if board_id in self._sessions:
                raise AlreadyError(f"{board_id} is already open in this engine",
                                   hint="use engine.session(board_id), or close it first")
        try:
            self._http.post("/boards", {"candidate": candidate, "note": note})
            owned = True
        except AlreadyError:
            owned = False        # open in the daemon already: share that session
        session = self._attach(board_id, owned=owned)
        if not owned:
            missing = {(lk.kind, lk.address) for lk in candidate.links} - {
                (lk.kind, lk.address) for lk in session.candidate.links}
            if missing:
                log.warning("%s is shared with the session harness-manager-daemon already has open, "
                            "which lacks %s", board_id, sorted(str(a) for _, a in missing))
        return session

    def _attach(self, board_id: str, *, owned: bool) -> RemoteSession:
        meta = self._http.get(f"/boards/{q(board_id)}/session")
        session = RemoteSession(self, from_json(Candidate, meta["candidate"]), owned=owned,
                                adapters=meta.get("adapters", {}),
                                reset_targets=tuple(meta.get("reset_targets", ())))
        with self._mu:
            self._sessions[board_id] = session
        return session

    def session(self, board_id: str) -> BoardSession:
        with self._mu:
            session = self._sessions.get(board_id)
        if session is not None:
            return session
        try:
            return self._attach(board_id, owned=False)
        except AbsentError:
            raise AbsentError(f"{board_id} is not open in harness-manager-daemon",
                              hint="open it first (engine.open(candidate))") from None

    def info(self, board_id: str) -> BoardInfo:
        return from_json(BoardInfo, self._http.get(f"/boards/{q(board_id)}"))

    def close(self, board_id: str) -> None:
        """Close a board this client opened; a shared one stays open in the daemon."""
        with self._mu:
            session = self._sessions.pop(board_id, None)
        self.consoles.close_all(board_id)
        if session is not None and session.owned:
            self._http.delete(f"/boards/{q(board_id)}")

    def close_all(self) -> None:
        with self._mu:
            ids = list(self._sessions)
        errors: list[HarnessError] = []
        for board_id in ids:
            try:
                self.close(board_id)
            except HarnessError as exc:
                errors.append(exc)
        self.consoles.close_everything()
        with self._mu:
            pump, self._pump = self._pump, None
        if pump is not None:
            pump.stop()
        if errors:
            raise errors[0]

    def lock_owner(self, board_id: str) -> LockOwner | None:
        holder = self._http.get(f"/boards/{q(board_id)}/lock").get("holder")
        return from_json(LockOwner, holder) if holder else None

    def open_boards(self) -> list[str]:
        return [b["board_id"] for b in self._http.get("/boards").get("boards", [])
                if b.get("open")]

    def __enter__(self) -> RemoteEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close_all()


# --- the session and its adapter proxies -----------------------------------------------------------


def _readings(payload: dict[str, Any]) -> list[Reading]:
    return [from_json(Reading, r) for r in payload.get("readings", [])]


def _absolute(path: Any) -> str:
    return str(Path(path).expanduser().resolve())


class _Proxy:
    def __init__(self, engine: RemoteEngine, board_id: str) -> None:
        self._engine = engine
        self._board_id = board_id

    def _path(self, suffix: str) -> str:
        return f"/boards/{q(self._board_id)}/{suffix}"

    def _forced(self, body: dict[str, Any]) -> dict[str, Any]:
        """SLOT-TIMING: a reset run inside ``reset_guard.guarded(..., force=True)`` carries its
        ``force``/``consent`` to the service, which checks the card job itself."""
        from harness_manager.services import reset_guard

        scope = reset_guard.scope_of(self._board_id)
        if scope is not None and scope.force:
            body = {**body, "force": True, "consent": scope.consent}
        return body


class _DeployAdapter(_Proxy):
    def overlays(self) -> Sequence[OverlayRef]:
        return self._engine.deploy._overlays(self._board_id)

    def preflight(self, overlay: OverlayRef) -> Sequence[PreflightItem]:
        return self._engine.deploy._preflight(self._board_id, overlay)

    def deploy(self, overlay: OverlayRef, progress: Progress | None = None, *,
               keep_on_card: bool = False) -> DeployResult:
        return self._engine.deploy._deploy(self._board_id, overlay, progress,
                                           keep_on_card=keep_on_card)

    def card_status(self) -> CardStatus:
        return self._engine.deploy._card_status(self._board_id)

    def baseline(self) -> OverlayRef | None:
        raise UnavailableError("deploy_partial", "harness-manager-daemon resolves the baseline itself; "
                                                 "use engine.deploy.restore_baseline(session)")


class _ConsoleAdapter(_Proxy):
    def console_endpoints(self) -> dict[str, str]:
        # ?rates=0: names only, so listing consoles never opens the board's control port.
        names = self._engine._http.get(self._path("consoles") + "?rates=0").get("names", [])
        return {n: f"harness-manager-daemon:{n}" for n in names}


class _DebugAdapter(_Proxy):
    def _unavailable(self) -> UnavailableError:
        return UnavailableError("debug_dut", "OpenOCD runs inside harness-manager-daemon; use engine.debug")

    def openocd_config(self) -> tuple[str, ...]:
        raise self._unavailable()

    def openocd_probe_args(self) -> tuple[str, ...]:
        raise self._unavailable()

    def openocd_search_paths(self) -> tuple[Path, ...]:
        raise self._unavailable()


class _Resets(_Proxy):
    def __init__(self, engine: RemoteEngine, board_id: str, targets: tuple[str, ...]) -> None:
        super().__init__(engine, board_id)
        self._targets = targets

    def reset_targets(self) -> Sequence[str]:
        return self._targets

    def reset(self, target: str) -> None:
        self._engine._http.post(self._path("reset"), {"target": target})


class _Clocks(_Proxy):
    def clocks(self) -> Sequence[Reading]:
        return _readings(self._engine._http.get(self._path("clocks")))

    def set_clock(self, name: str, mhz: float) -> Reading:
        payload = self._engine._http.post(self._path("clocks"), {"name": name, "mhz": mhz})
        return from_json(Reading, payload["reading"])


class _Telemetry(_Proxy):
    def readings(self) -> Sequence[Reading]:
        return _readings(self._engine._http.get(self._path("telemetry")))


class _Controller(_Proxy):
    def command(self, line: str, *, arm: bool = False) -> str:
        payload = self._engine._http.post(self._path("controller/command"),
                                          self._forced({"line": line, "arm": arm}))
        return str(payload.get("reply", ""))

    def reboot(self, progress: Progress | None = None,
               wait_s: float | None = None) -> dict | None:
        body = {} if wait_s is None else {"wait_s": wait_s}
        return self._engine.run_job(self._path("controller/reboot"), self._forced(body),
                                    progress=progress)

    def temperatures(self) -> Sequence[Reading]:
        return _readings(self._engine._http.get(self._path("controller/temps")))

    def oscillators(self) -> Sequence[Reading]:
        return _readings(self._engine._http.get(self._path("controller/osc")))


class _Storage(_Proxy):
    """No ``load_backup`` on purpose: see the module docstring."""

    def locate(self) -> str:
        raise UnavailableError("storage_backup", "harness-manager-daemon locates the SD volume itself")

    def backup(self, dest_dir: Path, progress: Progress | None = None) -> BackupRecord:
        result = self._engine.run_job(self._path("storage/backup"),
                                      {"dest_dir": _absolute(dest_dir)}, progress=progress)
        return from_json(BackupRecord, result)

    def install(self, files: Mapping[str, Path], *, backup: BackupRecord,
                progress: Progress | None = None, allow_mcc_update: bool = False) -> None:
        body: dict[str, Any] = {"files": {dest: _absolute(src) for dest, src in files.items()},
                                "backup_path": _absolute(backup.path)}
        if allow_mcc_update:                   # FIX-PACK-7: only when asked
            body["allow_mcc_update"] = True
        result = self._engine.run_job(self._path("storage/install"), body, progress=progress)
        notes = (result or {}).get("notes") if isinstance(result, dict) else None
        self.install_notes = [str(n) for n in notes or []]

    def restore(self, backup: BackupRecord, progress: Progress | None = None) -> None:
        self._engine.run_job(self._path("storage/restore"),
                             {"backup_path": _absolute(backup.path)}, progress=progress)

    def pending(self) -> dict | None:
        return self._engine._http.get(self._path("storage/pending")).get("pending")


class _Power(_Proxy):
    """``session.power`` over ``GET/POST /boards/{bid}/power[/cycle]`` (lane L4). The daemon
    always waits for the outlet to report ON again, so ``wait`` is not sent."""

    def _state(self) -> dict[str, Any]:
        return self._engine._http.get(self._path("power"))

    @property
    def label(self) -> str:
        return str(self._state().get("device") or "")

    def read(self) -> list[Reading]:
        return list(_readings(self._state()))

    @property
    def cycle_reason(self) -> str:
        return str(self._state().get("cycle_reason", ""))

    def power_cycle(self, off_s: float = 5.0, *, wait: bool = True,
                    progress: Progress | None = None) -> dict:
        return self._engine.run_job(self._path("power/cycle"), self._forced({"off_s": off_s}),
                                    progress=progress)


class _Panel(_Proxy):
    """``session.panel`` over ``GET /panel``, ``GET /panel/frame`` and ``POST /identify``
    (docs/API.md "Front panel"; CCR PANEL-5). Mirrors ``RemoteXvc``: the daemon's reply
    without ``ok``/``board_id``, rebuilt into the core model.

    ``GET /panel`` answers support, state and presence at once. The last answer is reused
    for ``VIEW_S`` (the daemon's own state cache is 1 s), so ``support`` then ``state`` is
    one request. ``support`` alone asks ``?state=0``, which never reads the board's panel:
    an Identify costs the board its ``locate`` only.

    The daemon beats for its boards and rides its own connections, so ``hello`` is
    UNAVAILABLE here and there is no ``offer``/``withdraw``. ``locate`` sends no ``who``:
    the daemon names itself (it runs as this user, on this host).
    """

    VIEW_S = 1.0
    HELLO_REASON = ("harness-manager-daemon sends this board's hellos itself (presence runs "
                    "in the Harness Manager service)")

    def __init__(self, engine: RemoteEngine, board_id: str, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(engine, board_id)
        self._clock = clock
        self._view: tuple[float, bool, dict[str, Any]] | None = None   # (at, with state, body)

    def _read(self, *, state: bool) -> dict[str, Any]:
        now = self._clock()
        view = self._view
        if view is not None and now - view[0] < self.VIEW_S and (view[1] or not state):
            return view[2]
        payload = self._engine._http.get(self._path("panel" if state else "panel?state=0"))
        body = {k: v for k, v in payload.items() if k not in ("ok", "board_id")}
        self._view = (now, state, body)
        return body

    def support(self) -> PanelSupport:
        return from_json(PanelSupport, self._read(state=False).get("support") or {})

    def state(self) -> PanelState:
        body = self._read(state=True)
        if body.get("panel") is None:
            raise UnavailableError(C.FRONT_PANEL, str(body.get("reason") or
                                                      "the daemon read no panel state"))
        return from_json(PanelState, body["panel"])

    def frame(self) -> PanelFrame:
        payload = self._engine._http.get(self._path("panel/frame"))
        return from_json(PanelFrame, {k: v for k, v in payload.items()
                                      if k not in ("ok", "board_id")})

    def hello(self, hello: Any) -> PanelState:
        raise UnavailableError(C.PRESENCE, self.HELLO_REASON)

    def locate(self, seconds: int, who: str) -> float:
        self._view = None                      # a blink changes identify.until
        payload = self._engine._http.post(self._path("identify"), {"seconds": int(seconds)})
        return float(payload.get("until") or 0.0)

    def presence(self) -> dict[str, Any]:
        """The daemon's presence for this board (``GET /panel``'s ``presence``)."""
        return dict(self._read(state=False).get("presence") or {})

    def identify_until(self) -> float | None:
        """When the daemon's running Identify stops (epoch seconds), or None."""
        until = (self._read(state=False).get("identify") or {}).get("until")
        return float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) \
            else None


class _Reads(_Proxy):
    """A read the service answers in one GET (SERIAL-6900 4a): the ``*_reason`` check and
    the status that follows it are ONE request (the answer is kept for ``VIEW_S``). Reads
    only: the changes (``harness-manager slot|card push/commit/rollback/verify/clear``) run
    on the in-process engine, so these proxies have no such methods."""

    VIEW_S = 1.0
    ROUTE = ""

    def __init__(self, engine: RemoteEngine, board_id: str, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(engine, board_id)
        self._clock = clock
        self._view: tuple[float, dict[str, Any]] | None = None

    def _read(self) -> dict[str, Any]:
        now = self._clock()
        if self._view is not None and now - self._view[0] < self.VIEW_S:
            return self._view[1]
        payload = self._engine._http.get(self._path(self.ROUTE))
        self._view = (now, payload)
        return payload

    def _fresh(self) -> dict[str, Any]:
        payload = self._read()
        self._view = None                    # a status is read once; the next one asks again
        return payload


class _OsSlots(_Reads):
    """``session.os_slots``'s reads over ``GET /boards/{bid}/slots`` (the service's own
    adapter answers; nothing here touches the board)."""

    ROUTE = "slots"
    CAPABILITY = "OS slot update"

    def busy_job(self) -> None:
        """The reset guard's read (``services.reset_guard``): the service checks the card job
        itself when it runs the reset (``_Proxy._forced`` carries ``--force``), so this
        client adds no read of its own: None, as before it had this proxy."""
        return None

    def slots_reason(self) -> str:
        payload = self._read()
        return "" if payload.get("available") else str(payload.get("reason")
                                                       or "the service reports no OS slots")

    def status(self) -> Any:
        from harness_manager.services.slots import slot_status_from_json

        payload = self._fresh()
        if not payload.get("available"):
            raise UnavailableError(self.CAPABILITY, str(payload.get("reason") or
                                                        "the service reports no OS slots"))
        return slot_status_from_json(payload.get("slots") or {})


class _Card(_Reads):
    """``session.card``'s reads over ``GET /boards/{bid}/card`` (the default's RM name and
    the Linux OS slots included: the service annotates its own read)."""

    ROUTE = "card"
    CAPABILITY = "user microSD"

    def card_reason(self) -> str:
        card = self._read().get("card") or {}
        if not card.get("store"):
            return str(card.get("reason") or "this harness has no microSD store")
        return ""

    def status(self) -> CardStatus:
        from harness_manager.services.slots import card_status_from_json

        return card_status_from_json(self._fresh().get("card") or {"store": False})


class _LabClient(_Proxy):
    """The pyverify ``ShellClient`` verbs ``cli/cmd_lab.py`` uses, as daemon lab calls.

    Each returns an object with the attributes the verb reads. A refusal by the
    shell comes back as the daemon's error and is raised as it.
    """

    def _lab(self, verb: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._engine._http.post(self._path(f"lab/{verb}"), body)

    def link(self, event: str) -> Any:
        self._lab("link", {"event": event})
        return SimpleNamespace(ok=True, err="")

    def display_owner(self) -> Any:
        data = self._lab("display", {"owner": "query"})
        return SimpleNamespace(ok=True, err="", owner=data.get("owner"))

    def display_settled(self, owner: str, *, timeout: float = 2.0) -> Any:
        data = self._lab("display", {"owner": owner, "timeout": timeout})
        return SimpleNamespace(ok=True, err="", requested=data.get("requested"),
                               owner=data.get("owner"), landed=data.get("landed"),
                               polls=data.get("polls", 0), waited_s=data.get("waited_s", 0.0))

    def macgen(self, *, gen: bool = True, chk: bool = True, inject: str = "none") -> Any:
        data = self._lab("macgen", {"gen": gen, "chk": chk, "inject": inject})
        return SimpleNamespace(ok=True, tx=data.get("tx"), rx=data.get("rx"),
                               err=data.get("err"))

    def read_dut_frame(self) -> tuple[bytes | None, Any]:
        data = self._lab("dutrx", {"frames": 1})
        frames = data.get("frames") or []
        frame = bytes.fromhex(frames[0]["data"]) if frames else None
        last = SimpleNamespace(ok=True, err="", frames=data.get("frames_waiting"),
                               rx=data.get("rx"), drop_full=data.get("drop_full"),
                               drop_giant=data.get("drop_giant"), ovf=data.get("ovf"),
                               desync=data.get("desync"))
        return frame, last

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise UnavailableError("mps3.shell", f"the shell verb {name!r} is not proxied by "
                                             "harness-manager-daemon; set HARNESS_MANAGER_NO_DAEMON=1 to run it "
                                             "in-process")


class RemoteLabShell(_Proxy):
    """``session.shell`` for the lab verbs: ``call(fn)`` runs ``fn`` on a ``_LabClient``."""

    def call(self, fn: Callable[[Any], Any]) -> Any:
        return fn(_LabClient(self._engine, self._board_id))


class RemoteSession(BoardSession):
    def __init__(self, engine: RemoteEngine, candidate: Candidate, *, owned: bool,
                 adapters: dict[str, bool], reset_targets: tuple[str, ...] = ()) -> None:
        self.candidate = candidate
        self.owned = owned
        self._engine = engine
        bid = candidate.board_id
        has = adapters.get
        self.deploy = _DeployAdapter(engine, bid) if has("deploy") else None
        self.consoles = _ConsoleAdapter(engine, bid) if has("consoles") else None
        self.debug = _DebugAdapter(engine, bid) if has("debug") else None
        self.resets = _Resets(engine, bid, reset_targets) if has("resets") else None
        self.clocks = _Clocks(engine, bid) if has("clocks") else None
        self.telemetry = _Telemetry(engine, bid) if has("telemetry") else None
        self.controller = _Controller(engine, bid) if has("controller") else None
        self.storage = _Storage(engine, bid) if has("storage") else None
        self.power = _Power(engine, bid) if has("power") else None
        self.panel = _Panel(engine, bid) if has("panel") else None
        self.shell = RemoteLabShell(engine, bid) if has("shell") else None
        # SERIAL-6900 4a: the reads of the OS slots and the card (a service that predates
        # them does not list them: no adapter, and the verb says so)
        self.os_slots = _OsSlots(engine, bid) if has("os_slots") else None
        self.card = _Card(engine, bid) if has("card") else None

    def identity(self) -> Any:
        return self._engine.info(self.candidate.board_id).identity

    def health(self) -> Any:
        return self._engine.info(self.candidate.board_id).health


# --- services ----------------------------------------------------------------------------------------


def _bid(session: BoardSession) -> str:
    return session.candidate.board_id


def _overlay_body(overlay: OverlayRef) -> dict[str, Any]:
    return {"name": overlay.name, "rm_id": overlay.rm_id, "static_id": overlay.static_id,
            "source": overlay.source, "static_usercode": overlay.static_usercode}


class RemoteDeploy:
    """``DeployService`` over the API."""

    def __init__(self, engine: RemoteEngine) -> None:
        self._engine = engine

    def _overlays(self, board_id: str) -> list[OverlayRef]:
        payload = self._engine._http.get(f"/boards/{q(board_id)}/overlays")
        return [from_json(OverlayRef, o) for o in payload.get("overlays", [])]

    def _preflight(self, board_id: str, overlay: OverlayRef) -> list[PreflightItem]:
        payload = self._engine._http.post(f"/boards/{q(board_id)}/preflight",
                                          {"overlay": _overlay_body(overlay)})
        return [from_json(PreflightItem, i) for i in payload.get("items", [])]

    def _deploy(self, board_id: str, overlay: OverlayRef,
                progress: Progress | None = None, *, keep_on_card: bool = False,
                force: bool = False, allow_dut_flash_write: bool = False) -> DeployResult:
        body: dict[str, Any] = {"overlay": _overlay_body(overlay)}
        if keep_on_card:                  # sent only when asked: the default writes no card
            body["keep_on_card"] = True
        if force:                         # FIX-PACK-7: only when asked (--force)
            body["force"] = True
        if allow_dut_flash_write:         # FIX-PACK-8: only when given (the typed word, the flag)
            body["allow_dut_flash_write"] = True
        result = self._engine.run_job(f"/boards/{q(board_id)}/deploy", body, progress=progress)
        return from_json(DeployResult, result)

    def _card_status(self, board_id: str) -> CardStatus:
        payload = self._engine._http.get(f"/boards/{q(board_id)}/card")
        return from_json(CardStatus, payload.get("card") or {"store": False})

    def overlays(self, session: BoardSession) -> Sequence[OverlayRef]:
        return self._overlays(_bid(session))

    def compatible(self, session: BoardSession) -> tuple[list[OverlayRef], dict[str, str]]:
        payload = self._engine._http.get(f"/boards/{q(_bid(session))}/overlays")
        return ([from_json(OverlayRef, o) for o in payload.get("loadable", [])],
                {str(k): str(v) for k, v in payload.get("blocked", {}).items()})

    def preflight(self, session: BoardSession, overlay: OverlayRef) -> Sequence[PreflightItem]:
        return self._preflight(_bid(session), overlay)

    def deploy(self, session: BoardSession, overlay: OverlayRef, *,
               keep_on_card: bool = False, force: bool = False,
               allow_dut_flash_write: bool = False) -> DeployResult:
        return self._deploy(_bid(session), overlay, keep_on_card=keep_on_card, force=force,
                            allow_dut_flash_write=allow_dut_flash_write)

    def card_status(self, session: BoardSession) -> CardStatus:
        return self._card_status(_bid(session))

    def restore_baseline(self, session: BoardSession, *, force: bool = False) -> DeployResult:
        body = {"force": True} if force else None          # FIX-PACK-7: only when asked
        result = self._engine.run_job(f"/boards/{q(_bid(session))}/restore", body)
        return from_json(DeployResult, result)


class RemoteConsoleStream:
    """``ConsoleStream`` over the console WebSocket: bytes both ways."""

    def __init__(self, engine: RemoteEngine, board_id: str, name: str) -> None:
        self.name = name
        self.board_id = board_id
        self.dropped = 0
        self.state = "connecting"
        self.detail = ""
        self._error: HarnessError | None = None
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        path = f"/boards/{q(board_id)}/consoles/{q(name)}"
        self._ws = ws_connect(engine._http.ws_url(path))
        self._thread = threading.Thread(target=self._reader, daemon=True,
                                        name=f"harness-manager-console-{name}")
        self._thread.start()

    @property
    def closed(self) -> bool:
        return self._closed

    def _reader(self) -> None:
        try:
            for message in self._ws:
                if isinstance(message, (bytes, bytearray)):
                    with self._cond:
                        self._buf += message
                        excess = len(self._buf) - CONSOLE_BUFFER
                        if excess > 0:
                            del self._buf[:excess]
                            self.dropped += excess
                        self._cond.notify_all()
                    continue
                try:
                    frame = json.loads(message)
                except ValueError:
                    continue
                if "state" in frame:
                    self.state = str(frame["state"])
                    self.detail = str(frame.get("detail", ""))
                if "dropped" in frame:
                    self.dropped += int(frame.get("dropped") or 0)
                if isinstance(frame.get("error"), dict):
                    self._error = error_from_json(frame["error"])
        except Exception as exc:  # noqa: BLE001 - the socket closed
            log.debug("console socket ended: %r", exc)
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    def read(self, timeout: float | None = None) -> bytes:
        with self._cond:
            self._cond.wait_for(lambda: self._buf or self._closed, timeout)
            out = bytes(self._buf)
            self._buf.clear()
            return out

    def write(self, data: bytes) -> None:
        if self._error is not None:
            err, self._error = self._error, None
            raise err
        if self._closed:
            raise UsageError(f"console {self.name!r} subscription is closed")
        try:
            self._ws.send(bytes(data))
        except Exception as exc:  # noqa: BLE001
            raise UnreachableError(f"console {self.name!r}: the daemon connection dropped ({exc})") \
                from exc

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        with self._cond:
            self._closed = True
            self._buf.clear()
            self._cond.notify_all()

    def __enter__(self) -> RemoteConsoleStream:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class RemoteConsoles:
    """``ConsoleBroker`` over the API. ``close_all`` closes THIS client's streams only."""

    def __init__(self, engine: RemoteEngine) -> None:
        self._engine = engine
        self._mu = threading.Lock()
        self._streams: dict[str, list[RemoteConsoleStream]] = {}

    def names(self, session: BoardSession) -> list[str]:
        return list(self._engine._http.get(f"/boards/{q(_bid(session))}/consoles?rates=0")
                    .get("names", []))

    def subscribe(self, session: BoardSession, name: str) -> RemoteConsoleStream:
        stream = RemoteConsoleStream(self._engine, _bid(session), name)
        with self._mu:
            self._streams.setdefault(_bid(session), []).append(stream)
        return stream

    def export_tcp(self, session: BoardSession, name: str, port: int = 0) -> int:
        payload = self._engine._http.post(
            f"/boards/{q(_bid(session))}/consoles/{q(name)}/export", {"port": port})
        return int(payload["port"])

    # -- lane L2: PTYs for screen, and baud (docs/API.md "Week-plan additions") ----------------

    @staticmethod
    def _console_path(board_id: str, name: str, leaf: str) -> str:
        return f"/boards/{q(board_id)}/consoles/{q(name)}/{leaf}"

    def pty(self, session: BoardSession, name: str) -> dict[str, Any]:
        """The daemon's PTY for console ``name`` (created if needed): ``{name, path, device,
        command, clients}``. It lives in the daemon while the board is open there."""
        payload = self._engine._http.post(self._console_path(_bid(session), name, "pty"))
        return _pick(payload, ("name", "path", "device", "command", "clients"))

    def pty_info(self, board_id: str, name: str) -> dict[str, Any] | None:
        pty = self._engine._http.get(self._console_path(board_id, name, "pty")).get("pty")
        return dict(pty) if isinstance(pty, dict) else None

    def close_pty(self, board_id: str, name: str) -> bool:
        payload = self._engine._http.delete(self._console_path(board_id, name, "pty"))
        return bool(payload.get("closed", True))

    def baud(self, session: BoardSession, name: str) -> dict[str, Any]:
        """``{name, kind, baud, settable, reason, choices, source, ...}``."""
        payload = self._engine._http.get(self._console_path(_bid(session), name, "baud"))
        return {k: v for k, v in payload.items() if k not in ("ok", "board_id")}

    def set_baud(self, session: BoardSession, name: str, baud: int) -> dict[str, Any]:
        """``{name, baud, source, ...}``; UNAVAILABLE (with the reason) when it cannot change."""
        payload = self._engine._http.post(self._console_path(_bid(session), name, "baud"),
                                          {"baud": baud})
        return {k: v for k, v in payload.items() if k not in ("ok", "board_id")}

    def consoles(self, session: BoardSession) -> list[dict[str, Any]]:
        """``GET /boards/{bid}/consoles`` rows: ``{name, kind, baud, settable, pty, ...}``."""
        rows = self._engine._http.get(f"/boards/{q(_bid(session))}/consoles").get("consoles")
        return [dict(r) for r in rows] if isinstance(rows, list) else []

    def close_all(self, board_id: str) -> None:
        with self._mu:
            streams = self._streams.pop(board_id, [])
        for stream in streams:
            stream.close()

    def close_everything(self) -> None:
        with self._mu:
            boards = list(self._streams)
        for board_id in boards:
            self.close_all(board_id)


def _pick(payload: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {k: payload[k] for k in keys if k in payload}


class RemoteDebug:
    """``DebugService`` over the API. OpenOCD runs in (and belongs to) the daemon."""

    def __init__(self, engine: RemoteEngine) -> None:
        self._engine = engine

    def detect(self, session: BoardSession) -> str:
        return str(self._engine._http.post(f"/boards/{q(_bid(session))}/debug/detect")["idcode"])

    def up(self, session: BoardSession) -> DebugStatus:
        return from_json(DebugStatus, self._engine.run_job(f"/boards/{q(_bid(session))}/debug/up"))

    def down(self, session: BoardSession) -> DebugStatus:
        return from_json(DebugStatus,
                         self._engine._http.post(f"/boards/{q(_bid(session))}/debug/down"))

    def status(self, session: BoardSession) -> DebugStatus:
        return from_json(DebugStatus, self._engine._http.get(f"/boards/{q(_bid(session))}/debug"))

    def openocd_report(self, session: BoardSession | None = None) -> dict[str, Any] | None:
        """The daemon's OpenOCD verdict (``services.debug.openocd_report``): it runs OpenOCD,
        so its binary is the one that counts. ``None``: an older daemon, or no board."""
        if session is None:
            return None
        got = self._engine._http.get(f"/boards/{q(_bid(session))}/debug").get("openocd")
        return got if isinstance(got, dict) else None


class RemoteXvc:
    """The XVC service (``services.xvc``) over the API. The relay and hw_server run in, and
    belong to, the daemon; ``ltx`` returns the file's metadata (the daemon is local, so its
    ``path`` is readable here)."""

    def __init__(self, engine: RemoteEngine) -> None:
        self._engine = engine

    def _path(self, session: BoardSession, leaf: str = "") -> str:
        return f"/boards/{q(_bid(session))}/xvc{leaf}"

    @staticmethod
    def _status(payload: dict[str, Any]) -> Any:
        from harness_manager.services.xvc import XvcStatus

        return from_json(XvcStatus, {k: v for k, v in payload.items()
                                     if k not in ("ok", "board_id")})

    def status(self, session: BoardSession, *, refresh: bool = False) -> Any:
        return self._status(self._engine._http.get(self._path(session)))

    def open(self, session: BoardSession, *, byo: bool = False) -> Any:
        return self._status(self._engine.run_job(self._path(session, "/open"), {"byo": byo}))

    def close(self, session: BoardSession, *, reason: str = "") -> Any:
        return self._status(self._engine._http.post(self._path(session, "/close")))

    def tcl(self, session: BoardSession, *, byo: bool | None = None,
            refresh: bool = False) -> dict[str, Any]:
        leaf = "/tcl" if byo is None else f"/tcl?byo={'true' if byo else 'false'}"
        payload = self._engine._http.get(self._path(session, leaf))
        return {k: v for k, v in payload.items() if k not in ("ok", "board_id")}

    def ltx(self, session: BoardSession, which: str = "auto", *,
            refresh: bool = False) -> dict[str, Any]:
        payload = self._engine._http.get(self._path(session, f"/ltx?which={q(which)}&format=json"))
        return {k: v for k, v in payload.items() if k not in ("ok", "board_id")}


class RemoteSlots:
    """``services.slots.SlotService`` over the API (FIX-PACK-6 item 1): the CLI's ``slot`` and
    ``card`` changes go through the service that holds the board, as ``program``, ``restore``
    and ``mcc reboot`` do. H1 Z1: ``card clear`` ran in-process and the service's board lock
    refused it ("in use — held by … harness-manager-daemon").

    The reads (``slots``, ``card``, ``status``, ``card_status``) are the session's proxies
    (``_OsSlots``/``_Card``); each change is the daemon's job (card_api.py), which checks the
    lease and the card itself, so ``confirm: true`` is sent only after the CLI asked. The
    results have the in-process service's shapes."""

    def __init__(self, engine: RemoteEngine) -> None:
        from harness_manager.services.slots import SlotService

        self._engine = engine
        self._reads = SlotService()

    def _path(self, session: BoardSession, leaf: str) -> str:
        return f"/boards/{q(_bid(session))}/{leaf}"

    @staticmethod
    def _status(doc: Any) -> Any:
        from harness_manager.services.slots import slot_status_from_json

        return slot_status_from_json(doc or {})

    @staticmethod
    def _changed(session: BoardSession) -> None:
        """After a change, the proxies' kept answer (``_Reads.VIEW_S``) is stale."""
        for name in ("os_slots", "card"):
            proxy = getattr(session, name, None)
            if isinstance(proxy, _Reads):
                proxy._view = None

    def _job(self, session: BoardSession, leaf: str, body: dict[str, Any],
             progress: Progress | None = None) -> dict[str, Any]:
        try:
            out = self._engine.run_job(self._path(session, leaf), body, progress=progress)
        finally:
            self._changed(session)
        return out if isinstance(out, dict) else {}

    # -- reads (the session's proxies) ----------------------------------------------------------

    def slots(self, session: BoardSession) -> Any:
        return self._reads.slots(session)

    def card(self, session: BoardSession) -> Any:
        return self._reads.card(session)

    def status(self, session: BoardSession) -> Any:
        return self._reads.status(session)

    def card_status(self, session: BoardSession) -> CardStatus:
        return self._reads.card_status(session)

    # -- the changes (daemon jobs) ----------------------------------------------------------------

    def push(self, session: BoardSession, source: Any, *, rollback_first: bool = False,
             progress: Progress | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"confirm": True, "image": _absolute(source.image),
                                "static_id": source.static_id, "version": source.version,
                                "rollback_first": bool(rollback_first)}
        bundle = getattr(source, "bundle", None)
        if bundle:
            body["bundle"] = _absolute(bundle)
        out = self._job(session, "slots/push", body, progress)
        return {"act": "push", "slot": out.get("slot", ""),
                "rolled_back_first": out.get("rolled_back_first", ""),
                "status": self._status(out.get("slots"))}

    def commit(self, session: BoardSession, slot: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"confirm": True}
        if slot:
            body["slot"] = slot
        out = self._job(session, "slots/commit", body)
        return {"act": "commit", "slot": out.get("slot", ""), "note": out.get("note", ""),
                "status": self._status(out.get("slots"))}

    def verify(self, session: BoardSession, slot: str | None = None,
               progress: Progress | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"slot": slot} if slot else {}
        out = self._job(session, "slots/verify", body, progress)
        return {"act": "verify", "slot": out.get("slot", ""),
                "status": self._status(out.get("slots"))}

    def rollback(self, session: BoardSession, *, reboot: bool = True, wait_s: float = 180.0,
                 progress: Progress | None = None) -> dict[str, Any]:
        out = self._job(session, "slots/rollback",
                        {"confirm": True, "reboot": bool(reboot), "wait_s": wait_s}, progress)
        result: dict[str, Any] = {"act": "rollback", "slot": out.get("slot", ""),
                                  "rebooted": bool(out.get("rebooted")),
                                  "note": out.get("note", ""),
                                  "status": self._status(out.get("slots"))}
        if "evidence" in out:
            result["evidence"] = out["evidence"]
        return result

    def card_commit(self, session: BoardSession,
                    progress: Progress | None = None) -> dict[str, Any]:
        out = self._job(session, "card/commit", {"confirm": True}, progress)
        return dict(out.get("committed") or {})

    def card_clear(self, session: BoardSession) -> CardStatus:
        from harness_manager.services.slots import card_status_from_json

        out = self._job(session, "card/clear", {"confirm": True})
        return card_status_from_json(out.get("card") or {"store": False})


class RemoteClaim:
    """The SSH claim service (``services.claim``) over the API. The daemon asks the board and
    writes the pin; ``check_lease`` is left to the route (409 HELD before the job starts)."""

    def __init__(self, engine: RemoteEngine) -> None:
        self._engine = engine

    def _path(self, session: BoardSession) -> str:
        return f"/boards/{q(_bid(session))}/claim"

    def check_claimable(self, session: BoardSession) -> None:
        """Nothing to claim (bare metal): refused here, before the question."""
        if self.status(session) is None:
            from harness_manager.services.claim import CAPABILITY, NO_ADAPTER

            raise UnavailableError(CAPABILITY, NO_ADAPTER)

    def status(self, session: BoardSession, *, refresh: bool = False) -> Any:
        leaf = "?refresh=true" if refresh else ""
        return self._engine._http.get(self._path(session) + leaf).get("claim")

    def refresh(self, session: BoardSession) -> Any:
        return self.status(session, refresh=True)

    def claim(self, session: BoardSession, *, confirm: bool, key: str | None = None,
              adopt: bool = False, replace_host_key: bool = False,
              expect_host_key: str | None = None, progress: Any = None) -> Any:
        def phase(text: str, _done: int, _total: int) -> None:
            if progress is not None:
                progress(text)

        body = {"confirm": bool(confirm), "adopt": bool(adopt),
                "replace_host_key": bool(replace_host_key)}
        if expect_host_key:
            body["expect_host_key"] = str(expect_host_key)
        if key:
            body["key"] = str(Path(key).expanduser().resolve())
        out = self._engine.run_job(self._path(session), body, progress=phase)
        return (out or {}).get("claim") if isinstance(out, dict) else out

    def repin(self, session: BoardSession, *, confirm: bool, fingerprint: str,
              progress: Any = None) -> Any:
        def phase(text: str, _done: int, _total: int) -> None:
            if progress is not None:
                progress(text)

        body = {"confirm": bool(confirm), "fingerprint": str(fingerprint)}
        out = self._engine.run_job(f"/boards/{q(_bid(session))}/repin", body,
                                   progress=phase)
        return (out or {}).get("claim") if isinstance(out, dict) else out

    def ssh_argv(self, session: BoardSession, command: Any = (), *, tty: bool = False) -> list[str]:
        import shlex

        leaf = f"?command={q(shlex.join(list(command)))}" if command else ""
        argv = list(self._engine._http.get(f"/boards/{q(_bid(session))}/ssh{leaf}")["argv"])
        if tty and "-t" not in argv:
            argv.insert(argv.index("-l") if "-l" in argv else len(argv), "-t")
        return argv


class RemoteIdentity:
    """The board identity service (``services.board_identity``) over the API. The daemon reads
    the board and the hub and runs the fix; the checks that need the lease run in its route
    (409 HELD before the job starts)."""

    def __init__(self, engine: RemoteEngine) -> None:
        self._engine = engine

    def _path(self, session: BoardSession) -> str:
        return f"/boards/{q(_bid(session))}/identity"

    def status(self, session: BoardSession, *, refresh: bool = False,
               cheap: bool = False) -> Any:
        leaf = "?refresh=true" if refresh else ""
        return self._engine._http.get(self._path(session) + leaf).get("identity")

    @staticmethod
    def _body(want: Any, from_hub: bool, clear: bool, hub_fixed: str,
              other_subnet: bool, confirm_subnet: bool = False) -> dict[str, Any]:
        """The POST body; a field the CLI drops (``""``) goes as ``unset``."""
        w = dict(want or {})
        unset = [k for k, v in w.items() if v == ""]
        body: dict[str, Any] = {"from_hub": bool(from_hub), "clear": bool(clear),
                                **{k: v for k, v in w.items() if v != ""}}
        if unset:
            body["unset"] = unset
        if hub_fixed:
            body["hub_fixed"] = hub_fixed
        if other_subnet:
            body["other_subnet"] = True
        if confirm_subnet:
            body["confirm_subnet"] = True
        return body

    def preflight(self, session: BoardSession, *, want: Any = None, from_hub: bool = False,
                  clear: bool = False, hub_fixed: str = "",
                  other_subnet: bool = False, confirm_subnet: bool = False) -> Any:
        """Lane IDENTITY: the job's checks and choices, nothing sent (``dry_run``)."""
        body = {**self._body(want, from_hub, clear, hub_fixed, other_subnet, confirm_subnet),
                "dry_run": True}
        return self._engine._http.post(self._path(session), body).get("preflight") or {}

    def propose(self, session: BoardSession, **query: Any) -> Any:
        """Lane IDENTITY: GET .../identity/proposal."""
        from urllib.parse import urlencode

        qs = urlencode({k: v for k, v in query.items() if v not in (None, "")})
        return self._engine._http.get(self._path(session) + "/proposal"
                                      + (f"?{qs}" if qs else "")).get("proposal")

    def fix(self, session: BoardSession, *, confirm: str, want: Any = None,
            from_hub: bool = False, clear: bool = False, wait_s: float | None = None,
            progress: Any = None, hub_fixed: str = "", other_subnet: bool = False,
            confirm_subnet: bool = False) -> Any:
        def phase(text: str, _done: int, _total: int) -> None:
            if progress is not None:
                progress(text)

        body: dict[str, Any] = {"confirm": confirm,
                                **self._body(want, from_hub, clear, hub_fixed, other_subnet,
                                             confirm_subnet)}
        if wait_s is not None:
            body["wait_s"] = wait_s
        out = self._engine.run_job(self._path(session), body, progress=phase)
        return out if isinstance(out, dict) else {}


class RemoteTelemetry:
    """``TelemetryService`` over the API."""

    def __init__(self, engine: RemoteEngine) -> None:
        self._engine = engine

    def readings(self, session: BoardSession) -> list[Reading]:
        return _readings(self._engine._http.get(f"/boards/{q(_bid(session))}/telemetry"))
