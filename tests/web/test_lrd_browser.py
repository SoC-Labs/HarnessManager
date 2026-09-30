"""Lane LR-D in the browser: lease requests, force release and leaving the queue
(docs/LEASE_REQUESTS.md "UI"), over the T14 mock (tests/fakes/t14_lease_requests.py).

Three people use it: the requester (Request board, the waiting bar, Leave queue, Force),
the holder (the prompt, from any section) and the victim (the banner). The mock's fake
clock (``advance``) stands in for the two minutes; no test sleeps them. Each behaviour has
its negative twin. Nothing here reaches a hub.
"""

from __future__ import annotations

import re
import time

import pytest

from harness_manager.demo import BOARD_FIELDED, BOARD_USB
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser, pytest.mark.week_plan("hub_api", sim=True)]
T = 10_000
# The fake clock moves the hub's note, not the page's copy: the page sees it at its next
# lease read, which comes every 10 s while a request is out (docs: "Polling").
POLL = 15_000
APP = {"width": 1440, "height": 900}
HOLDER = "alice@lab-pc-07"
BOARD = BOARD_FIELDED           # the demo's lab board: N1 names it "mps3-01" (from the hub)
NAME = "mps3-01"                # what every lease text calls it
ADDR = "192.168.10.101:6900"    # TARGET in the `$ lease ...` lines (the CLI takes the address)


# --- helpers (test_lrd_screenshots.py uses them too) --------------------------------------------


def open_board(page, board_id=BOARD):
    page.locator(f'.board-item[data-board="{board_id}"]').click()
    page.locator('[data-action="open"]').click()
    page.wait_for_selector('[data-testid="fact-shell"]:not(:has-text("unknown"))', timeout=T)
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)


def section(page, key):
    """0.1.0's tab ``key`` where UI v2 put it (tests/web/nav.py)."""
    nav.section(page, key)


def reqs(daemon):
    return daemon.app.state.sim.requests


def wait_until(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def send_request(page, message="need it for the 15:00 demo"):
    page.locator('[data-testid="fact-hub"] [data-action="lease_request_open"]').click()
    form = page.locator('[data-testid="lease-request-form"]')
    expect(form).to_be_visible()
    if message:
        form.locator("textarea").fill(message)
    form.locator('[data-action="lease_request"]').click()
    expect(form).to_have_count(0)
    bar = page.locator('[data-testid="lease-request"]')
    expect(bar.locator('[data-testid="req-asked"]')).to_be_visible(timeout=T)
    return bar


def requester(page_factory, daemon, scheme="light", **kw):
    """A board leased to alice; this page asks for it."""
    daemon.app.state.sim.behind_hub(BOARD, lease="other")
    page = page_factory(scheme, **APP)
    open_board(page)
    return page, send_request(page, **kw)


def force_button(page):
    return page.locator('[data-testid="lease-request"] [data-action="lease_force_open"]')


def answered_then_ran_out(daemon, minutes=5):
    """The holder answered from Harness Manager (keep), and the keep ran out: a Harness
    Manager session holds it (D12), so the plain confirm is enough."""
    r = reqs(daemon)
    r.advance(BOARD, 121)
    r.answer(BOARD, "keep", minutes=minutes, message="one more run")
    r.advance(BOARD, 60 * minutes + 1)


# --- the requester -------------------------------------------------------------------------------


def test_request_board_replaces_queue_for_it_and_its_form_can_be_cancelled(page_factory, daemon):
    daemon.app.state.sim.behind_hub(BOARD, lease="other")
    page = page_factory(**APP)
    open_board(page)
    hub = page.locator('[data-testid="fact-hub"]')
    expect(hub.locator('[data-action="lease_request_open"]')).to_have_text("Request board")
    assert hub.locator('[data-action="lease_acquire"]').count() == 0     # no "Queue for it"
    # the twin: Cancel (and Escape) send nothing, and focus goes back to the button
    hub.locator('[data-action="lease_request_open"]').click()
    expect(page.locator("#lease-message")).to_be_focused()
    page.locator('[data-action="lease_request_cancel"]').click()
    expect(page.locator('[data-testid="lease-request-form"]')).to_have_count(0)
    expect(hub.locator('[data-action="lease_request_open"]')).to_be_focused()
    hub.locator('[data-action="lease_request_open"]').click()
    expect(page.locator("#lease-message")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator('[data-testid="lease-request-form"]')).to_have_count(0)
    assert reqs(daemon).outgoing == {}
    assert page.locator('[data-testid="lease-request"]').count() == 0


def test_a_request_shows_position_asked_countdown_and_its_command(page_factory, daemon):
    page, bar = requester(page_factory, daemon)
    expect(bar.locator('[data-testid="req-position"]')).to_have_text("position 1 in the queue")
    expect(bar.locator('[data-testid="req-asked"]')).to_contain_text("the holder has been asked")
    clock = bar.locator('[data-testid="req-countdown"]')
    expect(clock).to_contain_text(re.compile(r"[12]:[0-5]\d"))
    first = int(clock.get_attribute("data-left"))
    assert 110 <= first <= 120
    expect(clock).not_to_have_attribute("data-left", str(first), timeout=3000)   # it runs
    expect(bar).to_contain_text("“need it for the 15:00 demo”")
    expect(bar.locator('[data-testid="result-lease_req"]')).to_contain_text(
        f"$ lease request {ADDR} --message 'need it for the 15:00 demo'  (running")
    expect(page.locator('[data-testid="lease-requested"]')).to_contain_text("requested · position 1")
    expect(bar.locator('[data-action="lease_leave"]')).to_be_focused()        # keyboard: next step
    note = reqs(daemon).outgoing[BOARD]
    assert note["message"] == "need it for the 15:00 demo"
    # the twin: before the deadline Force is offered but disarmed, with the live reason
    expect(force_button(page)).to_have_attribute("aria-disabled", "true")
    expect(bar.locator('[data-testid="reason-lease_force"]')).to_contain_text(f"if {HOLDER} has not answered")


def test_the_countdown_reaching_zero_enables_force_without_the_event(page_factory, daemon):
    page, bar = requester(page_factory, daemon)
    r = reqs(daemon)
    r.announce_force = False               # only the page's own read at 0:00 can find it
    expect(force_button(page)).to_have_attribute("aria-disabled", "true")
    r.advance(BOARD, 117)              # the fake clock: 0:03 left
    clock = bar.locator('[data-testid="req-countdown"]')
    expect(clock).to_have_attribute("data-left", re.compile(r"^[0-3]$"), timeout=POLL)
    expect(clock).to_have_attribute("data-left", "0", timeout=T)
    expect(clock).to_contain_text("0:00")
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=T)
    expect(bar.locator('[data-testid="reason-lease_force"]')).to_have_count(0)
    expect(bar).to_have_class(re.compile(r"\bdue\b"))


