"""The web UI end to end: a headless system Chrome on harness-manager-daemon (T13's app) over DemoEngine.

Each test opens the page the way ``harness-manager ui`` will (``/#token=...``) and drives it
with clicks and keys only. What each proves is in its name; each has a negative twin
(the board, design or state where the opposite must show).

Screenshots of every section, in light and dark, land in tests/web/screenshots/
(gitignored) for review.
"""

from __future__ import annotations

import re
import time

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_HELD, BOARD_USB

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser

SECTIONS = ["overview", "program", "consoles", "debug", "power", "clocks", "xdc", "activity"]
T = 10_000   # ms: the longest any single UI wait may take


def rail(page, board_id):
    return page.locator(f'.board-item[data-board="{board_id}"]')


def open_board(page, board_id):
    rail(page, board_id).click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="board-header"]', timeout=T)
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)


def open_details(page):
    """The Overview's Details (identity, counters, telemetry, capabilities) start collapsed."""
    toggle = page.locator('[data-action="details"]')
    toggle.wait_for(timeout=T)
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    page.wait_for_selector('[data-testid="identity-card"]', timeout=T)


def section(page, key):
    page.locator(f'[data-section="{key}"]').click()
    page.wait_for_selector(f'[data-testid="section-{key}"]', timeout=T)


def result_text(page, testid):
    return page.locator(f'[data-testid="{testid}"]').inner_text()


def no_missing_icons(page):
    assert page.locator("[data-missing-icon]").count() == 0, page.eval_on_selector_all(
        "[data-missing-icon]", "els => els.map(e => e.dataset.missingIcon)")


def hold(engine, service, method):
    """Make ``engine.<service>.<method>`` wait until the returned event is set (a job or a
    call that stays in flight exactly as long as the test needs, however loaded the host)."""
    import threading

    release = threading.Event()
    release.entered = []                   # calls that reached the service and wait here
    target = getattr(engine, service)
    original = getattr(target, method)

    def held(*args, **kwargs):
        release.entered.append(args)
        release.wait(30)
        return original(*args, **kwargs)

    setattr(target, method, held)
    return release


