"""Board packs declare their own settings (lane SET-PACK; ``docs/design/SETTINGS.md`` §3.2).

A pack's ``BoardPack.settings()`` returns ``Setting`` rows. Each says ``pack=<name>`` and is
keyed under the pack's prefix (``mps3.console.pace_ms``) or in a board table
(``boards.*.xvc.reach``); ``Schema.extend(rows, pack=name)`` refuses any other key, and a key
the core or another pack already declares. So KR260 and HAPS add rows, not UI or core code.

**The pack layer.** A pack row's default is the pack's own value, as that pack instance was
built (``Mps3Pack(console_pace_s=0)`` pace 0 ms), so those defaults are the resolver's ``pack``
layer: below the machine default and the user, above the schema's built-in default
(``resolve.py``). ``source == "pack"``, ``where == "the <name> pack"``.

    >>> schema, defaults = with_packs(core_schema(), packs)      # what the engine does
    >>> Resolver(schema, pack_defaults=defaults)                 # or Resolver(packs=packs)

This module is stdlib-only and never imports a pack: it only calls ``settings()``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from .schema import Schema, Setting

log = logging.getLogger(__name__)


def pack_rows(pack: Any) -> tuple[Setting, ...]:
    """The pack's rows; ``()`` for a pack that declares none (the method is optional)."""
    rows = getattr(pack, "settings", None)
    return tuple(rows()) if callable(rows) else ()


def pack_defaults(rows: Iterable[Setting]) -> dict[str, Any]:
    """The ``pack`` layer from a pack's rows: each row's default (a secret has none)."""
    return {s.key: s.default for s in rows if not s.secret and s.default is not None}


def with_packs(base: Schema, packs: Iterable[Any], *,
               on_refused: Callable[[str, Exception], None] | None = None,
               ) -> tuple[Schema, dict[str, Any]]:
    """``(a new schema: base + every pack's rows, the pack layer)``. ``base`` is not changed.

    A pack's rows join all or nothing. Rows the schema refuses (a key outside the pack's
    prefix, one declared twice) raise ``ValueError``, or, with ``on_refused``, are reported
    as ``on_refused(pack name, error)`` and left out (so is a ``settings()`` that raises):
    one broken pack cannot take the Settings menu down (the engine's choice, as
    ``core.registry`` does for a pack that fails to load). A pack whose rows ``base``
    already holds is not added again.
    """
    schema = Schema(base.rows)
    defaults: dict[str, Any] = {}
    present = {s.pack for s in base.rows if s.pack}
    for pack in packs:
        name = getattr(pack, "name", "") or ""
        try:
            rows = pack_rows(pack)
            if not rows:
                continue
            if not name:
                raise ValueError(f"a pack without a name declares {len(rows)} settings")
            if name not in present:
                schema.extend(rows, pack=name)
                present.add(name)
        except Exception as exc:  # noqa: BLE001 - with on_refused, a broken pack is reported
            if on_refused is None:
                raise
            on_refused(name or "?", exc)
            continue
        defaults.update(pack_defaults(rows))
    return schema, defaults


def log_refused(name: str, exc: Exception) -> None:
    """The engine's ``on_refused``: say which pack, and carry on without its rows."""
    log.error("board pack %r declares settings the schema refuses; its settings are not "
              "shown: %s", name, exc)


def for_engine(engine: Any) -> tuple[Schema, dict[str, Any]]:
    """``(schema, pack layer)`` for an engine: the core's rows plus those of every pack it
    has loaded (``engine.packs()``, in load order). A pack whose rows are refused is logged
    and left out, so when two packs declare one board table the first loaded keeps it: a
    table several packs read belongs in the core, as ``boards.*.power`` does. Works for any
    engine with ``packs()`` (``Engine``, ``DemoEngine``)."""
    from .resolve import core_schema

    return with_packs(core_schema(), engine.packs().values(), on_refused=log_refused)
