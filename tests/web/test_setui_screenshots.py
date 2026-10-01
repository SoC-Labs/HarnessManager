"""Lane SET-UI review screenshots, light and dark, over the REAL daemon: the Settings dialog's
sections as a user meets them (General under an admin policy, Hubs with a tested REST hub and
a machine hub, a 401 at auth, Boards with the MPS3 pack's rows, Tools with Detect, a secret,
the restart note). SET-UI-MERGE adds main's rows: the OS-slot card timing under the MPS3
pack, an SSH hub's SD stage directory, an MCC entry left in boards.toml (the MCC's path,
never a share), the developer seams, the restart note's start flag and the demo's Hubs.

They land in tests/web/screenshots/review/ (gitignored); the curated copies for david are
committed as docs/review/2026-09-26/settings-*.png (SET-UI) and docs/review/2026-09-27/
settings-*.png (SET-UI-MERGE, the whole set again on today's main). Each test asserts the
state it photographs, so a picture never shows a broken page.
"""

from __future__ import annotations

import os

import pytest

from tests.web.test_setui_browser import (  # noqa: F401 - fixtures
    APP,
    SECRET,
    T,
    _no_fpgahub_login,
    add_rest_hub,
    by_id,
    fake_tool,
    fpgahub,
    open_settings,
    row,
    set_token,
    source,
    world,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
SCHEMES = pytest.mark.parametrize("scheme", ["light", "dark"])
POLICY = ('[lock]\n"debug.xvc_port_base" = 23650\n'
          '[default]\n"updates.check_interval" = "12h"\n')
BOARDS = ('# the lab board, reached through the hub\n[boards.lab]\nmatch = ["192.168.10.101"]\n'
          'name = "mps3-01"\nvia = "hub"\n'
          'hub = { use = "remote", target = "mps3_01_pl", shares = { fpga_uart1 = "/dev/mps3_01_pl/tty_01" } }\n'
          'xvc = { reach = "board-ssh" }\nssh = { user = "root" }\n')


@pytest.fixture
def review(screenshots):
    out = screenshots / "review"
    out.mkdir(parents=True, exist_ok=True)
    return out


def shoot(page, review, name):
    page.wait_for_timeout(400)
    assert not page.errors, page.errors
    by_id(page, "settings").screenshot(path=str(review / name))


@SCHEMES
def test_review_settings_general_under_a_policy(page_factory, world, review, scheme):  # noqa: F811
    world(policy=POLICY, settings='[general]\nwindow_size = "1600x1000"\n',
          env={"HARNESS_MANAGER_APP_BROWSER": "chromium"})
    page = open_settings(page_factory, "general", scheme, dev=True)     # window_size: nothing reads it
    expect(by_id(page, "settings-policy-note")).to_be_visible(timeout=T)
    expect(source(row(page, "general.window_size"))).to_have_attribute("data-source", "user")
    expect(source(row(page, "general.app_browser"))).to_have_attribute("data-source", "env")
    shoot(page, review, f"settings-general-{scheme}.png")


@SCHEMES
def test_review_settings_hubs_tested(page_factory, world, fpgahub, review, scheme):  # noqa: F811
    world(policy='[hubs.lab]\ntransport = "ssh"\nhost = "mapstone-dev.ecs.soton.ac.uk"\n')
    page = open_settings(page_factory, "hubs", scheme)
    card = add_rest_hub(page, "remote", fpgahub.url)
    set_token(card, fpgahub.add_token("alice", "write"))
    card.locator('[data-action="hub-test"]').click()
    expect(card.locator('[data-testid="test-result"]')).to_have_attribute("data-passed", "true", timeout=T)
    card.locator('tr[data-target="mps3_01_pl"] [data-action="hub-add-board"]').click()
    expect(card.locator('tr[data-target="mps3_01_pl"]')).to_contain_text("used by", timeout=T)
    expect(page.locator('[data-testid="hub-card"][data-hub="lab"]')).to_have_attribute("data-machine", "true")
    card.locator('[data-testid="hub-targets"]').scroll_into_view_if_needed()
    shoot(page, review, f"settings-hubs-{scheme}.png")
    page.locator('[data-testid="hub-card"][data-hub="lab"]').scroll_into_view_if_needed()
    shoot(page, review, f"settings-hubs-machine-{scheme}.png")


@SCHEMES
def test_review_settings_hub_auth_fails(page_factory, world, fpgahub, review, scheme):  # noqa: F811
    world(settings=f'[hubs.remote]\ntransport = "rest"\nurl = "{fpgahub.url}"\n')
    page = open_settings(page_factory, "hubs", scheme)
    card = page.locator('[data-testid="hub-card"][data-hub="remote"]')
    set_token(card, "a-token-the-hub-never-made")
    card.locator('[data-action="hub-test"]').click()
    expect(card.locator('[data-testid="test-failure"]')).to_have_attribute("data-step", "auth", timeout=T)
    shoot(page, review, f"settings-hub-auth-401-{scheme}.png")


@SCHEMES
def test_review_settings_boards_with_the_pack_rows(page_factory, world, fpgahub, review, scheme):  # noqa: F811
    world(settings=f'[hubs.remote]\ntransport = "rest"\nurl = "{fpgahub.url}"\n', boards=BOARDS)
    page = open_settings(page_factory, "boards", scheme)
    card = page.locator('[data-testid="board-card"][data-board="lab"]')
    expect(card).to_be_visible(timeout=T)
    expect(row(page, 'boards.lab.ssh.user')).to_be_visible()
    expect(row(page, 'boards.lab.xvc.reach')).to_be_visible()
    card.locator('[data-testid="setting-group"][data-group="board:lab:hub"]').scroll_into_view_if_needed()
    shoot(page, review, f"settings-boards-{scheme}.png")


@SCHEMES
@pytest.mark.skipif(os.name != "posix", reason="a /bin/sh script stands in for vivado")
def test_review_settings_tools_detect(page_factory, world, review, scheme, tmp_path):  # noqa: F811
    viv = fake_tool(tmp_path / "Xilinx" / "Vivado" / "2024.1" / "bin" / "vivado",
                    'echo "vivado v2024.1 (64-bit)"\n')
    fake_tool(tmp_path / "Xilinx" / "Vivado" / "2024.1" / "bin" / "hw_server", "exit 0\n")
    world(env={"HARNESS_MANAGER_OPENOCD": "/opt/openocd-0.12/bin/openocd"},
          settings=f'[tools]\nvivado = "{viv}"\nopenocd = "/usr/local/bin/openocd"\n',
          policy=f'[lock]\n"tools.hw_server" = "{viv.parent / "hw_server"}"\n')
    page = open_settings(page_factory, "tools", scheme)
    for key in ("tools.vivado", "tools.hw_server"):
        r = row(page, key)
        r.locator('[data-action="tool-detect"]').click()
        expect(r.locator('[data-testid="tool-result"]')).to_have_attribute("data-ok", "true", timeout=T)
    r = row(page, "tools.openocd")
    r.locator('[data-action="tool-detect"]').click()
    expect(r.locator('[data-testid="tool-result"]')).to_have_attribute("data-ok", "false", timeout=T)
    shoot(page, review, f"settings-tools-{scheme}.png")


@SCHEMES
def test_review_settings_secret_and_restart(page_factory, world, review, scheme):  # noqa: F811
    sctx = world()
    sctx.store().set("updates.github_token", SECRET)
    page = open_settings(page_factory, "advanced", scheme)
    r = row(page, "advanced.log_level")
    r.locator('select[data-testid="setting-input"]').select_option("debug")
    expect(by_id(page, "settings-restart")).to_be_visible(timeout=T)
    page.locator('[data-testid="settings-nav"] [data-settings-section="updates"]').click()
    expect(row(page, "updates.github_token").locator('[data-testid="secret-field"]')).to_have_attribute(
        "data-set", "true", timeout=T)
    row(page, "updates.github_token").scroll_into_view_if_needed()
    assert SECRET not in page.content()
    shoot(page, review, f"settings-updates-secret-restart-{scheme}.png")


# --- SET-UI-MERGE: main's rows ------------------------------------------------------------------------


@SCHEMES
def test_review_settings_slot_rows_under_the_pack(page_factory, world, review, scheme):  # noqa: F811
    world(settings='[mps3.slot]\njob_timeout_s = 2400.0\n')
    page = open_settings(page_factory, "harness-kits", scheme)
    group = page.locator('[data-testid="setting-group"][data-group="harness-kits:mps3"]')
    group.locator('[data-action="show-more"]').click()
    expect(group.locator('[data-key="mps3.slot.card_read_bps"]')).to_be_visible(timeout=T)
    r = row(page, "mps3.slot.push_timeout_s")
    r.locator('[data-testid="setting-input"]').fill("0")
    r.locator('[data-testid="setting-input"]').press("Enter")
    expect(r.locator('[data-testid="row-error"]')).to_contain_text("more than 0", timeout=T)
    group.scroll_into_view_if_needed()
    shoot(page, review, f"settings-harness-kits-slot-{scheme}.png")


@SCHEMES
def test_review_settings_ssh_hub_stage_dir(page_factory, world, review, scheme):  # noqa: F811
    world(settings='[hubs.lab]\ntransport = "ssh"\nhost = "mapstone-dev.ecs.soton.ac.uk"\n'
                   'stage_dir = "/srv/fpga/hm-stage"\n')
    page = open_settings(page_factory, "hubs", scheme)
    card = page.locator('[data-testid="hub-card"][data-hub="lab"]')
    card.locator('[data-action="show-more"]').click()
    expect(row(page, "hubs.lab.stage_dir")).to_be_visible(timeout=T)
    row(page, "hubs.lab.stage_dir").scroll_into_view_if_needed()
    shoot(page, review, f"settings-hubs-ssh-stage-dir-{scheme}.png")


@SCHEMES
def test_review_settings_boards_mcc_path_never_a_share(page_factory, world, review, scheme):  # noqa: F811
    world(settings='[hubs.lab]\ntransport = "ssh"\nhost = "mapstone-dev.ecs.soton.ac.uk"\n',
          boards='[boards.lab]\nmatch = ["192.168.10.101"]\nname = "mps3-01"\nvia = "hub"\n'
                 'hub = { use = "lab", target = "mps3_01_pl", shares = { mcc = "/dev/mps3_01_pl/tty_00", '
                 'fpga_uart1 = "/dev/mps3_01_pl/tty_01" } }\n')
    page = open_settings(page_factory, "boards", scheme)
    mcc = page.locator('[data-testid="mcc-path"]')
    expect(mcc).to_be_visible(timeout=T)
    mcc.scroll_into_view_if_needed()
    shoot(page, review, f"settings-boards-mcc-path-{scheme}.png")


@SCHEMES
def test_review_settings_developer_seams(page_factory, world, review, scheme):  # noqa: F811
    world(env={"HARNESS_MANAGER_DEBUG": "1"})
    page = open_settings(page_factory, "advanced", scheme)
    page.locator('[data-action="show-dev"]').check()
    expect(row(page, "dev.debug")).to_have_attribute("data-dev", "true", timeout=T)
    row(page, "advanced.log_level").locator("select").select_option("debug")
    expect(by_id(page, "settings-restart").locator('[data-testid="restart-flags"]')).to_be_visible(timeout=T)
    page.locator('[data-testid="setting-group"][data-group="advanced:dev"]').scroll_into_view_if_needed()
    shoot(page, review, f"settings-advanced-dev-seams-{scheme}.png")


@SCHEMES
def test_review_settings_demo_hubs(browser, tmp_path, monkeypatch, request, review, scheme):
    from tests.web.test_demo_all_browser import make_showcase

    shows = make_showcase(browser, tmp_path, monkeypatch, request)
    show = next(shows)
    try:
        show.daemon.app.state.daemon.settings.demo = True          # as run_daemon --demo does
        page = show.page(scheme)
        page.locator('[data-action="settings"]').click()
        page.locator('[data-testid="settings-nav"] [data-settings-section="hubs"]').click()
        page.locator('[data-action="hub-add-open"]').click()
        by_id(page, "hub-add-form").locator('[data-field="host"]').fill("mapstone-dev.ecs.soton.ac.uk")
        expect(by_id(page, "hubs-demo-note")).to_be_visible(timeout=T)
        expect(by_id(page, "hub-add-form").locator('[data-action="hub-add-test"]')).to_be_disabled()
        shoot(page, review, f"settings-demo-hubs-{scheme}.png")
    finally:
        shows.close()
