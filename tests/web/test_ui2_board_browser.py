"""UI v2 Phase 2 (lane UI2-BOARD): the Board tab's six sub-pages to the round-3 design, over the
REAL daemon and ``DemoEngine(showcase=True)`` (what ``--demo`` serves): the side list with its
status lines and "opens what needs a look", Recover's ladder (the keeps matrix, the lease gate
R3, the card-busy guard, the service's 409 HELD in words), Versions (the OS slots and a roll
back, netboot, the configuration SD, "Ask for it…"), Connections from ``mcc_route``,
Readings from G4, Access (the G10 clash, the SSH claim, who may drive), About, and the one
screen at 1440 x 900. Each behaviour has its negative twin.

A few states the showcase cannot reach are made with ``page.route`` answering the one read
(a card being written, a refusal from the service, a Linux release in the catalogue): the
shapes are docs/API.md's.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from harness_manager.demo_showcase import BOARD_LEASED, BOARD_LINUX, BOARD_SPARE, BOARD_V011
from tests.web import nav
from tests.web.test_demo_all_browser import make_showcase

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = nav.T
APP = {"width": 1440, "height": 900}
PAGES = ("recover", "versions", "connections", "readings", "access", "about")


@pytest.fixture
def show(browser, tmp_path, monkeypatch, request):
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def board(show: Any, bid: str, sub: str = "", **size: Any) -> Any:
    page = show.page(**(size or APP))
    nav.open_board(page, bid)
    nav.tab(page, "board")
    if sub:
        nav.board_page(page, sub)
    return page


def reply(route: Any, status: int, body: dict) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


# --- the side list ---------------------------------------------------------------------------------


def test_the_side_list_says_each_pages_status_and_a_clash_opens_access(show):
    page = board(show, BOARD_LEASED)                          # mps3-02: G10's MAC clash, alice's lease
    for sub in PAGES:
        expect(page.locator(f'[data-board-page="{sub}"]')).to_be_visible()
    expect(page.locator('[data-board-page="access"]')).to_have_attribute("aria-current", "page", timeout=T)
    expect(by_id(page, "board-status-access")).to_contain_text("MAC clash with mps3-03")
    expect(page.locator('[data-board-page="access"] [data-dot="err"]')).to_be_visible()
    expect(by_id(page, "board-status-connections")).to_have_text("Debug USB: to the hub")   # UI2-POLISH
    expect(by_id(page, "board-status-recover")).to_have_text("watch only: no lease")
    expect(page.locator('[data-board-page="recover"] [data-dot="held"]')).to_be_visible()
    assert not page.errors, page.errors


def test_twin_a_board_with_nothing_to_look_at_opens_recover(show):
    page = board(show, BOARD_V011)                            # mps3-01: no hub, Debug USB here
    expect(page.locator('[data-board-page="recover"]')).to_have_attribute("aria-current", "page", timeout=T)
    expect(by_id(page, "board-page-recover")).to_be_visible()
    expect(by_id(page, "board-status-recover")).to_have_text("steps 1-4 of 5 here")
    expect(by_id(page, "board-status-connections")).to_have_text("Debug USB: to this PC")
    assert page.locator('.bt-nav [data-dot="err"]').count() == 0
    # a deep link wins over "what needs a look": the address bar names the page
    nav.board_page(page, "about")
    expect(page.locator('[data-board-page="about"]')).to_have_attribute("aria-current", "page")
    assert page.evaluate("location.hash").endswith("/board/about")


# --- Recover ----------------------------------------------------------------------------------------


def test_recover_refuses_every_step_on_a_board_someone_else_leases(show):
    page = board(show, BOARD_LEASED, "recover")
    held = "for the lease holder only: alice@lab-pc-07 holds this board"
    expect(by_id(page, "recover-lease")).to_contain_text(f"Recovery is {held}", timeout=T)
    for key in ("reset_dut", "restore", "reset_shell", "reboot"):
        expect(page.locator(f'[data-action="{key}"]')).to_have_attribute("aria-disabled", "true")
        expect(by_id(page, f"reason-{key}")).to_contain_text(held)
    by_id(page, "arm-reset-dut").locator("input").check()
    page.locator('[data-action="reset_dut"]').click(force=True)
    expect(by_id(page, "reset-dut-result")).to_contain_text("Nothing was run.", timeout=T)
    assert show.engine.called("resets.reset") == []
    assert not page.errors, page.errors


def test_twin_a_board_with_no_hub_resets_its_dut_once_armed(show):
    page = board(show, BOARD_V011, "recover")
    expect(by_id(page, "recover-can")).to_contain_text("steps 1-4", timeout=T)
    expect(by_id(page, "reason-reset_dut")).to_contain_text("not armed")
    # what each step keeps: a reset loses the DUT's state and keeps the rest
    keeps = by_id(page, "reset-dut").locator(".kp")
    expect(keeps).to_have_count(5)
    assert [keeps.nth(i).get_attribute("data-keep") for i in range(5)] == ["lost", "kept", "kept", "kept", "kept"]
    reboot = [by_id(page, "reboot-card").locator(".kp").nth(i).get_attribute("data-keep") for i in range(5)]
    assert reboot == ["lost", "back", "lost", "kept", "reload"]
    by_id(page, "arm-reset-dut").locator("input").check()
    page.locator('[data-action="reset_dut"]').click()
    expect(by_id(page, "reset-dut-result")).to_contain_text("rc 0", timeout=T)
    assert show.engine.called("resets.reset")
    assert not page.errors, page.errors


def test_back_to_greybox_swaps_the_partition_then_says_it_is_greybox(show):
    page = board(show, BOARD_V011, "recover")                  # mps3-01 runs nanosoc
    by_id(page, "arm-greybox").locator("input").check()
    page.locator('[data-action="restore"]').click()
    expect(by_id(page, "greybox-result")).to_contain_text("rc 0", timeout=T)
    expect(page.locator('[data-testid="fact-design"]')).to_contain_text("greybox", timeout=T)
    # its twin: once the board runs greybox the step has nothing to do, and says so
    expect(by_id(page, "reason-restore")).to_contain_text("already greybox", timeout=T)
    expect(page.locator('[data-action="restore"]')).to_have_attribute("aria-disabled", "true")
    assert not page.errors, page.errors


def test_a_step_the_service_refuses_says_so_in_words(show):
    page = board(show, BOARD_V011, "recover")
    page.route("**/api/v1/boards/*/reset", lambda r: reply(r, 409, {"ok": False, "error": {
        "code": 4, "name": "HELD", "message": "the board's hub lease is bob@lab-pc-03's",
        "holder": "bob@lab-pc-03", "data": {"reason": "LEASE", "lease": {"holder": "bob@lab-pc-03"}}}}))
    by_id(page, "arm-reset-dut").locator("input").check()
    page.locator('[data-action="reset_dut"]').click()
    result = by_id(page, "reset-dut-result")
    expect(result).to_contain_text("HELD", timeout=T)
    expect(result).to_contain_text("holder: bob@lab-pc-03")
    expect(result).to_contain_text("The service refused it: the hub lease is bob@lab-pc-03's")


def test_restart_and_reboot_wait_while_the_card_is_written(show):
    busy = {"act": "push", "slot": "B", "state": "writing", "got": 12_300_000, "len": 29_000_000,
            "busy": "writing", "text": "writing slot B: 12.3 MB / 29 MB, ~6 min left"}

    def card(route):
        res = route.fetch()
        body = res.json()
        if body.get("card") and body["card"].get("os_slots"):
            body["card"]["os_slots"]["job"] = busy
        reply(route, 200, body)
    page = show.page(**APP)
    page.route("**/api/v1/boards/*/card", card)
    nav.open_board(page, BOARD_LINUX)
    nav.tab(page, "board")
    nav.board_page(page, "recover")
    expect(by_id(page, "recover-guard")).to_contain_text("Restart, reboot and power-cycle wait", timeout=T)
    expect(by_id(page, "recover-guard")).to_contain_text("writing slot B")
    expect(page.locator('[data-action="reset_shell"]')).to_have_attribute("aria-disabled", "true")
    expect(by_id(page, "reason-reset_shell")).to_contain_text("waits:")
    # the gentle step does not touch the card: it waits only for its arm box
    expect(by_id(page, "reason-reset_dut")).to_contain_text("not armed")


def test_twin_an_idle_card_does_not_hold_the_restart(show):
    page = board(show, BOARD_LINUX, "recover")
    expect(by_id(page, "reason-reset_shell")).to_contain_text("not armed", timeout=T)
    assert by_id(page, "recover-guard").count() == 0


# --- Versions ---------------------------------------------------------------------------------------


def test_versions_shows_the_os_slots_and_rolls_back_to_the_other_slot(show):
    page = board(show, BOARD_LINUX, "versions")
    a, b = by_id(page, "slot-A"), by_id(page, "slot-B")
    expect(a).to_have_attribute("data-running", "yes", timeout=T)
    expect(a).to_contain_text("Running · default")
    expect(b).to_contain_text("2.0.0-rc2")
    expect(b).to_contain_text("Fallback")
    expect(by_id(page, "overlay-store")).to_contain_text("boots nanosoc at power-on")
    page.locator('[data-action="slot-rollback-plan"]').click()
    plan = by_id(page, "slot-rollback")
    expect(plan).to_contain_text("Make B the default")
    expect(plan).to_contain_text("Reboot into B")
    plan.locator('[data-testid="arm-slot-rollback"] input').check()
    plan.locator('[data-action="slot_rollback"]').click()
    expect(by_id(page, "slot_rollback-result")).to_contain_text("rc 0", timeout=T)
    expect(b).to_have_attribute("data-running", "yes", timeout=T)
    expect(b).to_have_attribute("data-default", "yes")
    assert not page.errors, page.errors


def test_twin_a_bare_metal_board_shows_its_configuration_sd(show):
    page = board(show, BOARD_V011, "versions")
    sd = by_id(page, "config-sd")
    expect(sd).to_contain_text("to this PC", timeout=T)
    expect(sd).to_contain_text("none taken from this page")
    expect(sd.locator('[data-action="sd_backup"]').first).to_be_visible()     # Back up now
    assert by_id(page, "slot-A").count() == 0 and by_id(page, "os-netboot").count() == 0


def _config_sd_steps(page: Any) -> None:
    sd = by_id(page, "config-sd")
    expect(sd).to_contain_text("Configuration SD (the MCC's card)", timeout=T)
    expect(sd.locator('[data-action="sd_backup"]').first).to_be_visible()
    sd.locator('[data-action="sd-more"]').click()
    flow = by_id(page, "sd-flow")
    for step in ("sd-step-backup", "sd-step-install", "sd-step-reboot", "sd-step-restore"):
        expect(flow.locator(f'[data-testid="{step}"]')).to_be_visible()
    expect(flow).to_contain_text("Back up the SD")
    expect(flow).to_contain_text("Reboot and witness it")
    # the write is armed: the install button is not live until its box is ticked
    install = by_id(page, "sd-step-install").locator('[data-action="sd_install"]')
    expect(install).to_be_disabled()


def test_a_linux_card_board_shows_the_configuration_sd_apart_from_the_user_microsd(show):
    page = board(show, BOARD_LINUX, "versions")
    expect(by_id(page, "os-here")).to_contain_text("User microSD (the OS slots)", timeout=T)
    expect(by_id(page, "slot-A")).to_be_visible()
    _config_sd_steps(page)
    assert by_id(page, "os-here").count() == 1 and by_id(page, "config-sd").count() == 1
    assert not page.errors, page.errors


def test_a_netbooted_linux_board_also_shows_the_configuration_sd(show):
    show.engine._board(BOARD_LINUX).card = False
    page = board(show, BOARD_LINUX, "versions")
    expect(by_id(page, "os-netboot")).to_contain_text("no OS slots", timeout=T)
    _config_sd_steps(page)
    assert by_id(page, "slot-A").count() == 0
    assert not page.errors, page.errors


def test_twin_the_bare_metal_page_keeps_its_one_configuration_sd_card(show):
    page = board(show, BOARD_V011, "versions")
    sd = by_id(page, "config-sd")
    expect(sd).to_contain_text("Configuration SD", timeout=T)
    assert "the MCC's card" not in sd.inner_text()
    assert by_id(page, "config-sd").count() == 1 and by_id(page, "slot-A").count() == 0


def test_a_netbooted_linux_board_says_so_and_asks_for_a_linux_release(show):
    show.engine._board(BOARD_LINUX).card = False               # no user microSD: the hub's image
    catalog = {"ok": True, "catalog": "mps3-harness", "board_id": BOARD_LINUX, "channels": [], "offer": "2.0.1",
               "rollback": [], "warnings": [], "at": 0, "board": {"running_release": "2.0.0", "running": {}},
               "releases": [{"version": "2.0.1", "channels": ["stable"], "channel": "stable", "impl": "linux",
                             "static_id": "0x44ee76d5", "marks": ["offered"], "verdict": "fits", "why": "same shell",
                             "size": 29_000_000, "released_at": "2026-09-30"}]}
    page = show.page(**APP)
    page.route("**/api/v1/harness/catalog?*", lambda r: reply(r, 200, catalog))
    nav.open_board(page, BOARD_LINUX)
    nav.tab(page, "board")
    nav.board_page(page, "versions")
    expect(by_id(page, "os-netboot")).to_contain_text("no OS slots", timeout=T)
    assert by_id(page, "slot-A").count() == 0
    row = page.locator('.hrow[data-release="2.0.1"]')
    expect(row.locator('[data-action="install"]')).to_have_count(0)
    row.locator('[data-action="ask"]').click()
    ask = by_id(page, "harness-ask")
    expect(ask).to_contain_text("Harness Manager can't write the hub's images")
    expect(ask.locator('[data-action="ask-copy"]')).to_be_visible()


def test_twin_a_card_board_installs_the_same_release_instead_of_asking(show):
    catalog = {"ok": True, "catalog": "mps3-harness", "board_id": BOARD_LINUX, "channels": [], "offer": "2.0.1",
               "rollback": [], "warnings": [], "at": 0, "board": {"running_release": "2.0.0", "running": {}},
               "releases": [{"version": "2.0.1", "channels": ["stable"], "channel": "stable", "impl": "linux",
                             "static_id": "0x44ee76d5", "marks": ["offered"], "verdict": "fits", "why": "same shell",
                             "size": 29_000_000, "released_at": "2026-09-30"}]}
    page = show.page(**APP)
    page.route("**/api/v1/harness/catalog?*", lambda r: reply(r, 200, catalog))
    nav.open_board(page, BOARD_LINUX)
    nav.tab(page, "board")
    nav.board_page(page, "versions")
    row = page.locator('.hrow[data-release="2.0.1"]')
    expect(row.locator('[data-action="install"]')).to_be_visible(timeout=T)
    assert row.locator('[data-action="ask"]').count() == 0


# --- Connections, Readings -----------------------------------------------------------------------------


def test_connections_draws_where_the_debug_usb_goes(show):
    page = board(show, BOARD_LEASED, "connections")
    fig = by_id(page, "cx-diagram")
    expect(fig).to_have_attribute("data-usb", "hub", timeout=T)
    expect(fig).to_have_attribute("data-hub", "yes")
    expect(by_id(page, "cx-usb")).to_contain_text("to the hub")
    expect(by_id(page, "cx-usb").locator(".cx-give:not(.no)")).to_contain_text(["MCC console"])
    expect(by_id(page, "cx-ethernet")).to_contain_text("through the hub's ssh tunnel")
    expect(by_id(page, "cx-tunnel")).to_contain_text("ssh tunnel up")
    expect(by_id(page, "cx-jtag")).to_contain_text("Built in")


def test_twin_an_ethernet_only_board_has_no_debug_usb_and_says_the_fix(show):
    page = board(show, BOARD_LINUX, "connections")
    fig = by_id(page, "cx-diagram")
    expect(fig).to_have_attribute("data-usb", "none", timeout=T)
    expect(fig).to_have_attribute("data-hub", "no")
    usb = by_id(page, "cx-usb")
    expect(usb).to_contain_text("none (Ethernet only)")
    expect(usb).to_contain_text("Fix: Connect J8")
    assert usb.locator(".cx-give:not(.no)").count() == 0
    expect(by_id(page, "cx-ssh")).to_contain_text("Claimed by you")


def test_readings_show_health_answer_time_and_uptimes_with_their_sources(show):
    page = board(show, BOARD_LINUX, "readings")
    expect(by_id(page, "reading-health")).to_contain_text("Healthy", timeout=T)
    expect(by_id(page, "reading-answer")).to_contain_text("ms")
    expect(by_id(page, "reading-uptime")).to_contain_text("Harness up (harnessd)")
    expect(by_id(page, "reading-os-uptime")).to_be_visible()
    temp = page.locator('[data-testid="telemetry-table"] [data-reading="mcc_temp"]')
    expect(temp).to_have_attribute("data-available", "no", timeout=T)
    expect(temp).to_contain_text("needs the Debug USB cable")
    assert " 0 " not in temp.inner_text()
    # F4: no "supply not measured" row
    expect(page.locator('[data-testid="telemetry-table"] [data-reading="board_power"]')).to_have_count(0)


def test_twin_a_bare_metal_board_has_no_linux_uptime_and_reads_its_temperature(show):
    page = board(show, BOARD_V011, "readings")
    temp = page.locator('[data-testid="telemetry-table"] [data-reading="mcc_temp"]')
    expect(temp).to_have_attribute("data-available", "yes", timeout=T)
    expect(temp).to_contain_text("41 °C")
    expect(by_id(page, "reading-uptime")).to_contain_text("Harness up (the shell)")
    assert by_id(page, "reading-os-uptime").count() == 0
    expect(by_id(page, "board-status-readings")).to_contain_text("41.0 °C")


# --- Access, About ------------------------------------------------------------------------------------


def test_access_names_the_clash_and_goes_to_the_other_board(show):
    page = board(show, BOARD_LEASED, "access")
    clash = by_id(page, "access-clash")
    expect(clash).to_have_attribute("data-field", "mac", timeout=T)
    expect(clash).to_contain_text("Identity clash with mps3-03")
    expect(by_id(page, "access-drive-rule")).to_contain_text("alice@lab-pc-07 holds the hub lease")
    page.locator('[data-action="access-go-clash"]').click()
    expect(nav.rail(page, BOARD_SPARE)).to_have_attribute("aria-current", "true", timeout=T)


def test_twin_a_board_without_a_clash_or_hub_shows_neither(show):
    page = board(show, BOARD_LINUX, "access")
    expect(by_id(page, "access-claim")).to_contain_text("Claimed by you", timeout=T)
    expect(by_id(page, "access-drive-rule")).to_contain_text("No hub lease")
    assert by_id(page, "access-clash").count() == 0
    page2 = board(show, BOARD_V011, "access")
    expect(by_id(page2, "access-identity")).to_contain_text("not reported by the bare-metal harness", timeout=T)
    assert by_id(page2, "access-claim").count() == 0                      # no SSH on bare metal


def test_about_lists_the_features_and_what_is_not_here_with_why(show):
    page = board(show, BOARD_LINUX, "about")
    expect(by_id(page, "about-features")).to_contain_text("lcd_mirror", timeout=T)
    expect(by_id(page, "about-missing").locator('li[data-capability="reboot_board"]')).to_contain_text(
        "needs the Debug USB cable")
    expect(by_id(page, "about-shell")).to_have_text("0x44ee76d5")   # the rc2 static (UI2-POLISH)


def test_twin_the_board_with_a_debug_usb_can_reboot(show):
    page = board(show, BOARD_V011, "about")
    expect(by_id(page, "about-missing")).to_be_visible(timeout=T)
    assert by_id(page, "about-missing").locator('li[data-capability="reboot_board"]').count() == 0


# --- one screen ---------------------------------------------------------------------------------------


def overflow(page: Any) -> list:
    return page.evaluate("""() => { const s = document.querySelector('.section-body');
        return [s.scrollHeight - s.clientHeight, document.documentElement.scrollWidth - document.documentElement.clientWidth]; }""")


def test_every_board_page_fits_one_1440x900_screen(show):
    for bid in (BOARD_LINUX, BOARD_V011):
        page = board(show, bid)
        for sub in PAGES:
            nav.board_page(page, sub)
            page.wait_for_timeout(600)
            tall, wide = overflow(page)
            assert tall <= 2 and wide <= 0, (bid, sub, tall, wide)
    assert not page.errors, page.errors


def test_twin_below_1180_px_the_list_becomes_tiles_and_nothing_overflows_sideways(show):
    page = board(show, BOARD_LEASED, width=1024, height=768)
    display = page.evaluate("getComputedStyle(document.querySelector('.bt-nav')).display")
    assert display == "grid"
    for sub in PAGES:
        nav.board_page(page, sub)
        page.wait_for_timeout(300)
        assert overflow(page)[1] <= 0, sub


def _each_slot(doc: Any):
    """Every OS slot record in a /slots or /card reply, wherever it sits."""
    if isinstance(doc, dict):
        sl = doc.get("slots")
        if isinstance(sl, dict) and set(sl) >= {"A", "B"} and all(isinstance(v, dict) for v in sl.values()):
            yield sl
        for v in doc.values():
            yield from _each_slot(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _each_slot(v)


def _route_slots(page: Any, edit: Any) -> None:
    def handler(route: Any) -> None:
        body = route.fetch().json()
        for sl in _each_slot(body):
            edit(sl)
        reply(route, 200, body)
    page.route("**/api/v1/boards/*/slots", handler)
    page.route("**/api/v1/boards/*/card", handler)


def _versions(show: Any, edit: Any) -> Any:
    page = show.page(**APP)
    _route_slots(page, edit)
    nav.open_board(page, BOARD_LINUX)
    nav.tab(page, "board")
    nav.board_page(page, "versions")
    return page


def test_a_valid_slot_without_a_version_record_is_not_drawn_empty(show):
    # v2.0.0's image has no version record: the running slot must not read "Empty" (7 Oct, board 1)
    def no_versions(sl: dict) -> None:
        for v in sl.values():
            v["version"] = ""
    page = _versions(show, no_versions)
    a = by_id(page, "slot-A")
    expect(a).to_contain_text("Running · default", timeout=T)
    assert "Empty" not in a.inner_text() and "nothing boots" not in a.inner_text()
    expect(by_id(page, "slot-B")).to_contain_text("Fallback")


def test_twin_an_empty_slot_still_reads_empty(show):
    def b_empty(sl: dict) -> None:
        sl["B"].update(state="empty", version="", sid="")
    page = _versions(show, b_empty)
    expect(by_id(page, "slot-B")).to_contain_text("Empty", timeout=T)
    expect(by_id(page, "slot-B")).to_contain_text("nothing boots from it")
