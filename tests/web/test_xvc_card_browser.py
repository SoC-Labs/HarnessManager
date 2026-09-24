"""Lane XVC-UI in the browser: the Debug section's XVC card (docs/design/XVC_DEBUG.md 4.1,
docs/API.md "Fabric debug over XVC"), clicks only, over the T14 mock
(``tests/fakes/x3_mock_xvc.py``: the real ``XvcStatus`` and Tcl, nothing listening).

The mock's knobs stand in for the world outside the page (another client takes the board's
slot, Vivado attaches, the hub lease is someone else's); every action on the page is a
click. ``test_xvc_card_real_daemon.py`` runs the round trip and a swap over the real daemon.
Each behaviour has its negative twin. Nothing reaches a board, a hub or Vivado.
"""

from __future__ import annotations

import pytest

from harness_manager.core.events import Event
from harness_manager.demo import BOARD_FIELDED, BOARD_USB, FIELDED_FEATURES

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser, pytest.mark.week_plan("xvc_api", sim=True)]
T = 10_000
APP = {"width": 1440, "height": 1100}
BOARD = BOARD_FIELDED
ADDR = "192.168.10.101:6900"
SCOPE = ("XVC reaches the reconfigurable partition's debug chain (Debug Bridge, debug hub and "
         "ILAs of the loaded design). It never gives whole-device JTAG.")
ILA = {"rm_id": "0x0100000A", "rm_name": "nanosoc_ila"}


# --- helpers (test_xvc_screenshots.py and test_xvc_card_real_daemon.py use them too) ------------


def xvc_board(engine, bid=BOARD, *, linux=False, lock=False, **ident):
    """A demo board whose harness serves XVC on the Debug Bridge (``xvc_dbgbr``)."""
    feats = (*FIELDED_FEATURES, "xvc_dbgbr", *(("xvc_lock",) if lock else ()))
    extra = {"harness_impl": "linux"} if linux else {}
    engine._set_identity(bid, features=feats, **{**ILA, **ident}, **extra)


def open_board(page, bid=BOARD):
    page.locator(f'.board-item[data-board="{bid}"]').click()
    if page.locator('[data-action="open"]').count():
        page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)


def to_debug(page):
    page.locator('[data-section="debug"]').click()
    page.wait_for_selector('[data-testid="xvc-card"]', timeout=T)


def card(page):
    return page.locator('[data-testid="xvc"]')


def state_is(page, state, timeout=T):
    expect(card(page)).to_have_attribute("data-state", state, timeout=timeout)


def button(page, action):
    return page.locator(f'[data-testid="xvc-card"] [data-action="{action}"]')


def by_id(page, name):
    return page.locator(f'[data-testid="{name}"]')


def debug_page(page_factory, scheme="light", bid=BOARD):
    page = page_factory(scheme, **APP)
    open_board(page, bid)
    to_debug(page)
    return page


def sim(daemon):
    return daemon.app.state.xvc


def colours(page, selector, token="--held"):
    """(the element's colour, the token's colour), both as the browser computes them."""
    return page.evaluate("""([sel, token]) => {
      const el = document.querySelector(sel);
      const probe = document.createElement('span');
      probe.style.color = `var(${token})`;
      document.body.appendChild(probe);
      const want = getComputedStyle(probe).color;
      probe.remove();
      return [el ? getComputedStyle(el).color : null, want];
    }""", [selector, token])


# --- open, attached, close -------------------------------------------------------------------------


def test_open_attached_close(page_factory, daemon, engine):
    xvc_board(engine)
    page = debug_page(page_factory)
    expect(page.locator('[data-testid="xvc-card"] .card-sub')).to_have_text(SCOPE)
    state_is(page, "down")
    # the twin, before: nothing is open, so Close says why and nobody is attached
    expect(by_id(page, "reason-xvc_close")).to_have_text("no XVC session is open")
    expect(button(page, "xvc_close")).to_have_attribute("aria-disabled", "true")
    expect(by_id(page, "xvc-attached")).to_have_text("-")
    button(page, "xvc_open").click()
    state_is(page, "ready")
    st = sim(daemon).status(BOARD)
    expect(by_id(page, "xvc-url")).to_contain_text(f"localhost:{st.hw_server_port}")
    expect(by_id(page, "xvc-result")).to_contain_text(f"$ xvc open {ADDR}")
    expect(by_id(page, "xvc-attached")).to_have_text("nobody yet")
    expect(by_id(page, "reason-xvc_open")).to_have_text("the session is already open")
    sim(daemon).attach(BOARD, command="hw_server -q -p0 -s TCP:127.0.0.1:23601")
    state_is(page, "attached")
    expect(by_id(page, "xvc-attached")).to_contain_text(f"pid {st.hw_server_pid}")
    expect(by_id(page, "xvc-attached")).to_contain_text("Harness Manager's hw_server")
    expect(by_id(page, "xvc-attached-cmd")).to_have_text("hw_server -q -p0 -s TCP:127.0.0.1:23601")
    expect(page.locator('[data-testid="xvc-card"] .card-sub')).to_have_text(SCOPE)   # always
    button(page, "xvc_close").click()
    state_is(page, "down")
    expect(by_id(page, "xvc-attached")).to_have_text("-")
    expect(by_id(page, "xvc-url")).to_have_text("open a session first")
    assert sim(daemon).sessions == {}
    assert page.errors == []


