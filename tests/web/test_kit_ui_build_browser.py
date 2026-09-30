"""UI v2 Build tab (lane UI2-BUILD; KIT-UI before it, david K9) in a headless system Chrome,
driven by clicks only, over the real harness-manager-daemon (``kit_api`` in ``EXTENSIONS``) and
over the T14 mock (``tests/fakes/kit_mock.py`` runs the same routes).

The five-step bar (Setup · Design · Build · Check · Add) over one step panel, on the guide
(GET /boards/{bid}/guide) and the page's own record of what the user chose. The demo boards
run the previous static 0x3F1A560F; ``fielded(engine)`` moves the USB board to 0x72BB0A36, the
fixture kit's static (``tests/fakes/kit_fixture``). Vivado is never run:
``HARNESS_MANAGER_VIVADO`` is ``off`` (tests/conftest.py), or a fake script that prints a
version and answers the guide's launch (``kit_fakes.fake_vivado_script``, KIT-LIC). A running
build is ``ui2_build_fakes.running_log``, the pblock's utilisation ``util_report``, My RTL's
folder ``rtl``. Every behaviour has its negative twin.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
import urllib.request
import zipfile
from typing import Any
from urllib.parse import urlencode

import pytest

from harness_manager.demo import BOARD_USB
from tests.fakes import kit_fakes as kf
from tests.fakes import ui2_build_fakes as uf
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000
SID = "0x72BB0A36"
OLD = "0x3F1A560F"
OTHER = "0x44EE76D5"


# --- helpers ---------------------------------------------------------------------------------------


def fielded(engine, shell: str = "0x72bb0a36") -> None:
    """Move the demo USB board onto another static (the demo scripts 0x3f1a560f)."""
    b = engine._board(BOARD_USB)
    b.identity = dataclasses.replace(b.identity, shell_id=shell)
    b.candidate = dataclasses.replace(b.candidate, identity=b.identity)


def api(daemon, method: str, path: str, body: dict | None = None, query: dict | None = None):
    url = f"{daemon.url}/api/v1{path}" + (f"?{urlencode(query)}" if query else "")
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": f"Bearer {daemon.token}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=10) as r:        # noqa: S310 - 127.0.0.1 only
        return json.loads(r.read())


def import_kit(daemon, path=kf.FIXTURE) -> None:
    assert api(daemon, "POST", "/kits/import", {"path": str(path)})["ok"]


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def open_build(page: Any, bid: str = BOARD_USB) -> None:
    nav.open_board(page, bid)
    nav.tab(page, "build")
    page.wait_for_selector('[data-testid="bd-panel"]', timeout=T)


def node(page: Any, step: str) -> Any:
    return by_id(page, f"bd-node-{step}")


def expect_node(page: Any, step: str, state: str) -> None:
    expect(node(page, step)).to_have_attribute("data-state", state, timeout=T)


def nodes(page: Any) -> dict[str, str]:
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll(
      '[data-testid="bd-stepper"] .bd-node')].map((el) => [el.dataset.step, el.dataset.state]))""")


def expect_panel(page: Any, step: str, state: str | None = None) -> Any:
    p = by_id(page, "bd-panel")
    expect(p).to_have_attribute("data-step", step, timeout=T)
    if state:
        expect(p).to_have_attribute("data-state", state, timeout=T)
    return p


def look(page: Any, step: str) -> Any:
    node(page, step).locator("button").click()
    return expect_panel(page, step)


def choose_design(page: Any) -> None:
    by_id(page, "bd-continue").click()
    expect_node(page, "design", "done")
    expect_panel(page, "build")


def watch(page: Any, build_dir: Any) -> None:
    """The build directory is written already (by the CLI, or a test): watch it."""
    by_id(page, "build-dir").fill(str(build_dir))
    by_id(page, "build-watch").click()


def picked(page: Any, bid: str = BOARD_USB) -> str:
    return page.evaluate("(bid) => import('./js/store.js').then((m) => m.boardState(bid).selectedOverlay)", bid)


def no_overflow(page: Any) -> list[str]:
    return page.evaluate("""() => {
      const w = document.documentElement.clientWidth;
      return [...document.querySelectorAll('.card')].filter(
        (el) => el.getBoundingClientRect().right > w + 1).map((el) => el.dataset.testid || '?');
    }""")


# --- 1. the bar follows the guide ---------------------------------------------------------------