def test_a_disarmed_force_says_why_and_runs_nothing(page_factory, daemon):
    page, bar = requester(page_factory, daemon)
    force_button(page).click(force=True)                                  # aria-disabled
    expect(page.locator('[data-testid="force-confirm"]')).to_have_count(0)      # no confirm
    expect(bar.locator('[data-testid="result-lease_force"]')).to_contain_text("Nothing was run.")
    expect(bar.locator('[data-testid="result-lease_force"]')).to_contain_text("(not run)")
    assert reqs(daemon).revokes == []


def test_force_confirm_is_red_names_the_holder_and_cancel_runs_nothing(page_factory, daemon):
    page, bar = requester(page_factory, daemon)
    answered_then_ran_out(daemon)                  # an HM holder: no name to type (D12)
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=POLL)
    expect(force_button(page)).to_have_text("Force release…")
    force_button(page).click()
    modal = page.locator('[data-testid="force-confirm"]')
    expect(modal).to_be_visible()
    assert modal.get_attribute("role") == "alertdialog"
    expect(modal).to_have_class(re.compile(r"\bdanger\b"))
    expect(modal.locator('[data-testid="force-what"]')).to_have_text(
        f"This kicks {HOLDER} off {NAME} now; anything they are running is interrupted.")
    expect(modal.locator('[data-action="force_cancel"]')).to_be_focused()   # the safe default
    expect(modal.locator('[data-testid="force-board-name"]')).to_have_count(0)
    modal.locator('[data-action="force_cancel"]').click()
    expect(modal).to_have_count(0)
    expect(force_button(page)).to_be_focused()
    force_button(page).click()
    expect(modal.locator('[data-action="force_cancel"]')).to_be_focused()
    page.keyboard.press("Escape")
    expect(modal).to_have_count(0)
    assert reqs(daemon).revokes == []
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text(f"leased to {HOLDER}")


def type_board_name(page, name=NAME):
    modal = page.locator('[data-testid="force-confirm"]')
    modal.locator('[data-testid="force-board-name"]').fill(name)
    return modal


