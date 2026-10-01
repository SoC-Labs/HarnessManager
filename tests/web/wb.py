"""The Workbench's own helpers for the browser tests (lane UI2-WORKBENCH).

UI v2 round 3 moved 0.1.0's overlay table into a picker (a combo): a design is picked by
opening the combo and clicking its row (``[data-overlay=NAME]``, as the table's rows were).
``pick`` does both, so a test written against the table reads the same.
"""

from __future__ import annotations

from typing import Any

T = 10_000


def picker(page: Any) -> Any:
    return page.locator('[data-testid="design-picker"]')


def open_picker(page: Any) -> None:
    """Open the design list (idempotent)."""
    p = picker(page)
    p.wait_for(timeout=T)
    if p.get_attribute("aria-expanded") != "true":
        p.click()
    page.locator('[data-testid="design-list"]').wait_for(timeout=T)


def pick(page: Any, name: str, *, wait: bool = False) -> None:
    """Pick ``name`` in the Program strip. ``wait``: until its preflight answered."""
    open_picker(page)
    page.locator(f'[data-testid="design-list"] [data-overlay="{name}"]').click()
    page.wait_for_selector('[data-testid="design-list"]', state="detached", timeout=T)
    if wait:
        page.wait_for_selector('[data-testid="preflight-summary"]', timeout=T)
