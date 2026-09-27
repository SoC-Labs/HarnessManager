"""DEBUG-OCD in the Settings dialog: Tools → Detect on OpenOCD says whether it has remote_bitbang.

Over the REAL daemon, in a real browser, with fake OpenOCDs written here (``/bin/sh`` scripts
that print what the real builds print, measured on srv03335 2026-09-27): a SoC Labs-like build
(jlink, buspirate, hostio4) and an xPack-like one (``name { transports }``, with
remote_bitbang). Every action is a click or typing; each behaviour has its negative twin.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness_manager.services import openocd_probe
from tests.web.test_setui_browser import (  # noqa: F401 - fixtures
    APP,
    T,
    _no_fpgahub_login,
    fake_tool,
    open_settings,
    row,
    source,
    world,
)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(os.name != "posix", reason="/bin/sh scripts stand in for OpenOCD")]

SOCLABS = ('echo "Open On-Chip Debugger 0.12.0-g9ea7f3d-dirty (2026-09-24-08:41)" >&2\n'
           'case "$2" in "adapter list") printf "The following debug adapters are available:\\n'
           '1: jlink\\n2: buspirate\\n3: hostio4\\n\\nshutdown command invoked\\n" >&2;; esac\n')
XPACK = ('echo "xPack Open On-Chip Debugger 0.12.0+dev-02228-ge5888bda3-dirty" >&2\n'
         'case "$2" in "adapter list") printf "cmsis-dap      { jtag swd }\\n'
         'ftdi           { jtag swd }\\nremote_bitbang { jtag swd }\\n'
         'shutdown command invoked\\n" >&2;; esac\n')


def test_detect_shows_an_openocd_without_remote_bitbang_as_not_ok_with_the_fix(
        page_factory, world, tmp_path):  # noqa: F811 - the SET-UI fixture
    bad = fake_tool(tmp_path / "soclabs" / "bin" / "openocd", SOCLABS)
    good = fake_tool(tmp_path / "xpack" / "bin" / "openocd", XPACK)
    world(settings=f'[tools]\nopenocd = "{bad}"\n')
    page = open_settings(page_factory, "tools")
    r = row(page, "tools.openocd")
    r.locator('[data-action="tool-detect"]').click()
    res = r.locator('[data-testid="tool-result"]')
    expect(res).to_have_attribute("data-ok", "false", timeout=T)
    expect(res).to_contain_text(f"{bad}: no remote_bitbang (it has: jlink, buspirate, hostio4)")
    expect(res.locator(".hint")).to_have_text(openocd_probe.fix_hint())
    expect(r.locator('[data-action="tool-use"]')).to_have_count(0)
    # only the probes ran: the version, then the adapter list (no config, no init)
    assert Path(f"{bad}.argv").read_text().splitlines() == ["--version",
                                                            "-c adapter list -c shutdown"]
    # twin: the xPack-like build, typed into the same row, is ok and says so
    r.locator('[data-testid="setting-input"]').fill(str(good))
    r.locator('[data-testid="setting-input"]').press("Enter")
    expect(source(r)).to_have_attribute("data-source", "user", timeout=T)
    r.locator('[data-action="tool-detect"]').click()
    res = r.locator('[data-testid="tool-result"]')
    expect(res).to_have_attribute("data-ok", "true", timeout=T)
    expect(res).to_contain_text(f"OpenOCD 0.12.0+dev-02228-ge5888bda3-dirty at {good}: "
                                "has remote_bitbang (3 adapters)")
    expect(res.locator(".hint")).to_have_count(0)


def test_negative_twin_on_the_path_detect_skips_the_build_without_it_and_offers_the_good_one(
        page_factory, world, tmp_path):  # noqa: F811 - the SET-UI fixture
    bad = fake_tool(tmp_path / "soclabs" / "bin" / "openocd", SOCLABS)
    good = fake_tool(tmp_path / "xpack" / "bin" / "openocd", XPACK)
    sctx = world(env={"PATH": f"{bad.parent}:{good.parent}:/usr/bin:/bin"})
    page = open_settings(page_factory, "tools")
    r = row(page, "tools.openocd")
    r.locator('[data-action="tool-detect"]').click()
    res = r.locator('[data-testid="tool-result"]')
    expect(res).to_have_attribute("data-ok", "true", timeout=T)
    expect(res).to_contain_text(f"at {good}: has remote_bitbang")        # the second on PATH
    r.locator('[data-action="tool-use"]').click()                         # as debug up would
    expect(source(r)).to_have_attribute("data-source", "user", timeout=T)
    expect(r.locator('[data-testid="setting-input"]')).to_have_value(str(good))
    assert (sctx.config_dir / "settings.toml").read_text().count(str(good)) == 1
    # twin: with only the SoC Labs-like build on PATH, Detect fails and offers nothing to use
    r.locator('[data-action="setting-reset"]').click()
    expect(source(r)).to_have_attribute("data-source", "default", timeout=T)
    sctx.env = {**sctx.env, "PATH": f"{bad.parent}:/usr/bin:/bin"}
    r.locator('[data-action="tool-detect"]').click()
    res = r.locator('[data-testid="tool-result"]')
    expect(res).to_have_attribute("data-ok", "false", timeout=T)
    expect(res).to_contain_text("no remote_bitbang (it has: jlink, buspirate, hostio4)")
    expect(res.locator(".hint")).to_contain_text("xPack OpenOCD 0.12")
    expect(r.locator('[data-action="tool-use"]')).to_have_count(0)