def test_force_revokes_and_the_board_is_ours(page_factory, daemon):
    page, bar = requester(page_factory, daemon)
    reqs(daemon).advance(BOARD, 121)
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=POLL)
    force_button(page).click()
    type_board_name(page)                          # alice never answered: maybe a script (D12)
    page.locator('[data-testid="force-confirm"] [data-action="force_confirm"]').click()
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours", timeout=T)
    result = bar.locator('[data-testid="result-lease_force"]')
    expect(result).to_contain_text(f"$ lease force {ADDR} --confirm-board {NAME}  (rc 0")
    expect(result).to_contain_text(f"{HOLDER} was force-released")
    expect(bar.locator('[data-testid="req-title"]')).to_have_text(f"{NAME} is yours.")
    (revoke,) = reqs(daemon).revokes
    assert revoke["prior_holder"] == HOLDER and "no answer to a request made at" in revoke["reason"]
    bar.locator('[data-action="lease_request_dismiss"]').click()
    expect(page.locator('[data-testid="lease-request"]')).to_have_count(0)


def test_force_refused_by_the_daemon_shows_its_reason(page_factory, daemon):
    page, bar = requester(page_factory, daemon)
    r = reqs(daemon)
    r.advance(BOARD, 121)
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=POLL)
    r.refuse_force = "force is not available: carol@lab-pc-09 is ahead of you in the queue"
    force_button(page).click()
    type_board_name(page)
    page.locator('[data-testid="force-confirm"] [data-action="force_confirm"]').click()
    result = bar.locator('[data-testid="result-lease_force"]')
    expect(result).to_contain_text("(rc 15", timeout=T)
    expect(result).to_contain_text("REFUSED")
    expect(result).to_contain_text("carol@lab-pc-09 is ahead of you in the queue")
    assert r.revokes == []
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text(f"leased to {HOLDER}")


# --- D12: a holder that never answered may be a script: the board's name, typed ---------------------


def test_a_holder_that_never_answered_needs_the_board_name_typed(page_factory, daemon):
    page, bar = requester(page_factory, daemon)
    r = reqs(daemon)
    r.advance(BOARD, 121)
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=POLL)
    force_button(page).click()
    modal = page.locator('[data-testid="force-confirm"]')
    expect(modal.locator('[data-testid="force-script-why"]')).to_have_text(
        f"No Harness Manager session is known to hold {NAME}; it may be a script (a soak or "
        f"runner). Type {NAME} to force-release.")
    expect(modal.locator('[data-testid="force-script-reason"]')).to_contain_text("has answered")
    field = modal.locator('[data-testid="force-board-name"]')
    expect(field).to_be_focused()                  # typing is the confirmation
    go = modal.locator('[data-action="force_confirm"]')
    expect(go).to_have_attribute("aria-disabled", "true")          # nothing typed yet
    field.fill("mps3-0")
    expect(go).to_have_attribute("aria-disabled", "true")          # not the name yet
    go.click(force=True)
    field.press("Enter")
    expect(modal).to_be_visible()                  # neither the click nor Enter ran anything
    assert r.revokes == []
    field.fill("MPS3-01")                          # the name, in any case
    expect(go).not_to_have_attribute("aria-disabled", "true")
    field.press("Enter")
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours", timeout=T)
    expect(bar.locator('[data-testid="result-lease_force"]')).to_contain_text(
        f"$ lease force {ADDR} --confirm-board MPS3-01  (rc 0")
    (revoke,) = r.revokes
    assert revoke["prior_holder"] == HOLDER


def test_negative_twin_a_holder_that_answered_is_forced_without_typing(page_factory, daemon):
    page, bar = requester(page_factory, daemon)
    answered_then_ran_out(daemon)
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=POLL)
    force_button(page).click()
    modal = page.locator('[data-testid="force-confirm"]')
    expect(modal).to_be_visible()
    expect(modal.locator('[data-testid="force-script"]')).to_have_count(0)
    modal.locator('[data-action="force_confirm"]').click()
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours", timeout=T)
    expect(bar.locator('[data-testid="result-lease_force"]')).to_contain_text(
        f"$ lease force {ADDR} --yes  (rc 0")
    assert len(reqs(daemon).revokes) == 1


def test_not_at_the_head_force_stays_disarmed_with_the_daemons_reason(page_factory, daemon):
    daemon.app.state.sim.behind_hub(BOARD, lease="other")
    reqs(daemon).queue_ahead(BOARD, "carol@lab-pc-09")
    page = page_factory(**APP)
    open_board(page)
    bar = send_request(page)
    expect(bar.locator('[data-testid="req-position"]')).to_have_text("position 2 in the queue")
    reqs(daemon).advance(BOARD, 121)
    expect(bar.locator('[data-testid="req-countdown"]')).to_contain_text("0:00", timeout=POLL)
    expect(bar.locator('[data-testid="reason-lease_force"]')).to_contain_text(
        "carol@lab-pc-09 would get the board", timeout=T)
    expect(force_button(page)).to_have_attribute("aria-disabled", "true")


