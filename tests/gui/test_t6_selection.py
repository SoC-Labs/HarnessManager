"""System Selection: lists the boards, names the holder, Select opens the main window."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402

from socharness.gui.app import AppController  # noqa: E402
from socharness.gui.demo_engine import BOARD_FIELDED, BOARD_HELD, BOARD_USB  # noqa: E402
from socharness.gui.selection import SelectionDialog  # noqa: E402

from .conftest import drain  # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture
def controller(qtbot, engine, runner):
    ctl = AppController(engine, runner=runner)
    ctl.start()
    qtbot.addWidget(ctl.selection)
    yield ctl
    if ctl.main_window is not None:
        qtbot.addWidget(ctl.main_window)
    ctl.shutdown()
    drain()


def summaries_done(dlg: SelectionDialog) -> bool:
    return len(dlg.board_ids()) == 3 and len(dlg.summaries) == 3


def test_lists_the_demo_boards(qtbot, controller):
    dlg = controller.selection
    qtbot.waitUntil(lambda: summaries_done(dlg), timeout=5000)
    assert dlg.board_ids() == [BOARD_FIELDED, BOARD_USB, BOARD_HELD]
    assert dlg.cell(BOARD_FIELDED, "Shell") == "0x3f1a560f"
    assert dlg.cell(BOARD_USB, "Links") == "Ethernet, USB serial, USB storage"
    assert dlg.cell(BOARD_FIELDED, "Links") == "Ethernet"
    assert "UNCHECKED" in dlg.cell(BOARD_FIELDED, "Harness")
    assert dlg.status.text().startswith("$ probe  (rc 0,")


def test_negative_twin_no_boards_says_so(qtbot, engine, runner, monkeypatch):
    monkeypatch.setattr(engine, "probe", lambda hints=None: [])
    dlg = SelectionDialog(engine, runner)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: "no boards found" in dlg.status.text(), timeout=5000)
    assert dlg.board_ids() == []
    assert not dlg.select_button.isEnabled()
    assert dlg.reason.text() == "select a board in the table"


def test_held_board_shows_its_holder(qtbot, controller):
    dlg = controller.selection
    qtbot.waitUntil(lambda: summaries_done(dlg), timeout=5000)
    assert "alice on lab-pc-07" in dlg.cell(BOARD_HELD, "Holder/Lock")
    assert dlg.cell(BOARD_FIELDED, "Holder/Lock") == "free"          # negative twin
    dlg.select_board(BOARD_HELD)
    assert "held by alice" in dlg.reason.text()


def test_select_opens_the_main_window_with_the_board_in_its_title(qtbot, controller, engine):
    dlg = controller.selection
    qtbot.waitUntil(lambda: summaries_done(dlg), timeout=5000)
    dlg.select_board(BOARD_FIELDED)
    qtbot.mouseClick(dlg.select_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: controller.main_window is not None, timeout=5000)
    win = controller.main_window
    assert win.windowTitle() == f"Harness Manager — {BOARD_FIELDED}"
    assert win.isVisible() and not dlg.isVisible()
    assert engine.open_boards() == [BOARD_FIELDED]
    assert win.tab_names() == ["System", "Program", "Consoles", "Debug", "Reset", "Clocks",
                               "Board & XDC", "Log"]


def test_negative_twin_select_on_a_held_board_is_refused_and_named(qtbot, controller, engine):
    dlg = controller.selection
    qtbot.waitUntil(lambda: summaries_done(dlg), timeout=5000)
    dlg.select_board(BOARD_HELD)
    assert dlg.select()                         # the engine decides, not the GUI
    qtbot.waitUntil(lambda: "HELD" in dlg.status.text(), timeout=5000)
    assert "(rc 4," in dlg.status.text()
    assert "alice" in dlg.status.text() and "not opened" in dlg.status.text()
    assert controller.main_window is None and engine.open_boards() == []


def test_negative_twin_select_with_nothing_selected_runs_nothing(qtbot, engine, runner,
                                                                 monkeypatch):
    monkeypatch.setattr(engine, "probe", lambda hints=None: [])
    dlg = SelectionDialog(engine, runner)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: "no boards found" in dlg.status.text(), timeout=5000)
    assert not dlg.select()
    assert "Nothing was run." in dlg.status.text()
    assert engine.called("open") == []


def test_add_by_address(qtbot, controller, engine):
    dlg = controller.selection
    qtbot.waitUntil(lambda: summaries_done(dlg), timeout=5000)
    dlg.add_by_address("10.0.0.9")
    qtbot.waitUntil(lambda: "mps3@10.0.0.9:6900" in dlg.board_ids(), timeout=5000)
    assert engine.called("candidate_for")[-1] == ("10.0.0.9", "mps3")
    dlg.add_by_address("not an address")              # negative twin
    qtbot.waitUntil(lambda: "USAGE" in dlg.status.text(), timeout=5000)
    assert "(rc 2," in dlg.status.text()


def test_select_another_board_closes_the_first(qtbot, controller, engine):
    dlg = controller.selection
    qtbot.waitUntil(lambda: summaries_done(dlg), timeout=5000)
    dlg.select_board(BOARD_USB)
    dlg.select()
    qtbot.waitUntil(lambda: controller.main_window is not None, timeout=5000)
    first = controller.main_window
    qtbot.addWidget(first)
    first.reselect_requested.emit()
    second = controller.selection
    qtbot.addWidget(second)
    assert second is not dlg and second.isVisible()
    qtbot.waitUntil(lambda: summaries_done(second), timeout=5000)
    assert engine.open_boards() == []
    assert engine.called("close")[-1] == (BOARD_USB,)
    assert second.cell(BOARD_USB, "Holder/Lock") == "free"
