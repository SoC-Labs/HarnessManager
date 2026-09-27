"""Lane SET-UI-MERGE: the Settings dialog on today's main, in a real browser over the REAL daemon.
SET-UI met SET-WIRE (apply classes, developer seams, ``daemon start`` flags), SLOT-TIMING (the
``mps3.slot.*`` rows), MCC-FIX (``hubs.*.stage_dir``; never a share on tty_00) and DEMO-ALL
(``app --demo``). Every action is a click or typing; each behaviour has its negative twin.

The world is SET-UI's (``test_setui_browser.world``): the daemon's own config dir, a policy in
``tmp_path``, ``HARNESS_MANAGER_KEYRING=off``. No ssh, no real hub, no board.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from harness_manager.demo import BOARD_USB
from tests.web.test_demo_all_browser import make_showcase
from tests.web.test_setui_browser import (  # noqa: F401 - fixtures
    APP,
    SECRET,
    T,
    _no_fpgahub_login,
    by_id,
    fpgahub,
    open_settings,
    row,
    source,
    user_file,
    world,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser


def pane(page):
    return by_id(page, "settings-pane")


def put(daemon: Any, body: dict) -> int:
    req = urllib.request.Request(f"{daemon.url}/api/v1/settings", method="PUT",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {daemon.token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:        # noqa: S310 - loopback
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def type_in(r, text: str) -> None:
    box = r.locator('[data-testid="setting-input"]')
    box.fill(text)
    box.press("Enter")


# --- developer seams (SET-WIRE: a variable only; config set refuses them) ----------------------------


@pytest.mark.mock_too
def test_developer_seams_show_read_only_with_their_variable_and_offer_no_edit(page_factory, world, daemon):  # noqa: F811
    sctx = world(settings='[dev]\nkeyring = "auto"\n')
    page = open_settings(page_factory, "advanced")
    page.locator('[data-action="show-dev"]').check()
    r = row(page, "dev.keyring")
    expect(r).to_have_attribute("data-dev", "true", timeout=T)
    expect(r.locator('[data-testid="dev-var"]')).to_have_text("$HARNESS_MANAGER_KEYRING")
    expect(r.locator('[data-testid="setting-value"]')).to_have_text("off")        # the service's
    expect(source(r)).to_have_attribute("data-source", "env")
    expect(r.locator('[data-testid="row-problem"]')).to_contain_text("developer seam")
    expect(r.locator('input, select, [data-action="setting-reset"]')).to_have_count(0)
    # a seam with no variable: the pack's --pack-overrides
    page.locator('[data-testid="settings-nav"] [data-settings-section="debug"]').click()
    rbb = row(page, "mps3.rbb_port")
    expect(rbb.locator('[data-testid="dev-var"]')).to_have_text("--pack-overrides", timeout=T)
    expect(rbb.locator("input")).to_have_count(0)
    # and the service refuses to set one, as the page never offers
    assert put(daemon, {"dev.keyring": "off"}) == 400
    assert user_file(sctx)["dev"] == {"keyring": "auto"}
    assert not page.errors, page.errors


@pytest.mark.mock_too
def test_negative_twin_without_the_toggle_no_developer_seam_shows(page_factory, world):  # noqa: F811
    world()
    page = open_settings(page_factory, "advanced")
    expect(row(page, "advanced.log_level")).to_be_visible(timeout=T)
    expect(page.locator('[data-dev="true"]')).to_have_count(0)
    expect(row(page, "dev.keyring")).to_have_count(0)
    page.locator('[data-testid="settings-nav"] [data-settings-section="debug"]').click()
    expect(row(page, "debug.hw_server_mode")).to_be_visible(timeout=T)
    expect(row(page, "mps3.rbb_port")).to_have_count(0)


# --- SLOT-TIMING: the OS-slot card timing rows, under the MPS3 pack, with their bounds --------------


@pytest.mark.mock_too
def test_the_slot_rows_show_under_the_mps3_pack_and_refuse_0_before_sending(page_factory, world):  # noqa: F811
    sctx = world()
    page = open_settings(page_factory, "harness-kits")
    group = page.locator('[data-testid="setting-group"][data-group="harness-kits:mps3"]')
    r = group.locator('[data-testid="setting-row"][data-key="mps3.slot.job_timeout_s"]')
    box = r.locator('[data-testid="setting-input"]')
    expect(box).to_have_attribute("type", "number", timeout=T)
    expect(box).to_have_attribute("min", "0")
    expect(box).to_have_value("1800")
    expect(source(r)).to_have_attribute("data-source", "pack")
    type_in(r, "0")
    expect(r.locator('[data-testid="row-error"]')).to_contain_text("must be more than 0", timeout=T)
    assert not (sctx.config_dir / "settings.toml").exists()
    # the card's rates are behind "more", ints of at least 1
    expect(group.locator('[data-key="mps3.slot.card_write_bps"]')).to_have_count(0)
    group.locator('[data-action="show-more"]').click()
    w = group.locator('[data-testid="setting-row"][data-key="mps3.slot.card_write_bps"]')
    expect(w.locator('[data-testid="setting-input"]')).to_have_attribute("min", "1", timeout=T)
    expect(w.locator('[data-testid="setting-input"]')).to_have_value("70000")
    assert not page.errors, page.errors


@pytest.mark.mock_too
def test_negative_twin_a_slot_value_in_range_is_saved_and_applies_live(page_factory, world):  # noqa: F811
    sctx = world()
    page = open_settings(page_factory, "harness-kits")
    r = row(page, "mps3.slot.job_timeout_s")
    type_in(r, "1200")
    expect(source(r)).to_have_attribute("data-source", "user", timeout=T)
    assert user_file(sctx)["mps3"]["slot"]["job_timeout_s"] == 1200
    expect(r.locator('[data-testid="apply-note"], [data-testid="row-error"]')).to_have_count(0)
    expect(by_id(page, "settings-restart")).to_have_count(0)


# --- MCC-FIX: hubs.*.stage_dir on an SSH hub's card --------------------------------------------------


@pytest.mark.mock_too
def test_an_ssh_hubs_card_has_its_sd_stage_dir_behind_more_checked_by_the_service(page_factory, world):  # noqa: F811
    sctx = world(settings='[hubs.lab]\ntransport = "ssh"\nhost = "hub.invalid"\n')
    page = open_settings(page_factory, "hubs")
    card = page.locator('[data-testid="hub-card"][data-hub="lab"]')
    expect(card).to_be_visible(timeout=T)
    expect(card.locator('[data-key="hubs.lab.stage_dir"]')).to_have_count(0)
    card.locator('[data-action="show-more"]').click()
    r = card.locator('[data-testid="setting-row"][data-key="hubs.lab.stage_dir"]')
    expect(r.locator("label")).to_have_text("SD stage directory", timeout=T)
    expect(r.locator('[data-testid="setting-input"]')).to_have_attribute("placeholder", ".cache/harness-manager/hub-sd")
    type_in(r, "has a space")
    expect(r.locator('[data-testid="row-error"]')).to_contain_text("no spaces", timeout=T)
    assert "stage_dir" not in user_file(sctx)["hubs"]["lab"]
    type_in(r, "/srv/fpga/hm-stage")
    expect(source(r)).to_have_attribute("data-source", "user", timeout=T)
    assert user_file(sctx)["hubs"]["lab"]["stage_dir"] == "/srv/fpga/hm-stage"


@pytest.mark.mock_too
def test_negative_twin_a_rest_hubs_card_has_no_stage_dir(page_factory, world, fpgahub):  # noqa: F811
    world(settings=f'[hubs.remote]\ntransport = "rest"\nurl = "{fpgahub.url}"\n')
    page = open_settings(page_factory, "hubs")
    card = page.locator('[data-testid="hub-card"][data-hub="remote"]')
    expect(card).to_be_visible(timeout=T)
    card.locator('[data-action="show-more"]').click()
    expect(card.locator('[data-key="hubs.remote.insecure"]')).to_be_visible(timeout=T)
    expect(card.locator('[data-key="hubs.remote.stage_dir"]')).to_have_count(0)


# --- MCC-FIX: never a share on tty_00 -----------------------------------------------------------------

LEGACY = ('[boards.lab]\nmatch = ["192.168.10.101"]\nvia = "hub"\n'
          'hub = { use = "lab", target = "mps3_01_pl", shares = { mcc = "/dev/mps3_01_pl/tty_00", '
          'fpga_uart1 = "/dev/mps3_01_pl/tty_01" } }\n')


@pytest.mark.mock_too
def test_an_mcc_entry_in_boards_toml_is_shown_as_the_mccs_path_never_as_a_share(page_factory, world):  # noqa: F811
    sctx = world(settings='[hubs.lab]\ntransport = "ssh"\nhost = "hub.invalid"\n', boards=LEGACY)
    page = open_settings(page_factory, "boards")
    card = page.locator('[data-testid="board-card"][data-board="lab"]')
    mcc = card.locator('[data-testid="mcc-path"]')
    expect(mcc).to_have_attribute("data-key", "boards.lab.hub.shares.mcc", timeout=T)
    expect(mcc).to_contain_text("is never a share: it only names the MCC console's path on the hub")
    expect(mcc).to_contain_text("It is the default path")
    expect(row(page, "boards.lab.hub.shares.mcc")).to_have_count(0)            # no share row
    lane = row(page, "boards.lab.hub.shares.fpga_uart1")                      # a lane share is one
    expect(lane.locator("label")).to_contain_text("fpga_uart1 share")
    expect(lane.locator('[data-testid="setting-input"]')).to_be_enabled()
    mcc.locator('[data-action="mcc-path-remove"]').click()
    expect(card.locator('[data-testid="mcc-path"]')).to_have_count(0, timeout=T)
    text = (sctx.config_dir / "boards.toml").read_text()
    assert "mcc" not in text and "tty_00" not in text and 'fpga_uart1 = "/dev/mps3_01_pl/tty_01"' in text
    assert not page.errors, page.errors


@pytest.mark.mock_too
def test_negative_twin_an_mcc_entry_naming_another_path_is_kept_and_a_lane_share_is_a_row(page_factory, world):  # noqa: F811
    sctx = world(settings='[hubs.lab]\ntransport = "ssh"\nhost = "hub.invalid"\n',
                 boards=LEGACY.replace("/dev/mps3_01_pl/tty_00", "/dev/mps3_02_pl/tty_00"))
    page = open_settings(page_factory, "boards")
    mcc = page.locator('[data-testid="mcc-path"]')
    expect(mcc).to_contain_text("/dev/mps3_02_pl/tty_00", timeout=T)
    expect(mcc.locator('[data-action="mcc-path-remove"]')).to_have_count(0)    # it means something
    expect(row(page, "boards.lab.hub.shares.fpga_uart1")).to_be_visible()
    assert "/dev/mps3_02_pl/tty_00" in (sctx.config_dir / "boards.toml").read_text()
    world(settings='[hubs.lab]\ntransport = "ssh"\nhost = "hub.invalid"\n',
          boards=LEGACY.replace('mcc = "/dev/mps3_01_pl/tty_00", ', ""))
    page = open_settings(page_factory, "boards")
    expect(row(page, "boards.lab.hub.shares.fpga_uart1")).to_be_visible(timeout=T)
    expect(page.locator('[data-testid="mcc-path"]')).to_have_count(0)


@pytest.mark.mock_too
def test_a_lane_share_left_on_tty_00_is_refused_with_the_reason_and_can_be_removed(page_factory, world):  # noqa: F811
    sctx = world(settings='[hubs.lab]\ntransport = "ssh"\nhost = "hub.invalid"\n',
                 boards=LEGACY.replace('mcc = "/dev/mps3_01_pl/tty_00"', 'fpga_uart0 = "/dev/mps3_01_pl/tty_00"'))
    page = open_settings(page_factory, "boards")
    r = row(page, "boards.lab.hub.shares.fpga_uart0")
    expect(r.locator('[data-testid="row-problem"]')).to_contain_text(
        "tty_00 is the MCC console; Harness Manager never shares it; the MCC is reached on the hub", timeout=T)
    expect(r.locator('[data-testid="setting-input"]')).to_have_value("")          # not in force
    expect(page.locator('[data-testid="mcc-path"]')).to_have_count(0)
    type_in(r, "/dev/mps3_01_pl/tty_00")                                        # typed again: refused
    expect(r.locator('[data-testid="row-error"]')).to_contain_text("must not be on tty_00", timeout=T)
    r.locator('[data-action="share-remove"]').click()
    expect(row(page, "boards.lab.hub.shares.fpga_uart0")).to_have_count(0, timeout=T)
    text = (sctx.config_dir / "boards.toml").read_text()
    assert "tty_00" not in text and 'fpga_uart1 = "/dev/mps3_01_pl/tty_01"' in text


@pytest.mark.mock_too
def test_negative_twin_a_lane_share_on_tty_01_has_no_problem_and_no_remove(page_factory, world):  # noqa: F811
    world(settings='[hubs.lab]\ntransport = "ssh"\nhost = "hub.invalid"\n',
          boards=LEGACY.replace('mcc = "/dev/mps3_01_pl/tty_00", ', ""))
    page = open_settings(page_factory, "boards")
    r = row(page, "boards.lab.hub.shares.fpga_uart1")
    expect(r.locator('[data-testid="setting-input"]')).to_have_value("/dev/mps3_01_pl/tty_01", timeout=T)
    expect(r.locator('[data-testid="row-problem"], [data-action="share-remove"]')).to_have_count(0)


# --- apply classes: a hub's reopen-class row reopens the boards that use that hub ---------------------

HUBS = ('[hubs.lab]\ntransport = "ssh"\nhost = "hub.invalid"\n'
        '[hubs.other]\ntransport = "ssh"\nhost = "other.invalid"\n')
BENCH = ('[boards.bench]\nmatch = ["192.168.10.102:6900"]\nvia = "hub"\n'
         'hub = { use = "lab", target = "mps3_01_pl" }\n')


def open_hubs_with_a_board_open(page_factory, world):  # noqa: F811
    from tests.web.test_updui_browser import open_board

    world(settings=HUBS, boards=BENCH)
    page = page_factory("light", **APP)
    open_board(page, BOARD_USB)
    page.locator('[data-action="settings"]').click()
    page.locator('[data-testid="settings-nav"] [data-settings-section="hubs"]').click()
    return page


def test_a_reopen_class_hub_change_offers_reopen_for_the_board_that_uses_the_hub(page_factory, world, engine):  # noqa: F811
    page = open_hubs_with_a_board_open(page_factory, world)
    r = row(page, "hubs.lab.host")
    expect(r.locator(".apply-mark")).to_have_text("on reopen", timeout=T)
    type_in(r, "hub2.invalid")
    note = r.locator('[data-testid="apply-note"]')
    expect(note).to_have_attribute("data-apply", "reopen", timeout=T)
    opens = len(engine.called("open"))
    note.locator('[data-action="reopen-board"]').click()
    expect(note).to_have_count(0, timeout=T)
    assert len(engine.called("open")) == opens + 1
    assert [a[0] for a in engine.called("close")][-1] == BOARD_USB


def test_negative_twin_a_hub_no_open_board_uses_or_a_live_row_offers_no_reopen(page_factory, world, engine):  # noqa: F811
    page = open_hubs_with_a_board_open(page_factory, world)
    r = row(page, "hubs.other.host")
    type_in(r, "other2.invalid")
    expect(r.locator('[data-testid="apply-note"]')).to_have_attribute("data-apply", "reopen", timeout=T)
    expect(r.locator('[data-action="reopen-board"]')).to_have_count(0)        # nothing to reopen
    live = row(page, "hubs.lab.lease_ttl")
    expect(live.locator(".apply-mark")).to_have_count(0)
    type_in(live, "20m")
    expect(source(live)).to_have_attribute("data-source", "user", timeout=T)
    expect(live.locator('[data-testid="apply-note"]')).to_have_count(0)
    assert engine.called("close") == []


def test_a_reopen_class_change_made_elsewhere_offers_reopen_for_the_open_board(page_factory, world, engine, daemon):  # noqa: F811
    page = open_hubs_with_a_board_open(page_factory, world)
    r = row(page, "hubs.lab.host")
    expect(r).to_be_visible(timeout=T)
    assert put(daemon, {"hubs.lab.host": "hub3.invalid"}) == 200             # the CLI, another tab
    expect(r.locator('[data-testid="setting-input"]')).to_have_value("hub3.invalid", timeout=T)
    expect(r.locator('[data-action="reopen-board"]')).to_be_visible(timeout=T)


def test_negative_twin_a_change_made_elsewhere_to_a_hub_no_open_board_uses_says_nothing(page_factory, world, engine, daemon):  # noqa: F811,E501
    page = open_hubs_with_a_board_open(page_factory, world)
    r = row(page, "hubs.other.host")
    expect(r).to_be_visible(timeout=T)
    assert put(daemon, {"hubs.other.host": "other3.invalid"}) == 200
    expect(r.locator('[data-testid="setting-input"]')).to_have_value("other3.invalid", timeout=T)
    page.wait_for_timeout(300)
    expect(r.locator('[data-testid="apply-note"]')).to_have_count(0)


# --- the restart note: SET-WIRE's `daemon start` reads the setting a flag would beat -----------------


@pytest.mark.mock_too
def test_the_restart_note_says_which_start_flag_would_win_over_the_setting(page_factory, world):  # noqa: F811
    world()
    page = open_settings(page_factory, "advanced")
    row(page, "advanced.log_level").locator("select").select_option("debug")
    note = by_id(page, "settings-restart")
    expect(note.locator('[data-testid="restart-command"]')).to_have_text(
        "harness-manager daemon stop && harness-manager ui", timeout=T)
    expect(note.locator('[data-testid="restart-flags"]')).to_contain_text("--log-level")


@pytest.mark.mock_too
def test_negative_twin_a_restart_row_with_no_start_flag_names_none(page_factory, world):  # noqa: F811
    world()
    page = open_settings(page_factory, "debug")
    page.locator('[data-testid="setting-group"][data-group="debug"] [data-action="show-more"]').click()
    type_in(row(page, "debug.port_base"), "24000")
    note = by_id(page, "settings-restart")
    expect(note).to_contain_text("debug.port_base", timeout=T)
    expect(note.locator('[data-testid="restart-flags"]')).to_have_count(0)


def test_a_restart_change_made_elsewhere_shows_the_banner_with_this_services_command(page_factory, world, daemon):  # noqa: F811
    world()
    page = page_factory("light", **APP)                  # the dialog never opened in this page
    page.wait_for_selector(".board-item", timeout=T)
    assert put(daemon, {"advanced.log_level": "debug"}) == 200                 # the CLI
    banner = by_id(page, "restart-banner")
    expect(banner).to_contain_text("advanced.log_level", timeout=T)
    expect(banner.locator('[data-testid="restart-command"]')).to_have_text(
        "harness-manager daemon stop && harness-manager ui", timeout=T)


# --- the demo: `app --demo`'s Settings keep to the demo's own dir, reach no hub -----------------------


@pytest.fixture
def demo(browser, tmp_path, monkeypatch, request):
    """The showcase demo daemon, marked as ``run_daemon --demo`` marks its settings."""
    shows = make_showcase(browser, tmp_path, monkeypatch, request)
    show = next(shows)
    show.daemon.app.state.daemon.settings.demo = True
    yield show
    shows.close()


@pytest.fixture
def not_demo(browser, tmp_path, monkeypatch, request):
    """The same showcase engine and daemon, its settings NOT marked as the demo's."""
    yield from make_showcase(browser, tmp_path, monkeypatch, request)