def test_a_keep_answer_shows_its_message_and_the_minutes_left(page_factory, daemon):
    page, bar = requester(page_factory, daemon)
    assert bar.locator('[data-testid="req-answer"]').count() == 0              # the twin
    reqs(daemon).answer(BOARD, "keep", minutes=15, message="finishing a run")
    answer = bar.locator('[data-testid="req-answer"]')
    expect(answer).to_contain_text(f"{HOLDER} is keeping it for 15 min", timeout=T)
    expect(answer).to_contain_text("“finishing a run”")
    expect(answer.locator('[data-testid="req-keep-left"]')).to_contain_text(re.compile(r"1[45] min left \(until \d\d:\d\d:\d\d\)"))
    assert bar.locator('[data-testid="req-countdown"]').count() == 0          # answered: no 2:00
    # D1: the answer is a phase; the request job waits on (we stay in the queue)
    expect(bar.locator('[data-testid="result-lease_req"]')).to_contain_text("the holder answered: you stay in the queue")
    expect(bar.locator('[data-testid="result-lease_req"]')).to_contain_text("(running")
    expect(page.locator('[data-testid="job-chip"]')).to_contain_text("lease request running")
    # kept: even past the 2:00, Force is not offered until the 15 min run out
    reqs(daemon).advance(BOARD, 125)
    expect(bar.locator('[data-testid="reason-lease_force"]')).to_contain_text("keep for 15 min", timeout=POLL)
    expect(force_button(page)).to_have_attribute("aria-disabled", "true")
    reqs(daemon).advance(BOARD, 15 * 60)
    expect(answer.locator('[data-testid="req-keep-left"]')).to_contain_text("their 15 min ran out", timeout=POLL)
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=POLL)


def test_a_release_answer_gives_us_the_board(page_factory, daemon):
    page, bar = requester(page_factory, daemon)
    reqs(daemon).answer(BOARD, "release")
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours", timeout=T)
    expect(bar.locator('[data-testid="result-lease_req"]')).to_contain_text(   # LEASE-BOARD
        re.compile(rf"{NAME} is yours: lease held on mps3_01 until "))
    assert reqs(daemon).revokes == []                                           # nothing forced


def test_leave_queue_withdraws_the_request_and_frees_the_board(page_factory, daemon, engine):
    page, bar = requester(page_factory, daemon)
    # UI v2: Reset DUT left the Overview (the Workbench's toolbar, Board > Recover)
    section(page, "power")
    tile = page.locator('[data-testid="board-page-recover"]')
    expect(tile.locator('[data-testid="reason-reset_dut"]').first).to_contain_text("your request is queued")
    bar.locator('[data-action="lease_leave"]').click()
    expect(bar.locator('[data-testid="result-lease_leave"]')).to_contain_text(
        f"$ lease leave {ADDR}  (rc 0", timeout=T)
    expect(bar.locator('[data-testid="result-lease_leave"]')).to_contain_text("you left the queue")
    expect(bar.locator('[data-testid="result-lease_req"]')).to_contain_text("the request is withdrawn", timeout=T)
    expect(bar.locator('[data-testid="result-lease_req"]')).not_to_contain_text("ACTION_FAILED")
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text(f"leased to {HOLDER}")
    expect(page.locator('[data-testid="fact-hub"] [data-action="lease_request_open"]')).to_be_visible(timeout=T)
    expect(page.locator('[data-testid="lease-queued"]')).to_have_count(0)      # no stale marker
    expect(page.locator('[data-testid="lease-requested"]')).to_have_count(0)
    # the board is free of the request job; the lease rule (FIX-PACK-4) now stops a reset
    expect(tile.locator('[data-testid="reason-reset_dut"]').first).to_contain_text(
        f"Reset DUT is for the lease holder only: {HOLDER}", timeout=T)
    assert reqs(daemon).outgoing == {}
    assert not engine.called("resets.reset")


# --- the holder --------------------------------------------------------------------------------


def holder_page(page_factory, daemon, scheme="light", where="consoles"):
    daemon.app.state.sim.behind_hub(BOARD, lease="mine")
    page = page_factory(scheme, **APP)
    open_board(page)
    section(page, where)
    return page


def test_the_holder_prompt_appears_from_a_non_overview_section(page_factory, daemon):
    page = holder_page(page_factory, daemon, where="power")
    assert page.locator('[data-testid="lease-wanted"]').count() == 0          # the twin
    rid = reqs(daemon).incoming(BOARD, by="bob@lab-pc-02", message="demo at 3")
    prompt = page.locator(f'[data-testid="lease-wanted"][data-request="{rid}"]')
    expect(prompt).to_be_visible(timeout=T)
    expect(nav.panel(page, "power")).to_be_visible()     # still on Power (Board > Recover)
    expect(prompt.locator('[data-testid="wanted-title"]')).to_have_text(f"bob@lab-pc-02 wants {NAME}: “demo at 3”")
    expect(prompt.locator('[data-testid="wanted-countdown"]')).to_contain_text(re.compile(r"[12]:[0-5]\d"))
    for m in (5, 15, 30, 60):
        expect(prompt.locator(f'[data-action="respond_keep_{m}"]')).to_have_text(f"{m} min")
    # and with the Activity drawer open too
    section(page, "activity")
    expect(prompt).to_be_visible()