def wait_until(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


# --- start-up and the token ---------------------------------------------------------------------


def test_the_token_moves_from_the_url_into_session_storage(page_factory, daemon):
    page = page_factory()
    page.wait_for_selector(".board-item", timeout=T)
    assert "token" not in page.url
    assert page.evaluate("sessionStorage.getItem('harness_manager.token')") == daemon.token
    assert page.locator('[data-testid="daemon-line"]').inner_text().startswith("harness-manager-daemon")
    assert page.errors == []


def test_a_wrong_token_says_the_session_expired(page_factory, daemon):
    page = page_factory(url=f"{daemon.url}/#token=not-the-token")
    page.wait_for_selector(".banner.err", timeout=T)
    assert "Session expired: run harness-manager ui again." in page.locator(".banner.err").inner_text()
    assert "session expired" in page.locator('[data-testid="daemon-line"]').inner_text()


def test_the_rail_lists_every_board_with_its_design_and_lock(page_factory):
    page = page_factory()
    page.wait_for_selector(f'.board-item[data-board="{BOARD_HELD}"]', timeout=T)
    assert rail(page, BOARD_USB).inner_text().count("nanosoc") >= 1
    held = rail(page, BOARD_HELD).inner_text()
    assert "alice" in held
    assert "alice" not in rail(page, BOARD_FIELDED).inner_text()


# --- overview --------------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_selecting_a_board_renders_identity_with_unchecked_as_a_warning(page_factory):
    page = page_factory()
    rail(page, BOARD_FIELDED).click()
    preview = page.locator('[data-testid="preview-build"]')
    assert preview.get_attribute("data-level") == "unk" and "Unchecked" in preview.inner_text()
    open_board(page, BOARD_FIELDED)
    open_details(page)
    assert page.locator('[data-testid="id-shell"]').inner_text() == "0x3f1a560f"
    for testid in ("build-chip", "id-build"):
        chip = page.locator(f'[data-testid="{testid}"]')
        assert chip.get_attribute("data-level") == "unk", testid
        assert "Unchecked" in chip.inner_text()
    assert "not a pass" in page.locator('[data-testid="id-build-note"]').inner_text()
    assert "Yours" in page.locator('[data-testid="lock-chip"]').inner_text()
    no_missing_icons(page)
    assert page.errors == []


def test_a_board_whose_build_check_passed_shows_ok_not_unchecked(page_factory):
    page = page_factory()
    open_board(page, BOARD_USB)
    chip = page.locator('[data-testid="build-chip"]')
    assert chip.get_attribute("data-level") == "ok" and "OK" in chip.inner_text()


def test_telemetry_shows_unavailable_with_its_reason_never_zero(page_factory):
    page = page_factory()
    open_board(page, BOARD_FIELDED)
    open_details(page)
    row = page.locator('[data-telemetry] [data-reading="mcc_temp"]')
    row.wait_for(timeout=T)
    assert row.get_attribute("data-available") == "no"
    text = row.inner_text()
    assert "unavailable" in text and "needs the Debug USB cable" in text and " 0 " not in text
    assert "50 MHz" in page.locator('[data-telemetry] [data-reading="dut_clk"]').inner_text()


# --- capabilities ----------------------------------------------------------------------------------


def test_a_capability_the_board_lacks_is_disabled_with_its_reason(page_factory, engine):
    page = page_factory()
    open_board(page, BOARD_FIELDED)
    open_details(page)
    missing = page.locator('[data-testid="capabilities-card"] li[data-capability="reboot_board"]')
    assert "needs the Debug USB cable" in missing.inner_text()
    section(page, "power")
    button = page.locator('[data-action="reboot"]')
    assert button.get_attribute("aria-disabled") == "true"
    assert "Cannot: needs the Debug USB cable" in page.locator('[data-testid="reason-reboot"]').inner_text()
    page.locator('[data-testid="arm-reboot"] input').check()
    button.click(force=True)            # the interlock, not the engine, answers
    expect(page.locator('[data-testid="reboot-result"]')).to_contain_text("Nothing was run.")
    assert "not run" in result_text(page, "reboot-result")
    assert engine.called("controller.reboot") == []
    # No metered outlet in boards.toml: the cold power cycle says why, and offers no button.
    expect(page.locator('[data-testid="power-cycle-reason"]')).to_contain_text("networked power plug")
    assert page.locator('[data-testid="power-card"] [data-action="power_cycle"]').count() == 0


def test_with_the_usb_link_reboot_is_gated_only_by_the_arm_box(page_factory, engine):
    page = page_factory()
    open_board(page, BOARD_USB)
    section(page, "power")
    assert "not armed" in page.locator('[data-testid="reason-reboot"]').inner_text()
    page.locator('[data-testid="arm-reboot"] input').check()
    expect(page.locator('[data-action="reboot"]')).not_to_have_attribute("aria-disabled", "true")
    page.locator('[data-action="reboot"]').click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"reboot-result\"]')?.innerText.includes('rc 0')",
        timeout=T)
    # The demo reboot returns the same evidence dict as the MPS3 controller (lead fix).
    text = result_text(page, "reboot-result")
    assert "REBOOT witnessed" in text and "down after 0.3 s, back after 0.9 s" in text, text
    assert len(engine.called("controller.reboot")) == 1
    expect(page.locator('[data-testid="arm-reboot"] input')).not_to_be_checked()   # disarmed after


# --- program ------------------------------------------------------------------------------------


def test_program_mismatch_blocks_and_nothing_is_pushed(page_factory, engine):
    page = page_factory()
    open_board(page, BOARD_USB)
    section(page, "program")
    page.locator('[data-overlay="nanosoc_multicore"]').click()
    page.wait_for_selector('[data-testid="preflight-list"] li[data-check="mismatch"]', timeout=T)
    assert "Program is refused" in page.locator('[data-testid="preflight-summary"]').inner_text()
    page.locator('[data-testid="arm-program"] input').check()
    button = page.locator('[data-action="program"]')
    expect(page.locator('[data-testid="reason-program"]')).to_contain_text("preflight MISMATCH")
    assert button.get_attribute("aria-disabled") == "true"
    button.click(force=True)
    expect(page.locator('[data-testid="program-result"]')).to_contain_text("Nothing was run.")
    assert "$ program nanosoc_multicore" in result_text(page, "program-result")
    assert engine.called("deploy.deploy") == []
    # UNCHECKED rows are shown as their own state and never block.
    assert page.locator('[data-testid="preflight-list"] li[data-check="unchecked"]').count() == 1