def test_negative_twin_an_image_without_the_debug_bridge_server_cannot_open(page_factory,
                                                                             daemon, engine):
    engine._set_identity(BOARD, features=(*FIELDED_FEATURES, "xvc_jtagbb"))
    page = debug_page(page_factory)
    state_is(page, "down")
    expect(by_id(page, "xvc-reason")).to_contain_text("drives jtag_bb (the Identify path)")
    expect(by_id(page, "reason-xvc_open")).to_contain_text("Cannot: 2542 on this image drives jtag_bb")
    button(page, "xvc_open").click(force=True)        # an interlock: nothing is sent
    expect(by_id(page, "xvc-result")).to_contain_text("Nothing was run.")
    expect(page.locator('[data-testid="xvc-card"] .card-sub')).to_have_text(SCOPE)
    assert sim(daemon).sessions == {}


# --- held -------------------------------------------------------------------------------------------


def test_held_by_another_client_is_shown_in_the_held_colour(page_factory, daemon, engine):
    xvc_board(engine)
    sim(daemon).hold_slot(BOARD, who="alice's Vivado on the hub")
    page = debug_page(page_factory)
    button(page, "xvc_open").click()
    state_is(page, "held")
    held = by_id(page, "xvc-held")
    expect(held).to_contain_text("Held by another client")
    expect(held).to_contain_text("alice's Vivado on the hub")
    expect(by_id(page, "xvc-state")).to_have_attribute("data-level", "held")
    got, want = colours(page, '[data-testid="xvc-held"]')
    assert got == want, (got, want)
    got, want = colours(page, '[data-testid="xvc-state"]')
    assert got == want, (got, want)
    expect(by_id(page, "xvc-result")).to_contain_text("HELD")
    # An open session that loses the slot to another client is held too.
    sim(daemon).held_by.clear()
    button(page, "xvc_open").click()
    state_is(page, "ready")
    expect(by_id(page, "xvc-held")).to_have_count(0)
    sim(daemon).slot_taken(BOARD, who="bob's hw_server")
    state_is(page, "held")
    expect(by_id(page, "xvc-held")).to_contain_text("bob's hw_server")


def test_negative_twin_a_free_slot_is_not_held(page_factory, daemon, engine):
    xvc_board(engine)
    page = debug_page(page_factory)
    button(page, "xvc_open").click()
    state_is(page, "ready")
    expect(by_id(page, "xvc-held")).to_have_count(0)
    expect(by_id(page, "xvc-state")).to_have_attribute("data-level", "ok")
    got, held = colours(page, '[data-testid="xvc-state"]')
    assert got != held


# --- the lease holder only (X6) --------------------------------------------------------------------


def test_not_the_lease_holder_disables_the_buttons_with_the_reason(page_factory, daemon, engine):
    xvc_board(engine)
    daemon.app.state.sim.behind_hub(BOARD, lease="other", holder="alice@lab-pc-07")
    page = debug_page(page_factory)
    why = "XVC is for the lease holder only: alice@lab-pc-07 holds this board"
    expect(by_id(page, "reason-xvc_open")).to_have_text(why, timeout=T)
    expect(by_id(page, "reason-xvc_close")).to_have_text(why)
    expect(button(page, "xvc_open")).to_have_attribute("aria-disabled", "true")
    expect(button(page, "xvc_close")).to_have_attribute("aria-disabled", "true")
    button(page, "xvc_open").click(force=True)        # an interlock: nothing is sent
    expect(by_id(page, "xvc-result")).to_contain_text("Nothing was run.")
    state_is(page, "down")
    assert sim(daemon).sessions == {}