def test_the_holder_keeps_it_with_a_message_and_sees_what_they_answered(page_factory, daemon):
    page = holder_page(page_factory, daemon)
    rid = reqs(daemon).incoming(BOARD, by="bob@lab-pc-02", message="demo at 3")
    prompt = page.locator('[data-testid="lease-wanted"]')
    expect(prompt).to_be_visible(timeout=T)
    prompt.locator('[data-testid="wanted-message"]').fill("ten more minutes")
    prompt.locator('[data-action="respond_keep_15"]').click()
    answered = page.locator(f'[data-testid="lease-answered"][data-request="{rid}"]')
    expect(answered).to_contain_text("You answered bob@lab-pc-02: keep mps3-01 for 15 min", timeout=T)
    expect(answered).to_contain_text("“ten more minutes”")
    expect(answered).to_contain_text(re.compile(r"They may force-release it after \d\d:\d\d:\d\d"))
    expect(answered).to_contain_text(f"$ lease respond {ADDR} {rid} --keep 15 --message 'ten more minutes'  (rc 0")
    expect(prompt).to_have_count(0)
    assert reqs(daemon).answers[BOARD][rid] == {
        "answer": "keep", "minutes": 15, "message": "ten more minutes",
        "at": reqs(daemon).answers[BOARD][rid]["at"]}
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("lease yours")   # kept
    # a reload (this page's memory gone) still knows: D5, the answer rides on the request
    page.reload()
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    expect(page.locator('[data-testid="lease-answered"]')).to_be_visible(timeout=T)
    assert page.locator('[data-testid="lease-wanted"]').count() == 0


def test_the_holder_releases_now_and_the_board_goes_to_the_requester(page_factory, daemon):
    page = holder_page(page_factory, daemon)
    rid = reqs(daemon).incoming(BOARD, by="bob@lab-pc-02", message="")
    prompt = page.locator('[data-testid="lease-wanted"]')
    expect(prompt.locator('[data-testid="wanted-title"]')).to_contain_text("(no message)", timeout=T)
    prompt.locator('[data-action="respond_release"]').click()
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("leased to bob@lab-pc-02", timeout=T)
    answered = page.locator(f'[data-testid="lease-answered"][data-request="{rid}"]')
    expect(answered).to_contain_text(f"You released {NAME} to bob@lab-pc-02")
    expect(prompt).to_have_count(0)
    answered.locator('[data-action="lease_answer_dismiss"]').click()
    expect(answered).to_have_count(0)
    assert reqs(daemon).answers[BOARD][rid]["answer"] == "release"


def test_a_malformed_answer_is_never_sent_keep_needs_one_of_the_four_durations(page_factory, daemon):
    # The page offers exactly 5 / 15 / 30 / 60: nothing else can be clicked.
    page = holder_page(page_factory, daemon)
    reqs(daemon).incoming(BOARD)
    prompt = page.locator('[data-testid="lease-wanted"]')
    expect(prompt).to_be_visible(timeout=T)
    keeps = prompt.locator('[data-action^="respond_keep_"]')
    assert [keeps.nth(i).get_attribute("data-action") for i in range(keeps.count())] == [
        "respond_keep_5", "respond_keep_15", "respond_keep_30", "respond_keep_60"]


# --- the victim ------------------------------------------------------------------------------------


def test_the_victim_banner_persists_across_a_reload_until_dismissed(page_factory, daemon):
    page = holder_page(page_factory, daemon, where="program")
    assert page.locator('[data-testid="lease-taken"]').count() == 0            # the twin
    t = reqs(daemon).taken(BOARD, by="bob@lab-pc-02")
    banner = page.locator('[data-testid="lease-taken"]')
    expect(banner).to_contain_text(re.compile(
        rf"{NAME} was force-released by bob@lab-pc-02 at \d\d:\d\d:\d\d: force-released by bob@lab-pc-02 via Harness Manager"), timeout=T)
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("leased to bob@lab-pc-02", timeout=T)
    section(page, "activity")
    row = page.locator('[data-testid="activity-table"] tr[data-level="error"]', has_text="was force-released by bob@lab-pc-02")
    expect(row).to_have_count(1)
    page.reload()
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    expect(banner).to_be_visible(timeout=T)                                    # still there
    expect(banner).to_contain_text(t["reason"])
    banner.locator('[data-action="lease_taken_dismiss"]').click()
    expect(banner).to_have_count(0)
    # D11: the daemon forgot it too (GET /lease says taken: null)
    assert wait_until(lambda: BOARD not in reqs(daemon).last_taken)
    page.reload()
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    page.wait_for_timeout(600)
    expect(banner).to_have_count(0)                                            # dismissed stays
    # a new force release is a new banner
    daemon.app.state.sim.behind_hub(BOARD, lease="mine")
    time.sleep(1.1)                                                            # a new `at`
    reqs(daemon).taken(BOARD, by="carol@lab-pc-09")
    expect(banner).to_contain_text("force-released by carol@lab-pc-09", timeout=T)


