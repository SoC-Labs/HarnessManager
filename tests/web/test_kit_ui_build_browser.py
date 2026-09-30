"""KIT-UI: the Build section (david K9) in a headless system Chrome, driven by clicks only,
over the real harness-manager-daemon (``kit_api`` in ``EXTENSIONS``) and over the T14 mock
(``tests/fakes/kit_mock.py`` runs the same routes).

The demo boards run the previous static 0x3F1A560F; ``fielded(engine)`` moves the USB board
to 0x72BB0A36, the fixture kit's static (``tests/fakes/kit_fixture``). Vivado is never run:
``HARNESS_MANAGER_VIVADO`` is ``off`` (tests/conftest.py), or a fake script that prints a
version and answers the guide's launch (``kit_fakes.fake_vivado_script``, KIT-LIC). Every
test has its negative twin.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.request
import zipfile
from urllib.parse import quote, urlencode

import pytest

from harness_manager.demo import BOARD_USB
from tests.fakes import kit_fakes as kf

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 10_000
SID = "0x72BB0A36"
OLD = "0x3F1A560F"


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


def open_build(page):
    page.wait_for_selector(".board-item", timeout=T)
    page.locator(f'.board-item[data-board="{BOARD_USB}"]').click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="board-header"]', timeout=T)
    page.locator('[data-section="build"]').click()
    page.wait_for_selector('[data-testid="step-target"]', timeout=T)


def states(page) -> dict[str, str]:
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll(
      '[data-testid="build-steps"] .step-card')].map((el) => [el.dataset.testid.slice(5), el.dataset.state]))""")


def expect_state(page, step: str, state: str) -> None:
    expect(page.locator(f'[data-testid="step-{step}"]')).to_have_attribute("data-state", state, timeout=T)


def refresh(page) -> None:
    page.locator('[data-testid="build-refresh"]').click()
    page.wait_for_selector('[data-testid="build-refresh"]:not([aria-busy="true"])', timeout=T)


def no_overflow(page) -> list[str]:
    return page.evaluate("""() => {
      const w = document.documentElement.clientWidth;
      return [...document.querySelectorAll('.card')].filter(
        (el) => el.getBoundingClientRect().right > w + 1).map((el) => el.dataset.testid || '?');
    }""")


# --- 1. the six steps, with the guide's states ------------------------------------------------------


@pytest.mark.mock_too
def test_the_steps_render_with_the_guides_states_and_a_board_on_another_static_fails_target(
        page_factory, daemon, engine):
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light")
    open_build(page)
    expect_state(page, "kit", "done")
    guide = api(daemon, "GET", f"/boards/{quote(BOARD_USB, safe='')}/guide",
                query={"design": "minimal"})
    want = {s["id"]: s["state"] for s in guide["steps"]}
    assert want == {"target": "done", "tools": "next", "kit": "done", "wrapper": "done",
                    "build": "blocked", "check": "blocked"}
    assert states(page) == want                       # the page shows the guide's own states
    expect(page.locator('[data-testid="state-kit"]')).to_have_text("Done")
    expect(page.locator('[data-testid="state-tools"]')).to_have_text("Next")
    expect(page.locator('[data-testid="build-next"]')).to_contain_text("2 Tools")
    expect(page.locator('[data-testid="build-kit-id"]')).to_have_text("mps3/0x72BB0A36/vivado-2024.1")
    # K2: kits are public, and the card shows the kit's licence note
    expect(page.locator('[data-testid="kit-access"]')).to_have_text("public")
    expect(page.locator('[data-testid="kit-licence"]')).to_contain_text("no Arm IP")
    # K8: the built-in design names no rm_id, so HM proposes one from the user range
    expect(page.locator('[data-testid="wrapper-rm-id"]')).to_contain_text("rm_id 0x0100F28A")
    expect(page.locator('[data-testid="rm-id-proposed"]')).to_be_visible()
    expect(page.locator('[data-testid="step-build-reason"]')).to_contain_text("waits for 2 tools")
    # the licence is never a pass
    expect(page.locator('[data-testid="licence"]')).to_contain_text("[Common 17-345]")
    # the page is local: the paths are on this machine
    expect(page.locator('[data-testid="build-paths-hint"]')).to_contain_text("Paths are on this machine")
    # UI v2: five tabs (Checks only behind a hub); Build sits between the Workbench and the
    # Board, and XDC is Build's own fold
    tabs = page.evaluate("() => [...document.querySelectorAll('.section-tab')].map((t) => t.dataset.section)")
    assert tabs == ["overview", "workbench", "build", "board"], tabs
    assert no_overflow(page) == []
    page.locator('[data-testid="open-xdc"]').click()               # the Wrapper card links to XDC
    page.wait_for_selector('[data-testid="xdc-model"]', timeout=T)
    assert page.errors == []