def test_negative_twin_the_lease_holder_opens(page_factory, daemon, engine):
    xvc_board(engine)
    daemon.app.state.sim.behind_hub(BOARD, lease="mine")
    page = debug_page(page_factory)
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours", timeout=T)
    expect(by_id(page, "reason-xvc_open")).to_have_count(0)
    button(page, "xvc_open").click()
    state_is(page, "ready")
    assert BOARD in sim(daemon).sessions


# --- the bare-metal warning (X6) ---------------------------------------------------------------------


def test_bare_metal_xvc_warns_it_is_unauthenticated(page_factory, daemon, engine):
    xvc_board(engine)
    page = debug_page(page_factory)
    expect(by_id(page, "xvc-unauth")).to_contain_text("XVC on this harness is unauthenticated")
    expect(by_id(page, "xvc-static")).to_have_text(
        "unavailable: the bare-metal static has no MIG debug hub")
    expect(by_id(page, "xvc-static")).to_have_attribute("data-available", "false")


def test_negative_twin_a_linux_harness_with_the_xvc_lock_has_no_warning(page_factory, daemon,
                                                                       engine):
    xvc_board(engine, linux=True, lock=True)
    page = debug_page(page_factory)
    button(page, "xvc_open").click()
    state_is(page, "ready")
    expect(by_id(page, "xvc-unauth")).to_have_count(0)
    expect(by_id(page, "xvc-static")).to_have_attribute("data-available", "true")
    expect(by_id(page, "xvc-static")).to_contain_text("config_rm_greybox_static.ltx")
    # ...and without the lock a Linux harness still warns
    button(page, "xvc_close").click()
    state_is(page, "down")
    xvc_board(engine, linux=True, lock=False)
    page.reload()
    open_board(page)
    to_debug(page)
    expect(by_id(page, "xvc-unauth")).to_contain_text("unauthenticated")


# --- the Vivado Tcl ---------------------------------------------------------------------------------


def copy_tcl(page):
    button(page, "xvc-copy-tcl").click()
    expect(button(page, "xvc-copy-tcl")).to_contain_text("Copied")
    return page.evaluate("navigator.clipboard.readText()")


def test_the_tcl_copies_the_snippet_for_the_open_session(page_factory, daemon, engine):
    xvc_board(engine)
    page = debug_page(page_factory)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    button(page, "xvc_open").click()
    state_is(page, "ready")
    port = sim(daemon).status(BOARD).hw_server_port
    expect(by_id(page, "xvc-tcl")).to_contain_text(f"connect_hw_server -url localhost:{port}")
    text = copy_tcl(page)
    assert f"connect_hw_server -url localhost:{port}\nopen_hw_target\n" in text
    assert "set_property PROBES.FILE {/tmp/harness-manager-mock/nanosoc_ila.ltx}" in text
    assert text.startswith("# XVC here is scoped to the reconfigurable partition's debug chain")


def test_negative_twin_before_open_the_tcl_is_a_preview_and_byo_opens_the_relay(page_factory,
                                                                              daemon, engine):
    xvc_board(engine)
    page = debug_page(page_factory)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    expect(by_id(page, "xvc-tcl")).to_contain_text("<H: run `xvc open` first>")
    assert "localhost:<H" in copy_tcl(page)
    page.locator('[data-testid="xvc-byo"] input').check()
    expect(by_id(page, "xvc-tcl")).to_contain_text("open_hw_target -xvc_url 127.0.0.1:<R>")
    button(page, "xvc_open").click()
    state_is(page, "ready")
    relay = sim(daemon).status(BOARD).relay_port
    expect(by_id(page, "xvc-tcl")).to_contain_text(f"open_hw_target -xvc_url 127.0.0.1:{relay}")
    text = copy_tcl(page)
    assert "connect_hw_server -url localhost:3121" in text and "localhost:<H" not in text
    assert sim(daemon).status(BOARD).mode == "byo"
    expect(page.locator('[data-testid="xvc-byo"] input')).to_be_disabled()


# --- the probes file (.ltx) --------------------------------------------------------------------------


