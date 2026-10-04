"""Lane IDENTITY in the browser: the ONE "Name this board" dialog, from Board > Access and from
the bring-up wizard, on the REAL daemon over ``DemoEngine(showcase=True)`` (what ``app --demo``
serves: its Linux board has a net identity, the service is the product's own). The cases the
demo has no board for (behind a hub, another subnet, a board that moved) answer through
page.route with the service's own shapes. Every behaviour has its negative twin.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from harness_manager import demo_catalog as cat
from harness_manager.demo import DemoEngine
from harness_manager.demo_showcase import BOARD_LINUX, BOARD_V011
from tests.fakes.t14_mock_api import real_daemon
from tests.web import nav
from tests.web.conftest import dump_failed_pages


@pytest.fixture(autouse=True)
def _pool_from_its_first_address(monkeypatch):
    """These tests read the pool from its first address: ``--ip auto``'s start from the
    random MAC (``pool_start``) is tested in tests/unit/test_identity_assign.py."""
    from harness_manager.services import identity_assign as _IA

    monkeypatch.setattr(_IA, "pool_start", lambda addrs, mac=None: 0)

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
expect = sync_api.expect

pytestmark = pytest.mark.browser
T = 20_000
TOKEN = "identity"


class Demo:
    def __init__(self, browser: Any, daemon: Any, engine: DemoEngine) -> None:
        self.browser, self.daemon, self.engine = browser, daemon, engine
        self.contexts: list[Any] = []
        self.pages: list[Any] = []

    def page(self, scheme: str = "light") -> Any:
        ctx = self.browser.new_context(viewport={"width": 1440, "height": 900},
                                       color_scheme=scheme, reduced_motion="reduce")
        self.contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.on("console", lambda m: page.errors.append(m.text) if m.type == "error"
                and "status of 4" not in m.text and "status of 5" not in m.text else None)
        page.goto(self.daemon.ui_url)
        page.wait_for_selector(".board-item", timeout=T)
        self.pages.append(page)
        return page


@pytest.fixture
def demo(browser, tmp_path, monkeypatch, request) -> Iterator[Demo]:
    monkeypatch.delenv(cat.UPDATE_ENV, raising=False)
    sdir = tmp_path / "demo"
    engine = DemoEngine(speed=0.05, showcase=True, state_dir=sdir)
    try:
        with real_daemon(engine, token=TOKEN, state_dir=sdir) as d:
            show = Demo(browser, d, engine)
            try:
                yield show
            finally:
                dump_failed_pages(request, show.pages)
                for ctx in show.contexts:
                    ctx.close()
    finally:
        engine.close_all()


def by(page: Any, testid: str) -> Any:
    return page.locator(f'[data-testid="{testid}"]')


def act(page: Any, action: str) -> Any:
    return page.locator(f'[data-action="{action}"]')


def seg(page: Any, testid: str, label: str) -> Any:
    return by(page, testid).get_by_role("button", name=label, exact=True)


def from_access(page: Any, bid: str = BOARD_LINUX) -> Any:
    nav.open_board(page, bid)
    nav.board_page(page, "access")
    act(page, "access-identity-name").click()
    modal = by(page, "name-board-modal")
    expect(modal).to_be_visible(timeout=T)
    return modal


# --- from Board > Access, on the demo's Linux board -----------------------------------------------


def test_name_this_board_from_access_names_it_with_a_random_mac_and_a_pool_ip(demo):
    page = demo.page()
    from_access(page)
    name = by(page, "nb-name")
    expect(name).to_have_value("", timeout=T)                    # the image's own name: no name
    expect(by(page, "nb-name-count")).to_have_text("0/16")
    name.fill("lab-07")
    expect(name).to_have_value("LAB-07")                          # upper-cased as typed
    expect(by(page, "nb-name-count")).to_have_text("6/16")
    # the MAC: random by default (the image default MAC), regenerate gives another
    expect(seg(page, "nb-mac-mode", "Random")).to_have_attribute("aria-pressed", "true")
    mac = by(page, "nb-mac")
    expect(mac).to_have_text(re.compile(r"^02:[0-9a-f]{2}(:[0-9a-f]{2}){4}$"), timeout=T)
    first = mac.inner_text()
    assert not first.startswith("02:00:00:")
    act(page, "nb-mac-regenerate").click()
    expect(mac).not_to_have_text(first, timeout=T)
    # the IP: the board's stage0 address is kept by default; Auto takes the pool's next
    expect(seg(page, "nb-ip-mode", "Keep")).to_have_attribute("aria-pressed", "true")
    seg(page, "nb-ip-mode", "Auto").click()
    expect(by(page, "nb-ip-value")).to_have_text("192.168.10.110", timeout=T)
    expect(by(page, "nb-ip-note")).to_have_text(
        "This PC must be on the same /24 (e.g. 192.168.10.1/24).")
    expect(by(page, "nb-rescue")).to_contain_text(
        "Until mint 4, stage0 rescue still answers on 192.168.10.101 with the image's default "
        "MAC 02:00:00:4d:50:53")
    changes = by(page, "identity-changes")
    expect(changes.locator('li[data-field="label"]')).to_contain_text("MPS3 → LAB-07")
    expect(changes.locator('li[data-field="ip"]')).to_contain_text("192.168.10.104 → 192.168.10.110")
    expect(by(page, "nb-phrase-want")).to_have_text("LAB-07")
    confirm = act(page, "identity-fix-confirm")
    expect(confirm).to_be_disabled()
    by(page, "identity-phrase").fill("LAB-0")
    expect(confirm).to_be_disabled()                              # a near miss is not the phrase
    by(page, "identity-phrase").fill("LAB-07")
    expect(confirm).to_be_enabled()
    confirm.click()
    expect(by(page, "nb-done")).to_contain_text("label LAB-07", timeout=T)
    expect(by(page, "nb-done").locator('[data-testid="nb-ip-value"]')).to_have_text(
        "192.168.10.110")
    act(page, "nb-close").click()
    card = by(page, "access-identity")
    expect(card).to_contain_text("LAB-07", timeout=T)
    expect(card).to_contain_text("192.168.10.110/24")
    seen = json.loads((demo.engine.state_dir / "identity" / "seen.json").read_text())
    assert seen[BOARD_LINUX]["history"]["ip"]["192.168.10.110"]["how"] == "assigned"
    assert not page.errors, page.errors


@pytest.mark.parametrize("typed,words", [
    ("x" * 17, "It is 17 characters (at most 16: the panel shows 16)"),
    ("lab_07", "It has '_' (only A-Z, 0-9 and - are allowed)"),
    ("lab 07", "It has a space"),
])
def test_twin_a_name_outside_the_rule_says_the_rule_and_cannot_be_set(demo, typed, words):
    page = demo.page()
    from_access(page)
    by(page, "nb-name").fill(typed)
    why = by(page, "nb-name-why")
    expect(why).to_contain_text(words, timeout=T)
    expect(why).to_contain_text("A name is 1-16 characters of A-Z, 0-9 and -.")
    if len(typed) > 16:
        expect(by(page, "nb-name-count")).to_have_text("17/16")
        assert "over" in by(page, "nb-name-count").get_attribute("class")
    phrase = by(page, "nb-phrase-want")
    if phrase.count():
        by(page, "identity-phrase").fill(phrase.inner_text())
        expect(act(page, "identity-fix-confirm")).to_be_disabled()
    assert not page.errors, page.errors


def test_twin_a_custom_mac_in_the_image_range_is_refused_in_the_dialog(demo):
    page = demo.page()
    from_access(page)
    by(page, "nb-name").fill("LAB-09")
    seg(page, "nb-mac-mode", "Custom").click()
    by(page, "nb-mac-custom").fill("02:00:00:12:34:56")
    expect(by(page, "nb-mac-why")).to_contain_text("02:00:00:* is reserved", timeout=T)
    by(page, "identity-phrase").fill("LAB-09")
    expect(act(page, "identity-fix-confirm")).to_be_disabled()
    by(page, "nb-mac-custom").fill("02:5e:00:00:00:09")
    expect(by(page, "nb-mac-why")).to_have_count(0, timeout=T)
    expect(act(page, "identity-fix-confirm")).to_be_enabled()
    assert not page.errors, page.errors


# --- from the bring-up wizard: the same dialog, pre-filled ----------------------------------------


def test_the_wizard_opens_the_same_dialog_pre_filled(demo):
    page = demo.page()
    nav.open_board(page, BOARD_LINUX)
    page.evaluate("a => import('./js/modal.js').then(m => m.openModal('name-board', a))",
                  {"bid": BOARD_LINUX, "prefill": {"label": "mps3-mo20",
                                                   "mac": "02:5e:3a:91:c0:17",
                                                   "ip": "192.168.10.111/24"}})
    expect(by(page, "name-board-modal")).to_be_visible(timeout=T)
    expect(by(page, "nb-name")).to_have_value("MPS3-MO20")
    expect(seg(page, "nb-mac-mode", "Random")).to_have_attribute("aria-pressed", "true")
    expect(by(page, "nb-mac")).to_have_text("02:5e:3a:91:c0:17", timeout=T)
    expect(seg(page, "nb-ip-mode", "Auto")).to_have_attribute("aria-pressed", "true")
    expect(by(page, "nb-ip-value")).to_have_text("192.168.10.111", timeout=T)
    expect(by(page, "nb-phrase-want")).to_have_text("MPS3-MO20")
    assert not page.errors, page.errors


def test_twin_the_wizard_on_a_bare_metal_board_shows_why_and_nothing_to_press(demo):
    page = demo.page()
    nav.open_board(page, BOARD_V011)
    page.evaluate("a => import('./js/modal.js').then(m => m.openModal('name-board', a))",
                  {"bid": BOARD_V011, "prefill": {"label": "MPS3-MO20"}, "impl": "bare-metal"})
    expect(by(page, "identity-refusal")).to_contain_text(
        "This board cannot take a name: the bare-metal harness has no identity store", timeout=T)
    expect(by(page, "nb-name")).to_have_count(0)
    expect(act(page, "identity-fix-confirm")).to_have_count(0)
    assert not page.errors, page.errors


# --- the cases the demo has no board for: the service's shapes through page.route ----------------

IDENTITY_URL = re.compile(r".*/api/v1/boards/[^/]+/identity(\?.*)?$")
PROPOSAL_URL = re.compile(r".*/api/v1/boards/[^/]+/identity/proposal(\?.*)?$")
HUB = {"target": "mps3_02_pl", "board": "mps3_02", "label": "MPS3-02", "board_ip": "192.168.11.101",
       "prefix": 24, "board_mac": "02:00:00:00:02:fe", "hostname": "mps3-02-pl",
       "discovered_mac": "", "mac_suspect": "", "hub": "mapstone-dev"}
REPORTED = {"label": "MPS3", "hostname": "mps3", "ip": "192.168.11.101/24",
            "mac": "02:00:00:4d:50:53", "impl": "linux", "feature": True, "feature_known": True,
            "source": {"label": "default", "ip": "stage0", "mac": "default"}, "persist": True}


def status(hub: dict | None = None) -> dict:
    changes = [{"field": "label", "from": "MPS3", "to": "MPS3-02"},
               {"field": "mac", "from": "02:00:00:4d:50:53", "to": "02:00:00:00:02:fe"}] \
        if hub else []
    return {"status": "unset", "level": "warn", "reported": REPORTED, "hub": hub, "findings": [],
            "fix": {"changes": changes, "phrase": "MPS3-02" if hub else "MPS3", "notes": [],
                    "ready": bool(changes), "refusal": None},
            "notes": [], "live": True, "checked_at": "now"}


def proposal(q: dict, *, hub: bool = False, local: str = "") -> dict:
    # the service's defaults for this board (not the image default, or behind a hub): keep
    mac_q, ip_q = q.get("mac") or "keep", q.get("ip") or "keep"
    mac = "02:5e:00:00:00:42" if mac_q == "random" else (REPORTED["mac"] if mac_q == "keep"
                                                         else mac_q)
    ip = "192.168.11.101/24" if ip_q == "keep" else ("192.168.10.110/24" if ip_q == "auto"
                                                     else ip_q if "/" in ip_q else f"{ip_q}/24")
    how = {"keep": "keep", "random": "random", "auto": "auto"}
    label = str(q.get("label") or "").upper()
    changes = [c for c in ({"field": "label", "from": "MPS3", "to": label},
                           {"field": "mac", "from": REPORTED["mac"], "to": mac},
                           {"field": "ip", "from": REPORTED["ip"], "to": ip})
               if c["to"] and c["to"] != c["from"]]
    addr = ip.split("/")[0]
    net = ".".join(addr.split(".")[:3])
    lnet = ".".join(local.split(".")[:3]) if local else ""
    return {"board_id": "x", "current": {k: REPORTED[k] for k in ("label", "hostname", "ip", "mac")},
            "defaults": {"mac": "keep", "ip": "keep"}, "label": label, "label_problem": "",
            "mac": mac, "mac_how": how.get(mac_q, "custom"), "ip": ip,
            "ip_how": how.get(ip_q, "custom"), "errors": {},
            "changes": changes, "phrase": label or "MPS3",
            "address": {"ip": addr, "same_net": f"this PC must be on the same /24 (e.g. {net}.1/24)",
                        "pc_example": f"{net}.1/24",
                        "subnet": {"local": local, "network": f"{lnet}.0/24" if local else "",
                                   "same": (lnet == net) if local else None},
                        "moving": False},
            "pool": {"range": "192.168.10.110-199", "setting": "mps3.identity.ip_pool"},
            "rules": {"name_max": 16}, "notes": [],
            "rescue_note": "until mint 4, stage0 rescue still answers on 192.168.10.101",
            "hub": {"name": "mapstone-dev", "target": "mps3_02_pl", "record": HUB,
                    "known_bad": {"mps3_01_pl": "its board_mac 00:e0:4c:46:dc:f8 is the hub's "
                                                "own USB adapter, not the board"},
                    "guard": any(c["field"] in ("mac", "ip") for c in changes)} if hub else None,
            "refusal": None}


def serve(page: Any, *, hub: bool = False, local: str = "", result: dict | None = None) -> list:
    from urllib.parse import parse_qsl, urlsplit

    posts: list[dict] = []

    def identity(route: Any) -> None:
        if route.request.method == "POST":
            posts.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(status=202, content_type="application/json",
                          body=json.dumps({"ok": True, "job": "nb-job"}))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"ok": True, "board_id": "x", "identity": status(HUB if hub else None)}))

    def propose(route: Any) -> None:
        q = dict(parse_qsl(urlsplit(route.request.url).query))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"ok": True, "board_id": "x", "proposal": proposal(q, hub=hub, local=local)}))

    def job(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "id": "nb-job", "kind": "identity", "state": "done",
            "result": result or {"action": "set", "verified": True, "notes": [], "moved": None,
                                 "changes": [], "address": None}}))

    page.route(PROPOSAL_URL, propose)
    page.route(IDENTITY_URL, identity)
    page.route("**/api/v1/jobs/nb-job*", job)
    return posts


def open_stubbed(page: Any, **kw: Any) -> list:
    posts = serve(page, **kw)
    nav.open_board(page, BOARD_V011)
    page.evaluate("a => import('./js/modal.js').then(m => m.openModal('name-board', a))",
                  {"bid": BOARD_V011, **({"hub": True} if kw.get("hub") and kw.get("hub_mode")
                                         else {})})
    expect(by(page, "name-board-modal")).to_be_visible(timeout=T)
    return posts


def test_behind_a_hub_a_new_mac_needs_the_hubs_name_typed(demo):
    page = demo.page()
    posts = open_stubbed(page, hub=True)
    by(page, "nb-name").fill("MPS3-02")
    expect(by(page, "nb-hub")).to_contain_text("behind the hub mapstone-dev", timeout=T)
    expect(by(page, "nb-hub-warning")).to_have_count(0)            # MAC and IP kept: no guard
    seg(page, "nb-mac-mode", "Random").click()
    warn = by(page, "nb-hub-warning")
    expect(warn).to_contain_text("Changing its MAC before the hub's record is fixed loses the "
                                 "board's address", timeout=T)
    expect(warn).to_contain_text("mps3_01_pl's hub record is known to be wrong today")
    by(page, "identity-phrase").fill("MPS3-02")
    confirm = act(page, "identity-fix-confirm")
    expect(confirm).to_be_disabled()
    by(page, "nb-hub-fixed").fill("mapstone")
    expect(confirm).to_be_disabled()                              # a near miss names no hub
    by(page, "nb-hub-fixed").fill("mapstone-dev")
    expect(confirm).to_be_enabled()
    confirm.click()
    expect(by(page, "nb-done")).to_be_visible(timeout=T)
    assert posts == [{"confirm": "MPS3-02", "label": "MPS3-02", "mac": "02:5e:00:00:00:42",
                      "hub_fixed": "mapstone-dev"}]
    assert not page.errors, page.errors


def test_twin_the_hub_entry_mode_is_the_old_fix_identity(demo):
    page = demo.page()
    posts = open_stubbed(page, hub=True)
    by(page, "name-board-modal").get_by_role("button", name=re.compile("Match its hub entry")).click()
    expect(by(page, "nb-hub-entry")).to_contain_text("mps3_02_pl", timeout=T)
    expect(by(page, "identity-changes").locator('li[data-field="mac"]')).to_contain_text(
        "02:00:00:00:02:fe")
    expect(by(page, "nb-phrase-want")).to_have_text("MPS3-02")
    by(page, "identity-phrase").fill("MPS3-02")
    act(page, "identity-fix-confirm").click()
    expect(by(page, "nb-done")).to_be_visible(timeout=T)
    assert posts == [{"confirm": "MPS3-02", "from_hub": True}]       # no hub name: its own record
    assert not page.errors, page.errors


def test_an_ip_outside_this_pcs_24_needs_the_box_ticked(demo):
    page = demo.page()
    posts = open_stubbed(page, local="192.168.11.1")
    by(page, "nb-name").fill("LAB-11")
    seg(page, "nb-ip-mode", "Custom").click()
    by(page, "nb-ip-custom").fill("192.168.12.7")
    expect(by(page, "nb-subnet")).to_contain_text(
        "192.168.12.7 is not on this PC's network (this PC is 192.168.11.1 in 192.168.11.0/24)",
        timeout=T)
    by(page, "identity-phrase").fill("LAB-11")
    confirm = act(page, "identity-fix-confirm")
    expect(confirm).to_be_disabled()
    by(page, "nb-other-subnet").check()
    expect(confirm).to_be_enabled()
    confirm.click()
    expect(by(page, "nb-done")).to_be_visible(timeout=T)
    assert posts[0]["other_subnet"] is True and posts[0]["ip"] == "192.168.12.7/24"
    assert not page.errors, page.errors


def test_twin_an_ip_in_this_pcs_24_needs_no_box(demo):
    page = demo.page()
    open_stubbed(page, local="192.168.11.1")
    by(page, "nb-name").fill("LAB-11")
    seg(page, "nb-ip-mode", "Custom").click()
    by(page, "nb-ip-custom").fill("192.168.11.7")
    expect(by(page, "nb-ip-value")).to_have_text("192.168.11.7", timeout=T)
    expect(by(page, "nb-subnet")).to_have_count(0)
    by(page, "identity-phrase").fill("LAB-11")
    expect(act(page, "identity-fix-confirm")).to_be_enabled()
    assert not page.errors, page.errors


def test_a_board_that_moved_offers_to_open_it_at_its_new_address(demo):
    page = demo.page()
    open_stubbed(page, result={
        "action": "set", "verified": True, "notes": [], "board_id": "mps3@192.168.10.110:6900",
        "changes": [{"field": "ip", "from": "192.168.11.101/24", "to": "192.168.10.110/24"}],
        "address": {"ip": "192.168.10.110", "pc_example": "192.168.10.1/24",
                    "same_net": "this PC must be on the same /24 (e.g. 192.168.10.1/24)"},
        "moved": {"from": BOARD_V011, "to": "mps3@192.168.10.110:6900", "host": "192.168.10.110",
                  "address": "192.168.10.110:6900", "old_host": "192.168.10.105",
                  "records": ["claims.json: moved"]}})
    by(page, "nb-name").fill("LAB-10")
    seg(page, "nb-ip-mode", "Auto").click()
    by(page, "identity-phrase").fill("LAB-10")
    act(page, "identity-fix-confirm").click()
    moved = by(page, "nb-moved")
    expect(moved).to_contain_text("found at 192.168.10.110 by its SSH host key", timeout=T)
    expect(act(page, "nb-open-moved")).to_have_text(re.compile("Open it at 192.168.10.110"))
    expect(by(page, "nb-done").locator('[data-testid="nb-ip-value"]')).to_have_text(
        "192.168.10.110")
    assert not page.errors, page.errors


def test_twin_a_board_that_did_not_move_offers_nothing_to_open(demo):
    page = demo.page()
    open_stubbed(page)
    by(page, "nb-name").fill("LAB-10")
    by(page, "identity-phrase").fill("LAB-10")
    act(page, "identity-fix-confirm").click()
    expect(by(page, "nb-done")).to_be_visible(timeout=T)
    expect(by(page, "nb-moved")).to_have_count(0)
    assert not page.errors, page.errors


def moved_result(to: str, host: str) -> dict:
    return {"action": "set", "verified": True, "notes": [], "board_id": to,
            "changes": [{"field": "label", "from": "MPS3", "to": "LAB-10"}],
            "address": {"ip": host, "pc_example": "192.168.10.1/24",
                        "same_net": "this PC must be on the same /24 (e.g. 192.168.10.1/24)"},
            "moved": {"from": BOARD_V011, "to": to, "host": host, "address": f"{host}:6900",
                      "old_host": "192.168.10.105", "records": []}}


def set_and_open(page: Any) -> None:
    by(page, "nb-name").fill("LAB-10")
    by(page, "identity-phrase").fill("LAB-10")
    act(page, "identity-fix-confirm").click()
    expect(by(page, "nb-moved")).to_be_visible(timeout=T)
    act(page, "nb-open-moved").click()


def test_open_it_there_opens_the_board_at_its_new_address_and_closes_the_old(demo):
    page = demo.page()
    # the demo's Linux board stands in for "the board at its new address"
    open_stubbed(page, result=moved_result(BOARD_LINUX, "192.168.10.104"))
    set_and_open(page)
    expect(page.locator(f'main[data-board="{BOARD_LINUX}"]')).to_be_visible(timeout=T)
    expect(by(page, "board-page-access")).to_be_visible(timeout=T)
    assert BOARD_LINUX in demo.engine.open_boards()
    for _ in range(50):                                        # the old session is closed
        if BOARD_V011 not in demo.engine.open_boards():
            break
        page.wait_for_timeout(100)
    assert BOARD_V011 not in demo.engine.open_boards()
    assert not page.errors, page.errors


def test_twin_nothing_at_the_new_address_says_so_and_keeps_the_old_session(demo):
    page = demo.page()
    open_stubbed(page, result=moved_result("mps3@192.168.10.199:6900", "192.168.10.199"))
    set_and_open(page)
    expect(by(page, "toast")).to_contain_text("Not open at 192.168.10.199 yet: add it By address",
                                              timeout=T)
    assert BOARD_V011 in demo.engine.open_boards()
