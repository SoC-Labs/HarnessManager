"""UI2-POLISH item 1 (david): the showcase demo's Linux board builds end to end.

The demo's Linux board ``mps3-lx`` ran the placeholder static 0x4C1A0003, which the MPS3 pin
model (shells 0x72BB0A36 and 0x44EE76D5) does not describe, so Build > Design said "refused:
xdc:static_id ... no shell '0x4C1A0003'". It now runs the REAL Linux static 0x44EE76D5 (the
fielded rc2, its kit ``mps3/0x44EE76D5/vivado-2026.1`` cached by the demo), and Build walks
Setup -> Design -> Build -> Check -> Add on it.

Where the demo itself stops: Harness Manager never runs Vivado, so in ``app --demo`` the walk
waits at Build for a receipt that only a real Vivado writes. Here the receipt (and its
utilisation report) is the build fakes' (``kit_fakes.passed_build``, ``ui2_build_fakes``),
written into the build directory the page wrote, as a finished Vivado run would. Setup's
Vivado is ``kit_fakes.fake_vivado_script`` at 2026.1 (the demo uses the real discovery: a
machine with no Vivado gets Setup's warning, which never blocks; the twin shows it).

The REAL daemon over ``DemoEngine(showcase=True)``, clicks only. Each behaviour has its twin.
"""

from __future__ import annotations

import dataclasses
import json
import os
import urllib.request
from typing import Any

import pytest

from harness_manager import demo_catalog as cat
from harness_manager.demo_showcase import BOARD_LINUX
from tests.fakes import kit_fakes as kf
from tests.fakes import ui2_build_fakes as uf
from tests.web import nav
from tests.web.test_demo_all_browser import showcase  # noqa: F401 - the fixture

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 15_000
APP = {"width": 1440, "height": 900}
RC2, RC2_USERCODE = "0x44EE76D5", "0xFB1F8C76"
OLD_PLACEHOLDER = "0x4C1A0003"
MINIMAL_RM_ID = "0x0100F28A"            # the rm_id HM proposes for the built-in minimal (K8)


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def node(page: Any, step: str) -> Any:
    return by_id(page, f"bd-node-{step}")


def expect_node(page: Any, step: str, state: str) -> None:
    expect(node(page, step)).to_have_attribute("data-state", state, timeout=T)


def expect_panel(page: Any, step: str, state: str | None = None) -> Any:
    p = by_id(page, "bd-panel")
    expect(p).to_have_attribute("data-step", step, timeout=T)
    if state:
        expect(p).to_have_attribute("data-state", state, timeout=T)
    return p


def open_build(page: Any) -> None:
    nav.open_board(page, BOARD_LINUX)
    nav.tab(page, "build")
    page.wait_for_selector('[data-testid="bd-panel"]', timeout=T)


def picked(page: Any) -> str:
    return page.evaluate("(bid) => import('./js/store.js').then((m) => m.boardState(bid).selectedOverlay)",
                         BOARD_LINUX)


def vivado_2026(tmp_path: Any, monkeypatch: Any) -> None:
    exe = kf.fake_vivado_script(tmp_path / "vivado-2026.1", release="2026.1",
                                build=cat.VIVADO_2026_1_BUILD)
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(exe))
    # ... and first on PATH: a bare `vivado` is the kit's release too (this lab's PATH has 2024.1)
    monkeypatch.setenv("PATH", f"{exe.parent}{os.pathsep}{os.environ.get('PATH', '')}")


def test_the_demo_linux_board_runs_the_rc2_static_and_its_kit_is_cached(showcase):  # noqa: F811
    eng = showcase.engine
    ident = eng._board(BOARD_LINUX).identity
    assert (ident.shell_id, ident.usercode, ident.harness_impl) == (RC2.lower(), RC2_USERCODE.lower(), "linux")
    req = urllib.request.Request(f"{showcase.daemon.url}/api/v1/kits",
                                 headers={"Authorization": f"Bearer {showcase.daemon.token}"})
    with urllib.request.urlopen(req, timeout=10) as r:                   # noqa: S310 - 127.0.0.1
        kits = {k["static_id"]: k for k in json.loads(r.read())["kits"]}
    assert kits[RC2]["kit_id"] == "mps3/0x44EE76D5/vivado-2026.1"
    assert kits[RC2]["vivado"]["build"] == cat.VIVADO_2026_1_BUILD
    assert OLD_PLACEHOLDER not in kits