def test_download_ltx_saves_the_probes_file(page_factory, daemon, engine, tmp_path):
    xvc_board(engine)
    page = debug_page(page_factory)
    expect(by_id(page, "xvc-ltx")).to_contain_text("nanosoc_ila.ltx", timeout=T)
    expect(by_id(page, "xvc-ltx-which")).to_contain_text("partition (RM)")
    with page.expect_download(timeout=T) as dl:
        button(page, "xvc-ltx-preferred").click()
    assert dl.value.suggested_filename == "nanosoc_ila.ltx"
    saved = tmp_path / "got.ltx"
    dl.value.save_as(saved)
    assert saved.read_bytes() == sim(daemon).ltx_body
    expect(by_id(page, "xvc-downloaded")).to_contain_text("Saved nanosoc_ila.ltx")
    # X5: once the mint stages a full-design file, that one is offered
    sim(daemon).stage_full(BOARD)
    button(page, "xvc_open").click()
    state_is(page, "ready")
    expect(by_id(page, "xvc-ltx-which")).to_contain_text("full design")
    with page.expect_download(timeout=T) as dl:
        button(page, "xvc-ltx-preferred").click()
    assert dl.value.suggested_filename == "nanosoc_ila_full.ltx"


def test_negative_twin_a_design_without_ilas_has_no_probes_file(page_factory, daemon, engine):
    xvc_board(engine, rm_id="0x00000000", rm_name="greybox")
    page = debug_page(page_factory)
    button(page, "xvc_open").click()
    state_is(page, "ready")
    expect(by_id(page, "xvc-ltx")).to_have_text("unavailable: the greybox has no ILAs")
    expect(button(page, "xvc-ltx-preferred")).to_have_count(0)
    expect(by_id(page, "xvc-static")).to_have_attribute("data-available", "false")


# --- a swap -------------------------------------------------------------------------------------------


def test_a_swap_shows_swapping_then_re_attached(page_factory, daemon, engine):
    xvc_board(engine)
    page = debug_page(page_factory)
    button(page, "xvc_open").click()
    state_is(page, "ready")
    sim(daemon).attach(BOARD)
    state_is(page, "attached")
    engine.bus.publish(Event("deploy.started", BOARD, {"overlay": "nanosoc_ila_b"}))
    state_is(page, "swapping")
    expect(by_id(page, "xvc-swapping")).to_contain_text("Swapping...")
    expect(by_id(page, "xvc-attached")).to_have_text("nobody yet")
    engine._set_identity(BOARD, rm_id="0x0100000B", rm_name="nanosoc_ila_b")
    engine.bus.publish(Event("deploy.done", BOARD, {"verified": True, "rm_id": "0x0100000B"}))
    state_is(page, "ready")
    expect(by_id(page, "xvc-swapping")).to_have_count(0)
    expect(by_id(page, "xvc-reattached")).to_contain_text("Re-attached on nanosoc_ila_b")
    expect(by_id(page, "xvc-design")).to_contain_text("nanosoc_ila_b")
    expect(by_id(page, "xvc-tcl")).to_contain_text("nanosoc_ila_b.ltx")


def test_negative_twin_an_unverified_swap_closes_and_does_not_re_attach(page_factory, daemon,
                                                                       engine):
    xvc_board(engine)
    page = debug_page(page_factory)
    button(page, "xvc_open").click()
    state_is(page, "ready")
    engine.bus.publish(Event("deploy.started", BOARD, {"overlay": "nanosoc_ila_b"}))
    state_is(page, "swapping")
    engine.bus.publish(Event("deploy.done", BOARD, {"verified": False, "rm_id": "0x0100000B"}))
    state_is(page, "down")
    expect(by_id(page, "xvc-reattached")).to_have_count(0)
    expect(by_id(page, "xvc-swapping")).to_have_count(0)
    expect(by_id(page, "xvc-detail")).to_contain_text("the swap was not verified")


def test_a_swap_by_clicks_in_program_re_attaches_on_the_new_design(page_factory, daemon, engine):
    """The whole path by clicks: open XVC, program another RM, come back to Debug."""
    xvc_board(engine, BOARD_USB)
    page = debug_page(page_factory, bid=BOARD_USB)
    button(page, "xvc_open").click()
    state_is(page, "ready")
    page.locator('[data-section="program"]').click()
    page.locator('[data-overlay="led"]').click()
    page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
    page.locator('[data-testid="arm-program"] input').check()
    page.locator('[data-action="program"]').click()
    expect(page.locator('[data-testid="deploy-outcome"]')).to_have_attribute("data-state", "done",
                                                                             timeout=T)
    to_debug(page)
    state_is(page, "ready")
    expect(by_id(page, "xvc-reattached")).to_contain_text("Re-attached on led")
