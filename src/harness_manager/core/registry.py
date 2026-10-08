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


def preflight() -> None:
    """Fail at service start, with one clear message, when a pack's dependencies are wrong.

    A pack package may carry a ``selfcheck`` module with ``check()`` (raising a
    ``HarnessError``). Today: ``harness_manager_mps3.selfcheck`` (the loaded pyverify).
    """
    import importlib
    import importlib.util

    seen: set[str] = set()
    for ep in entry_points(group=GROUP):
        top = ep.value.split(":")[0].split(".")[0]
        if top in seen:
            continue
        seen.add(top)
        if importlib.util.find_spec(f"{top}.selfcheck") is not None:
            importlib.import_module(f"{top}.selfcheck").check()
