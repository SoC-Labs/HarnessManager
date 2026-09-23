"""Program tab: MISMATCH rows in the error colour disable Program; the OK path shows its
progress events in order and a done state."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from socharness.core.errors import ActionFailedError  # noqa: E402
from socharness.gui.demo_engine import BOARD_FIELDED  # noqa: E402
from socharness.gui.style import ERROR, OK, WARNING  # noqa: E402

pytestmark = pytest.mark.gui


def program_tab(qtbot, win, overlay: str):
    win.tabs.setCurrentWidget(win.program)
    tab = win.program
    qtbot.waitUntil(lambda: len(tab.overlays) == 6, timeout=5000)
    tab.select_overlay(overlay)
    qtbot.waitUntil(lambda: tab.preflight_items is not None, timeout=5000)
    return tab


def colour(tab, row: int) -> str:
    return tab.preflight_table.item(row, 1).foreground().color().name()


def row_of(tab, check_name: str) -> int:
    return next(r for r in range(tab.preflight_table.rowCount())
                if tab.preflight_table.item(r, 0).text() == check_name)


def test_mismatch_row_is_error_coloured_and_program_is_refused(qtbot, open_window):
    tab = program_tab(qtbot, open_window(BOARD_FIELDED), "nanosoc_multicore")
    bad = row_of(tab, "shell_id matches")
    assert tab.preflight_table.item(bad, 1).text() == "MISMATCH"
    assert colour(tab, bad) == ERROR
    assert tab.mismatch_rows() == [bad]
    tab.panel.set_checked("arm", True)         # even armed, Program stays refused
    assert not tab.panel.buttons["program"].isEnabled()
    assert "MISMATCH" in tab.panel.reasons["program"].text()
    # The overlay list shows the service's reason too.
    status = tab.overlay_table.item(tab.overlays.index(tab.selected), 5)
    assert "shell_id matches" in status.text() and status.foreground().color().name() == ERROR


def test_negative_twin_a_matching_overlay_has_no_error_rows(qtbot, open_window):
    tab = program_tab(qtbot, open_window(BOARD_FIELDED), "nanosoc")
    assert tab.mismatch_rows() == []
    assert colour(tab, row_of(tab, "shell_id matches")) == OK
    assert not tab.panel.buttons["program"].isEnabled()       # not armed yet
    tab.panel.set_checked("arm", True)
    assert tab.panel.buttons["program"].isEnabled()


def test_unchecked_is_a_warning_not_a_pass(qtbot, open_window):
    tab = program_tab(qtbot, open_window(BOARD_FIELDED), "nanosoc")
    row = row_of(tab, "control channel free")
    assert tab.preflight_table.item(row, 1).text() == "UNCHECKED"
    assert colour(tab, row) == WARNING and colour(tab, row) != OK


def test_program_shows_progress_in_order_and_done(qtbot, open_window, engine):
    tab = program_tab(qtbot, open_window(BOARD_FIELDED), "nanosoc")
    tab.panel.set_checked("arm", True)
    assert tab.panel.trigger("program")
    qtbot.waitUntil(lambda: tab.done_label.text().startswith("DONE"), timeout=5000)
    events = tab.event_texts()
    assert events[0] == "started nanosoc"
    assert events[-1].startswith("done rm_id 0x01000001 verified=yes")
    progress = [e.split()[1] for e in events if e.startswith("progress")]
    assert progress[0] == "guard" and progress[-1] == "verify" and "push" in progress
    assert all(e.startswith("progress") for e in events[1:-1]) and len(events) > 2
    assert tab.done_label.property("level") == "ok"
    assert tab.progress.value() == 100
    qtbot.waitUntil(lambda: "(rc 0," in tab.panel.answer_text(), timeout=5000)
    assert tab.panel.answer_text().startswith("$ program nanosoc  (rc 0,")
    assert engine.called("deploy.deploy") == [(BOARD_FIELDED, "nanosoc")]


def test_negative_twin_a_failed_push_shows_failed_not_done(qtbot, open_window, engine):
    tab = program_tab(qtbot, open_window(BOARD_FIELDED), "nanosoc")
    engine.failures["deploy.push"] = ActionFailedError("push window timed out")
    tab.panel.set_checked("arm", True)
    tab.panel.trigger("program")
    qtbot.waitUntil(lambda: tab.done_label.text().startswith("FAILED"), timeout=5000)
    assert tab.done_label.property("level") == "error"
    assert "push window timed out" in tab.done_label.text()
    assert not any(e.startswith("done") for e in tab.event_texts())
    qtbot.waitUntil(lambda: "(rc 6," in tab.panel.answer_text(), timeout=5000)


def test_restore_baseline_calls_the_service(qtbot, open_window, engine):
    win = open_window(BOARD_FIELDED)
    win.tabs.setCurrentWidget(win.program)
    panel = win.program.panel
    panel.set_checked("arm", True)
    assert panel.trigger("restore")
    qtbot.waitUntil(lambda: "(rc 0," in panel.answer_text(), timeout=5000)
    assert engine.called("deploy.restore_baseline") == [(BOARD_FIELDED,)]
    assert panel.answer_text().startswith("$ restore  (rc 0,")
