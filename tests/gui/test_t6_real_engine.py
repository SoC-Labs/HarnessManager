"""The GUI over the REAL stack: T1's Engine, T2's DeployService, the MPS3 pack, pyverify,
and a FakeShell pinned to the fielded firmware (VirtualMps3). No demo engine here.

It uses T2's overlay test helpers (tests/fakes/t2_overlays.py) to build overlay triples.
Services not merged yet (consoles, debug) are the engine's stubs: the GUI must say
"Cannot: ... unavailable" rather than offer a button that can only fail.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pytestqt")

from socharness.core.pack import ProbeHints  # noqa: E402
from socharness.engine import Engine  # noqa: E402
from socharness.gui.app import AppController  # noqa: E402
from socharness.gui.style import ERROR  # noqa: E402
from socharness_board_mps3.pack import Mps3Pack  # noqa: E402
from tests.fakes.t2_overlays import (  # noqa: E402
    FIELDED_USERCODE,
    GREYBOX_RM_ID,
    OTHER_STATIC_ID,
    make_overlay,
    point_pushes_at,
    use_overlay_dirs,
)

from .conftest import drain  # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture
def real(qtbot, vboard, tmp_path, monkeypatch, runner):
    root = tmp_path / "overlays"
    make_overlay(root, "greybox", rm_id=GREYBOX_RM_ID, static_usercode=FIELDED_USERCODE)
    make_overlay(root, "synth", static_usercode=FIELDED_USERCODE)
    make_overlay(root, "alien", rm_id=0x0100_7A58, static_id=OTHER_STATIC_ID)
    use_overlay_dirs(monkeypatch, root)
    point_pushes_at(monkeypatch, vboard)
    pack = Mps3Pack(console_ports=vboard.console_ports, push_port=vboard.shell.raw_tcp_port,
                    tftp_port=vboard.shell.tftp_port)
    engine = Engine(packs={"mps3": pack})
    hints = ProbeHints(hosts=(vboard.shell_endpoint,), scan_usb=False, timeout_s=2.0)
    ctl = AppController(engine, hints=hints, runner=runner)
    ctl.start()
    qtbot.addWidget(ctl.selection)
    dlg = ctl.selection
    board_id = f"mps3@{vboard.shell_endpoint}"
    qtbot.waitUntil(lambda: board_id in dlg.summaries, timeout=10000)
    assert dlg.cell(board_id, "Holder/Lock") == "free"
    dlg.select_board(board_id)
    assert dlg.select()
    qtbot.waitUntil(lambda: ctl.main_window is not None, timeout=10000)
    win = ctl.main_window
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win.ctx.info is not None, timeout=10000)
    yield engine, win, vboard, board_id
    ctl.shutdown()
    drain()


def test_real_engine_opens_the_board_and_renders_identity(qtbot, real):
    engine, win, vboard, board_id = real
    assert win.windowTitle() == f"Harness Manager — {board_id}"
    assert engine.open_boards() == [board_id]
    owner = engine.lock_owner(board_id)
    assert owner is not None and owner.note == "socharness-gui"
    assert win.system.fields["shell"].text() == "0x3f1a560f"
    # The fielded mint cannot read USR_ACCESS: its build check is UNCHECKED, a warning.
    assert win.system.build_label.property("level") == "warning"
    reboot = win.reset.reboot
    reboot.set_checked("arm", True)
    assert reboot.reasons["reboot"].text().startswith("Cannot: needs the Debug USB cable")


def test_real_deploy_refuses_a_mismatch_and_programs_a_match(qtbot, real):
    engine, win, vboard, _ = real
    tab = win.program
    win.tabs.setCurrentWidget(tab)
    qtbot.waitUntil(lambda: len(tab.overlays) == 3, timeout=10000)
    tab.select_overlay("alien")
    qtbot.waitUntil(lambda: tab.preflight_items is not None, timeout=10000)
    assert tab.mismatch_rows(), "the alien overlay must show a MISMATCH row"
    assert all(tab.preflight_table.item(r, 1).foreground().color().name() == ERROR
               for r in tab.mismatch_rows())
    tab.panel.set_checked("arm", True)
    assert not tab.panel.buttons["program"].isEnabled()
    # Negative twin: the matching overlay programs through the real FakeShell.
    tab.select_overlay("synth")
    qtbot.waitUntil(lambda: tab.preflight_items is not None and tab.selected.name == "synth",
                    timeout=10000)
    assert tab.mismatch_rows() == []
    tab.panel.set_checked("arm", True)
    assert tab.panel.trigger("program")
    qtbot.waitUntil(lambda: tab.done_label.text().startswith("DONE"), timeout=20000)
    events = tab.event_texts()
    assert events[0].startswith("started synth") and events[-1].startswith("done")
    assert vboard.shell.swaps[-1]["final"] == "DONE"
    qtbot.waitUntil(lambda: win.system.fields["design"].text().startswith("synth")
                    or "0x01007a57" in win.system.fields["design"].text(), timeout=10000)


def test_missing_services_are_shown_as_cannot(qtbot, real):
    engine, win, _, _ = real
    for service, check in (("consoles", lambda: win.consoles.status.text()),
                           ("debug", lambda: win.debug.panel.reasons["detect"].text())):
        stub_reason = getattr(getattr(engine, service), "reason", None)
        if not isinstance(stub_reason, str):
            continue                    # that service has merged: nothing to assert here
        win.consoles.load()
        qtbot.waitUntil(lambda c=check, s=service: f"the {s} service is unavailable" in c(),
                        timeout=5000)


def test_reset_dut_through_the_real_shell(qtbot, real):
    engine, win, _, _ = real
    panel = win.reset.dut
    panel.set_checked("arm", True)
    assert panel.trigger("reset")
    qtbot.waitUntil(lambda: "(rc " in panel.answer_text(), timeout=10000)
    assert panel.answer_text().startswith("$ reset dut  (rc 0,")
