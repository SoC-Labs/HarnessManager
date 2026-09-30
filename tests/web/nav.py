"""UI v2 navigation for the browser tests (docs/planning/UI_V2_PLAN.md §3.3): one map from
0.1.0's twelve tab keys to the five tabs, and the helpers every file used to define for itself.

The page's own map is ``js/route.js`` ``OLD_KEYS``; ``section(page, "sd")`` goes where a user
following an old link lands (Board > Versions), so a test written against a 0.1.0 tab reads
the same card in its new place. The five tabs keep ``[data-section=KEY]`` and
``[data-testid="section-KEY"]``; the Board tab's pages are ``[data-board-page=SUB]`` /
``[data-testid="board-page-SUB"]``; the Workbench's parts ``[data-testid="part-KEY"]``;
Activity is a drawer (``[data-testid="activity"]``).
"""

from __future__ import annotations

from typing import Any

T = 20_000

#: The five tabs, in order; Checks only on a board behind a hub.
TABS = ("overview", "workbench", "build", "board", "checks")
BOARD_PAGES = ("recover", "versions", "connections", "readings", "access", "about")

#: 0.1.0's key -> (tab, Board page or "", Workbench part or ""). js/route.js OLD_KEYS.
OLD_KEYS: dict[str, tuple[str, str, str]] = {
    "overview": ("overview", "", ""),
    "xdc": ("build", "", "xdc"),
    "build": ("build", "", ""),
    "program": ("workbench", "", "program"),
    "consoles": ("workbench", "", "consoles"),
    "debug": ("workbench", "", "debug"),
    "power": ("board", "recover", ""),
    "clocks": ("board", "readings", ""),
    "sd": ("board", "versions", ""),
    "update": ("board", "versions", ""),
    "checks": ("checks", "", ""),
    "activity": ("overview", "", ""),        # + the Activity drawer
}


def by_id(page: Any, name: str) -> Any:
    return page.locator(f'[data-testid="{name}"]')


def rail(page: Any, bid: str) -> Any:
    return page.locator(f'.board-item[data-board="{bid}"]')


def open_board(page: Any, bid: str) -> None:
    """Select ``bid`` in the rail, open it if it is not open here, wait until its header has
    read the board (the Shell fact). An open Activity drawer is closed first (it covers the
    workspace, where Open is)."""
    close_activity(page)
    rail(page, bid).click()
    page.wait_for_selector(f'main[data-board="{bid}"], [data-action="open"]', timeout=T)
    if page.locator(f'main[data-board="{bid}"]').count() == 0:
        page.locator('[data-action="open"]').click()
    page.wait_for_selector(f'main[data-board="{bid}"] [data-testid="fact-shell"]'
                           ':not(:has-text("unknown"))', timeout=T)


def tab(page: Any, key: str) -> None:
    """Click one of the five tabs and wait for its panel."""
    assert key in TABS, f"{key!r} is not a UI v2 tab (use section() for a 0.1.0 key)"
    page.locator(f'.section-tab[data-section="{key}"]').click()
    page.wait_for_selector(f'[data-testid="section-{key}"]', timeout=T)


def board_page(page: Any, sub: str) -> None:
    """The Board tab's page ``sub`` (recover, versions, connections, readings, access, about)."""
    assert sub in BOARD_PAGES, sub
    if page.locator('[data-testid="section-board"]').count() == 0:
        tab(page, "board")
    page.locator(f'[data-board-page="{sub}"]').click()
    page.wait_for_selector(f'[data-testid="board-page-{sub}"]', timeout=T)


def activity(page: Any, level: str = "all") -> Any:
    """Open the Activity drawer (the rail's foot); ``level`` "err" shows errors only. The
    drawer's locator (``data-testid="activity"``, as the tab's was)."""
    if page.locator('[data-testid="activity-drawer"]').count() == 0:
        page.locator('[data-action="activity"]').click()
    drawer = page.locator('[data-testid="activity"]')
    drawer.wait_for(timeout=T)
    drawer.get_by_role("button", name="Errors" if level == "err" else "All", exact=True).click()
    return drawer


def close_activity(page: Any) -> None:
    if page.locator('[data-testid="activity-drawer"]').count():
        page.locator('[data-action="activity-close"]').click()
        page.wait_for_selector('[data-testid="activity-drawer"]', state="detached", timeout=T)


def section(page: Any, key: str) -> None:
    """Go where 0.1.0's tab ``key`` is now (or to a UI v2 tab by its own key) and wait for it:
    the tab, the Board page, the Workbench part scrolled into view, the XDC fold open, or the
    Activity drawer."""
    if key in TABS:
        close_activity(page)
        tab(page, key)
        return
    if key == "activity":
        activity(page)
        return
    close_activity(page)
    where, sub, part = OLD_KEYS[key]
    tab(page, where)
    if sub:
        board_page(page, sub)
    if part == "xdc":
        fold = page.locator('[data-action="xdc-fold"]')
        if fold.get_attribute("aria-expanded") != "true":
            fold.click()
        page.wait_for_selector('[data-action="xdc-fold"][aria-expanded="true"]', timeout=T)
    elif part:
        el = page.locator(f'[data-testid="part-{part}"]')
        el.wait_for(timeout=T)
        el.scroll_into_view_if_needed()


def panel(page: Any, key: str) -> Any:
    """The locator of what 0.1.0 called ``[data-testid="section-KEY"]``: the Workbench part,
    the Board page, the XDC fold, or the tab's own panel."""
    if key in TABS:
        return page.locator(f'[data-testid="section-{key}"]')
    where, sub, part = OLD_KEYS[key]
    if key == "activity":
        return page.locator('[data-testid="activity"]')
    if sub:
        return page.locator(f'[data-testid="board-page-{sub}"]')
    if part == "xdc":
        return page.locator('[data-testid="xdc-fold"]')
    if part:
        return page.locator(f'[data-testid="part-{part}"]')
    return page.locator(f'[data-testid="section-{where}"]')
