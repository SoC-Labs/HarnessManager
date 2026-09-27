"""Lane SET-UI review screenshots, light and dark, over the REAL daemon: the Settings dialog's
sections as a user meets them (General under an admin policy, Hubs with a tested REST hub and
a machine hub, a 401 at auth, Boards with the MPS3 pack's rows, Tools with Detect, a secret,
the restart note).

They land in tests/web/screenshots/review/ (gitignored); the curated copies for david are
committed as docs/review/2026-09-26/settings-*.png. Each test asserts the state it
photographs, so a picture never shows a broken page.
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
    page = open_settings(page_factory, "general", scheme)
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
