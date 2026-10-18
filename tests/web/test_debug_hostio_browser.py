"""HM 1.2.1 (preview): the Debug card's "Debug via" choice and capability chips.

The REAL daemon app over ``DemoEngine(showcase=True)`` in Chrome (the fixtures of
``test_demo_all_browser.py``), with the demo's hostio view set per test. Negative twins: JTAG
(the default) sends no body and keeps Halt/Step enabled; the console-mode board has the hostio
choice disabled with its reason.
"""

from __future__ import annotations

import pytest

from harness_manager.demo_showcase import BOARD_LINUX
from harness_manager.services import debug_onboard as OB
from harness_manager.services.debug import gdb_command
from tests.web.test_demo_all_browser import (  # noqa: F401 - fixtures
    T,
    by_id,
    open_board,
    section,
    showcase,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser

LABEL = "hostio (memory only, preview)"
MODE_GATE = ("the DUT's pins are in console mode: press \"Switch to hostio4\" (Consoles), "
             "then load the design again")


def pins(engine, mode: str, pending: str = "") -> None:
    view = {"mode": mode, "pending": pending or mode, "changing": bool(pending),
            "held": False, "target_rst": False, "ch0": {}, "ch1": {},
            "drops": {"ch0": 0, "ch1": 0}, "note": ""}
    engine.consoles.hostio = lambda _session: dict(view)


def card(showcase, mode: str = "hostio4", **kw):  # noqa: F811
    pins(showcase.engine, mode, **kw)
    page = showcase.page()
    open_board(page, BOARD_LINUX)
    section(page, "debug")
    return page


def cap(page, name):
    return page.locator(f'[data-testid="debug-caps"] [data-cap="{name}"]')


def test_hostio_choice_opens_a_hostio_session_and_halt_step_are_disabled(showcase):  # noqa: F811
    page = card(showcase)
    choice = page.locator('[data-testid="debug-via"] [data-via="hostio"]')
    expect(choice).to_have_text(LABEL)
    expect(choice).not_to_have_attribute("aria-disabled", "true", timeout=T)
    choice.click()
    page.locator('[data-action="up"]').click()
    expect(by_id(page, "debug-state")).to_have_text("up", timeout=T)
    expect(by_id(page, "debug-via-live")).to_contain_text(LABEL)
    for name in ("halt", "step", "registers", "breakpoints"):
        expect(cap(page, name)).to_have_attribute("data-enabled", "false")
        expect(cap(page, name)).to_have_attribute("aria-disabled", "true")
        expect(cap(page, name)).to_have_attribute("title", OB.HOSTIO_LIMITS)
    expect(by_id(page, "debug-caps-reason")).to_have_text(OB.HOSTIO_LIMITS)
    port = showcase.engine._board(BOARD_LINUX).debug.gdb_port
    expect(by_id(page, "debug-attach")).to_contain_text(gdb_command(port, gdb_init=OB.HOSTIO_GDB_INIT))
    assert not page.errors, page.errors


def test_twin_jtag_is_the_default_and_halt_step_are_enabled(showcase):  # noqa: F811
    page = card(showcase)
    expect(page.locator('[data-testid="debug-via"] [data-via="jtag"]')).to_have_attribute(
        "aria-checked", "true", timeout=T)
    page.locator('[data-action="up"]').click()
    expect(by_id(page, "debug-state")).to_have_text("up", timeout=T)
    for name in ("halt", "step", "registers", "breakpoints"):
        expect(cap(page, name)).to_have_attribute("data-enabled", "true")
        expect(cap(page, name)).not_to_have_attribute("aria-disabled", "true")
    expect(by_id(page, "debug-caps-reason")).to_have_count(0)
    port = showcase.engine._board(BOARD_LINUX).debug.gdb_port
    expect(by_id(page, "debug-attach")).to_contain_text(gdb_command(port))
    expect(by_id(page, "debug-attach")).not_to_contain_text("p-packet")
    assert not page.errors, page.errors


def test_hostio_is_disabled_with_its_reason_when_the_pins_are_in_console_mode(showcase):  # noqa: F811
    page = card(showcase, mode="console")
    choice = page.locator('[data-testid="debug-via"] [data-via="hostio"]')
    expect(choice).to_have_attribute("aria-disabled", "true", timeout=T)
    expect(choice).to_have_attribute("title", MODE_GATE)
    expect(by_id(page, "debug-via-reason")).to_contain_text(MODE_GATE)
    choice.click(force=True)                        # a click changes nothing
    expect(page.locator('[data-testid="debug-via"] [data-via="jtag"]')).to_have_attribute(
        "aria-checked", "true")
    assert not page.errors, page.errors


def test_pending_hostio4_says_it_needs_a_design_load(showcase):  # noqa: F811
    page = card(showcase, mode="console", pending="hostio4")
    expect(by_id(page, "debug-via-reason")).to_contain_text(
        "hostio4 takes effect at the next design load: load the design again", timeout=T)


def test_hostio_is_greyed_when_the_board_says_the_design_has_no_hostio(showcase):  # noqa: F811
    showcase.engine._board(BOARD_LINUX).hostio_design = {"design": "nanosoc_upy", "hostio": False}
    page = card(showcase)
    choice = page.locator('[data-testid="debug-via"] [data-via="hostio"]')
    expect(choice).to_have_attribute("aria-disabled", "true", timeout=T)
    expect(by_id(page, "debug-via-reason")).to_contain_text("nanosoc_upy has no hostio")


def test_twin_an_unknown_flag_allows_hostio(showcase):  # noqa: F811
    showcase.engine._board(BOARD_LINUX).hostio_design = {"design": "nanosoc", "hostio": None}
    page = card(showcase)
    choice = page.locator('[data-testid="debug-via"] [data-via="hostio"]')
    expect(choice).not_to_have_attribute("aria-disabled", "true", timeout=T)
