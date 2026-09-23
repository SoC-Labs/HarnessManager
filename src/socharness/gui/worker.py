"""Every engine call runs here, on a worker thread, never on the GUI thread.

``Runner.submit(fn, on_done, budget_s=...)`` runs ``fn`` on a ``QThreadPool``
and calls ``on_done(TaskResult)`` back on the GUI thread through a queued
signal. A call that has not answered within its budget gets ``on_overdue``
once ("still running after N s"); it is never killed and never blocks, and
its result is still delivered when it lands.

The result carries the exit code (``rc``) the CLI would return for the same
outcome, so a panel can print ``$ action  (rc N, T s)`` above the answer.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot

from socharness.core.errors import ExitCode, HarnessError, HeldError

log = logging.getLogger(__name__)


def rc_for(exc: BaseException | None) -> int:
    if exc is None:
        return int(ExitCode.OK)
    if isinstance(exc, HarnessError):
        return int(exc.code)
    return int(ExitCode.FAILED)


def describe_error(exc: BaseException) -> str:
    """The engine's own words: what went wrong, then the next action."""
    if isinstance(exc, HarnessError):
        lines = [f"{exc.code.name.replace('_', ' ')}: {exc.message}"]
        if isinstance(exc, HeldError) and exc.holder:
            lines.append(f"holder: {exc.holder}")
        if exc.hint:
            lines.append(f"next: {exc.hint}")
        return "\n".join(lines)
    return f"FAILED (a bug, not a board problem): {type(exc).__name__}: {exc}"


@dataclass(frozen=True)
class TaskResult:
    value: Any = None
    error: BaseException | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def rc(self) -> int:
        return rc_for(self.error)


def seconds_text(seconds: float | None) -> str:
    """ "0.3", "2.5", "120": one decimal below ten seconds."""
    value = seconds or 0.0
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


def rc_line(command: str, result: TaskResult) -> str:
    return f"$ {command}  (rc {result.rc}, {result.seconds:.1f} s)"


class _Emitter(QObject):
    finished = Signal(int, object)


class _Task(QRunnable):
    def __init__(self, task_id: int, fn: Callable[[], Any], emitter: _Emitter) -> None:
        super().__init__()
        self._id, self._fn, self._emitter = task_id, fn, emitter

    def run(self) -> None:
        t0 = time.monotonic()
        try:
            result = TaskResult(value=self._fn(), seconds=time.monotonic() - t0)
        except BaseException as exc:  # noqa: BLE001 - every outcome goes back to the GUI
            result = TaskResult(error=exc, seconds=time.monotonic() - t0)
        try:
            self._emitter.finished.emit(self._id, result)
        except RuntimeError:  # the GUI side is gone (window closed during the call)
            log.debug("task %d finished after its runner was deleted", self._id)


@dataclass
class TaskHandle:
    id: int
    label: str
    started: float
    budget_s: float | None
    overdue: bool = False
    done: bool = False

    def elapsed(self) -> float:
        return time.monotonic() - self.started


class Runner(QObject):
    """Submit engine calls; results come back on the GUI thread."""

    def __init__(self, pool: QThreadPool | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = pool or QThreadPool.globalInstance()
        self._ids = itertools.count(1)
        self._live: dict[int, tuple[TaskHandle, Callable[[TaskResult], None],
                                    _Emitter, QTimer | None]] = {}

    @property
    def pool(self) -> QThreadPool:
        return self._pool

    def submit(self, fn: Callable[[], Any], on_done: Callable[[TaskResult], None], *,
               label: str = "", budget_s: float | None = None,
               on_overdue: Callable[[TaskHandle], None] | None = None) -> TaskHandle:
        task_id = next(self._ids)
        handle = TaskHandle(task_id, label, time.monotonic(), budget_s)
        emitter = _Emitter(self)
        emitter.finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        timer = None
        if budget_s is not None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(max(1, int(budget_s * 1000)))
            timer.timeout.connect(lambda: self._overdue(task_id, on_overdue))
            timer.start()
        self._live[task_id] = (handle, on_done, emitter, timer)
        self._pool.start(_Task(task_id, fn, emitter))
        return handle

    def pending(self) -> int:
        return len(self._live)

    def wait(self, msecs: int = 5000) -> bool:
        """Block until the pool drains. Only for shutdown (after the event loop) and tests."""
        return self._pool.waitForDone(msecs)

    def _overdue(self, task_id: int, on_overdue: Callable[[TaskHandle], None] | None) -> None:
        entry = self._live.get(task_id)
        if entry is None:
            return
        handle = entry[0]
        handle.overdue = True
        # INFO: the caller shows (and logs) the overdue state in its own words.
        log.info("%s still running after %s s", handle.label or "engine call",
                 seconds_text(handle.budget_s))
        if on_overdue is not None:
            on_overdue(handle)

    @Slot(int, object)
    def _deliver(self, task_id: int, result: TaskResult) -> None:
        entry = self._live.pop(task_id, None)
        if entry is None:
            return
        handle, on_done, emitter, timer = entry
        handle.done = True
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        emitter.deleteLater()
        on_done(result)