@pytest.mark.mock_too
def test_the_bar_follows_the_guide_and_lands_on_design(page_factory, daemon, engine):
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light", width=1440, height=900)
    open_build(page)
    guide = api(daemon, "GET", f"/boards/{BOARD_USB}/guide".replace("@", "%40"), query={"design": "minimal"})
    want = {s["id"]: s["state"] for s in guide["steps"]}
    assert want["target"] == "done" and want["kit"] == "done" and want["tools"] == "next"
    # target + kit done, no Vivado here: Setup warns (never blocks), Design is the step now
    expect_node(page, "design", "current")
    assert nodes(page) == {"setup": "warn", "design": "current", "build": "blocked",
                           "check": "blocked", "add": "blocked"}
    expect_panel(page, "design", "current")
    expect(by_id(page, "build-next")).to_contain_text("Step 2 of 5")
    ready = by_id(page, "build-ready")
    expect(ready).to_have_attribute("data-level", "warn")
    expect(ready).to_contain_text("No Vivado on this machine")
    # the built-in the catalogue proposes, and the rm_id HM proposes for it (K8)
    expect(page.locator('[data-testid="build-design"] .choice.on')).to_have_attribute("data-design", "minimal")
    expect(by_id(page, "wrapper-rm-id")).to_contain_text("rm_id 0x0100F28A")
    expect(by_id(page, "rm-id-proposed")).to_be_visible()
    # G8: the partition's pblock facts and its capacity, from the pin model
    pb = by_id(page, "bd-pblock")
    expect(pb).to_contain_text("pblock_rp_dut")
    expect(pb).to_contain_text("SLICE_X48Y0:SLICE_X95Y119")
    expect(pb.locator('[data-meter="LUT"]')).to_contain_text("42,824")
    expect(pb.locator('[data-meter="DSP"]')).to_contain_text("not in the pin model")   # null: shown so
    # a board with no hub: no lease note (building never needs one)
    expect(by_id(page, "build-lease-note")).to_have_count(0)
    tabs = page.evaluate("() => [...document.querySelectorAll('.section-tab')].map((t) => t.dataset.section)")
    assert tabs == ["overview", "workbench", "build", "board"], tabs
    assert no_overflow(page) == []
    assert page.errors == []


@pytest.mark.mock_too
def test_twin_a_board_on_a_static_the_pack_does_not_know_fails_setup_and_blocks_the_rest(page_factory, daemon):
    page = page_factory("light")                     # the demo board runs 0x3F1A560F: no kit
    open_build(page)
    expect_node(page, "setup", "failed")
    assert nodes(page) == {"setup": "failed", "design": "blocked", "build": "blocked",
                           "check": "blocked", "add": "blocked"}
    expect_panel(page, "setup", "failed")
    expect(by_id(page, "build-ready")).to_have_attribute("data-level", "err")
    expect(by_id(page, "step-target")).to_have_attribute("data-state", "failed")
    expect(by_id(page, "step-target-detail")).to_contain_text(f"knows nothing of static {OLD}")
    expect(by_id(page, "step-kit-reason")).to_contain_text("waits for 1 target")
    # Design waits: its panel says so and offers nothing to continue with
    look(page, "design")
    expect(by_id(page, "bd-continue")).to_have_count(0)
    assert page.errors == []


# --- 2. Setup: fetch the kit, its progress then done ----------------------------------------------------