@pytest.mark.mock_too
def test_program_ok_path_shows_progress_to_done(page_factory, engine, screenshots):
    engine.speed = 1.5            # a deploy long enough to see its push phase
    page = page_factory()
    open_board(page, BOARD_USB)
    section(page, "program")
    page.locator('[data-overlay="led"]').click()
    page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
    assert page.locator('[data-testid="preflight-list"] li[data-check="mismatch"]').count() == 0
    page.locator('[data-testid="arm-program"] input').check()
    expect(page.locator('[data-action="program"]')).not_to_have_attribute("aria-disabled", "true")
    page.locator('[data-action="program"]').click()
    busy = page.locator('[data-action="program"][aria-busy="true"]')
    busy.wait_for(timeout=T)
    assert re.search(r"Programming\.\.\.\s*\d+ s", busy.inner_text())
    assert "waiting for Programming" in page.locator('[data-testid="reason-restore"]').inner_text()
    try:                          # best effort: a loaded host may render straight to done
        page.wait_for_selector('[data-testid="deploy-phase"]:has-text("push")', timeout=3000)
        page.screenshot(path=str(screenshots / "light-program-running.png"))
    except sync_api.TimeoutError:
        pass
    outcome = page.locator('[data-testid="deploy-outcome"]')
    outcome.wait_for(timeout=T)
    assert outcome.get_attribute("data-state") == "done"
    assert "verified by the board" in outcome.inner_text()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"program-result\"]')?.innerText.includes('rc 0')",
        timeout=T)
    assert re.search(r"\$ program led\s+\(rc 0, [\d.]+ s\)", result_text(page, "program-result"))
    assert "0x0100001e" in page.locator('[data-testid="deploy-events"]').inner_text()
    page.wait_for_selector('[data-testid="fact-design"]:has-text("led")', timeout=T)
    assert len(engine.called("deploy.deploy")) == 1
    expect(page.locator('[data-testid="arm-program"] input')).not_to_be_checked()
    page.screenshot(path=str(screenshots / "light-program-done.png"))


def test_program_needs_the_arm_box(page_factory, engine):
    page = page_factory()
    open_board(page, BOARD_USB)
    section(page, "program")
    page.locator('[data-overlay="greybox"]').click()
    page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
    expect(page.locator('[data-testid="reason-program"]')).to_contain_text("not armed")
    page.locator('[data-action="program"]').click(force=True)
    expect(page.locator('[data-testid="program-result"]')).to_contain_text("Nothing was run.")
    assert engine.called("deploy.deploy") == []


# --- consoles -------------------------------------------------------------------------------------


def console_text(page, name):
    return page.evaluate(f"window.__harness_managerConsoles.text({BOARD_USB!r}, {name!r})") or ""


@pytest.mark.mock_too
def test_console_output_appears_and_send_works(page_factory, engine, screenshots):
    page = page_factory()
    open_board(page, BOARD_USB)
    section(page, "consoles")
    page.wait_for_selector('[data-testid="console-state"]:has-text("up")', timeout=T)
    assert wait_until(lambda: engine.inject_console(BOARD_USB, "uart0",
                                                    "MicroPython v1.24 on nanosoc\r\n>>> ") > 0)
    assert wait_until(lambda: "MicroPython v1.24 on nanosoc" in console_text(page, "uart0"))
    page.locator('[data-testid="send-line"]').fill("print(1+1)")
    page.locator('[data-testid="send-line"]').press("Enter")
    assert wait_until(lambda: (BOARD_USB, "uart0", b"print(1+1)\r\n") in engine.consoles.writes)
    expect(page.locator('[data-testid="console-result"]')).to_contain_text("rc 0")
    assert wait_until(lambda: "print(1+1)" in console_text(page, "uart0"))   # the demo DUT echoes
    # david (L3): attach with screen, never the CLI line; that row is gone.
    # (Q1: this checked a data-testid the page no longer has anywhere, so it could not
    # fail. The removed row read "harness-manager console <address> <name>".)
    section_text = page.locator('[data-testid="section-consoles"]').inner_text()
    assert "rc 0" in section_text and "harness-manager console" not in section_text
    page.screenshot(path=str(screenshots / "light-consoles-live.png"))


def test_an_empty_send_is_refused_and_nothing_is_written(page_factory, engine):
    page = page_factory()
    open_board(page, BOARD_USB)
    section(page, "consoles")
    page.wait_for_selector('[data-testid="console-state"]:has-text("up")', timeout=T)
    before = list(engine.consoles.writes)
    page.locator('[data-action="send"]').click()
    expect(page.locator('[data-testid="console-result"]')).to_contain_text("Nothing was run.")
    assert engine.consoles.writes == before


