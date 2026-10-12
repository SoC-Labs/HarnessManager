"""HUB-CLAIM in the browser: Board > Access on a board behind a hub.

identify (UDP 6899) does not cross the SSH tunnel, so ``info`` never knows a hub board's claim by
itself. The SSH claim card asks the hub when it loads (``GET /claim?hub=true``: identify run on
the hub over ssh, never the lease), and "Check through the hub" asks now (``?refresh=true``).
Over the T14 mock (``tests/fakes/lc_mock_claim.py``: ``ClaimSim.behind_hub`` scripts the hub's
answer). Each check has a twin.
"""

from __future__ import annotations

import pytest

from harness_manager.demo import BOARD_FIELDED
from tests.web.test_small4_claim_hostkey_browser import open_board

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = [pytest.mark.browser, pytest.mark.week_plan("claim_api", sim=True)]
T = 10_000
CARD = '[data-testid="access-claim"]'
CLAIM_BTN = '[data-action="access-claim"]'
HUB_BTN = '[data-action="access-claim-hub-check"]'


def asks(daemon) -> list[str]:
    return [how for bid, how in daemon.app.state.claim.hub_asks if bid == BOARD_FIELDED]


def test_a_hub_board_is_asked_through_the_hub_on_load_and_offers_the_claim(page_factory,
                                                                           daemon):
    daemon.app.state.claim.behind_hub(BOARD_FIELDED, "unclaimed")
    page = page_factory()
    open_board(page)
    card = page.locator(CARD)
    expect(card.locator("[data-claim]")).to_have_attribute("data-claim", "unclaimed", timeout=T)
    expect(card).to_contain_text("Not claimed")
    expect(card).to_contain_text("last check")
    expect(page.locator(CLAIM_BTN)).to_have_text("Claim with my key…")
    assert asks(daemon) == ["hub"]                        # once, by itself, not a refresh
    expect(card).not_to_contain_text("not checked through the hub yet")
    assert not page.errors, page.errors


def test_negative_twin_a_hub_board_claimed_by_you_shows_mine_and_no_claim_button(page_factory,
                                                                                 daemon):
    daemon.app.state.claim.behind_hub(BOARD_FIELDED, "mine")
    page = page_factory()
    open_board(page)
    card = page.locator(CARD)
    expect(card.locator("[data-claim]")).to_have_attribute("data-claim", "mine", timeout=T)
    expect(card).to_contain_text("Claimed by you")
    expect(page.locator(CLAIM_BTN)).to_have_count(0)
    assert asks(daemon) == ["hub"]
    assert not page.errors, page.errors


def test_a_hub_that_cannot_be_asked_says_why_and_offers_check_through_the_hub(page_factory,
                                                                             daemon):
    sim = daemon.app.state.claim
    sim.behind_hub(BOARD_FIELDED, "down")
    page = page_factory()
    open_board(page)
    card = page.locator(CARD)
    expect(card).to_contain_text("cannot run ssh to the hub", timeout=T)
    expect(card).to_contain_text("The claim could not be read through the hub.")
    expect(card.locator("[data-claim]")).to_have_attribute("data-claim", "unknown")
    expect(page.locator(CLAIM_BTN)).to_have_count(0)      # nothing to claim on a guess
    button = page.locator(HUB_BTN)
    expect(button).to_have_text("Check through the hub")
    with sim._mu:
        sim.hub_answers[BOARD_FIELDED] = "unclaimed"       # the hub is back
    button.click()
    expect(card.locator("[data-claim]")).to_have_attribute("data-claim", "unclaimed", timeout=T)
    expect(page.locator(CLAIM_BTN)).to_have_count(1)
    assert asks(daemon) == ["hub", "refresh"]             # the button asks now
    assert not page.errors, page.errors


def test_negative_twin_a_lan_board_is_never_sent_to_the_hub(page_factory, daemon):
    daemon.app.state.claim.linux(BOARD_FIELDED, "unclaimed")
    page = page_factory()
    open_board(page)
    card = page.locator(CARD)
    expect(page.locator(CLAIM_BTN)).to_have_count(1, timeout=T)
    expect(page.locator(HUB_BTN)).to_have_count(0)
    page.wait_for_timeout(500)
    assert daemon.app.state.claim.hub_asks == []
    expect(card).not_to_contain_text("last check")
    assert not page.errors, page.errors


def test_the_hub_answer_fills_the_identity_card(page_factory, daemon):
    claims = daemon.app.state.claim
    claims.behind_hub(BOARD_FIELDED, "unclaimed")
    claims.on_hub_answer = lambda: daemon.app.state.identity.matching(BOARD_FIELDED)
    page = page_factory()
    open_board(page)
    ident = page.locator('[data-testid="access-identity"]')
    expect(ident).to_contain_text("MPS3-02", timeout=T)
    expect(page.locator('[data-testid="access-identity-none"]')).to_have_count(0)
    assert not page.errors, page.errors


def test_negative_twin_before_the_hub_answers_the_identity_card_does_not_blame_the_image(
        page_factory, daemon):
    daemon.app.state.claim.behind_hub(BOARD_FIELDED, "down")     # no identity comes back
    page = page_factory()
    open_board(page)
    none = page.locator('[data-testid="access-identity-none"]')
    expect(none).to_contain_text("This board is behind a hub", timeout=T)
    expect(none).not_to_contain_text("net-protocol v0.16 adds it")
    assert not page.errors, page.errors