@pytest.mark.mock_too
def test_fetch_shows_its_progress_then_done_and_a_kit_for_another_static_is_refused(
        page_factory, daemon, engine, tmp_path):
    fielded(engine)
    page = page_factory("light")
    open_build(page)
    expect_node(page, "setup", "current")                 # no kit yet: Setup is the step now
    expect(by_id(page, "build-ready")).to_contain_text("is not here yet")
    expect(by_id(page, "step-kit")).to_have_attribute("data-state", "next")
    by_id(page, "step-kit").locator('button:has-text("A folder or zip")').click()
    # the twin first: a folder that holds another static's kit is refused, and nothing changes
    other = kf.build_fixture(tmp_path / "other", OLD)
    by_id(page, "kit-source-path").fill(str(other))
    page.locator('[data-action="kit_fetch"]').click()
    result = by_id(page, "kit-fetch-result")
    expect(result).to_contain_text("REFUSED", timeout=T)
    expect(result).to_contain_text(f"holds the kit for {OLD}, not {SID}")
    expect(result).to_contain_text("fetch: started")
    expect_node(page, "setup", "current")
    # the good folder: the progress line, then done; Setup warns only for Vivado now
    by_id(page, "kit-source-path").fill(str(kf.FIXTURE))
    page.locator('[data-action="kit_fetch"]').click()
    expect(result).to_contain_text("mps3/0x72BB0A36/vivado-2024.1 is cached (from path)", timeout=T)
    text = result.inner_text()
    assert text.index("fetch: started") < text.index("is cached")
    expect_node(page, "setup", "warn")
    expect_node(page, "design", "current")
    look(page, "setup")
    expect(by_id(page, "step-kit")).to_have_attribute("data-state", "done")
    expect(by_id(page, "kit-access")).to_have_text("public")
    expect(by_id(page, "kit-licence")).to_contain_text("no Arm IP")
    by_id(page, "kit-verify").click()
    expect(by_id(page, "kit-verified")).to_contain_text("Verified", timeout=T)
    with page.expect_download(timeout=T) as dl:
        by_id(page, "kit-zip").click()
    assert dl.value.suggested_filename == f"mps3-kit-{SID}.zip"
    with zipfile.ZipFile(dl.value.path()) as zf:
        assert f"{SID}/static/static_routed_locked.dcp" in zf.namelist()
    assert page.errors == []


# --- 3. Setup: the Vivado release (K4) and a Vivado that does not start (KIT-LIC) -------------------------


@pytest.mark.mock_too
def test_a_vivado_of_another_release_warns_and_its_twin_the_kits_release_makes_setup_done(
        page_factory, daemon, engine, tmp_path, monkeypatch):
    fielded(engine)
    import_kit(daemon)
    wrong = kf.fake_vivado_script(tmp_path / "v2023", release="2023.2", build=4029153)
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(wrong))
    page = page_factory("light")
    open_build(page)
    expect_node(page, "setup", "warn")
    expect(by_id(page, "build-ready")).to_contain_text("Vivado 2023.2 is not this kit's 2024.1")
    by_id(page, "build-ready-details").click()
    expect_panel(page, "setup")
    expect(by_id(page, "vivado-found")).to_contain_text("Vivado 2023.2")
    expect(by_id(page, "vivado-found")).to_contain_text("$HARNESS_MANAGER_VIVADO")
    expect(by_id(page, "vivado-chip")).to_have_text("different release")
    expect(by_id(page, "vivado-needed")).to_contain_text("Vivado 2024.1")
    expect(by_id(page, "vivado-mismatch")).to_contain_text("build_rm.tcl refuses to start")
    card = page.locator('[data-testid="trouble"][data-card="vivado_version"]')
    expect(card).to_have_attribute("data-failing", "true")
    expect(card.locator('[data-testid="trouble-fix"]')).to_contain_text("Runs 36-378")
    # the twin: the kit's own release: Setup is done and the Ready line says so
    right = kf.fake_vivado_script(tmp_path / "v2024", release="2024.1")
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(right))
    by_id(page, "build-refresh").click()
    expect_node(page, "setup", "done")
    expect(by_id(page, "build-ready")).to_have_attribute("data-level", "ok")
    expect(by_id(page, "build-ready")).to_contain_text(f"Ready to build for {SID} with Vivado 2024.1")
    expect(by_id(page, "vivado-chip")).to_have_text("matches")
    expect(card).to_have_attribute("data-failing", "false")
    assert page.errors == []


def test_a_vivado_that_does_not_start_fails_setup_and_its_twin_starts(
        page_factory, daemon, engine, tmp_path, monkeypatch):
    from harness_manager.services.kit import launch

    fielded(engine)
    import_kit(daemon)
    for k in launch.LICENCE_ENV:
        monkeypatch.delenv(k, raising=False)
    bad = kf.fake_vivado_script(tmp_path / "nolic", release="2024.1", launch="no-licence")
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(bad))
    page = page_factory("light")
    open_build(page)
    expect_node(page, "setup", "failed")
    expect_node(page, "design", "blocked")
    expect(by_id(page, "build-ready")).to_contain_text("does not start")
    expect(by_id(page, "vivado-launch-chip")).to_have_text("does not start")
    expect(by_id(page, "vivado-launch")).to_contain_text(
        "no licence file (set XILINXD_LICENSE_FILE or LM_LICENSE_FILE)")
    expect(by_id(page, "step-tools-reason")).to_contain_text("export XILINXD_LICENSE_FILE=PORT@SERVER")
    expect(by_id(page, "licence")).to_contain_text("Vivado 2024.1 Enterprise (2026.1: Core or higher)")
    right = kf.fake_vivado_script(tmp_path / "v2024", release="2024.1")
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(right))
    by_id(page, "build-refresh").click()
    expect_node(page, "setup", "done")
    expect_node(page, "design", "current")
    look(page, "setup")
    expect(by_id(page, "vivado-launch-chip")).to_have_text("starts")
    expect(by_id(page, "licence")).to_contain_text("[Common 17-345]")
    assert page.errors == []