@pytest.mark.mock_too
def test_twin_a_board_on_a_static_the_pack_does_not_know_fails_target_and_blocks_the_rest(
        page_factory, daemon):
    page = page_factory("light")                     # the demo board runs 0x3F1A560F: no kit
    open_build(page)
    expect_state(page, "target", "failed")
    assert states(page) == {"target": "failed", "tools": "next", "kit": "blocked",
                            "wrapper": "blocked", "build": "blocked", "check": "blocked"}
    expect(page.locator('[data-testid="step-target-detail"]')).to_contain_text(
        f"knows nothing of static {OLD}")
    expect(page.locator('[data-testid="step-kit-reason"]')).to_contain_text("waits for 1 target")
    expect(page.locator('[data-testid="script-generate"]')).to_be_disabled()
    assert page.errors == []


# --- 2. fetch: progress, then done ------------------------------------------------------------------


@pytest.mark.mock_too
def test_fetch_shows_its_progress_then_done_and_a_kit_for_another_static_is_refused(
        page_factory, daemon, engine, tmp_path):
    fielded(engine)
    page = page_factory("light")
    open_build(page)
    expect_state(page, "kit", "next")
    page.locator('[data-testid="step-kit"] button:has-text("A folder or zip")').click()
    # the twin first: a folder that holds another static's kit is refused, and nothing changes
    other = kf.build_fixture(tmp_path / "other", OLD)
    page.locator('[data-testid="kit-source-path"]').fill(str(other))
    page.locator('[data-action="kit_fetch"]').click()
    result = page.locator('[data-testid="kit-fetch-result"]')
    expect(result).to_contain_text("REFUSED", timeout=T)
    expect(result).to_contain_text(f"holds the kit for {OLD}, not {SID}")
    expect(result).to_contain_text("fetch: started")                 # it ran, then refused
    expect_state(page, "kit", "next")
    # the good folder: the progress line, then done, and the step turns done
    page.locator('[data-testid="kit-source-path"]').fill(str(kf.FIXTURE))
    page.locator('[data-action="kit_fetch"]').click()
    expect(result).to_contain_text("mps3/0x72BB0A36/vivado-2024.1 is cached (from path)", timeout=T)
    text = result.inner_text()
    assert text.index("fetch: started") < text.index("is cached")    # progress, then done
    assert "(rc 0," in text
    expect_state(page, "kit", "done")
    expect(page.locator('[data-testid="kit-licence"]')).to_contain_text("no Arm IP")
    page.locator('[data-testid="kit-verify"]').click()
    expect(page.locator('[data-testid="kit-verified"]')).to_contain_text("Verified", timeout=T)
    with page.expect_download(timeout=T) as dl:
        page.locator('[data-testid="kit-zip"]').click()
    assert dl.value.suggested_filename == f"mps3-kit-{SID}.zip"
    with zipfile.ZipFile(dl.value.path()) as zf:
        assert f"{SID}/static/static_routed_locked.dcp" in zf.namelist()
    assert page.errors == []


# --- 3. the Vivado release (K4) ----------------------------------------------------------------------