def test_a_daemon_without_lease_requests_still_queues_and_leaves(page_factory, daemon):
    # The routes land with LR-C; a daemon from before them answers 404 "no such endpoint".
    # The page then queues plainly (POST /lease) and leaves with DELETE /lease.
    daemon.app.state.sim.behind_hub(BOARD, lease="other")
    page = page_factory(**APP)
    missing = {"no": True}

    def gone(route):
        if missing["no"]:
            route.fulfill(status=404, json={"ok": False, "error": {
                "code": 3, "name": "ABSENT", "message": "no such endpoint: POST /api/v1/boards/.../lease/request"}})
        else:
            route.continue_()

    page.route(re.compile(r"/lease/(request|queue)$"), gone)
    open_board(page)
    page.locator('[data-testid="fact-hub"] [data-action="lease_request_open"]').click()
    page.locator('[data-testid="lease-request-form"] [data-action="lease_request"]').click()
    bar = page.locator('[data-testid="lease-request"]')
    expect(bar.locator('[data-testid="result-lease_req"]')).to_contain_text("queued without asking the holder", timeout=T)
    assert reqs(daemon).outgoing == {}                     # no request note: a plain queue
    bar.locator('[data-action="lease_leave"]').click()
    expect(bar.locator('[data-testid="result-lease_leave"]')).to_contain_text("you left the queue", timeout=T)
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text(f"leased to {HOLDER}")
    # the twin: with the routes there, the same click writes a request note
    missing["no"] = False
    bar.locator('[data-action="lease_request_dismiss"]').click()
    send_request(page)
    assert wait_until(lambda: BOARD in reqs(daemon).outgoing)


def test_an_unnamed_board_is_called_by_its_address_and_a_bare_revoke_has_no_reason(page_factory, daemon):
    # N1: no name from boards.toml, the harness or the hub -> the address. LR-A: a revoke
    # with no note on the hub has by = the next holder and reason "".
    daemon.app.state.sim.behind_hub(BOARD_USB, lease="mine")
    page = page_factory(**APP)
    open_board(page, BOARD_USB)
    reqs(daemon).incoming(BOARD_USB, by="bob@lab-pc-02", message="demo")
    expect(page.locator('[data-testid="wanted-title"]')).to_have_text(
        "bob@lab-pc-02 wants 192.168.10.102:6900: “demo”", timeout=T)
    reqs(daemon).taken(BOARD_USB, by="bob@lab-pc-02", reason="")
    title = page.locator('[data-testid="lease-taken"] strong')
    expect(title).to_have_text(re.compile(
        r"^192\.168\.10\.102:6900 was force-released by bob@lab-pc-02 at \d\d:\d\d:\d\d$"), timeout=T)
    # the twin: the named board says its name, never the address
    assert "mps3-01" not in title.inner_text()


# --- decisions D1-D8 (docs/LEASE_REQUESTS.md) ------------------------------------------------------


def test_the_confirm_names_the_physical_board_the_revoke_acts_on(page_factory, daemon):
    # D4: GET /lease.board ("mps3_01") differs from the target ("mps3_01_pl"): say so.
    page, bar = requester(page_factory, daemon)
    reqs(daemon).advance(BOARD, 121)
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=POLL)
    force_button(page).click()
    modal = page.locator('[data-testid="force-confirm"]')
    expect(modal.locator('[data-testid="force-board"]')).to_have_text(
        "It revokes board mps3_01 (the hub target mps3_01_pl is part of it).")
    modal.locator('[data-action="force_cancel"]').click()
    # the twin: a board named like its target needs no extra line
    reqs(daemon).set_board(BOARD, "mps3_01_pl")
    force_button(page).click()
    expect(modal).to_be_visible()
    expect(modal.locator('[data-testid="force-board"]')).to_have_count(0, timeout=POLL)   # next read
    modal.locator('[data-action="force_cancel"]').click()
    assert reqs(daemon).revokes == []


