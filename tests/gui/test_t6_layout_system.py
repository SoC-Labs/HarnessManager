"""The main window fits 1280x800 with no horizontal scrollbar in any tab; the System tab
renders the three-state build check and telemetry with its sources."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QPoint, QSize  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractScrollArea,
    QLabel,
    QScrollArea,
    QTabWidget,
    QWidget,
)

from socharness.core.model import Check  # noqa: E402
from socharness.gui.demo_engine import BOARD_FIELDED, BOARD_USB  # noqa: E402

pytestmark = pytest.mark.gui

SHOTS = Path(__file__).parent / "screenshots"


def overflow(window: QWidget) -> list[str]:
    """Every visible horizontal scrollbar, and every visible widget poking out of the window."""
    bad = []
    for area in window.findChildren(QAbstractScrollArea):
        if area.isVisible() and area.horizontalScrollBar().isVisible():
            bad.append(f"h-scrollbar on {type(area).__name__} {area.objectName()!r}")
    width = window.width()
    for w in window.findChildren(QWidget):
        if w.isVisible() and w.isWindow() is False:
            right = w.mapTo(window, QPoint(w.width(), 0)).x()
            if right > width + 1:
                bad.append(f"{type(w).__name__} {w.objectName()!r} ends at x={right} > {width}")
    return bad


def shot_name(tab: str) -> str:
    return tab.replace("&&", "and").replace("/", "-").replace(" ", "_")


def every_page(qtbot, win):
    """Show every tab (and every nested tab), yielding a name for each."""
    for i in range(win.tabs.count()):
        win.tabs.setCurrentIndex(i)
        qtbot.wait(60)
        page = win.tabs.widget(i)
        nested = page.findChildren(QTabWidget)
        if not nested:
            yield win.tabs.tabText(i)
        for sub in nested:
            for j in range(sub.count()):
                sub.setCurrentIndex(j)
                qtbot.wait(40)
                yield f"{win.tabs.tabText(i)}/{sub.tabText(j)}"


def test_main_window_fits_1280x800_in_every_tab(qtbot, open_window, engine):
    # The busiest board: five consoles, USB links, and an interrupted SD install to show.
    engine.set_sd_journal(BOARD_USB, {"op": "install", "state": "interrupted",
                                      "backup": {"path": "/tmp/backup.zip"}})
    win = open_window(BOARD_USB)
    qtbot.waitUntil(lambda: win.reset.sd.isVisible(), timeout=5000)
    win.tabs.setCurrentWidget(win.consoles)
    qtbot.waitUntil(lambda: len(win.consoles.views) == 5, timeout=5000)
    win.tabs.setCurrentWidget(win.program)
    qtbot.waitUntil(lambda: len(win.program.overlays) == 6, timeout=5000)
    win.program.select_overlay("nanosoc_multicore")      # the longest reason text
    qtbot.waitUntil(lambda: win.program.preflight_items is not None, timeout=5000)
    assert win.size() == QSize(1280, 800)
    hint = win.minimumSizeHint()
    assert hint.width() <= 1280 and hint.height() <= 800, hint
    seen = []
    for name in every_page(qtbot, win):
        seen.append(name)
        assert overflow(win) == [], name
        if os.environ.get("SOCHARNESS_GUI_SHOTS") == "1":
            SHOTS.mkdir(exist_ok=True)
            win.grab().save(str(SHOTS / f"shot-{shot_name(name)}.png"))
    assert "Consoles/uart0" in seen and "Log" in seen and len(seen) >= 12


def test_negative_twin_the_overflow_check_catches_a_too_wide_widget(qtbot):
    host = QWidget()
    host.resize(400, 300)
    area = QScrollArea(host)
    area.resize(380, 280)
    wide = QLabel("x" * 20)
    wide.setMinimumWidth(3000)
    area.setWidget(wide)
    qtbot.addWidget(host)
    host.show()
    qtbot.wait(50)
    assert any("h-scrollbar" in b for b in overflow(host))


def test_selection_dialog_has_no_horizontal_scrollbar(qtbot, engine, runner):
    from socharness.gui.selection import SelectionDialog

    dlg = SelectionDialog(engine, runner)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: len(dlg.summaries) == 3, timeout=5000)
    qtbot.wait(50)
    assert overflow(dlg) == []
    if os.environ.get("SOCHARNESS_GUI_SHOTS") == "1":
        SHOTS.mkdir(exist_ok=True)
        dlg.grab().save(str(SHOTS / "shot-selection.png"))


# -- the System tab -------------------------------------------------------------------------


def test_build_check_unchecked_is_a_warning_not_ok(qtbot, open_window):
    win = open_window(BOARD_FIELDED)
    label = win.system.build_label
    assert label.property("level") == "warning"
    assert "UNCHECKED" in label.text() and "not a pass" in label.text()
    assert label.text() != "OK"


def test_negative_twin_build_check_ok_and_mismatch_render_as_such(qtbot, open_window, engine):
    win = open_window(BOARD_FIELDED)
    label = win.system.build_label
    engine.set_build_check(BOARD_FIELDED, Check.OK)
    qtbot.waitUntil(lambda: label.property("level") == "ok", timeout=5000)
    assert label.text() == "OK"
    engine.set_build_check(BOARD_FIELDED, Check.MISMATCH)
    qtbot.waitUntil(lambda: label.property("level") == "error", timeout=5000)
    assert label.text() == "MISMATCH"


def telemetry_row(tab, name: str) -> list[str]:
    table = tab.telemetry
    for r in range(table.rowCount()):
        if table.item(r, 0).text() == name:
            return [table.item(r, c).text() for c in range(table.columnCount())]
    raise KeyError(name)


def test_unavailable_reading_shows_its_reason_and_source_never_zero(qtbot, open_window):
    win = open_window(BOARD_FIELDED)
    tab = win.system
    qtbot.waitUntil(lambda: tab.telemetry.rowCount() == 4, timeout=5000)
    name, value, source, _observed, note = telemetry_row(tab, "mcc_temp")
    assert value == "unavailable" and "0" not in value
    assert source == "mcc-console"
    assert note == "needs the Debug USB cable"


def test_negative_twin_available_reading_shows_value_and_source(qtbot, open_window):
    win = open_window(BOARD_USB)
    tab = win.system
    qtbot.waitUntil(lambda: tab.telemetry.rowCount() == 5, timeout=5000)
    _name, value, source, observed, _note = telemetry_row(tab, "mcc_temp")
    assert value == "38.5 degC" and source == "mcc-console" and observed == "just now"


def test_capability_list_shows_reasons(qtbot, open_window):
    win = open_window(BOARD_FIELDED)
    table = win.system.capabilities
    rows = {table.item(r, 0).text(): (table.item(r, 1).text(), table.item(r, 2).text())
            for r in range(table.rowCount())}
    status, why = rows["Reboot the board (reload from SD)"]
    assert status == "Cannot" and why.startswith("needs the Debug USB cable")
    assert rows["Program a partition"] == ("available", "")