@pytest.mark.mock_too
def test_a_vivado_of_another_release_warns_and_its_twin_the_kits_release_does_not(
        page_factory, daemon, engine, tmp_path, monkeypatch):
    fielded(engine)
    import_kit(daemon)
    wrong = kf.fake_vivado_script(tmp_path / "v2023", release="2023.2", build=4029153)
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(wrong))
    page = page_factory("light")
    open_build(page)
    expect_state(page, "tools", "next")
    found = page.locator('[data-testid="vivado-found"]')
    expect(found).to_contain_text("Vivado 2023.2")
    expect(found).to_contain_text("$HARNESS_MANAGER_VIVADO")
    expect(page.locator('[data-testid="vivado-chip"]')).to_have_text("different release")
    expect(page.locator('[data-testid="vivado-needed"]')).to_contain_text("Vivado 2024.1")
    expect(page.locator('[data-testid="vivado-mismatch"]')).to_contain_text("build_rm.tcl refuses to start")
    card = page.locator('[data-testid="trouble"][data-card="vivado_version"]')
    expect(card).to_have_attribute("data-failing", "true")
    expect(card).to_have_attribute("open", "")
    expect(card.locator('[data-testid="trouble-fix"]')).to_contain_text("Runs 36-378")
    expect_state(page, "build", "blocked")                   # waits for 2 tools
    # the twin: the kit's own release
    right = kf.fake_vivado_script(tmp_path / "v2024", release="2024.1")
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(right))
    refresh(page)
    expect_state(page, "tools", "done")
    expect(page.locator('[data-testid="vivado-chip"]')).to_have_text("matches")
    expect(page.locator('[data-testid="vivado-mismatch"]')).to_have_count(0)
    expect(card).to_have_attribute("data-failing", "false")
    assert card.get_attribute("open") is None
    assert page.errors == []


def test_a_vivado_that_does_not_start_fails_tools_and_its_twin_starts(
        page_factory, daemon, engine, tmp_path, monkeypatch):
    # KIT-LIC: 2026.1 with no licence file exits 42 at launch; the fake plays it for 2024.1
    from harness_manager.services.kit import launch

    fielded(engine)
    import_kit(daemon)
    for k in launch.LICENCE_ENV:
        monkeypatch.delenv(k, raising=False)
    bad = kf.fake_vivado_script(tmp_path / "nolic", release="2024.1", launch="no-licence")
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(bad))
    page = page_factory("light")
    open_build(page)
    expect_state(page, "tools", "failed")
    chip = page.locator('[data-testid="vivado-launch-chip"]')
    expect(chip).to_have_text("does not start")
    expect(page.locator('[data-testid="vivado-launch"]')).to_contain_text(
        "no licence file (set XILINXD_LICENSE_FILE or LM_LICENSE_FILE)")
    expect(page.locator('[data-testid="step-tools-reason"]')).to_contain_text(
        "export XILINXD_LICENSE_FILE=PORT@SERVER")
    expect(page.locator('[data-testid="licence"]')).to_contain_text(
        "Vivado 2024.1 Enterprise (2026.1: Core or higher)")
    expect_state(page, "build", "blocked")
    # the twin: a Vivado that starts
    right = kf.fake_vivado_script(tmp_path / "v2024", release="2024.1")
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", str(right))
    refresh(page)
    expect_state(page, "tools", "done")
    expect(chip).to_have_text("starts")
    expect(page.locator('[data-testid="vivado-launch"]')).to_contain_text("Vivado 2024.1 starts")
    expect(page.locator('[data-testid="licence"]')).to_contain_text("[Common 17-345]")
    assert page.errors == []


# --- 4. generate the script: files, command, rm_id, zip -----------------------------------------------


