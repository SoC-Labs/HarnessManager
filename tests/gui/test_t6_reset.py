"""Reset tab: an interrupted SD install is shown before anything else, and a REBOOT that
was sent but never observed is an error, never "done"."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from socharness.core.errors import ActionFailedError  # noqa: E402
from socharness.gui.demo_engine import BOARD_FIELDED, BOARD_USB  # noqa: E402

pytestmark = pytest.mark.gui

JOURNAL = {"op": "install", "state": "interrupted", "pid": 4242, "host": "lab-pc-02",
           "current": "MB/HBI0309C/AN536/images.txt", "error": "OSError: [Errno 5] I/O error",
           "backup": {"path": "/home/u/.config/socharness/sd/backup-20260923.zip",
                      "sha256": "ab" * 32}}


def test_interrupted_sd_install_is_shown_first_and_restores(qtbot, open_window, engine):
    engine.set_sd_journal(BOARD_USB, dict(JOURNAL))
    win = open_window(BOARD_USB)
    panel = win.reset.sd
    qtbot.waitUntil(lambda: win.sd_journal is not None, timeout=5000)
    assert win.tabs.currentWidget() is win.reset                 # brought to the front
    assert panel.isVisible() and panel.title() == "Interrupted SD install — Restore"
    assert "install interrupted by pid 4242" in panel.value("journal")
    assert panel.value("backup").endswith("backup-20260923.zip")
    assert "Interrupted SD install" in win.header.text()
    assert win.header.property("level") == "error"
    assert not panel.buttons["restore"].isEnabled()               # armed first
    panel.set_checked("arm", True)
    assert panel.trigger("restore")
    qtbot.waitUntil(lambda: "(rc 0," in panel.answer_text(), timeout=5000)
    assert engine.called("storage.load_backup") == [(BOARD_USB, JOURNAL["backup"]["path"])]
    assert engine.called("storage.restore") == [(BOARD_USB, JOURNAL["backup"]["path"])]
    assert "restore: 100%" in panel.answer_text()
    qtbot.waitUntil(lambda: not panel.isVisible(), timeout=5000)
    assert win.sd_journal is None and "Interrupted" not in win.header.text()


def test_negative_twin_no_journal_no_panel(qtbot, open_window, engine):
    win = open_window(BOARD_USB)
    qtbot.waitUntil(lambda: bool(engine.called("storage.pending")), timeout=5000)
    qtbot.wait(100)
    assert not win.reset.sd.isVisible() and win.sd_journal is None
    assert win.tabs.currentWidget() is win.system
    fielded = open_window(BOARD_FIELDED)          # no USB storage link at all
    qtbot.wait(100)
    assert not fielded.reset.sd.isVisible()


def test_reboot_sent_but_not_observed_is_an_error(qtbot, open_window, engine):
    engine.failures["controller.reboot.confirm"] = ActionFailedError(
        "REBOOT sent but no restart observed", hint="power-cycle the board by hand")
    win = open_window(BOARD_USB)
    win.tabs.setCurrentWidget(win.reset)
    panel = win.reset.reboot
    panel.set_checked("arm", True)
    panel.trigger("reboot")
    qtbot.waitUntil(lambda: "(rc 6," in panel.answer_text(), timeout=5000)
    text = panel.answer_text()
    assert "ACTION FAILED: REBOOT sent but no restart observed" in text
    assert "came back" not in text and "done" not in text.splitlines()[-1]
    rows = win.log.rows()
    assert any(level == "error" and "no restart observed" in msg for level, msg in rows)