# --- 4. Build: write the directory, run it your way -----------------------------------------------------


@pytest.mark.mock_too
def test_writing_the_build_directory_gives_the_three_ways_to_run_it(page_factory, daemon, engine, tmp_path):
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light")
    open_build(page)
    choose_design(page)
    expect_panel(page, "build", "current")
    # the twin first: a relative folder is not a path on the daemon's host: nothing is written
    by_id(page, "build-dir").fill("builds/minimal")
    expect(by_id(page, "build-dir-hint")).to_contain_text("absolute path")
    expect(by_id(page, "script-write")).to_be_disabled()
    bdir = tmp_path / "build" / "minimal"
    by_id(page, "build-dir").fill(str(bdir))
    with page.expect_response(lambda r: r.url.endswith("/guide/script")) as resp:
        by_id(page, "script-write").click()
    assert resp.value.status == 200
    way = by_id(page, "bd-way")
    expect(way).to_have_attribute("data-way", "batch", timeout=T)
    assert (bdir / "build_rm.tcl").is_file() and (bdir / "kit" / "kit.json").is_file()
    cmd = by_id(page, "script-command").locator("code")
    expect(cmd).to_have_text(f"vivado -mode batch -source {bdir}/build_rm.tcl -log {bdir}/build_rm.log "
                             f"-journal {bdir}/build_rm.jou")
    expect(by_id(page, "bd-watch")).to_contain_text("no build_rm.log yet")
    expect(by_id(page, "build-next")).to_contain_text("run the Vivado command")
    way.locator('button:has-text("Vivado GUI")').click()
    expect(cmd).to_contain_text("vivado -mode gui -source")
    way.locator('button:has-text("Your open Vivado")').click()
    expect(cmd).to_have_text(f"cd {bdir}; set argv {{}}; set argc 0; source build_rm.tcl")
    expect(way).to_contain_text("sees only the verdict")
    # Stop after link: the directory is written again, and every way carries STOP_AFTER=link
    by_id(page, "bd-stop-after").check()
    expect(cmd).to_contain_text("STOP_AFTER=link", timeout=T)
    assert "STOP_AFTER" in (bdir / "build_rm.tcl").read_text()
    expect(by_id(page, "build-next")).to_contain_text("run Vivado to link, to floorplan")
    assert page.errors == []


@pytest.mark.mock_too
def test_download_as_a_zip_needs_no_directory_and_its_twin_with_no_kit_is_refused(
        page_factory, daemon, engine):
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light")
    open_build(page)
    choose_design(page)
    with page.expect_download(timeout=T) as dl:
        by_id(page, "script-zip").click()
    assert dl.value.suggested_filename == "minimal_build.zip"
    with zipfile.ZipFile(dl.value.path()) as zf:
        names = zf.namelist()
    assert "minimal/build_rm.tcl" in names and "minimal/kit/static/static_routed_locked.dcp" in names
    expect(by_id(page, "script-zip-saved")).to_contain_text("minimal_build.zip")
    assert page.errors == []


# --- 5. watching a build: the stage and the time ------------------------------------------------------