def test_consoles_switch_tabs_and_keep_their_output(page_factory, engine):
    page = page_factory()
    open_board(page, BOARD_USB)
    section(page, "consoles")
    page.wait_for_selector('[data-testid="console-state"]:has-text("up")', timeout=T)
    assert wait_until(lambda: engine.inject_console(BOARD_USB, "uart0", "kept line\r\n") > 0)
    assert wait_until(lambda: "kept line" in console_text(page, "uart0"))
    page.locator('[data-console-tab="mcc"]').click()
    page.wait_for_selector('[data-testid="console-mcc"]', timeout=T)
    section(page, "overview")
    section(page, "consoles")
    page.locator('[data-console-tab="uart0"]').click()
    assert "kept line" in console_text(page, "uart0")


# --- debug ------------------------------------------------------------------------------------------


@pytest.mark.mock_too
def test_debug_detect_up_and_down(page_factory, engine, screenshots):
    page = page_factory()
    open_board(page, BOARD_USB)
    section(page, "debug")
    down = page.locator('[data-action="down"]')
    assert down.get_attribute("aria-disabled") == "true"
    assert "the session is down" in page.locator('[data-testid="reason-down"]').inner_text()
    page.locator('[data-action="detect"]').click()
    page.wait_for_selector('[data-testid="idcode"]:has-text("0x6ba00477")', timeout=T)
    page.locator('[data-action="up"]').click()
    page.wait_for_selector('[data-testid="debug-state"]:has-text("up")', timeout=T)
    page.wait_for_selector('[data-port="gdb"]:has-text("127.0.0.1:3343")', timeout=T)
    assert "target extended-remote :3343" in page.locator('[data-testid="debug-ports"]').inner_text()
    expect(page.locator('[data-action="up"]')).to_have_attribute("aria-disabled", "true")
    page.screenshot(path=str(screenshots / "light-debug-up.png"))
    page.locator('[data-action="down"]').click()
    page.wait_for_selector('[data-testid="debug-state"]:has-text("down")', timeout=T)
    expect(page.locator('[data-port="gdb"]')).to_have_text("-")
    assert [n for n, _ in engine.calls if n.startswith("debug.")].count("debug.up") == 1


def test_detect_on_a_design_without_a_debug_port_says_so(page_factory):
    page = page_factory()
    open_board(page, BOARD_FIELDED)               # greybox: no DAP
    section(page, "debug")
    page.locator('[data-action="detect"]').click()
    page.wait_for_selector('[data-testid="debug-result"]:has-text("NOTHING_ON_TARGET")', timeout=T)
    expect(page.locator('[data-testid="debug-result"]')).to_contain_text("rc 13")


# --- SD recovery ----------------------------------------------------------------------------------


@pytest.mark.mock_too
def sd_backup_zip(tmp_path):
    import zipfile

    path = tmp_path / "sd-backup-20260923.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("config.txt", "TITLE: V2M-MPS3\n")
    return path


def test_the_sd_recovery_panel_shows_first_when_an_install_was_interrupted(page_factory, engine,
                                                                          screenshots, tmp_path):
    backup = sd_backup_zip(tmp_path)
    engine.set_sd_journal(BOARD_USB, {"op": "install", "state": "interrupted",
                                      "current": "MB/HBI0309C/AN536/images.txt",
                                      "backup": {"path": str(backup)}})
    page = page_factory()
    open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="sd-recovery"]', timeout=T)
    assert page.locator('[data-section="sd"]').get_attribute("aria-selected") == "true"
    first = page.locator('[data-testid="section-sd"] .card').first
    assert first.get_attribute("data-testid") == "sd-recovery"
    assert "images.txt" in page.locator('[data-testid="sd-journal"]').inner_text()
    assert page.locator('[data-testid="sd-banner"]').is_visible()
    page.screenshot(path=str(screenshots / "light-power-sd-recovery.png"))
    page.locator('[data-testid="arm-sd"] input').check()
    page.locator('[data-action="sd_restore"]').click()
    page.wait_for_selector('[data-testid="sd-recovery"]', state="detached", timeout=T)
    assert engine.called("storage.restore")
    expect(page.locator('[data-testid="sd-banner"]')).to_have_count(0)