@pytest.mark.mock_too
def test_generating_a_script_shows_its_files_and_command_and_offers_the_zip(
        page_factory, daemon, engine, tmp_path):
    fielded(engine)
    page = page_factory("light")
    open_build(page)
    # the twin first: with no kit the script cannot be generated, and the page says why
    expect(page.locator('[data-testid="script-generate"]')).to_be_disabled()
    expect(page.locator('[data-testid="script-zip"]')).to_be_disabled()
    expect(page.locator('[data-testid="step-build"]')).to_contain_text("needs the kit (3 Kit)")
    import_kit(daemon)
    refresh(page)
    expect_state(page, "kit", "done")
    with page.expect_response(lambda r: r.url.endswith("/guide/script")) as resp:
        page.locator('[data-testid="script-generate"]').click()
    assert resp.value.status == 200
    files = page.locator('[data-testid="script-files"]')
    expect(files).to_be_visible(timeout=T)
    expect(page.locator('[data-testid="script-file-body"]')).to_have_attribute("data-file", "build_rm.tcl")
    expect(page.locator('[data-testid="script-file-body"]')).to_contain_text("HM_RM_BUILD")
    for name in ("README.txt", "xdc/minimal_ooc.xdc"):
        expect(files.locator(f'[data-file="{name}"]')).to_be_visible()
    cmd = page.locator('[data-testid="script-command"] code')
    expect(cmd).to_have_text("cd minimal && vivado -mode batch -source build_rm.tcl -log build_rm.log "
                             "-journal build_rm.jou")
    result = page.locator('[data-testid="script-result"]')
    expect(result).to_contain_text("rm_id 0x0100F28A")
    expect(result.locator('[data-testid="rm-id-proposed"]')).to_be_visible()
    with page.expect_download(timeout=T) as dl:
        page.locator('[data-testid="script-zip"]').click()
    assert dl.value.suggested_filename == "minimal_build.zip"
    with zipfile.ZipFile(dl.value.path()) as zf:
        names = zf.namelist()
    assert "minimal/build_rm.tcl" in names and "minimal/kit/static/static_routed_locked.dcp" in names
    expect(page.locator('[data-testid="script-zip-saved"]')).to_contain_text("minimal_build.zip")
    # into a build directory on the daemon's host: written there, and the command is absolute
    bdir = tmp_path / "build" / "minimal"
    page.locator('[data-testid="build-dir"]').fill(str(bdir))
    page.locator('[data-testid="build-dir"]').press("Enter")
    page.locator('[data-testid="build-dir"]').blur()
    page.locator('[data-testid="script-generate"]').click()
    expect(result).to_contain_text(f"written to {bdir}", timeout=T)
    assert (bdir / "build_rm.tcl").is_file() and (bdir / "kit" / "kit.json").is_file()
    expect(cmd).to_have_text(f"vivado -mode batch -source {bdir}/build_rm.tcl -log {bdir}/build_rm.log "
                             f"-journal {bdir}/build_rm.jou")
    expect(page.locator('[data-testid="step-build-detail"]')).to_contain_text("no receipt", timeout=T)
    assert page.errors == []


@pytest.mark.mock_too
def test_k8_a_design_id_another_design_holds_warns_and_its_twin_is_unused(
        page_factory, daemon, engine):
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light")
    open_build(page)
    page.locator('button:has-text("Paste")').click()
    page.locator('[data-testid="build-design-json"]').fill(json.dumps(
        {"kind": "rm", "name": "my_rm", "rm_id": "0x01000001", "use": {"clkrst": {}}}))
    page.locator('[data-testid="script-generate"]').click()
    result = page.locator('[data-testid="script-result"]')
    expect(result.locator('[data-testid="rm-id-clash"]')).to_contain_text("already 'nanosoc'", timeout=T)
    expect(result).to_contain_text("from the design")
    expect_state(page, "wrapper", "done")                  # a clash warns; it never refuses
    # the twin: no rm_id, so HM proposes one no design holds
    page.locator('[data-testid="build-design-json"]').fill(json.dumps(
        {"kind": "rm", "name": "my_rm", "use": {"clkrst": {}}}))
    page.locator('[data-testid="script-generate"]').click()
    expect(result.locator('[data-testid="rm-id-proposed"]')).to_be_visible(timeout=T)
    expect(result.locator('[data-testid="rm-id-clash"]')).to_have_count(0)
    expect(result).to_contain_text("is unused")
    assert page.errors == []


