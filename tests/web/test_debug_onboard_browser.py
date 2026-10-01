"""DEBUG-ONBOARD in the browser: the Debug card says where OpenOCD runs, one Attach per core.

The REAL daemon app over ``DemoEngine(showcase=True)`` in the system Chrome (the fixtures of
``test_demo_all_browser.py``). The Linux board runs OpenOCD on the board: "on the board", its
gdb ports and one Attach row per core (nanosoc_multicore: two); the bare-metal board is the
negative twin ("on this PC", one Attach row, the telnet and Tcl ports).
"""

from __future__ import annotations

import dataclasses

import pytest

from harness_manager.demo_showcase import BOARD_LINUX, BOARD_V011
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


def _open_session(page) -> None:
    page.locator('[data-action="up"]').click()
    expect(by_id(page, "debug-state")).to_have_text("up", timeout=T)


def test_the_linux_board_runs_openocd_on_the_board_with_an_attach_row_per_core(
        showcase):  # noqa: F811 - the fixture
    board = showcase.engine._board(BOARD_LINUX)
    board.identity = dataclasses.replace(board.identity, rm_id="0x01000003",
                                         rm_name="nanosoc_multicore")
    page = showcase.page()
    open_board(page, BOARD_LINUX)
    section(page, "debug")
    _open_session(page)
    where = by_id(page, "debug-where")
    expect(where).to_have_attribute("data-where", "board")
    expect(where).to_contain_text("on the board")
    ports = showcase.engine._board(BOARD_LINUX).debug.gdb_ports
    assert len(ports) == 2
    for core, port in zip(("cpu0", "cpu1"), ports, strict=True):
        expect(by_id(page, f"debug-attach-{core}")).to_contain_text(gdb_command(port))
    expect(page.locator('[data-port="telnet"]')).to_contain_text("on the board only")   # "telnet …"
    assert not page.errors, page.errors


def test_negative_twin_the_bare_metal_board_runs_it_on_this_pc_with_one_attach_row(
        showcase):  # noqa: F811 - the fixture
    board = showcase.engine._board(BOARD_V011)
    board.identity = dataclasses.replace(board.identity, rm_id="0x01000001", rm_name="nanosoc")
    page = showcase.page()
    open_board(page, BOARD_V011)
    section(page, "debug")
    _open_session(page)
    where = by_id(page, "debug-where")
    expect(where).to_have_attribute("data-where", "host")
    expect(where).to_contain_text("on this PC")
    expect(page.locator('[data-testid^="debug-attach-"]')).to_have_count(0)
    port = showcase.engine._board(BOARD_V011).debug.gdb_port
    expect(by_id(page, "debug-ports")).to_contain_text(gdb_command(port))
    telnet = showcase.engine._board(BOARD_V011).debug.telnet_port         # UI v2: "telnet <port>"
    expect(page.locator('[data-port="telnet"]')).to_contain_text(f"telnet {telnet}")
    assert not page.errors, page.errors