def test_a_journal_naming_a_missing_backup_is_refused_and_the_panel_stays(page_factory, engine,
                                                                          tmp_path):
    engine.set_sd_journal(BOARD_USB, {"op": "install", "state": "interrupted",
                                      "backup": {"path": str(tmp_path / "gone.zip")}})
    page = page_factory()
    open_board(page, BOARD_USB)
    page.wait_for_selector('[data-testid="sd-recovery"]', timeout=T)
    page.locator('[data-testid="arm-sd"] input').check()
    page.locator('[data-action="sd_restore"]').click()
    expect(page.locator('[data-testid="sd-result"]')).to_contain_text("ABSENT", timeout=T)
    assert page.locator('[data-testid="sd-recovery"]').is_visible()
    assert engine.called("storage.restore") == []


def test_no_recovery_panel_when_nothing_is_pending(page_factory):
    page = page_factory()
    open_board(page, BOARD_USB)
    assert page.locator('[data-section="overview"]').get_attribute("aria-selected") == "true"
    assert page.locator('[data-testid="sd-banner"]').count() == 0
    assert page.locator('[data-testid="attention"] [data-attention="sd"]').count() == 0
    section(page, "sd")
    assert page.locator('[data-testid="sd-recovery"]').count() == 0


# --- a held board ----------------------------------------------------------------------------------


def test_opening_a_held_board_is_refused_and_names_the_holder(page_factory, engine):
    page = page_factory()
    rail(page, BOARD_HELD).click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="open-result"]', timeout=T)
    text = result_text(page, "open-result")
    assert "HELD" in text and "alice" in text and "was not opened" in text


# --- a job holds the board ----------------------------------------------------------------------------


def test_a_job_another_client_started_holds_the_boards_actions_until_it_ends(page_factory, engine,
                                                                             daemon):
    import httpx

    release = hold(engine, "deploy", "deploy")
    page = page_factory()
    open_board(page, BOARD_USB)
    section(page, "debug")
    # The CLI (another client of the same daemon) starts a deploy on this board.
    from urllib.parse import quote

    with httpx.Client(base_url=daemon.url, trust_env=False,
                      headers={"Authorization": f"Bearer {daemon.token}"}) as http:
        r = http.post(f"/api/v1/boards/{quote(BOARD_USB, safe='')}/deploy", json={"overlay": "led"})
        assert r.status_code == 202, r.text
    chip = page.locator('[data-testid="job-chip"]')
    expect(chip).to_contain_text("deploy running", timeout=T)
    expect(page.locator('[data-testid="reason-detect"]')).to_contain_text("waiting for the deploy job")
    page.locator('[data-action="detect"]').click(force=True)
    expect(page.locator('[data-testid="debug-result"]')).to_contain_text("Nothing was run.")
    assert engine.called("debug.detect") == []
    release.set()
    expect(chip).to_have_count(0, timeout=T)                 # job.done frees the board
    expect(page.locator('[data-testid="fact-design"]')).to_contain_text("led", timeout=T)
    expect(page.locator('[data-action="detect"]')).not_to_have_attribute("aria-disabled", "true")


# --- the page never freezes -------------------------------------------------------------------------


def test_a_slow_engine_call_leaves_the_page_usable(page_factory, engine):
    release = hold(engine, "debug", "detect")
    page = page_factory()
    open_board(page, BOARD_USB)
    section(page, "debug")
    page.locator('[data-action="detect"]').click()
    busy = page.locator('[data-action="detect"][aria-busy="true"]')
    busy.wait_for(timeout=T)
    assert "Detecting..." in busy.inner_text()
    expect(page.locator('[data-testid="reason-up"]')).to_contain_text("waiting for Detecting")
    # The call is still in flight (held): the page switches sections and back meanwhile,
    # renders the rail and the log, and the busy button counts on.
    section(page, "overview")
    section(page, "activity")
    section(page, "debug")
    expect(busy).to_contain_text("Detecting...")
    # In flight: it reached the service and has not finished. (Q1: `called == []` alone
    # was true by construction, since the demo engine records a call only once it runs.)
    assert len(release.entered) == 1 and engine.called("debug.detect") == []
    release.set()
    page.wait_for_selector('[data-testid="idcode"]:has-text("0x6ba00477")', timeout=T)


# --- help --------------------------------------------------------------------------------------------