# --- 5. a static mismatch: 409, and the page says why -------------------------------------------------


@pytest.mark.mock_too
def test_a_static_mismatch_is_refused_409_and_the_page_says_why(page_factory, daemon, tmp_path):
    # the demo board runs 0x3F1A560F; its kit is cached, but HM's pin model is 0x72BB0A36's
    import_kit(daemon, kf.build_fixture(tmp_path / "old", OLD))
    page = page_factory("light")
    open_build(page)
    expect_state(page, "kit", "done")
    expect_state(page, "wrapper", "failed")
    with page.expect_response(lambda r: r.url.endswith("/guide/script")) as resp:
        page.locator('[data-testid="script-generate"]').click()
    assert resp.value.status == 409
    err = page.locator('[data-testid="script-error"]')
    expect(err).to_contain_text("Refused: REFUSED", timeout=T)
    expect(err).to_contain_text(f"xdc:static_id: {OLD}: the mps3 pin model has no shell")
    expect(page.locator('[data-testid="script-refused"] [data-check="xdc:static_id"]')).to_be_visible()
    card = page.locator('[data-testid="trouble"][data-card="xdc:static_id"]')
    expect(card).to_have_attribute("data-failing", "true")
    expect(card.locator('[data-testid="trouble-fix"]')).to_contain_text("pin model describes another static")
    expect(page.locator('[data-testid="script-files"]')).to_have_count(0)
    assert page.errors == []


@pytest.mark.mock_too
def test_twin_the_boards_own_static_is_not_refused(page_factory, daemon, engine):
    fielded(engine)
    import_kit(daemon)
    page = page_factory("light")
    open_build(page)
    with page.expect_response(lambda r: r.url.endswith("/guide/script")) as resp:
        page.locator('[data-testid="script-generate"]').click()
    assert resp.value.status == 200
    expect(page.locator('[data-testid="script-files"]')).to_be_visible(timeout=T)
    expect(page.locator('[data-testid="script-error"]')).to_have_count(0)
    expect(page.locator('[data-testid="trouble"][data-failing="true"]')).to_have_count(0)
    assert page.errors == []


# --- 6. check, then add to Program -------------------------------------------------------------------


@pytest.mark.mock_too
def test_a_check_that_passes_enables_add_to_program(page_factory, daemon, engine, tmp_path):
    fielded(engine)
    import_kit(daemon)
    receipt = kf.passed_build(tmp_path / "b")
    page = page_factory("light")
    open_build(page)
    add = page.locator('[data-action="kit_pack"]')
    expect(add).to_have_attribute("aria-disabled", "true")
    expect(page.locator('[data-testid="reason-kit_pack"]')).to_have_text("check the build first")
    page.locator('[data-testid="receipt-path"]').fill(str(receipt))
    page.locator('[data-action="kit_check"]').click()
    expect(page.locator('[data-testid="check-result"]')).to_contain_text("passed:", timeout=T)
    expect(page.locator('[data-testid="check-list"] [data-check="board_static"]')).to_have_attribute(
        "data-state", "ok")
    expect(add).not_to_have_attribute("aria-disabled", "true")
    add.click()
    expect(page.locator('[data-testid="pack-result"]')).to_contain_text(
        "spike_rm rm_id 0x010080f0 is in Program", timeout=T)
    assert (tmp_path / "b" / "overlay" / "spike_rm" / "manifest.json").is_file()
    page.locator('[data-testid="open-program"]').click()
    page.wait_for_selector('[data-testid="overlays-card"]', timeout=T)
    assert page.errors == []