@pytest.mark.mock_too
def test_a_running_build_shows_its_stage_and_time_and_a_dead_one_does_not(page_factory, daemon, engine, tmp_path):
    fielded(engine)
    import_kit(daemon)
    bdir = tmp_path / "run"
    uf.running_log(bdir, "impl", stage_at=time.time() - 130)
    page = page_factory("light")
    open_build(page)
    choose_design(page)
    watch(page, bdir)
    expect_node(page, "build", "running")
    run = by_id(page, "bd-running")
    expect(run).to_have_attribute("data-stage", "impl")
    stages = by_id(page, "bd-run-stages")
    expect(stages.locator('[data-stage="impl"]')).to_have_attribute("data-stage-state", "running")
    expect(stages.locator('[data-stage="link"]')).to_have_attribute("data-stage-state", "done")
    expect(stages.locator('[data-stage="verify"]')).to_have_attribute("data-stage-state", "pending")
    expect(run).to_contain_text("HM_STAGE impl")
    expect(by_id(page, "bd-clock")).to_contain_text(" h ")        # the fixture's session began long ago
    expect(by_id(page, "build-next")).to_contain_text("Vivado running · impl")
    expect(by_id(page, "bd-panel")).to_contain_text("Don't start a second Vivado")
    # the twin: a log not written for over 30 min is a run that died: nothing is running
    dead = tmp_path / "dead"
    uf.running_log(dead, "synth", mtime=time.time() - 3 * 3600)
    by_id(page, "build-change-dir").click()
    watch(page, dead)
    expect_node(page, "build", "current")
    expect(by_id(page, "bd-watch")).to_contain_text("that run died")
    expect(by_id(page, "bd-running")).to_have_count(0)
    assert page.errors == []


# --- 6. Check: a failed build names its gate; a passed one shows the pblock's use ---------------------------


@pytest.mark.mock_too
def test_a_failed_build_names_its_one_gate_and_its_fix_and_a_passed_one_opens_none(
        page_factory, daemon, engine, tmp_path):
    fielded(engine)
    import_kit(daemon)
    bdir = tmp_path / "failed"
    kf.passed_build(bdir, state="failed", stage="impl", gates=[
        {"gate": "static_id", "verdict": "PASS", "detail": f"CRC-32 is {SID}"},
        {"gate": "rm_timing", "verdict": "FAIL", "detail": "setup WNS -0.412 ns"}])
    page = page_factory("light")
    open_build(page)
    choose_design(page)
    watch(page, bdir)
    expect_node(page, "build", "done")
    expect_node(page, "check", "failed")
    expect_node(page, "add", "blocked")
    refused = by_id(page, "check-refused")
    expect(refused).to_have_attribute("data-gate", "rm_timing")
    expect(refused).to_contain_text("setup WNS -0.412 ns")
    expect(by_id(page, "check-fix")).to_contain_text("RM_XDC")
    groups = by_id(page, "check-groups")
    expect(groups.locator('[data-group="identity"]')).to_have_attribute("data-state", "ok")
    expect(groups.locator('[data-group="timing"]')).to_have_attribute("data-state", "err")
    expect(groups.locator('[data-group="pr_verify"]')).to_have_attribute("data-state", "skip")
    expect(groups.locator('[data-group="files"]')).to_contain_text("did not finish")
    st = by_id(page, "check-stages")
    expect(st.locator('[data-stage="impl"]')).to_have_attribute("data-stage-state", "failed")
    expect(st.locator('[data-stage="link"]')).to_have_attribute("data-stage-state", "done")
    card = page.locator('[data-testid="trouble"][data-card="rm_timing"]')
    expect(card).to_have_attribute("data-failing", "true")
    # the gate a design change fixes: its button goes to Design
    by_id(page, "check-fix-design").click()
    expect_panel(page, "design")
    expect(by_id(page, "bd-viewing")).to_contain_text("the current step is Check")
    by_id(page, "bd-back").click()
    expect_panel(page, "check")
    # the twin: a passed build in another directory: no gate fails; the pblock's use shows
    good = tmp_path / "good"
    kf.passed_build(good)
    uf.util_report(good / "out", "spike_rm", lut_used=36000)
    look(page, "build")
    by_id(page, "build-change-dir").click()
    watch(page, good)
    expect_node(page, "check", "done")
    look(page, "check")
    expect(by_id(page, "check-passed")).to_contain_text("spike_rm 0x010080F0")
    expect(page.locator('[data-testid="trouble"][data-failing="true"]')).to_have_count(0)
    util = by_id(page, "bd-util")
    expect(util.locator('[data-meter="LUT"]')).to_contain_text("36,000 / 42,824")
    expect(util).to_have_attribute("data-worst", "warn")            # 84 % of the pblock's LUTs
    expect(util).to_contain_text("Fullest: LUT")
    assert page.errors == []