def test_help_shows_the_cli_help_tabs_and_escape_closes_it(page_factory, screenshots):
    from harness_manager.cli.helptext import tabs

    page = page_factory()
    page.wait_for_selector(".board-item", timeout=T)
    page.locator('[data-action="help"]').click()
    nav = page.locator('[data-testid="help"] .modal-nav button')
    expect(nav).to_have_count(len(tabs()))
    first_name, first_text = tabs()[0]
    expect(nav.first).to_have_text(first_name)
    assert first_text.strip().splitlines()[0] in page.locator(".modal-text").inner_text()
    page.screenshot(path=str(screenshots / "light-help.png"))
    page.keyboard.press("Escape")
    expect(page.locator('[data-testid="help"]')).to_have_count(0)


# --- layout, themes, focus, screenshots -------------------------------------------------------------


def horizontal_overflow(page) -> list[str]:
    return page.evaluate("""() => {
      const out = [];
      const w = document.documentElement.clientWidth;
      if (document.documentElement.scrollWidth > w) out.push(`page ${document.documentElement.scrollWidth} > ${w}`);
      for (const el of document.querySelectorAll('.section-body, .workspace, .rail, .board-header')) {
        if (el.scrollWidth > el.clientWidth + 1) out.push(`${el.className} ${el.scrollWidth} > ${el.clientWidth}`);
      }
      for (const el of document.querySelectorAll('.card')) {
        const r = el.getBoundingClientRect();
        if (r.right > w + 1) out.push(`card ${el.dataset.testid || ''} ends at ${r.right}`);
      }
      return out;
    }""")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_section_fits_1280x800_without_horizontal_scroll(page_factory, engine, screenshots, scheme):
    page = page_factory(scheme)
    page.wait_for_selector(".board-item", timeout=T)
    rail(page, BOARD_HELD).click()
    page.screenshot(path=str(screenshots / f"{scheme}-preview-held.png"))
    open_board(page, BOARD_USB)
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert (bg == "rgb(15, 18, 22)") == (scheme == "dark"), bg
    for key in SECTIONS:
        section(page, key)
        if key == "program":
            page.locator('[data-overlay="nanosoc_multicore"]').click()
            page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
        if key == "consoles":
            page.wait_for_selector('[data-testid="console-state"]:has-text("up")', timeout=T)
            engine.inject_console(BOARD_USB, "uart0", "\x1b[32mboot:\x1b[0m nanosoc ready\r\n>>> ")
            page.wait_for_timeout(200)
        page.wait_for_timeout(150)
        assert horizontal_overflow(page) == [], key
        no_missing_icons(page)
        page.screenshot(path=str(screenshots / f"{scheme}-{key}.png"))
    rail(page, BOARD_FIELDED).click()
    open_board(page, BOARD_FIELDED)
    section(page, "power")
    assert horizontal_overflow(page) == []
    page.screenshot(path=str(screenshots / f"{scheme}-power-ethernet-only.png"))
    section(page, "overview")
    page.screenshot(path=str(screenshots / f"{scheme}-overview-ethernet-only.png"))
    assert page.errors == []


def test_the_layout_scales_up_to_a_wide_screen(page_factory):
    page = page_factory(width=1920, height=1080)
    open_board(page, BOARD_USB)
    for key in ("overview", "program"):
        section(page, key)
        assert horizontal_overflow(page) == []


def test_the_theme_toggle_overrides_the_system_and_is_remembered(page_factory):
    page = page_factory("light")
    page.wait_for_selector(".board-item", timeout=T)
    page.locator('.rail .seg button:has-text("Dark")').click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(15, 18, 22)"
    page.reload()
    page.wait_for_selector(".board-item", timeout=T)
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    page.locator('.rail .seg button:has-text("Auto")').click()
    expect(page.locator("html")).not_to_have_attribute("data-theme", "dark")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(244, 245, 247)"


def test_keyboard_focus_is_visible(page_factory):
    page = page_factory()
    page.wait_for_selector(".board-item", timeout=T)
    for _ in range(6):
        page.keyboard.press("Tab")
        shadow = page.evaluate("getComputedStyle(document.activeElement).boxShadow")
        tag = page.evaluate("document.activeElement.tagName")
        if tag in ("BUTTON", "INPUT"):
            assert shadow and shadow != "none", tag
            return
    pytest.fail("Tab never reached a button")


def test_reduced_motion_stops_the_spinner(page_factory):
    page = page_factory()
    page.wait_for_selector(".board-item", timeout=T)
    anim = page.evaluate("""() => { const s = document.createElement('div'); s.className = 'spin';
      document.body.appendChild(s); const a = getComputedStyle(s).animationName; s.remove(); return a; }""")
    assert anim == "none"