def test_a_time_left_refusal_says_when_force_opens(page_factory, daemon):
    # D3: 422 UNAVAILABLE carries error.data {time_left_s, deadline_at}. The page never sends
    # force early itself, so the refusal is played at the route (a page behind the hub).
    page, bar = requester(page_factory, daemon)
    reqs(daemon).advance(BOARD, 121)
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=POLL)
    page.route(re.compile(r"/lease/force$"), lambda route: route.fulfill(status=422, json={
        "ok": False, "error": {"code": 12, "name": "UNAVAILABLE",
                               "message": "lease_force is unavailable: alice@lab-pc-07 has 95 s left to answer",
                               "hint": "force-release opens at the deadline",
                               "data": {"time_left_s": 95, "deadline_at": "2026-09-24T12:01:35+00:00"}}}))
    force_button(page).click()
    type_board_name(page)
    page.locator('[data-testid="force-confirm"] [data-action="force_confirm"]').click()
    result = bar.locator('[data-testid="result-lease_force"]')
    expect(result).to_contain_text("(rc 12", timeout=T)
    expect(result).to_contain_text("force-release opens in 1:35 (at ")
    assert reqs(daemon).revokes == []


def test_an_answer_given_elsewhere_shows_as_answered_not_as_a_prompt(page_factory, daemon):
    # D5: the holder answered from the CLI (or another page): incoming[].answer carries it.
    page = holder_page(page_factory, daemon)
    rid = reqs(daemon).incoming(BOARD, by="bob@lab-pc-02", message="demo at 3")
    expect(page.locator(f'[data-testid="lease-wanted"][data-request="{rid}"]')).to_be_visible(timeout=T)
    reqs(daemon).answer_incoming(BOARD, rid, "keep", minutes=30, message="from the CLI")
    answered = page.locator(f'[data-testid="lease-answered"][data-request="{rid}"]')
    expect(answered).to_contain_text(f"You answered bob@lab-pc-02: keep {NAME} for 30 min", timeout=POLL)
    expect(answered).to_contain_text("“from the CLI”")
    expect(page.locator('[data-testid="lease-wanted"]')).to_have_count(0)


# --- hub mode over fpgahub REST (T8, docs/HUB_MODE.md) ------------------------------------------------

NOTES_OFF = "the hub is reached over its REST API, which has no request notes"
ROLE_OFF = "force-release needs an admin token; this hub token's role is write"


def test_over_rest_the_holder_prompt_has_no_message_box_and_no_keep_and_says_why(page_factory, daemon):
    reqs(daemon).notes_reason = NOTES_OFF
    page = holder_page(page_factory, daemon)
    reqs(daemon).incoming(BOARD, by="bob@lab-pc-02", message="never arrives")
    prompt = page.locator('[data-testid="lease-wanted"]')
    expect(prompt.locator('[data-testid="wanted-title"]')).to_have_text(f"bob@lab-pc-02 wants {NAME}.", timeout=T)
    assert prompt.locator('[data-testid="wanted-message"]').count() == 0
    assert prompt.locator('[data-action^="respond_keep_"]').count() == 0
    expect(prompt.locator('[data-testid="wanted-notes-off"]')).to_contain_text(NOTES_OFF)
    expect(prompt.locator('[data-testid="wanted-notes-off"]')).to_contain_text("Release now still works")
    prompt.locator('[data-action="respond_release"]').click()
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("leased to bob@lab-pc-02", timeout=T)


def test_with_notes_the_holder_prompt_keeps_its_message_box_and_keep_buttons(page_factory, daemon):
    page = holder_page(page_factory, daemon)                   # the twin of the one above
    reqs(daemon).incoming(BOARD)
    prompt = page.locator('[data-testid="lease-wanted"]')
    expect(prompt.locator('[data-testid="wanted-message"]')).to_be_visible(timeout=T)
    expect(prompt.locator('[data-action^="respond_keep_"]')).to_have_count(4)
    assert prompt.locator('[data-testid="wanted-notes-off"]').count() == 0


def test_over_rest_the_request_form_has_no_message_and_says_why(page_factory, daemon):
    daemon.app.state.sim.behind_hub(BOARD, lease="other")
    reqs(daemon).notes_reason = NOTES_OFF
    page = page_factory(**APP)
    open_board(page)
    page.locator('[data-testid="fact-hub"] [data-action="lease_request_open"]').click()
    form = page.locator('[data-testid="lease-request-form"]')
    expect(form.locator('[data-testid="request-notes-off"]')).to_contain_text(NOTES_OFF)
    assert form.locator("textarea").count() == 0
    expect(form.locator('[data-action="lease_request"]')).to_be_focused()
    form.locator('[data-action="lease_request"]').click()
    bar = page.locator('[data-testid="lease-request"]')
    expect(bar.locator('[data-testid="result-lease_req"]')).to_contain_text(f"$ lease request {ADDR}  (running", timeout=T)
    assert wait_until(lambda: BOARD in reqs(daemon).outgoing)
    assert reqs(daemon).outgoing[BOARD]["message"] == ""


