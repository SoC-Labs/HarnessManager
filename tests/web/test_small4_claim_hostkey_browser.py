"""SMALL-4 in the browser: the Board tile's SSH line when the board's host key changed.

Over the T14 mock (``tests/fakes/lc_mock_claim.py``: ``info.claim`` as a test scripts it). The
Linux harness keeps its host key in /persist (the user microSD, or tmpfs when the card does not
mount), so a key can change BACK to one Harness Manager pinned before (``host_key.seen_before``):
the tile says so plainly, as a warning naming /persist and the re-pin. The negative twin, a key
never seen, keeps the loud error. Neither offers anything that accepts the key.
"""

from __future__ import annotations

import pytest

from harness_manager.demo import BOARD_FIELDED
from tests.web import nav

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

#: The mock only: its ClaimSim scripts the claim (week_plan(sim=True) = mock only).
pytestmark = [pytest.mark.browser, pytest.mark.week_plan("claim_api", sim=True)]
T = 10_000
PINNED = "SHA256:tmpfstmpfstmpfstmpfstmpfstmpfstmpfstmpfstmp"
CARD = "SHA256:cardcardcardcardcardcardcardcardcardcardcar"


def changed_key(daemon, *, seen_before):
    sim = daemon.app.state.claim
    st = sim._status("other")
    st["host_key"] = {"reported": CARD, "pinned": PINNED, "match": False,
                      "seen_before": seen_before}
    with sim._mu:
        sim.claims[BOARD_FIELDED] = st


def open_board(page):
    page.locator(f'.board-item[data-board="{BOARD_FIELDED}"]').click()
    page.wait_for_selector(f'main[data-board="{BOARD_FIELDED}"], [data-action="open"]', timeout=T)
    if page.locator(f'main[data-board="{BOARD_FIELDED}"]').count() == 0:
        page.locator('[data-action="open"]').click()
    nav.land(page, "board")                       # UI v2: a board opens on the Workbench
    nav.board_page(page, "access")                # UI v2: the Overview's Board tile is gone
    page.wait_for_selector('[data-testid="access-claim"]', timeout=T)    # UI v2: Board > Access's card


def test_a_key_changed_back_to_one_seen_before_is_a_plain_warning(page_factory, daemon):
    changed_key(daemon, seen_before="2026-09-24T09:30:00Z")
    page = page_factory()
    open_board(page)
    line = page.locator('[data-testid="claim-hostkey"]')
    expect(line).to_contain_text("Host key changed back to one seen on 2026-09-24", timeout=T)
    expect(line).to_contain_text("/persist (the user microSD) mounting or not")
    expect(line).to_contain_text("board claim --adopt, if you trust it")
    expect(line).to_contain_text(CARD)
    assert "warn" in line.get_attribute("class") and "err" not in line.get_attribute("class")
    expect(page.locator('[data-action="access-claim"]')).to_have_count(0)     # nothing accepts it
    assert not page.errors, page.errors


def test_negative_twin_a_key_never_seen_keeps_the_loud_error(page_factory, daemon):
    changed_key(daemon, seen_before=None)
    page = page_factory()
    open_board(page)
    line = page.locator('[data-testid="claim-hostkey"]')
    expect(line).to_have_text(f"Host key changed: pinned {PINNED}, the board reports {CARD}. "
                              "SSH is refused.", timeout=T)
    assert "err" in line.get_attribute("class")
    expect(line).not_to_contain_text("changed back")
    expect(page.locator('[data-action="access-claim"]')).to_have_count(0)
    assert not page.errors, page.errors


# --- HOSTKEY: the explicit re-pin (old and new key, an Arm box, exactly the key shown) ------------------------


def changed_with_details(daemon, new=CARD):
    changed_key(daemon, seen_before=None)
    sim = daemon.app.state.claim
    with sim._mu:
        hk = sim.claims[BOARD_FIELDED]["host_key"]
        hk.update(reported=new, pinned_at="2026-10-02T09:00:00Z", boot_id="boot-B", up_s=95.0)
        hk["refusal"] = {"message": f"THE BOARD'S SSH HOST KEY CHANGED: pinned {PINNED}, the "
                                    f"board now reports {new} (pinned on 2026-10-02). SSH to "
                                    "this board is refused", "hint": "re-pin"}


def test_the_access_card_shows_both_keys_and_repins_exactly_the_key_shown(page_factory, daemon):
    changed_with_details(daemon)
    page = page_factory()
    open_board(page)
    expect(page.locator('[data-testid="hostkey-old"]')).to_contain_text(PINNED, timeout=T)
    expect(page.locator('[data-testid="hostkey-old"]')).to_contain_text("pinned 2026-10-02")
    expect(page.locator('[data-testid="hostkey-new"]')).to_have_text(CARD)
    expect(page.locator('[data-testid="hostkey-boot"]')).to_contain_text("boot-B")
    page.locator('[data-action="access-repin"]').click()
    confirm = page.locator('[data-action="access-repin-confirm"]')
    assert confirm.is_disabled()                          # behind the Arm box
    page.locator('[data-testid="arm-repin"]').click()
    expect(confirm).to_be_enabled()
    confirm.click()
    sim = daemon.app.state.claim
    for _ in range(100):
        if any("repin" in p for p in sim.posts):
            break
        page.wait_for_timeout(50)
    posted = next(p["repin"] for p in sim.posts if "repin" in p)
    assert posted == {"confirm": True, "fingerprint": CARD}      # exactly the key on screen
    assert not page.errors, page.errors