@pytest.mark.mock_too
def test_a_build_for_another_shell_is_refused_exit_14_and_its_twin_passes(page_factory, daemon, engine, tmp_path):
    fielded(engine)
    import_kit(daemon)
    other = tmp_path / "other"
    kf.passed_build(other, static_id=OTHER)
    page = page_factory("light")
    open_build(page)
    choose_design(page)
    watch(page, other)
    expect_node(page, "check", "failed")
    refused = by_id(page, "check-refused")
    expect(refused).to_have_attribute("data-gate", "board_static")
    expect(refused).to_contain_text("exit 14 INCOMPATIBLE")
    expect(by_id(page, "check-groups").locator('[data-group="identity"]')).to_have_attribute("data-state", "err")
    by_id(page, "check-fix-rewrite").click()
    expect_panel(page, "build")
    expect(by_id(page, "script-write")).to_be_visible()
    # the twin: this board's static
    good = tmp_path / "good"
    kf.passed_build(good)
    watch(page, good)
    expect_node(page, "check", "done")
    expect_node(page, "add", "current")
    assert page.errors == []


# --- 7. Add: to the Workbench, landing there with it picked ---------------------------------------------


@pytest.mark.mock_too
def test_add_packs_it_and_lands_on_the_workbench_with_it_picked(page_factory, daemon, engine, tmp_path):
    fielded(engine)
    import_kit(daemon)
    bdir = tmp_path / "b"
    kf.passed_build(bdir)
    page = page_factory("light")
    open_build(page)
    choose_design(page)
    watch(page, bdir)
    expect_panel(page, "add", "current")
    expect(by_id(page, "add-lease")).to_contain_text("No lease needed")
    page.locator('[data-action="kit_pack"]').click()
    page.wait_for_selector('[data-testid="section-workbench"]', timeout=T)
    assert (bdir / "overlay" / "spike_rm" / "manifest.json").is_file()
    assert "/workbench" in page.evaluate("() => window.__harness_managerState().route")
    # picked on the Workbench (store.js boardState(bid).selectedOverlay; the demo engine's own
    # overlay list does not read the store, so its preflight cannot run here)
    assert picked(page) == "spike_rm"
    nav.tab(page, "build")
    expect_node(page, "add", "done")
    expect(by_id(page, "build-next")).to_contain_text("spike_rm is on the Workbench")
    expect(by_id(page, "pack-done")).to_contain_text("spike_rm")
    assert page.errors == []


@pytest.mark.mock_too
def test_twin_a_check_that_fails_keeps_add_waiting_and_packs_nothing(page_factory, daemon, engine, tmp_path):
    fielded(engine)
    import_kit(daemon)
    bdir = tmp_path / "b"
    kf.passed_build(bdir)
    partial = bdir / "out" / "spike_rm_partial.bin"
    partial.write_bytes(partial.read_bytes()[:-8])           # half-copied after the build
    page = page_factory("light")
    open_build(page)
    choose_design(page)
    watch(page, bdir)
    expect_node(page, "check", "failed")
    expect_node(page, "add", "blocked")
    expect(by_id(page, "check-refused")).to_contain_text("half-copied")
    expect(by_id(page, "check-fix")).to_contain_text("copy out/ from the build again")
    look(page, "add")
    expect(page.locator('[data-action="kit_pack"]')).to_have_count(0)
    assert not (bdir / "overlay").exists()
    assert page.errors == []


# --- 8. Design: My RTL, Paste, and a refused design --------------------------------------------------------


@pytest.mark.mock_too
def test_my_rtl_reads_the_folder_in_compile_order_and_continues(page_factory, daemon, engine, tmp_path):
    fielded(engine)
    import_kit(daemon)
    rtl = uf.rtl(tmp_path / "rtl")
    page = page_factory("light")
    open_build(page)
    page.locator('[data-testid="bd-design-source"] [data-src="rtl"]').click()
    expect(by_id(page, "design-todo")).to_contain_text("Give the folder")
    expect(by_id(page, "bd-continue")).to_be_disabled()
    # the twin first: nothing there
    by_id(page, "bd-rtl-path").fill(str(tmp_path / "nope"))
    by_id(page, "bd-rtl-read").click()
    expect(by_id(page, "design-refused")).to_contain_text("no such file or folder", timeout=T)
    expect_node(page, "design", "failed")
    expect(by_id(page, "bd-continue")).to_be_disabled()
    # the folder: its sources in compile order (packages first), its top, its image generic
    by_id(page, "bd-rtl-path").fill(str(rtl))
    by_id(page, "bd-rtl-read").click()
    scan = by_id(page, "bd-scan")
    expect(scan).to_contain_text("4 HDL files in compile order (1 package first)", timeout=T)
    expect(scan).to_contain_text("top rm_demo")
    expect(by_id(page, "bd-generics").locator('input[aria-label="Generic name"]')).to_have_value("IMG")
    expect(by_id(page, "wrapper-rm-id")).to_contain_text("rm_id 0x0100DFA0")
    expect_node(page, "design", "current")
    by_id(page, "bd-rm-xdc").fill(str(rtl / "demo_rm.xdc"))
    expect(by_id(page, "bd-constraints").locator(".bd-ff.on")).to_have_count(1)
    by_id(page, "bd-rtl-save").click()
    expect(by_id(page, "bd-rtl-saved")).to_contain_text("demo.json", timeout=T)
    assert json.loads(next(rtl.rglob("demo.json")).read_text())["build"]["top"] == "rm_demo"
    choose_design(page)
    expect(by_id(page, "build-next")).to_contain_text("write the build directory")
    assert page.errors == []


