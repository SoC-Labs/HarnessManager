"""The GUI thread never blocks: engine calls run on workers, results and events come back
on the GUI thread, and an overdue call says "still running after N s"."""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QThread, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from socharness.core.events import Event  # noqa: E402
from socharness.gui.bridge import EventBridge, topic_matches  # noqa: E402
from socharness.gui.context import BoardContext  # noqa: E402
from socharness.gui.demo_engine import BOARD_FIELDED, BOARD_USB  # noqa: E402
from socharness.gui.panels import PanelAction, PanelDesc, PanelWidget  # noqa: E402
from socharness.gui.worker import rc_line  # noqa: E402

from .conftest import ticks_during  # noqa: E402

pytestmark = pytest.mark.gui

SLOW_S = 2.0


def max_gap(stamps: list[float]) -> float:
    return max(b - a for a, b in zip(stamps, stamps[1:], strict=False))


def test_a_2s_engine_call_does_not_block_the_event_loop(qtbot, open_window, engine):
    win = open_window(BOARD_USB)
    win.tabs.setCurrentWidget(win.debug)
    panel = win.debug.panel
    engine.delays["debug.detect"] = SLOW_S
    t0 = time.monotonic()
    panel.buttons["detect"].click()
    assert panel.buttons["detect"].text().startswith("Detecting...")
    assert not panel.buttons["detect"].isEnabled()
    stamps = ticks_during(qtbot, lambda: "(rc 0," in panel.answer_text())
    took = time.monotonic() - t0
    assert took >= SLOW_S * 0.95
    assert len(stamps) >= 40                    # a 20 ms timer kept firing throughout
    assert max_gap(stamps) < 0.25
    assert panel.buttons["detect"].text() == "Detect"


def test_negative_twin_the_probe_catches_a_blocked_gui_thread(qtbot, open_window, engine):
    # The same measurement, with the engine called ON the GUI thread: the timer stalls.
    open_window(BOARD_USB)
    engine.delays["debug.detect"] = 0.6
    session = engine.session(BOARD_USB)
    done: list = []
    stamps: list[float] = []
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: stamps.append(time.monotonic()))
    timer.start()
    QTimer.singleShot(100, lambda: done.append(engine.debug.detect(session)))
    qtbot.wait(1000)
    timer.stop()
    assert done == ["0x6ba00477"]
    assert max_gap(stamps) >= 0.5


def budget_panel(qtbot, engine, runner, bridge, call, budget_s: float) -> PanelWidget:
    cand = engine.candidate_for("192.168.10.101")
    engine.open(cand)
    ctx = BoardContext(engine, cand, runner, bridge)
    panel = PanelWidget(PanelDesc("t", "test", actions=(
        PanelAction("go", "Go", call=call, command="go", busy_label="Going...",
                    budget_s=budget_s, render=str),)), ctx)
    qtbot.addWidget(panel)
    panel.show()
    return panel


def test_overdue_call_says_still_running_and_then_lands(qtbot, engine, runner, bridge):
    panel = budget_panel(qtbot, engine, runner, bridge, lambda env: time.sleep(1.0) or "late",
                         budget_s=0.3)
    panel.trigger("go")
    qtbot.waitUntil(lambda: "still running after 0.3 s" in panel.answer_text(), timeout=3000)
    assert "still running after" in panel.reasons["go"].text()
    assert panel.buttons["go"].text().startswith("Going...")
    qtbot.waitUntil(lambda: "(rc 0," in panel.answer_text(), timeout=5000)
    assert panel.answer_text().splitlines()[-1] == "late"


def test_negative_twin_a_fast_call_never_says_still_running(qtbot, engine, runner, bridge):
    panel = budget_panel(qtbot, engine, runner, bridge, lambda env: "quick", budget_s=5.0)
    panel.trigger("go")
    qtbot.waitUntil(lambda: "(rc 0," in panel.answer_text(), timeout=5000)
    assert "still running" not in panel.answer_text()


def test_results_and_events_arrive_on_the_gui_thread(qtbot, engine, runner, bridge):
    gui = QApplication.instance().thread()
    seen: list = []
    bridge.event.connect(lambda ev: seen.append((ev.topic, QThread.currentThread() is gui)))
    worker = threading.Thread(target=lambda: engine.bus.publish(Event("x.test", BOARD_FIELDED)))
    worker.start()
    worker.join()
    qtbot.waitUntil(lambda: ("x.test", True) in seen, timeout=3000)

    landed: list = []
    runner.submit(lambda: threading.current_thread().name,
                  lambda res: landed.append((res.value, QThread.currentThread() is gui)))
    qtbot.waitUntil(lambda: bool(landed), timeout=3000)
    worker_name, on_gui = landed[0]
    assert on_gui and worker_name != threading.main_thread().name


def test_negative_twin_a_closed_bridge_delivers_nothing(qtbot, engine):
    bridge = EventBridge(engine.bus)
    seen: list = []
    bridge.event.connect(seen.append)
    bridge.close()
    engine.bus.publish(Event("x.test", BOARD_FIELDED))
    qtbot.wait(100)
    assert seen == []


def test_topic_matching_follows_the_bus_rules():
    assert topic_matches("*", "deploy.done")
    assert topic_matches("deploy.*", "deploy.done")
    assert not topic_matches("deploy.*", "debug.state")
    assert topic_matches("deploy.done", "deploy.done")


def test_rc_line_carries_the_exit_code(qtbot, runner):
    from socharness.core.errors import HeldError

    results: list = []
    runner.submit(lambda: (_ for _ in ()).throw(HeldError("x is in use", holder="bob")),
                  results.append)
    qtbot.waitUntil(lambda: bool(results), timeout=3000)
    assert rc_line("open x", results[0]).startswith("$ open x  (rc 4, ")
