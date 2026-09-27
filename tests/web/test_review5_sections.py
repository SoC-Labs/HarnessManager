"""REVIEW-W5 17: the Settings "own service dir" note compares ONE spelling of a directory.

A Windows service reports its config dir as ``C:\\Users\\me\\...``; the ``advanced.state_dir``
row may say ``C:/Users/me/...`` (or carry a trailing slash): the same directory, so no note.
``sameDir`` (``web/static/js/settings/sections.js``) is imported into the real page.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

CASES = [
    # (row value, the service's config dir, same?)
    ("C:\\Users\\me\\hm", "C:/Users/me/hm", True),
    ("C:/Users/me/hm/", "C:\\Users\\me\\hm", True),
    ("~\\AppData\\Roaming\\hm", "C:\\Users\\me\\AppData\\Roaming\\hm", True),
    ("/home/me/.config/harness-manager", "/home/me/.config/harness-manager", True),
    ("~/.config/harness-manager", "/home/me/.config/harness-manager", True),
    # the twins: another directory is another directory, in any spelling
    ("C:\\Users\\me\\hm", "C:/Users/me/demo", False),
    ("/home/me/.config/harness-manager", "/tmp/state", False),
    ("", "/tmp/state", False),
]


def test_same_dir_compares_one_spelling(page_factory):
    page = page_factory()
    got = page.evaluate(
        """async (cases) => {
             const m = await import('./js/settings/sections.js');
             return cases.map(([a, b]) => m.sameDir(a, b));
           }""", [[a, b] for a, b, _ in CASES])
    assert got == [same for _, _, same in CASES], list(zip(CASES, got, strict=True))
    assert not page.errors, page.errors
