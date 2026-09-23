"""A missing capability disables its button AND shows the engine's reason as visible text.

A capability that appears (a cable plugged in) enables the button live, through the bus.
An intrusive action is armed by a tick box; an interlock says "Nothing was run."
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from socharness.gui.demo_engine import BOARD_FIELDED, BOARD_USB  # noqa: E402
from socharness.gui.panels import ARM_REASON, NOTHING_RUN  # noqa: E402

pytestmark = pytest.mark.gui


def reboot_panel(win):
    win.tabs.setCurrentWidget(win.reset)
    return win.reset.reboot


def test_unavailable_action_is_disabled_with_its_reason_visible(qtbot, open_window):
    win = open_window(BOARD_FIELDED)
    panel = reboot_panel(win)
    panel.set_checked("arm", True)              # armed, so only the capability is missing
    button, reason = panel.buttons["reboot"], panel.reasons["reboot"]
    assert not button.isEnabled()
    assert reason.isVisible()
    assert reason.text().startswith("Cannot: needs the Debug USB cable")


def test_negative_twin_available_action_has_no_reason(qtbot, open_window):
    win = open_window(BOARD_USB)
    panel = reboot_panel(win)
    panel.set_checked("arm", True)
    assert panel.buttons["reboot"].isEnabled()
    assert panel.reasons["reboot"].text() == ""


def test_capability_appearing_enables_the_button_live(qtbot, open_window, engine):
    win = open_window(BOARD_FIELDED)
    panel = reboot_panel(win)
    panel.set_checked("arm", True)
    button = panel.buttons["reboot"]
    # Negative twin first: a change on ANOTHER board changes nothing here.
    engine.set_features(BOARD_USB, ("clcd",))
    qtbot.wait(200)
    assert not button.isEnabled()
    engine.add_usb(BOARD_FIELDED)               # publishes board.identity
    qtbot.waitUntil(button.isEnabled, timeout=5000)
    assert panel.reasons["reboot"].text() == ""


def test_unarmed_intrusive_action_says_so_and_runs_nothing(qtbot, open_window, engine):
    win = open_window(BOARD_USB)
    panel = reboot_panel(win)
    assert not panel.buttons["reboot"].isEnabled()
    assert panel.reasons["reboot"].text() == ARM_REASON
    assert not panel.trigger("reboot")          # the click path, forced
    assert NOTHING_RUN in panel.answer_text()
    assert engine.called("controller.reboot") == []


def test_armed_reboot_runs_shows_phases_in_order_and_disarms(qtbot, open_window, engine):
    win = open_window(BOARD_USB)
    panel = reboot_panel(win)
    panel.set_checked("arm", True)
    assert panel.trigger("reboot")
    qtbot.waitUntil(lambda: "(rc 0," in panel.answer_text(), timeout=5000)
    text = panel.answer_text()
    assert text.startswith("$ mcc reboot  (rc 0,")
    assert text.index("reboot: sent") < text.index("reboot: down") < text.index("reboot: up")
    assert engine.called("controller.reboot") == [(BOARD_USB,)]
    assert panel.value("arm") == ""             # the arm tick clears after each run
    assert not panel.buttons["reboot"].isEnabled()


def test_shell_restart_needs_firmware_and_says_which(qtbot, open_window, engine):
    win = open_window(BOARD_FIELDED)
    win.tabs.setCurrentWidget(win.reset)
    panel = win.reset.shell
    panel.set_checked("arm", True)
    assert panel.reasons["restart"].text() == "Cannot: needs harness firmware with 'reboot'"
    engine.set_features(BOARD_FIELDED, ("windowed", "reboot"))
    qtbot.waitUntil(panel.buttons["restart"].isEnabled, timeout=5000)
