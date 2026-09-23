"""Board-pack discovery through the ``harness_manager.boards`` entry-point group."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from .errors import AbsentError
from .pack import BoardPack

log = logging.getLogger(__name__)

GROUP = "harness_manager.boards"


def load_packs() -> dict[str, BoardPack]:
    """Instantiate every installed board pack, keyed by name.

    A pack that fails to import is skipped and logged, never fatal: one broken
    pack must not take the app down.
    """
    packs: dict[str, BoardPack] = {}
    for ep in entry_points(group=GROUP):
        try:
            cls = ep.load()
            pack = cls()
        except Exception:  # noqa: BLE001
            log.exception("board pack %r failed to load", ep.name)
            continue
        packs[pack.name or ep.name] = pack
    return packs


def get_pack(name: str) -> BoardPack:
    packs = load_packs()
    if name not in packs:
        known = ", ".join(sorted(packs)) or "none installed"
        raise AbsentError(f"no board pack named {name!r}", hint=f"installed packs: {known}")
    return packs[name]
