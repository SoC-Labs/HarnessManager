"""Consoles: stream bytes appear, Send writes through the stream, Export gives a port.
Debug: Open shows the ports, Close returns to down, Detect reports NOTHING ON TARGET."""

from __future__ import annotations

import socket

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from socharness.gui.demo_engine import BOARD_FIELDED, BOARD_USB  # noqa: E402

pytestmark = pytest.mark.gui


def console_views(qtbot, win, *names: str):
    win.tabs.setCurrentWidget(win.consoles)
    tab = win.consoles
    qtbot.waitUntil(lambda: all(n in tab.views and tab.views[n].stream is not None
                                for n in names), timeout=5000)
    return [tab.views[n] for n in names]


def test_stream_lines_appear_in_their_own_console_only(qtbot, open_window, engine):
    win = open_window(BOARD_FIELDED)
    uart0, uart1 = console_views(qtbot, win, "uart0", "uart1")
    engine.inject_console(BOARD_FIELDED, "uart0", "Hello world\r\nTEST PASSED\r\n")
    engine.inject_console(BOARD_FIELDED, "uart1", "only-on-uart1\n")
    qtbot.waitUntil(lambda: "TEST PASSED" in uart0.text(), timeout=5000)
    qtbot.waitUntil(lambda: "only-on-uart1" in uart1.text(), timeout=5000)
    assert uart0.text() == "Hello world\nTEST PASSED\n"
    assert "only-on-uart1" not in uart0.text()                   # negative twin
    assert uart0.state.text() in ("connected", "up")


def test_send_writes_through_the_stream(qtbot, open_window, engine):
    win = open_window(BOARD_FIELDED)
    (uart0,) = console_views(qtbot, win, "uart0")
    uart0.line.setText("help")
    uart0.send_button.click()
    qtbot.waitUntil(lambda: (BOARD_FIELDED, "uart0", b"help\n") in engine.consoles.writes,
                    timeout=5000)
    qtbot.waitUntil(lambda: "help" in uart0.text(), timeout=5000)   # the demo DUT echoes
    qtbot.waitUntil(lambda: uart0.line.text() == "", timeout=5000)


def test_negative_twin_an_empty_line_writes_nothing(qtbot, open_window, engine):
    win = open_window(BOARD_FIELDED)
    (uart0,) = console_views(qtbot, win, "uart0")
    uart0.send()
    assert "Nothing was run." in uart0.status.text()
    qtbot.wait(100)
    assert engine.consoles.writes == []


def test_export_to_tcp_gives_a_local_port(qtbot, open_window, engine):
    win = open_window(BOARD_FIELDED)
    (uart0,) = console_views(qtbot, win, "uart0")
    uart0.export_button.click()
    qtbot.waitUntil(lambda: "127.0.0.1:" in uart0.export_label.text(), timeout=5000)
    port = int(uart0.export_label.text().split("127.0.0.1:")[1].split()[0])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        assert b"console uart0" in conn.recv(200)


def debug_panel(qtbot, win):
    win.tabs.setCurrentWidget(win.debug)
    qtbot.waitUntil(lambda: win.debug.debug_state == "down", timeout=5000)
    return win.debug.panel


def test_open_shows_the_ports_and_close_returns_to_down(qtbot, open_window, engine):
    panel = debug_panel(qtbot, open_window(BOARD_USB))
    assert not panel.buttons["down"].isEnabled()
    assert panel.reasons["down"].text() == "the session is down"
    panel.buttons["up"].click()
    qtbot.waitUntil(lambda: panel.value("state").startswith("up"), timeout=5000)
    assert "gdb 127.0.0.1:3343" in panel.value("ports")
    assert "nanosoc_mps3_jtag.cfg" in panel.value("config")
    qtbot.waitUntil(lambda: "(rc 0," in panel.answer_text(), timeout=5000)
    assert panel.answer_text().startswith("$ debug up  (rc 0,")
    assert not panel.buttons["up"].isEnabled()
    assert panel.reasons["up"].text() == "the session is already up"
    panel.buttons["down"].click()
    qtbot.waitUntil(lambda: panel.value("state").startswith("down"), timeout=5000)
    assert panel.value("ports") == "-"
    assert engine.called("debug.up") == [(BOARD_USB,)]
    assert engine.called("debug.down") == [(BOARD_USB,)]


def test_detect_reads_the_idcode(qtbot, open_window):
    panel = debug_panel(qtbot, open_window(BOARD_USB))
    panel.buttons["detect"].click()
    qtbot.waitUntil(lambda: panel.value("idcode") == "0x6ba00477", timeout=5000)
    assert panel.answer_text().startswith("$ debug detect  (rc 0,")


def test_negative_twin_detect_on_greybox_is_nothing_on_target(qtbot, open_window):
    panel = debug_panel(qtbot, open_window(BOARD_FIELDED))
    panel.buttons["detect"].click()
    qtbot.waitUntil(lambda: "(rc 13," in panel.answer_text(), timeout=5000)
    assert "NOTHING ON TARGET" in panel.answer_text()
    assert panel.value("idcode") == "NOTHING ON TARGET"
