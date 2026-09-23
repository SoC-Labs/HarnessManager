"""Version strings: parse and compare, without a third-party dependency.

Accepted: ``MAJOR[.MINOR[.PATCH]]`` optionally followed by a pre-release part
(``1.2.0-rc1``, ``1.2.0rc1``, ``0.4.0.dev3``, ``1.0.0-beta.2``) and a local part
after ``+`` that never affects ordering. A pre-release sorts before its
release: ``1.2.0rc1 < 1.2.0``. Anything else is a ``ValueError``: a channel
that names an unparsable version is refused, never guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_VERSION_RE = re.compile(
    r"^v?(?P<nums>\d+(?:\.\d+){0,3})"
    r"(?:[-.]?(?P<pre>(?:a|alpha|b|beta|rc|c|pre|preview|dev)[-.]?\d*))?"
    r"(?:\+(?P<local>[0-9A-Za-z.-]+))?$",
    re.IGNORECASE,
)
_PRE_RANK = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 3, "preview": 3, "c": 3,
             "rc": 3}


@total_ordering
@dataclass(frozen=True)
class Version:
    text: str
    nums: tuple[int, ...]
    pre: tuple[int, int] | None      # (rank, number) or None for a final release

    def _key(self) -> tuple:
        nums = self.nums + (0,) * (4 - len(self.nums))
        # A final release outranks every pre-release of the same numbers.
        return (nums, (1, 0, 0) if self.pre is None else (0, *self.pre))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __lt__(self, other: Version) -> bool:
        return self._key() < other._key()

    def __str__(self) -> str:
        return self.text


def parse_version(text: str) -> Version:
    if not isinstance(text, str):
        raise ValueError(f"a version must be a string, got {type(text).__name__}")
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise ValueError(f"not a version: {text!r}")
    nums = tuple(int(n) for n in m.group("nums").split("."))
    pre = None
    if m.group("pre"):
        pm = re.match(r"([A-Za-z]+)[-.]?(\d*)", m.group("pre"))
        assert pm is not None
        pre = (_PRE_RANK[pm.group(1).lower()], int(pm.group(2) or 0))
    return Version(text=text.strip(), nums=nums, pre=pre)


def is_version(text: object) -> bool:
    try:
        parse_version(text)  # type: ignore[arg-type]
    except ValueError:
        return False
    return True


def compare(a: str, b: str) -> int:
    """-1, 0 or 1 as ``a`` is older than, the same as, or newer than ``b``."""
    va, vb = parse_version(a), parse_version(b)
    return (va > vb) - (va < vb)


def compare_safe(a: str, b: str) -> int | None:
    """``compare``, or None when either side is not a version (a board may report anything)."""
    try:
        return compare(a, b)
    except ValueError:
        return None


def same_version(a: str, b: str) -> bool:
    """Equal as versions ("1.0" == "1.0.0"); unparsable strings compare as plain text."""
    c = compare_safe(a, b)
    return a.strip() == b.strip() if c is None else c == 0


def at_least(have: str, need: str) -> bool:
    """``have >= need``. An empty ``need`` is always satisfied."""
    if not need:
        return True
    return compare(have, need) >= 0