@pytest.mark.mock_too
def test_twin_a_check_that_fails_opens_its_card_and_keeps_add_disabled(
        page_factory, daemon, engine, tmp_path):
    fielded(engine)
    import_kit(daemon)
    receipt = kf.passed_build(tmp_path / "b")
    partial = tmp_path / "b" / "out" / "spike_rm_partial.bin"
    partial.write_bytes(partial.read_bytes()[:-8])           # half-copied after the build
    page = page_factory("light")
    open_build(page)
    page.locator('[data-testid="receipt-path"]').fill(str(receipt))
    page.locator('[data-action="kit_check"]').click()
    expect(page.locator('[data-testid="check-result"]')).to_contain_text("refused: partial", timeout=T)
    expect(page.locator('[data-testid="check-list"] [data-check="partial"]')).to_have_attribute(
        "data-state", "mismatch")
    card = page.locator('[data-testid="trouble"][data-card="partial"]')
    expect(card).to_have_attribute("data-failing", "true")
    expect(card).to_have_attribute("open", "")
    expect(card.locator('[data-testid="trouble-detail"]')).to_contain_text("half-copied")
    expect(card.locator('[data-testid="trouble-fix"]')).to_contain_text("copy out/ from the build again")
    add = page.locator('[data-action="kit_pack"]')
    expect(add).to_have_attribute("aria-disabled", "true")
    expect(page.locator('[data-testid="reason-kit_pack"]')).to_contain_text("the check refused this build")
    add.click(force=True)                                     # the interlock: nothing is run
    expect(page.locator('[data-testid="pack-result"]')).to_contain_text("not run")
    assert not (tmp_path / "b" / "overlay").exists()
    assert page.errors == []


# --- 7. a failed build opens its gate's card -----------------------------------------------------------


@pytest.mark.mock_too
def test_a_failed_build_opens_the_card_of_its_gate_and_a_passed_one_opens_none(
        page_factory, daemon, engine, tmp_path):
    fielded(engine)
    import_kit(daemon)
    bdir = tmp_path / "failed"
    kf.passed_build(bdir, state="failed", stage="impl", gates=[
        {"gate": "static_id", "verdict": "PASS", "detail": f"CRC-32 is {SID}"},
        {"gate": "rm_timing", "verdict": "FAIL", "detail": "setup WNS -0.412 ns"}])
    page = page_factory("light")
    open_build(page)
    page.locator('[data-testid="build-dir"]').fill(str(bdir))
    page.locator('[data-testid="build-dir"]').blur()
    expect_state(page, "build", "failed")
    expect(page.locator('[data-testid="step-build-detail"]')).to_contain_text("gate rm_timing")
    expect(page.locator('[data-testid="step-build-reason"]')).to_contain_text("RM_XDC")
    stages = page.locator('[data-testid="build-stages"]')
    expect(stages.locator('[data-stage="impl"]')).to_have_attribute("data-stage-state", "failed")
    expect(stages.locator('[data-stage="link"]')).to_have_attribute("data-stage-state", "done")
    expect(stages.locator('[data-stage="verify"]')).to_have_attribute("data-stage-state", "pending")
    card = page.locator('[data-testid="trouble"][data-card="rm_timing"]')
    expect(card).to_have_attribute("data-failing", "true")
    expect(card.locator('[data-testid="trouble-detail"]')).to_contain_text("setup WNS -0.412 ns")
    first = page.locator('[data-testid="trouble-list"] [data-testid="trouble"]').first
    expect(first).to_have_attribute("data-card", "rm_timing")      # the failing card leads
    # the receipt names itself as the path to check
    expect(page.locator('[data-testid="receipt-path"]')).to_have_value(
        str(bdir / "out" / "spike_rm_build.json"))
    # the twin: a passed build in another directory: every gate card is closed
    good = tmp_path / "good"
    kf.passed_build(good)
    page.locator('[data-testid="build-dir"]').fill(str(good))
    page.locator('[data-testid="build-dir"]').blur()
    expect_state(page, "build", "done")
    expect(page.locator('[data-testid="trouble"][data-failing="true"]')).to_have_count(0)
    expect(stages.locator('[data-stage="bitstream"]')).to_have_attribute("data-stage-state", "done")
    assert page.errors == []


# --- the pure helpers, in the page -------------------------------------------------------------------


def test_the_page_knows_a_remote_browser_from_a_local_one(page_factory):
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