@pytest.mark.mock_too
def test_k8_a_pasted_design_id_another_design_holds_warns_and_its_twin_is_proposed(page_factory, daemon, engine):
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light")
    open_build(page)
    page.locator('[data-testid="bd-design-source"] [data-src="paste"]').click()
    expect(by_id(page, "design-todo")).to_contain_text("Paste your design")
    by_id(page, "build-design-json").fill("{not json")
    expect(by_id(page, "design-refused")).to_contain_text("the pasted design is not JSON")
    expect_node(page, "design", "failed")
    by_id(page, "build-design-json").fill(json.dumps(
        {"kind": "rm", "name": "my_rm", "rm_id": "0x01000001", "use": {"clkrst": {}}}))
    expect_node(page, "design", "current")
    by_id(page, "bd-continue").click()
    expect_node(page, "design", "done")
    look(page, "design")
    expect(by_id(page, "rm-id-clash")).to_contain_text("already 'nanosoc'", timeout=T)
    expect(by_id(page, "wrapper-rm-id")).to_contain_text("from the design")
    # the twin: no rm_id, so HM proposes one no design holds
    by_id(page, "build-design-json").fill(json.dumps({"kind": "rm", "name": "my_rm", "use": {"clkrst": {}}}))
    expect_node(page, "design", "current")                   # a change starts the build again
    by_id(page, "bd-continue").click()
    expect_node(page, "design", "done")
    look(page, "design")
    expect(by_id(page, "rm-id-proposed")).to_be_visible(timeout=T)
    expect(by_id(page, "rm-id-clash")).to_have_count(0)
    expect(by_id(page, "wrapper-rm-id")).to_contain_text("is unused")
    assert page.errors == []


@pytest.mark.mock_too
def test_a_static_the_pin_model_lacks_refuses_the_design_and_its_twin_does_not(page_factory, daemon, engine, tmp_path):
    # the demo board runs 0x3F1A560F; its kit is cached, but HM's pin model is 0x72BB0A36's
    import_kit(daemon, kf.build_fixture(tmp_path / "old", OLD))
    page = page_factory("light")
    open_build(page)
    expect_node(page, "design", "failed")
    refused = by_id(page, "design-refused")
    expect(refused).to_have_attribute("data-gate", "xdc:static_id")
    expect(refused).to_contain_text(f"{OLD}: the mps3 pin model has no shell")
    expect(refused).to_contain_text("pin model describes another static")
    expect(by_id(page, "bd-continue")).to_be_disabled()
    card = page.locator('[data-testid="trouble"][data-card="xdc:static_id"]')
    expect(card).to_have_attribute("data-failing", "true")
    assert page.errors == []


@pytest.mark.mock_too
def test_twin_the_boards_own_static_is_not_refused(page_factory, daemon, engine):
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light")
    open_build(page)
    expect_node(page, "design", "current")
    expect(by_id(page, "design-refused")).to_have_count(0)
    expect(page.locator('[data-testid="trouble"][data-failing="true"]')).to_have_count(0)
    expect(by_id(page, "bd-continue")).to_be_enabled()
    assert page.errors == []


# --- 9. the route: a step is a link --------------------------------------------------------------