def test_build_walks_setup_design_build_check_add_on_the_demo_linux_board(
        showcase, tmp_path, monkeypatch):  # noqa: F811
    vivado_2026(tmp_path, monkeypatch)
    page = showcase.page(**APP)
    open_build(page)
    # Setup: the board's static, its kit and a Vivado of the kit's release: ready
    expect_node(page, "setup", "done")
    ready = by_id(page, "build-ready")
    expect(ready).to_have_attribute("data-level", "ok", timeout=T)
    expect(ready).to_contain_text(f"Ready to build for {RC2} with Vivado 2026.1")
    # Design: the Example minimal passes (the pin model describes 0x44EE76D5), with its pblock
    expect_panel(page, "design", "current")
    expect(page.locator('[data-testid="build-design"] .choice.on')).to_have_attribute("data-design", "minimal")
    expect(by_id(page, "design-refused")).to_have_count(0)
    expect(by_id(page, "wrapper-rm-id")).to_contain_text(f"rm_id {MINIMAL_RM_ID}")
    pb = by_id(page, "bd-pblock")
    expect(pb).to_contain_text("pblock_rp_dut")
    expect(pb.locator('[data-meter="LUT"]')).to_contain_text("42,824")
    by_id(page, "bd-continue").click()
    expect_node(page, "design", "done")
    # Build: the directory is written with the 2026.1 kit, and the page watches it
    expect_panel(page, "build", "current")
    bdir = tmp_path / "builds" / "minimal"
    by_id(page, "build-dir").fill(str(bdir))
    with page.expect_response(lambda r: r.url.endswith("/guide/script")) as resp:
        by_id(page, "script-write").click()
    assert resp.value.status == 200
    expect(by_id(page, "bd-way")).to_have_attribute("data-way", "batch", timeout=T)
    kit = json.loads((bdir / "kit" / "kit.json").read_text())
    assert (kit["static_id"], kit["vivado"]["release"]) == (RC2, "2026.1")
    expect(by_id(page, "bd-watch")).to_contain_text("no build_rm.log yet")
    # ... where the demo stops by itself: Vivado (never run by HM) writes the receipt. Here a
    # finished run's receipt and utilisation report land in that directory.
    kf.passed_build(bdir, name="minimal", rm_id=MINIMAL_RM_ID, static_id=RC2, usercode=RC2_USERCODE,
                    kit_id="mps3/0x44EE76D5/vivado-2026.1", vivado="2026.1")
    uf.util_report(bdir / "out", "minimal", lut_used=9000)
    # Check: passed, with the utilisation against pblock_rp_dut
    expect_node(page, "build", "done")
    expect_node(page, "check", "done")
    nav_check = node(page, "check").locator("button")
    nav_check.click()
    expect_panel(page, "check")
    expect(by_id(page, "check-passed")).to_contain_text(f"minimal {MINIMAL_RM_ID}")
    expect(page.locator('[data-testid="trouble"][data-failing="true"]')).to_have_count(0)
    util = by_id(page, "bd-util")
    expect(util.locator('[data-meter="LUT"]')).to_contain_text("9,000 / 42,824")
    expect(util).to_contain_text("pblock_rp_dut")
    # Add: packed, and the page lands on the Workbench with minimal picked
    node(page, "add").locator("button").click()
    expect_panel(page, "add", "current")
    expect(by_id(page, "add-lease")).to_contain_text("No lease needed")
    page.locator('[data-action="kit_pack"]').click()
    page.wait_for_selector('[data-testid="section-workbench"]', timeout=T)
    assert (bdir / "overlay" / "minimal" / "manifest.json").is_file()
    manifest = json.loads((bdir / "overlay" / "minimal" / "manifest.json").read_text())
    assert str(manifest.get("static_id", "")).lower() == RC2.lower(), manifest
    assert "/workbench" in page.evaluate("() => window.__harness_managerState().route")
    assert picked(page) == "minimal"
    assert page.errors == []


