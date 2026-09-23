"""GUI test fixtures (Team T6). Offscreen Qt, the demo engine, and clean teardown.

Every GUI test module starts with ``pytest.importorskip`` for PySide6 and
pytest-qt, so ``make check`` skips them cleanly where those are not installed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator

import pytest

# Must be set before any QApplication exists.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
    import pytestqt  # noqa: F401
    HAVE_QT = True
except ImportError:  # pragma: no cover - exercised where PySide6 is absent
    HAVE_QT = False

# The demo engine has no Qt in it, so it is importable either way.
from socharness.gui.demo_engine import DemoEngine  # noqa: E402

FAST = 0.02     # DemoEngine speed: 2 % of the demo's pacing


@pytest.fixture
def engine() -> Iterator[DemoEngine]:
    eng = DemoEngine(speed=FAST)
    yield eng
    eng.close_all()


if HAVE_QT:
    from PySide6.QtCore import QThreadPool

    @pytest.fixture
    def runner(qapp):
        from socharness.gui.worker import Runner

        r = Runner()
        yield r
        r.wait(10000)
        drain(qapp)

    @pytest.fixture
    def bridge(qapp, engine):
        from socharness.gui.bridge import EventBridge

        b = EventBridge(engine.bus)
        yield b
        b.close()

    @pytest.fixture
    def open_window(qtbot, engine, runner, bridge) -> Iterator[Callable]:
        """``open_window(board_id)`` opens the board in the engine and shows its main window."""
        from socharness.gui.main_window import MainWindow
        from socharness.gui.tabs.consoles import console_pool

        windows = []

        def _open(board_id: str, *, wait_info: bool = True):
            cand = engine.candidate_for(board_id.split("@", 1)[1])
            engine.open(cand, note="gui test")
            win = MainWindow(engine, cand, runner, bridge)
            qtbot.addWidget(win)
            win.show()
            windows.append(win)
            if wait_info:
                qtbot.waitUntil(lambda: win.ctx.info is not None, timeout=5000)
            return win

        yield _open
        for win in windows:
            win.shutdown()
        runner.wait(10000)
        engine.close_all()
        console_pool().waitForDone(10000)
        QThreadPool.globalInstance().waitForDone(10000)
        drain(qtbot)


def drain(_qt_owner=None) -> None:
    """Deliver queued results while their widgets still exist (so none land in a later test)."""
    from PySide6.QtWidgets import QApplication

    for _ in range(5):
        QApplication.processEvents()


def wait_for(qtbot, predicate: Callable[[], bool], timeout_ms: int = 5000) -> None:
    qtbot.waitUntil(predicate, timeout=timeout_ms)


def ticks_during(qtbot, done: Callable[[], bool], interval_ms: int = 20,
                 timeout_ms: int = 8000) -> list[float]:
    """Timestamps of a QTimer firing on the GUI thread until ``done()``."""
    from PySide6.QtCore import QTimer

    stamps: list[float] = []
    timer = QTimer()
    timer.setInterval(interval_ms)
    timer.timeout.connect(lambda: stamps.append(time.monotonic()))
    timer.start()
    try:
        qtbot.waitUntil(done, timeout=timeout_ms)
    finally:
        timer.stop()
    return stamps
