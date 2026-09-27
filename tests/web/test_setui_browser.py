"""Lane SET-UI: the Settings dialog, in a real browser, over the REAL daemon (and the T14 mock
where marked ``mock_too``: it serves the same settings and hubs routes over a resolver of its
own). Every action on the page is a click or typing; each behaviour has its negative twin.

The daemon's settings context is pointed at this test's own world: its config dir (the
daemon's tmp state dir), a policy file in ``tmp_path`` (never /etc), a controlled environment
(``HARNESS_MANAGER_KEYRING=off``: the 0600 file store, never the OS keyring) and the MPS3
pack's rows. Hubs are the T8 fake fpgahub on 127.0.0.1 (REST) only: no ssh, no real hub, no
board. Tools Detect runs fake executables written here.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import tomllib

from harness_manager.settings.packs import with_packs
from harness_manager.settings.resolve import core_schema
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t8_hub_rest import FakeFpgahub

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000
APP = {"width": 1440, "height": 1000}
SECRET = "ghp_SETUI-NEVER-SHOWN-7f3a91c2"


# --- the world -------------------------------------------------------------------------------------


def sctx_of(daemon: Any) -> Any:
    """The settings context of either server: the real daemon's ``d.settings``, the mock's own."""
    state = daemon.app.state
    return state.settings if hasattr(state, "settings") and not hasattr(state.daemon, "settings") \
        else state.daemon.settings


@pytest.fixture(autouse=True)
def _no_fpgahub_login(tmp_path, monkeypatch):
    # hub_rest reads the fpgahub login store from the process environment: never the user's.
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "no-fpgahub-login.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)
    monkeypatch.delenv("FPGAHUB_ADDR", raising=False)


@pytest.fixture
def world(daemon, tmp_path):
    """``world(env=..., policy=..., settings=..., boards=...)`` -> the settings context."""
    def make(*, env: dict[str, str] | None = None, policy: str = "", settings: str = "",
             boards: str = "") -> Any:
        sctx = sctx_of(daemon)
        sctx.env = {"HARNESS_MANAGER_KEYRING": "off", "PATH": "/usr/bin:/bin", **(env or {})}
        sctx.policy_path = tmp_path / "policy.toml"
        if policy:
            sctx.policy_path.write_text(policy)
        sctx.keyrings = []
        sctx._store = None
        sctx._layers = with_packs(core_schema(), [Mps3Pack()])
        root = sctx.config_dir
        root.mkdir(parents=True, exist_ok=True)
        if settings:
            (root / "settings.toml").write_text(settings)
        if boards:
            (root / "boards.toml").write_text(boards)
        return sctx
    return make


def open_settings(page_factory, section: str, scheme: str = "light"):
    page = page_factory(scheme, **APP)
    page.locator('[data-action="settings"]').click()
    expect(by_id(page, "settings")).to_be_visible(timeout=T)
    page.locator(f'[data-testid="settings-nav"] [data-settings-section="{section}"]').click()
    expect(by_id(page, "settings-pane")).to_have_attribute("data-settings-section", section, timeout=T)
    return page


def by_id(page, testid: str):
    return page.locator(f'[data-testid="{testid}"]')


def row(page, key: str):
    return page.locator(f'[data-testid="setting-row"][data-key="{key}"]')


def source(r) -> Any:
    return r.locator('[data-testid="source-chip"] [data-source]')


def user_file(sctx: Any, name: str = "settings.toml") -> dict[str, Any]:
    path = sctx.config_dir / name
    return tomllib.loads(path.read_text()) if path.exists() else {}


# --- edit and reset ----------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_editing_a_row_saves_it_as_yours_and_reset_returns_the_default(page_factory, world):
    sctx = world()
    page = open_settings(page_factory, "consoles")
    r = row(page, "consoles.scrollback")
    expect(source(r)).to_have_attribute("data-source", "default", timeout=T)
    r.locator('[data-testid="setting-input"]').fill("8000")
    r.locator('[data-testid="setting-input"]').press("Enter")
    expect(source(r)).to_have_attribute("data-source", "user", timeout=T)
    expect(source(r)).to_have_text("yours")
    assert user_file(sctx)["consoles"]["scrollback"] == 8000
    r.locator('[data-action="setting-reset"]').click()
    expect(source(r)).to_have_attribute("data-source", "default", timeout=T)
    expect(r.locator('[data-testid="setting-input"]')).to_have_value("5000")
    assert "scrollback" not in user_file(sctx).get("consoles", {})
    assert not page.errors, page.errors


@pytest.mark.mock_too
def test_negative_twin_a_value_the_service_refuses_is_shown_and_nothing_is_written(page_factory, world):
    sctx = world()
    page = open_settings(page_factory, "general")
    r = row(page, "general.window_size")
    r.locator('[data-testid="setting-input"]').fill("huge")
    r.locator('[data-testid="setting-input"]').press("Enter")
    expect(r.locator('[data-testid="row-error"]')).to_contain_text("WIDTHxHEIGHT", timeout=T)
    expect(source(r)).to_have_attribute("data-source", "default")
    assert not (sctx.config_dir / "settings.toml").exists()
    # and a number outside its range is refused before it is sent (the schema's bounds)
    r = row(page, "panel.identify_s")
    r.locator('[data-testid="setting-input"]').fill("99")
    r.locator('[data-testid="setting-input"]').press("Enter")
    expect(r.locator('[data-testid="row-error"]')).to_contain_text("1..30", timeout=T)
    assert not (sctx.config_dir / "settings.toml").exists()


# --- the admin policy --------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_a_locked_row_is_disabled_and_names_the_policy_file(page_factory, world, tmp_path):
    world(policy='[lock]\ntools.vivado = "/tools/Xilinx/Vivado/2024.1/bin/vivado"\n')
    page = open_settings(page_factory, "tools")
    r = row(page, "tools.vivado")
    expect(r).to_have_attribute("data-locked", "true", timeout=T)
    expect(r.locator('[data-testid="setting-input"]')).to_be_disabled()
    expect(r.locator('[data-testid="setting-input"]')).to_have_value("/tools/Xilinx/Vivado/2024.1/bin/vivado")
    expect(source(r)).to_have_attribute("data-source", "lock")
    expect(r.locator('[data-testid="locked-note"]')).to_contain_text(str(tmp_path / "policy.toml"))
    expect(r.locator('[data-action="setting-reset"]')).to_have_count(0)


@pytest.mark.mock_too
def test_negative_twin_a_policy_default_is_a_lab_default_the_user_may_change(page_factory, world):
    sctx = world(policy='[default]\ntools.vivado = "/tools/Xilinx/Vivado/2024.1/bin/vivado"\n')
    page = open_settings(page_factory, "tools")
    r = row(page, "tools.vivado")
    expect(source(r)).to_have_attribute("data-source", "machine", timeout=T)
    expect(source(r)).to_have_text("lab default")
    expect(r).to_have_attribute("data-locked", "false")
    expect(r.locator('[data-testid="locked-note"]')).to_have_count(0)
    r.locator('[data-testid="setting-input"]').fill("/opt/mine/vivado")
    r.locator('[data-testid="setting-input"]').press("Enter")
    expect(source(r)).to_have_attribute("data-source", "user", timeout=T)
    assert user_file(sctx)["tools"]["vivado"] == "/opt/mine/vivado"


# --- the environment -------------------------------------------------------------------------------


def test_a_value_shadowed_by_the_services_environment_says_so(page_factory, world):
    world(env={"HARNESS_MANAGER_OPENOCD": "/env/openocd"},
          settings='[tools]\nopenocd = "/mine/openocd"\n')
    page = open_settings(page_factory, "tools")
    r = row(page, "tools.openocd")
    expect(source(r)).to_have_attribute("data-source", "env", timeout=T)
    expect(source(r)).to_have_text("from $HARNESS_MANAGER_OPENOCD")
    note = r.locator('[data-testid="shadow-note"]')
    expect(note).to_contain_text("Your value is hidden by $HARNESS_MANAGER_OPENOCD")
    expect(note).to_contain_text("in the service's environment")
    expect(r.locator('[data-testid="setting-input"]')).to_have_value("/env/openocd")
    expect(r.locator('[data-testid="setting-input"]')).to_be_disabled()
    expect(r.locator('[data-action="setting-reset"]')).to_have_count(1)     # yours can go


def test_negative_twin_no_value_of_yours_is_overridden_not_hidden_and_without_env_it_is_yours(page_factory, world):
    world(env={"HARNESS_MANAGER_OPENOCD": "/env/openocd"}, settings='[tools]\nvivado = "/mine/vivado"\n')
    page = open_settings(page_factory, "tools")
    r = row(page, "tools.openocd")
    expect(r.locator('[data-testid="env-note"]')).to_contain_text(
        "Overridden by $HARNESS_MANAGER_OPENOCD in the service's environment", timeout=T)
    expect(r.locator('[data-testid="shadow-note"]')).to_have_count(0)
    v = row(page, "tools.vivado")
    expect(source(v)).to_have_attribute("data-source", "user")
    expect(v.locator('[data-testid="shadow-note"], [data-testid="env-note"]')).to_have_count(0)


# --- secrets ---------------------------------------------------------------------------------------------


def watch_bodies(page) -> list[str]:
    bodies: list[str] = []

    def on_response(resp):
        if "/api/v1/" in resp.url:
            try:
                bodies.append(resp.text())
            except Exception:  # noqa: BLE001, S110 - a body that is gone (a socket) is not text
                pass
    page.on("response", on_response)
    return bodies


def test_a_secret_is_set_shows_set_and_its_value_is_never_in_the_page_or_a_reply(page_factory, world):
    sctx = world()
    page = open_settings(page_factory, "updates")
    bodies = watch_bodies(page)
    r = row(page, "updates.github_token")
    field = r.locator('[data-testid="secret-field"]')
    expect(field).to_have_attribute("data-set", "false", timeout=T)
    r.locator('[data-action="secret-set"]').click()
    r.locator('[data-testid="secret-input"]').fill(SECRET)
    r.locator('[data-action="secret-save"]').click()
    expect(field).to_have_attribute("data-set", "true", timeout=T)
    expect(r.locator('[data-testid="secret-status"]')).to_contain_text("set, in a private file (0600)")
    expect(r.locator('[data-testid="secret-input"]')).to_have_count(0)
    page.wait_for_timeout(300)
    assert SECRET not in page.content()
    assert SECRET not in json.dumps(page.evaluate("window.__harness_managerState()"))
    assert any("updates.github_token" in b for b in bodies)            # the replies were seen
    assert not any(SECRET in b for b in bodies)
    assert sctx.store().get("updates.github_token") == SECRET            # and it is stored
    assert not page.errors, page.errors


def test_negative_twin_remove_clears_the_stored_secret(page_factory, world):
    sctx = world()
    sctx.store().set("updates.github_token", SECRET)
    page = open_settings(page_factory, "updates")
    r = row(page, "updates.github_token")
    field = r.locator('[data-testid="secret-field"]')
    expect(field).to_have_attribute("data-set", "true", timeout=T)
    assert SECRET not in page.content()
    r.locator('[data-action="secret-clear"]').click()
    expect(field).to_have_attribute("data-set", "false", timeout=T)
    expect(r.locator('[data-testid="secret-status"]')).to_have_text("not set")
    assert sctx.store().get("updates.github_token") is None


# --- hubs: Test connection against the fake fpgahub REST ------------------------------------------


@pytest.fixture
def fpgahub():
    with FakeFpgahub() as hub:
        yield hub


def add_rest_hub(page, name: str, url: str) -> Any:
    page.locator('[data-action="hub-add-open"]').click()
    form = by_id(page, "hub-add-form")
    form.locator('[data-value="rest"]').click()
    form.locator('[data-field="url"]').fill(url)
    form.locator('[data-field="name"]').fill(name)
    form.locator('[data-action="hub-add-save"]').click()
    card = page.locator(f'[data-testid="hub-card"][data-hub="{name}"]')
    expect(card).to_be_visible(timeout=T)
    return card


def set_token(card, value: str) -> None:
    r = card.locator('[data-testid="setting-row"][data-key$=".token"]')
    r.locator('[data-action="secret-set"], [data-action="secret-replace"]').click()
    r.locator('[data-testid="secret-input"]').fill(value)
    r.locator('[data-action="secret-save"]').click()
    expect(r.locator('[data-testid="secret-field"]')).to_have_attribute("data-set", "true", timeout=T)


def test_hub_test_connection_passes_lists_the_targets_and_adds_a_board(page_factory, world, fpgahub):
    sctx = world()
    page = open_settings(page_factory, "hubs")
    expect(by_id(page, "hubs-empty")).to_be_visible(timeout=T)
    card = add_rest_hub(page, "remote", fpgahub.url)
    token = fpgahub.add_token("alice", "write")
    set_token(card, token)
    card.locator('[data-action="hub-test"]').click()
    result = card.locator('[data-testid="test-result"]')
    expect(result).to_have_attribute("data-passed", "true", timeout=T)
    for step in ("config", "reach", "auth", "targets"):
        expect(card.locator(f'[data-testid="test-steps"] [data-step="{step}"]')).to_have_attribute("data-state", "ok")
    expect(card.locator('[data-testid="hub-test-chip"]')).to_be_visible()
    targets = card.locator('[data-testid="hub-targets"] tr[data-target]')
    expect(targets).to_have_count(3)
    card.locator('tr[data-target="mps3_01_pl"] [data-action="hub-add-board"]').click()
    expect(card.locator('tr[data-target="mps3_01_pl"]')).to_contain_text("used by mps3_01_pl", timeout=T)
    boards = user_file(sctx, "boards.toml")["boards"]["mps3_01_pl"]
    assert boards["via"] == "hub" and boards["hub"]["use"] == "remote"
    assert boards["hub"]["target"] == "mps3_01_pl"
    assert user_file(sctx)["hubs"]["remote"]["url"] == fpgahub.url
    # reads only: no lease was taken, joined or released, and the token went nowhere else
    assert not fpgahub.leases and not any(fpgahub.queues.values()) and not fpgahub.emitted("lease.")
    assert {f"{q['method']} {q['path'].split('?')[0]}" for q in fpgahub.requests} <= {
        "GET /api/v1/health", "GET /api/v1/whoami", "GET /api/v1/groups",
        "GET /api/v1/targets/mps3_01_pl"}
    assert token not in page.content()
    assert not page.errors, page.errors


def test_negative_twin_a_token_the_hub_refuses_fails_at_auth_with_its_hint(page_factory, world, fpgahub):
    world(settings=f'[hubs.remote]\ntransport = "rest"\nurl = "{fpgahub.url}"\n')
    page = open_settings(page_factory, "hubs")
    card = page.locator('[data-testid="hub-card"][data-hub="remote"]')
    set_token(card, "not-a-token-the-hub-made")
    card.locator('[data-action="hub-test"]').click()
    result = card.locator('[data-testid="test-result"]')
    expect(result).to_have_attribute("data-passed", "false", timeout=T)
    expect(card.locator('[data-step="reach"]')).to_have_attribute("data-state", "ok")
    expect(card.locator('[data-testid="test-steps"] [data-step="auth"]')).to_have_attribute("data-state", "fail")
    failure = card.locator('[data-testid="test-failure"]')
    expect(failure).to_have_attribute("data-step", "auth")
    expect(failure).to_contain_text("401")
    expect(failure.locator('[data-testid="step-hint"]')).to_contain_text("hub token remote")
    expect(card.locator('[data-testid="hub-targets"]')).to_have_count(0)
    expect(card.locator('[data-testid="hub-test-chip"]')).to_contain_text("auth")


def test_a_machine_hub_is_locked_with_the_policy_named_but_the_token_is_yours(page_factory, world, fpgahub, tmp_path):
    world(policy=f'[hubs.lab]\ntransport = "rest"\nurl = "{fpgahub.url}"\n')
    page = open_settings(page_factory, "hubs")
    card = page.locator('[data-testid="hub-card"][data-hub="lab"]')
    expect(card).to_have_attribute("data-machine", "true", timeout=T)
    expect(card.locator('[data-testid="hub-policy-note"]')).to_contain_text(str(tmp_path / "policy.toml"))
    url = card.locator('[data-testid="setting-row"][data-key="hubs.lab.url"]')
    expect(url).to_have_attribute("data-locked", "true")
    expect(card.locator('[data-action="hub-remove"]')).to_have_count(0)
    set_token(card, fpgahub.add_token("bob", "read"))                    # the token is yours
    card.locator('[data-action="hub-test"]').click()
    expect(card.locator('[data-testid="test-result"]')).to_have_attribute("data-passed", "true", timeout=T)


def test_make_this_a_hub_adopts_an_inline_hub_table(page_factory, world, fpgahub):
    sctx = world(boards=f'# the lab board\n[boards.lab]\nmatch = ["192.168.10.101"]\n'
                        f'hub = {{ url = "{fpgahub.url}", target = "mps3_01_pl" }}\n')
    page = open_settings(page_factory, "hubs")
    inline = page.locator('[data-testid="inline-hub"][data-board="lab"]')
    expect(inline).to_be_visible(timeout=T)
    inline.locator('[data-action="hub-adopt"]').click()
    card = page.locator('[data-testid="hub-card"][data-hub="127-0-0-1"]')
    expect(card).to_be_visible(timeout=T)
    expect(card.locator('[data-testid="hub-boards"]')).to_contain_text("lab")
    expect(page.locator('[data-testid="inline-hub"]')).to_have_count(0)
    text = (sctx.config_dir / "boards.toml").read_text()
    assert "# the lab board" in text and 'use = "127-0-0-1"' in text
    assert user_file(sctx)["hubs"]["127-0-0-1"]["url"] == fpgahub.url
    assert list(sctx.config_dir.glob("boards.toml.bak-*"))


def test_negative_twin_a_board_that_names_its_hub_offers_no_adopt_and_remove_is_refused_while_used(page_factory, world, fpgahub):
    sctx = world(settings=f'[hubs.remote]\ntransport = "rest"\nurl = "{fpgahub.url}"\n',
                 boards='[boards.lab]\nvia = "hub"\nhub = { use = "remote", target = "mps3_01_pl" }\n')
    page = open_settings(page_factory, "hubs")
    card = page.locator('[data-testid="hub-card"][data-hub="remote"]')
    expect(card).to_be_visible(timeout=T)
    expect(page.locator('[data-testid="inline-hub"]')).to_have_count(0)
    card.locator('[data-action="hub-remove"]').click()
    expect(card.locator('[data-testid="hub-remove-confirm"]')).to_contain_text("lab")
    card.locator('[data-action="hub-remove-confirm"]').click()          # force: its board is named
    expect(page.locator('[data-testid="hub-card"]')).to_have_count(0, timeout=T)
    assert "remote" not in user_file(sctx).get("hubs", {})


# --- restart ------------------------------------------------------------------------------------------


def test_a_restart_class_change_shows_the_restart_banner(page_factory, world):
    world()
    page = open_settings(page_factory, "advanced")
    r = row(page, "advanced.log_level")
    r.locator('select[data-testid="setting-input"]').select_option("debug")
    expect(source(r)).to_have_attribute("data-source", "user", timeout=T)
    note = by_id(page, "settings-restart")
    expect(note).to_contain_text("1 change needs the service restarted", timeout=T)
    expect(note).to_contain_text("advanced.log_level")
    expect(r.locator('[data-testid="apply-note"]')).to_have_attribute("data-apply", "restart")
    page.locator('[data-action="settings-close"]').click()
    expect(by_id(page, "restart-banner")).to_contain_text("advanced.log_level", timeout=T)
    page.reload()                                                     # kept for this service
    expect(by_id(page, "restart-banner")).to_be_visible(timeout=T)


def test_negative_twin_a_live_change_shows_no_restart_banner(page_factory, world):
    world()
    page = open_settings(page_factory, "consoles")
    r = row(page, "consoles.font_size")
    r.locator('[data-testid="setting-input"]').fill("15")
    r.locator('[data-testid="setting-input"]').press("Enter")
    expect(source(r)).to_have_attribute("data-source", "user", timeout=T)
    page.wait_for_timeout(300)
    expect(by_id(page, "settings-restart")).to_have_count(0)
    expect(r.locator('[data-testid="apply-note"]')).to_have_count(0)
    page.locator('[data-action="settings-close"]').click()
    expect(by_id(page, "restart-banner")).to_have_count(0)


# --- Tools: Detect ---------------------------------------------------------------------------------------


def fake_tool(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + f'echo "$@" >> "{path}.argv"\n' + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.skipif(os.name != "posix", reason="a /bin/sh script stands in for vivado")
def test_tools_detect_runs_only_the_version_probe_of_a_fake_vivado(page_factory, world, tmp_path):
    viv = fake_tool(tmp_path / "Xilinx" / "Vivado" / "2024.1" / "bin" / "vivado",
                    'echo "vivado v2024.1 (64-bit)"\necho "SW Build 5076996 on Wed May 22 18:36:09 MDT 2024"\n')
    world()
    page = open_settings(page_factory, "tools")
    r = row(page, "tools.vivado")
    r.locator('[data-testid="setting-input"]').fill(str(viv))
    r.locator('[data-testid="setting-input"]').press("Enter")
    expect(source(r)).to_have_attribute("data-source", "user", timeout=T)
    r.locator('[data-action="tool-detect"]').click()
    res = r.locator('[data-testid="tool-result"]')
    expect(res).to_have_attribute("data-ok", "true", timeout=T)
    expect(res).to_contain_text(f"Vivado 2024.1 at {viv}")
    assert Path(f"{viv}.argv").read_text().split() == ["-version"]       # nothing else ran


@pytest.mark.skipif(os.name != "posix", reason="a /bin/sh script stands in for vivado")
def test_negative_twin_a_vivado_that_does_not_run_is_refused(page_factory, world, tmp_path):
    viv = fake_tool(tmp_path / "broken" / "vivado", 'echo "segfault" >&2\nexit 139\n')
    world(settings=f'[tools]\nvivado = "{viv}"\n')
    page = open_settings(page_factory, "tools")
    r = row(page, "tools.vivado")
    r.locator('[data-action="tool-detect"]').click()
    res = r.locator('[data-testid="tool-result"]')
    expect(res).to_have_attribute("data-ok", "false", timeout=T)
    expect(res).to_contain_text("does not run")
    expect(r.locator('[data-action="tool-use"]')).to_have_count(0)


# --- reopen: a change that applies at the next board open ------------------------------------------


def test_a_reopen_class_change_offers_reopen_board_which_closes_and_opens_it(page_factory, world, engine):
    from tests.web.test_updui_browser import BOARD_USB, open_board

    world()
    page = page_factory("light", **APP)
    open_board(page, BOARD_USB)
    page.locator('[data-action="settings"]').click()
    page.locator('[data-testid="settings-nav"] [data-settings-section="consoles"]').click()
    r = row(page, "mps3.console.pace_ms")
    expect(source(r)).to_have_attribute("data-source", "pack", timeout=T)
    r.locator('[data-testid="setting-input"]').fill("30")
    r.locator('[data-testid="setting-input"]').press("Enter")
    note = r.locator('[data-testid="apply-note"]')
    expect(note).to_have_attribute("data-apply", "reopen", timeout=T)
    opens = len(engine.called("open"))
    note.locator('[data-action="reopen-board"]').click()
    expect(note).to_have_count(0, timeout=T)
    assert len(engine.called("open")) == opens + 1
    assert [a[0] for a in engine.called("close")][-1] == BOARD_USB
    page.locator('[data-action="settings-close"]').click()
    expect(by_id(page, "lock-chip")).to_be_visible(timeout=T)            # open again, here


def test_negative_twin_with_no_board_open_a_reopen_change_offers_no_button(page_factory, world, engine):
    world()
    page = open_settings(page_factory, "consoles")
    r = row(page, "mps3.console.pace_ms")
    r.locator('[data-testid="setting-input"]').fill("30")
    r.locator('[data-testid="setting-input"]').press("Enter")
    expect(r.locator('[data-testid="apply-note"]')).to_contain_text("next time the board opens", timeout=T)
    expect(r.locator('[data-action="reopen-board"]')).to_have_count(0)
    assert engine.called("close") == []


# --- a change made elsewhere (the CLI, another tab) reaches the open dialog ----------------------


@pytest.mark.mock_too
def test_a_change_made_elsewhere_shows_in_the_open_dialog(page_factory, world, daemon):
    import urllib.request

    world()
    page = open_settings(page_factory, "consoles")
    r = row(page, "consoles.line_ending")
    expect(r.locator('[aria-pressed="true"]')).to_have_attribute("data-value", "crlf", timeout=T)
    req = urllib.request.Request(f"{daemon.url}/api/v1/settings", method="PUT",
                                 data=json.dumps({"consoles.line_ending": "lf"}).encode(),
                                 headers={"Authorization": f"Bearer {daemon.token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:                # noqa: S310 - loopback
        assert resp.status == 200
    expect(r.locator('[aria-pressed="true"]')).to_have_attribute("data-value", "lf", timeout=T)
    expect(source(r)).to_have_attribute("data-source", "user")


@pytest.mark.mock_too
def test_negative_twin_a_hand_edit_is_read_when_the_dialog_opens(page_factory, world):
    sctx = world()
    (sctx.config_dir / "settings.toml").write_text('[consoles]\nline_ending = "cr"\n')
    page = open_settings(page_factory, "consoles")
    r = row(page, "consoles.line_ending")
    expect(r.locator('[aria-pressed="true"]')).to_have_attribute("data-value", "cr", timeout=T)


# --- Boards: add one, and its rows --------------------------------------------------------------------


@pytest.mark.mock_too
def test_add_a_board_writes_boards_toml_and_shows_its_rows(page_factory, world):
    sctx = world()
    page = open_settings(page_factory, "boards")
    expect(by_id(page, "boards-empty")).to_be_visible(timeout=T)
    page.locator('[data-action="board-add-open"]').click()
    form = by_id(page, "board-add-form")
    form.locator('[data-field="key"]').fill("bench")
    form.locator('[data-field="match"]').fill("192.168.10.120")
    form.locator('[data-field="name"]').fill("bench-mps3")
    form.locator('[data-action="board-add-save"]').click()
    card = page.locator('[data-testid="board-card"][data-board="bench"]')
    expect(card).to_be_visible(timeout=T)
    expect(row(page, "boards.bench.name").locator('[data-testid="setting-input"]')).to_have_value("bench-mps3")
    expect(row(page, "boards.bench.ssh.user")).to_be_visible()                # the pack's rows
    assert user_file(sctx, "boards.toml")["boards"]["bench"] == {"match": ["192.168.10.120"],
                                                                 "name": "bench-mps3"}


@pytest.mark.mock_too
def test_negative_twin_a_board_key_that_is_not_one_cannot_be_added(page_factory, world):
    sctx = world()
    page = open_settings(page_factory, "boards")
    page.locator('[data-action="board-add-open"]').click()
    form = by_id(page, "board-add-form")
    form.locator('[data-field="key"]').fill("no spaces allowed")
    expect(form.locator('[data-action="board-add-save"]')).to_be_disabled()
    assert not (sctx.config_dir / "boards.toml").exists()


def test_a_secret_stored_in_a_keyring_this_service_cannot_reach_says_so(page_factory, world):
    sctx = world()
    sctx.store()._write_index({"updates.github_token": {"backend": "secret-service"}})
    page = open_settings(page_factory, "updates")
    r = row(page, "updates.github_token")
    field = r.locator('[data-testid="secret-field"]')
    expect(field).to_have_attribute("data-set", "true", timeout=T)
    expect(field).to_have_attribute("data-reachable", "false")
    expect(r.locator('[data-testid="secret-status"]')).to_contain_text("which this service cannot reach")
    expect(r.locator('[data-testid="secret-unreachable"]')).to_contain_text("Stored, unreachable here (exit 7)")


def test_negative_twin_setting_it_again_here_moves_it_to_the_private_file(page_factory, world):
    sctx = world()
    sctx.store()._write_index({"updates.github_token": {"backend": "secret-service"}})
    page = open_settings(page_factory, "updates")
    r = row(page, "updates.github_token")
    r.locator('[data-action="secret-replace"]').click()
    r.locator('[data-testid="secret-input"]').fill(SECRET)
    r.locator('[data-action="secret-save"]').click()
    field = r.locator('[data-testid="secret-field"]')
    expect(field).to_have_attribute("data-reachable", "true", timeout=T)
    expect(field).to_have_attribute("data-backend", "file")
    expect(r.locator('[data-testid="secret-unreachable"]')).to_have_count(0)
    assert sctx.store().get("updates.github_token") == SECRET
    assert SECRET not in page.content()


@pytest.mark.mock_too
def test_add_an_ssh_hub_writes_its_table_and_nothing_is_run(page_factory, world):
    sctx = world()
    page = open_settings(page_factory, "hubs")
    page.locator('[data-action="hub-add-open"]').click()
    form = by_id(page, "hub-add-form")
    form.locator('[data-field="host"]').fill("hub.invalid")
    form.locator('[data-field="jump"]').fill("gateway.invalid")
    form.locator('[data-field="name"]').fill("lab")
    form.locator('[data-action="hub-add-save"]').click()
    card = page.locator('[data-testid="hub-card"][data-hub="lab"]')
    expect(card).to_have_attribute("data-transport", "ssh", timeout=T)
    expect(card.locator('[data-testid="test-result"]')).to_have_count(0)      # no test ran
    assert user_file(sctx)["hubs"]["lab"] == {"transport": "ssh", "host": "hub.invalid",
                                              "jump": "gateway.invalid"}


@pytest.mark.mock_too
def test_negative_twin_a_hub_without_a_host_or_a_good_name_cannot_be_added(page_factory, world):
    sctx = world()
    page = open_settings(page_factory, "hubs")
    page.locator('[data-action="hub-add-open"]').click()
    form = by_id(page, "hub-add-form")
    form.locator('[data-field="name"]').fill("lab")
    expect(form.locator('[data-action="hub-add-save"]')).to_be_disabled()      # no host
    form.locator('[data-field="host"]').fill("hub.invalid")
    form.locator('[data-field="name"]').fill("not a name")
    expect(form.locator('[data-action="hub-add-save"]')).to_be_disabled()
    assert not (sctx.config_dir / "settings.toml").exists()