def test_twin_with_no_vivado_setup_warns_and_design_still_passes(showcase):  # noqa: F811
    page = showcase.page(**APP)                       # tests/conftest.py: discovery is off
    open_build(page)
    expect_node(page, "setup", "warn")
    expect(by_id(page, "build-ready")).to_have_attribute("data-level", "warn", timeout=T)
    expect(by_id(page, "build-ready")).to_contain_text("No Vivado on this machine")
    expect_node(page, "design", "current")
    expect(by_id(page, "design-refused")).to_have_count(0)
    expect(by_id(page, "bd-continue")).to_be_enabled()
    assert page.errors == []


def test_twin_the_old_placeholder_static_is_still_refused_at_design(showcase, tmp_path):  # noqa: F811
    """What the demo did before the fix: a static the pin model lacks refuses the design."""
    eng = showcase.engine
    b = eng._board(BOARD_LINUX)
    b.identity = dataclasses.replace(b.identity, shell_id=OLD_PLACEHOLDER.lower(), usercode="0x3c0ffee3")
    b.candidate = dataclasses.replace(b.candidate, identity=b.identity)
    kit = cat.build_kit(tmp_path / "old-kit", OLD_PLACEHOLDER, usercode="0x3C0FFEE3",
                        release="2026.1", impl="linux", frames=None)
    req = urllib.request.Request(f"{showcase.daemon.url}/api/v1/kits/import", method="POST",
                                 data=json.dumps({"path": str(kit)}).encode(),
                                 headers={"Authorization": f"Bearer {showcase.daemon.token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:                   # noqa: S310 - 127.0.0.1
        assert json.loads(r.read())["ok"]
    page = showcase.page(**APP)
    open_build(page)
    expect_node(page, "design", "failed")
    refused = by_id(page, "design-refused")
    expect(refused).to_have_attribute("data-gate", "xdc:static_id")
    expect(refused).to_contain_text(f"{OLD_PLACEHOLDER}: the mps3 pin model has no shell")
    expect(by_id(page, "bd-continue")).to_be_disabled()
    assert page.errors == []


# --- UI2-POLISH item 4: the Debug USB route in one vocabulary (header, Overview, Board) ----------------

ROUTE_WORDS = {"hub": "to the hub", "pc": "to this PC", "self": "looped back into itself",
               "none": "none (Ethernet only)", "unknown": "not known"}


def _route_words(page: Any, bid: str) -> dict[str, str]:
    nav.open_board(page, bid)
    nav.tab(page, "overview")
    chip = by_id(page, "ov-usb")
    expect(chip).not_to_have_attribute("data-usb", "", timeout=T)
    out = {"to": chip.get_attribute("data-usb"), "overview": chip.inner_text().strip()}
    head = by_id(page, "fact-usb")
    expect(head).to_have_attribute("data-usb", out["to"], timeout=T)
    out["header"] = head.locator(".fact-value").inner_text().strip()
    nav.tab(page, "board")
    nav.board_page(page, "connections")
    out["board"] = by_id(page, "board-status-connections").inner_text().strip()
    out["connections"] = by_id(page, "cx-usb").locator(".cx-line .chip").inner_text().strip()
    return out


@pytest.mark.parametrize(("bid", "to"), [("mps3@192.168.10.106:6900", "hub"), (BOARD_LINUX, "none"),
                                         ("mps3@192.168.10.105:6900", "pc")])
def test_the_debug_usb_route_reads_the_same_in_the_header_overview_and_board(showcase, bid, to):  # noqa: F811
    page = showcase.page(**APP)
    got = _route_words(page, bid)
    words = ROUTE_WORDS[to]
    assert got["to"] == to, got
    assert got["overview"] == words and got["header"] == words, got
    assert got["board"] == f"Debug USB: {words}", got
    assert got["connections"] == words, got
    assert page.errors == []


def test_twin_the_old_words_are_gone_everywhere(showcase):  # noqa: F811
    page = showcase.page(**APP)
    got = _route_words(page, BOARD_LINUX)
    text = " | ".join(v for k, v in got.items() if k != "to")      # the route words, not the reasons
    for old in ("not plugged in", "no Debug USB", "Not connected", "not known yet"):
        assert old not in text, (old, text)
    rail = nav.rail(page, BOARD_LINUX).locator('[data-testid="rail-usb"]')
    expect(rail).to_have_text("no USB")                                   # the short tag stays
    assert (rail.get_attribute("title") or "").startswith("Debug USB: none (Ethernet only): ")