def test_over_rest_a_write_token_cannot_force_and_the_page_says_why(page_factory, daemon):
    daemon.app.state.sim.behind_hub(BOARD, lease="other")
    reqs(daemon).revoke_reason = ROLE_OFF
    page = page_factory(**APP)
    open_board(page)
    bar = send_request(page)
    # the role, not the countdown, is the reason: even while the 2:00 run
    expect(bar.locator('[data-testid="reason-lease_force"]')).to_contain_text(ROLE_OFF, timeout=T)
    reqs(daemon).advance(BOARD, 121)
    expect(bar.locator('[data-testid="req-countdown"]')).to_contain_text("0:00", timeout=POLL)
    page.wait_for_timeout(2500)                                # the reads at 0:00 are in
    expect(force_button(page)).to_have_attribute("aria-disabled", "true")
    expect(bar.locator('[data-testid="reason-lease_force"]')).to_contain_text(ROLE_OFF)
    force_button(page).click(force=True)
    expect(page.locator('[data-testid="force-confirm"]')).to_have_count(0)
    expect(bar.locator('[data-testid="result-lease_force"]')).to_contain_text("Nothing was run.")
    assert reqs(daemon).revokes == []
    # the twin: an admin token, and Force opens
    reqs(daemon).revoke_reason = ""
    expect(force_button(page)).not_to_have_attribute("aria-disabled", "true", timeout=POLL)



def test_dismiss_over_a_daemon_without_the_d11_route_is_remembered_by_this_browser(page_factory, daemon):
    # A daemon from before D11 answers 404: the banner still goes, and stays gone on reload.
    page = holder_page(page_factory, daemon)
    page.route(re.compile(r"/lease/taken$"), lambda route: route.fulfill(status=404, json={
        "ok": False, "error": {"code": 3, "name": "ABSENT", "message": "no such endpoint"}}))
    reqs(daemon).taken(BOARD, by="bob@lab-pc-02")
    banner = page.locator('[data-testid="lease-taken"]')
    banner.locator('[data-action="lease_taken_dismiss"]').click()
    expect(banner).to_have_count(0)
    assert BOARD in reqs(daemon).last_taken                     # the daemon still has it
    page.reload()
    page.wait_for_selector('[data-testid="lease-chip"]', timeout=T)
    page.wait_for_timeout(600)
    expect(banner).to_have_count(0)                              # this browser remembers
    # the twin: another browser (no memory) still sees it, since the daemon kept it
    other = page_factory(**APP)                                  # the board is open already
    other.locator(f'.board-item[data-board="{BOARD}"]').click()
    expect(other.locator('[data-testid="lease-taken"]')).to_be_visible(timeout=T)


def test_a_new_holder_is_asked_again_and_the_page_shows_the_new_deadline(page_factory, daemon):
    # D9: the board passes to carol while we wait; she was never asked, so the request goes
    # to her with a fresh 2:00, and force waits for that.
    page, bar = requester(page_factory, daemon)
    r = reqs(daemon)
    r.advance(BOARD, 110)                                       # 0:10 left for alice
    expect(bar.locator('[data-testid="req-countdown"]')).to_have_attribute(
        "data-left", re.compile(r"^([0-9]|10)$"), timeout=POLL)
    assert bar.locator('[data-testid="req-reasked"]').count() == 0          # the twin
    r.new_holder(BOARD, "carol@lab-pc-09")
    reasked = bar.locator('[data-testid="req-reasked"]')
    expect(reasked).to_contain_text(f"{NAME} passed to carol@lab-pc-09", timeout=POLL)
    expect(reasked).to_contain_text(re.compile(r"a new 2:00 runs to \d\d:\d\d:\d\d"))
    clock = bar.locator('[data-testid="req-countdown"]')
    expect(clock).to_contain_text(re.compile(r"^\s*1:[5]\d|^\s*2:00"))
    expect(page.locator('[data-testid="lease-chip"]')).to_contain_text("leased to carol@lab-pc-09")
    # past alice's old deadline, force stays shut: carol has her own 2:00
    page.wait_for_timeout(1500)
    expect(force_button(page)).to_have_attribute("aria-disabled", "true")
    expect(bar.locator('[data-testid="reason-lease_force"]')).to_contain_text("carol@lab-pc-09 has not answered")
    section(page, "activity")
    expect(page.locator('[data-testid="activity-table"]')).to_contain_text("passed to carol@lab-pc-09, who had not been asked")
