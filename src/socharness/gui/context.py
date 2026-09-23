"""One board's view model: what every tab of the main window shares.

``BoardContext`` holds the engine, the board id, the worker ``Runner`` and the
latest ``BoardInfo``. It re-reads ``engine.info(board_id)`` whenever the bus
says the board changed, so a capability that appears (a cable plugged in, new
firmware) enables its buttons without a restart. It holds no board logic: the
capability view is the engine's, rendered as it came.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal

from socharness.core.events import Event
from socharness.core.model import BoardInfo, Candidate

from .bridge import EventBridge
from .worker import Runner, TaskResult, describe_error, rc_line

# Events after which the capability view may have changed.
REFRESH_TOPICS = frozenset({
    "board.identity", "board.found", "board.lost", "session.opened", "session.closed",
    "deploy.done", "deploy.failed",
})


SERVICES = ("deploy", "consoles", "debug")


def service_reasons(engine: Any) -> dict[str, str]:
    """{service: why it is a stub} for each engine service that cannot work in this build.

    The engine puts a stub with a ``reason`` in place of a service that is not
    installed; its every call would raise ``UnavailableError``. Asked on a worker:
    the first touch of a service may import it.
    """
    out: dict[str, str] = {}
    for name in SERVICES:
        try:
            reason = getattr(getattr(engine, name), "reason", None)
        except Exception as exc:  # noqa: BLE001 - a service that cannot load is unavailable
            reason = f"failed to load: {exc}"
        if isinstance(reason, str) and reason:
            out[name] = reason
    return out


def capability_titles(engine: Any, pack: str) -> dict[str, str]:
    """Capability name -> the title its board pack gives it, in the pack's order."""
    packs = engine.packs()
    specs = packs[pack].capability_specs() if pack in packs else ()
    return {spec.name: spec.title for spec in specs}


class BoardContext(QObject):
    info_changed = Signal(object)          # BoardInfo | None (None: the read failed)
    board_event = Signal(object)           # an Event for this board, on the GUI thread
    session_changed = Signal(bool)
    logged = Signal(str, str, str)         # level, source, text

    def __init__(self, engine: Any, candidate: Candidate, runner: Runner, bridge: EventBridge,
                 *, session_open: bool = True, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.candidate = candidate
        self.board_id = candidate.board_id
        self.runner = runner
        self.bridge = bridge
        self.info: BoardInfo | None = None
        self.info_error = ""
        self.info_rc_line = ""
        self.titles: dict[str, str] = {}
        self.services_missing: dict[str, str] = {}
        self.session_open = session_open
        self._info_inflight = False
        self._info_dirty = False
        bridge.event.connect(self._on_event)

    # -- capabilities -----------------------------------------------------------------

    def capability(self, name: str) -> tuple[bool, str] | None:
        """(available, reason) as the engine reported it; None before the first read."""
        if self.info is None:
            return None
        if name in self.info.capabilities:
            return True, ""
        reason = self.info.unavailable.get(name)
        if reason is None:
            return False, f"this board pack does not offer '{name}'"
        return False, reason

    def service_missing(self, service: str) -> str:
        """Why an engine service cannot work in this build, or ""."""
        why = self.services_missing.get(service, "")
        return f"the {service} service is unavailable: {why}" if why else ""

    def title(self, name: str) -> str:
        return self.titles.get(name, name)

    # -- the info read ------------------------------------------------------------------

    def refresh_info(self) -> None:
        if self._info_inflight:
            self._info_dirty = True
            return
        self._info_inflight = True
        engine, board_id, pack = self.engine, self.board_id, self.candidate.pack
        need_titles = not self.titles

        def work() -> tuple[BoardInfo, dict[str, str] | None, dict[str, str]]:
            info = engine.info(board_id)
            titles = capability_titles(engine, pack) if need_titles else None
            return info, titles, service_reasons(engine)

        self.runner.submit(work, self._info_done, label="info", budget_s=20.0)

    def _info_done(self, result: TaskResult) -> None:
        self._info_inflight = False
        self.info_rc_line = rc_line("info", result)
        if result.ok:
            info, titles, self.services_missing = result.value
            self.info, self.info_error = info, ""
            if titles is not None:
                self.titles = titles
        else:
            self.info_error = describe_error(result.error)  # type: ignore[arg-type]
            self.log("error", "info", f"{self.info_rc_line}\n{self.info_error}")
        self.info_changed.emit(self.info if result.ok else None)
        if self._info_dirty:
            self._info_dirty = False
            self.refresh_info()

    # -- events and logging ---------------------------------------------------------------

    def _on_event(self, ev: Event) -> None:
        if ev.board_id != self.board_id:
            return
        self.board_event.emit(ev)
        if ev.topic == "session.closed" and self.session_open:
            self.set_session_open(False)
        elif ev.topic == "session.opened" and not self.session_open:
            self.set_session_open(True)
        if ev.topic in REFRESH_TOPICS or (
                ev.topic == "controller.reboot" and ev.data.get("phase") == "up"):
            self.refresh_info()

    def set_session_open(self, is_open: bool) -> None:
        self.session_open = is_open
        self.session_changed.emit(is_open)

    def log(self, level: str, source: str, text: str) -> None:
        self.logged.emit(level, source, text)