def test_a_step_is_a_link_and_the_current_one_is_the_tab_itself(page_factory, daemon, engine):
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light")
    open_build(page)
    expect_panel(page, "design")
    look(page, "setup")
    assert page.evaluate("location.hash").endswith("/build/setup")
    expect(by_id(page, "bd-viewing")).to_contain_text("You are looking at Setup")
    page.reload()                                     # the link lands on that step
    page.wait_for_selector('[data-testid="bd-panel"]', timeout=T)
    expect_panel(page, "setup")
    # the twin: back to the current step drops the step from the address
    by_id(page, "bd-back").click()
    expect_panel(page, "design")
    assert page.evaluate("location.hash").endswith("/build")
    # an old 0.1.0 link (xdc) lands on Build with the exports fold open
    nav.section(page, "xdc")
    page.wait_for_selector('[data-testid="xdc-model"]', timeout=T)
    assert page.errors == []


# --- 10. the lease: building needs none ----------------------------------------------------------------


HUB = pytest.mark.week_plan("hub_api", sim=True)


def hub_build_page(page_factory: Any, daemon: Any, engine: Any, lease: str) -> Any:
    fielded(engine)
    import_kit(daemon)
    daemon.app.state.sim.behind_hub(BOARD_USB, lease=lease)
    page = page_factory("light", width=1440, height=900)
    nav.open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    nav.tab(page, "build")
    page.wait_for_selector('[data-testid="bd-panel"]', timeout=T)
    return page


@HUB
def test_on_a_board_someone_else_leases_building_and_adding_still_run(page_factory, daemon, engine, tmp_path):
    page = hub_build_page(page_factory, daemon, engine, "other")
    note = by_id(page, "build-lease-note")
    expect(note).to_have_attribute("data-lease", "other", timeout=T)
    expect(note).to_contain_text("Building doesn't need the lease; only programming does. alice@lab-pc-07 holds")
    # nothing in Build drives the board: write, watch and Add run for a watcher too
    choose_design(page)
    bdir = tmp_path / "b"
    by_id(page, "build-dir").fill(str(bdir))
    by_id(page, "script-write").click()
    expect(by_id(page, "bd-way")).to_be_visible(timeout=T)
    assert (bdir / "build_rm.tcl").is_file()
    kf.passed_build(bdir)
    by_id(page, "build-reload").click()
    expect_panel(page, "add", "current")
    expect(by_id(page, "add-lease")).to_contain_text("Programming needs the lease: alice@lab-pc-07 holds")
    page.locator('[data-action="kit_pack"]').click()
    page.wait_for_selector('[data-testid="section-workbench"]', timeout=T)
    assert (bdir / "overlay" / "spike_rm" / "manifest.json").is_file()
    assert page.errors == []


@HUB
def test_negative_twin_the_lease_holder_is_told_programming_is_theirs(page_factory, daemon, engine, tmp_path):
    page = hub_build_page(page_factory, daemon, engine, "mine")
    expect(by_id(page, "build-lease-note")).to_have_count(0)
    bdir = tmp_path / "b"
    kf.passed_build(bdir)
    choose_design(page)
    watch(page, bdir)
    expect(by_id(page, "add-lease")).to_have_attribute("data-lease", "here", timeout=T)
    expect(by_id(page, "add-lease")).to_contain_text("Programming needs the lease: yours")
    assert page.errors == []


# --- the pure helpers, in the page -------------------------------------------------------------------


def test_the_page_helpers(page_factory):
    page = page_factory("light")
    page.wait_for_selector(".board-item", timeout=T)
    got = page.evaluate("""async () => {
      const m = await import('./js/sections/build.js');
      return ['127.0.0.1', 'localhost', '[::1]', 'hm.localhost', 'lab-pc-07', '192.168.10.5',
              'example.org'].map((h) => m.isLoopbackHost(h));
    }""")
    assert got == [True, True, True, True, False, False, False]
    cards = page.evaluate("""async () => {
      const m = await import('./js/sections/build.js');
      const checks = {role: 'x', xdc: 'y', partial: 'z'};
      return [m.cardFor('partial: role', checks), m.cardFor('xdc:direction', checks),
              m.cardFor('partial', checks), m.cardFor('nonesuch', checks)];
    }""")
    assert cards == ["role", "xdc", "partial", "nonesuch"]
    ref = page.evaluate("""async () => (await import('./js/sections/build.js')).referenceUse(
      'rm_nanosoc: 7,903 LUT (18.5%), 16.5 BRAM tiles (11.5%)')""")
    assert ref == {"name": "rm_nanosoc", "LUT": 7903, "BRAM": 16.5}
    none = page.evaluate("""async () => (await import('./js/sections/build.js')).referenceUse('')""")
    assert none == {"name": ""}                                   # the twin: nothing is guessed
    assert os.environ.get("HARNESS_MANAGER_VIVADO") == "off"