def demo_settings(show, section: str):
    page = show.page("light")
    page.locator('[data-action="settings"]').click()
    page.locator(f'[data-testid="settings-nav"] [data-settings-section="{section}"]').click()
    expect(by_id(page, "settings-pane")).to_have_attribute("data-settings-section", section, timeout=T)
    return page


def user_files() -> list[str]:
    root = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


def test_the_demos_settings_show_the_pack_rows_write_its_own_dir_and_reach_no_hub(demo, tmp_path):
    before = user_files()
    page = demo_settings(demo, "consoles")
    r = row(page, "mps3.console.pace_ms")                                     # the pack's rows
    type_in(r, "30")
    expect(source(r)).to_have_attribute("data-source", "user", timeout=T)
    assert "pace_ms = 30" in (tmp_path / "demo" / "settings.toml").read_text()
    page.locator('[data-testid="settings-nav"] [data-settings-section="updates"]').click()
    tok = row(page, "updates.github_token")
    tok.locator('[data-action="secret-set"]').click()
    tok.locator('[data-testid="secret-input"]').fill(SECRET)
    tok.locator('[data-action="secret-save"]').click()
    expect(tok.locator('[data-testid="secret-field"]')).to_have_attribute("data-backend", "file", timeout=T)
    assert (tmp_path / "demo" / "secrets" / "index.json").is_file()
    page.locator('[data-testid="settings-nav"] [data-settings-section="hubs"]').click()
    expect(by_id(page, "hubs-demo-note")).to_contain_text("The demo reaches no real hub", timeout=T)
    page.locator('[data-action="hub-add-open"]').click()
    form = by_id(page, "hub-add-form")
    form.locator('[data-field="host"]').fill("hub.invalid")
    expect(form.locator('[data-action="hub-add-test"]')).to_be_disabled()
    form.locator('[data-action="hub-add-save"]').click()
    card = page.locator('[data-testid="hub-card"][data-hub="hub"]')
    expect(card.locator('[data-action="hub-test"]')).to_be_disabled(timeout=T)
    page.locator('[data-testid="settings-nav"] [data-settings-section="advanced"]').click()
    expect(by_id(page, "service-dir-note")).to_contain_text(str(tmp_path / "demo"), timeout=T)
    row(page, "advanced.log_level").locator("select").select_option("debug")
    expect(by_id(page, "settings").locator('[data-testid="restart-command"]')).to_have_text(
        "harness-manager daemon stop --demo && harness-manager app --demo", timeout=T)
    page.locator('[data-action="settings-close"]').click()
    page.reload()                                     # the banner keeps the demo's command
    expect(by_id(page, "restart-banner").locator('[data-testid="restart-command"]')).to_have_text(
        "harness-manager daemon stop --demo && harness-manager app --demo", timeout=T)
    assert user_files() == before                                             # yours: untouched
    assert not page.errors, page.errors


def test_negative_twin_a_service_that_is_not_the_demo_tests_hubs_and_restarts_plainly(not_demo):
    page = demo_settings(not_demo, "hubs")
    expect(page.locator('[data-action="hub-add-open"]')).to_be_visible(timeout=T)
    expect(by_id(page, "hubs-demo-note")).to_have_count(0)
    page.locator('[data-action="hub-add-open"]').click()
    by_id(page, "hub-add-form").locator('[data-field="host"]').fill("hub.invalid")
    expect(by_id(page, "hub-add-form").locator('[data-action="hub-add-test"]')).to_be_enabled()
    page.locator('[data-testid="settings-nav"] [data-settings-section="advanced"]').click()
    row(page, "advanced.log_level").locator("select").select_option("debug")
    expect(by_id(page, "settings").locator('[data-testid="restart-command"]')).to_have_text(
        "harness-manager daemon stop && harness-manager ui", timeout=T)
