"""The guarded deploy service (Team T2). Board-agnostic.

It drives a board only through the session's ``DeployAdapter``
(``socharness.core.pack``) and implements the ``DeployService`` protocol
(``socharness.core.services``). One rule shapes it: a check that fails blocks,
and a check that could not be made is reported but does not block.

    deploy(session, overlay):
        preflight          any MISMATCH -> refuse. Nothing is pushed, and only
                           ``deploy.failed`` is published.
        deploy.started     {overlay, rm_id, preflight: [...]} (UNCHECKED items are listed here)
        deploy.progress    {phase, bytes, total}, one per adapter progress report
        adapter.deploy     the board-specific push; ``verified`` must be True
        confirm            re-read ``session.identity()``; its rm_id must be the overlay's
        deploy.done        {rm_id, verified}, or ``deploy.failed`` {reason} at any failing step

How a MISMATCH is refused:

- an IDENTITY mismatch (the overlay is keyed to another shell or another
  implementation run) raises ``IncompatibleError`` (exit 14);
- any other mismatch (corrupt payload, unpaired files, clearing too big for
  the arena) raises ``RefusedError`` (exit 15).

Which preflight items are identity items is fixed by name, below. A board pack
must use these names so the service can tell the two apart.

What the engine must provide: nothing. ``DeployService(None)`` works, and
publishes nothing. Given an engine, the service uses ``engine.bus`` (an
``EventBus``) when present. It also uses ``engine.store`` (a ``ContentStore``)
when present, handing it to any adapter that has a ``use_store(store)`` method,
so overlays in the content store become deployable.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from socharness.core.errors import (
    AbsentError,
    ActionFailedError,
    HarnessError,
    UnavailableError,
)
from socharness.core.events import Event
from socharness.core.model import Check
from socharness.core.pack import (
    BoardSession,
    DeployAdapter,
    DeployResult,
    OverlayRef,
    PreflightItem,
)

log = logging.getLogger(__name__)

CAPABILITY = "deploy_partial"

# Preflight item names. A board pack's adapter uses these exact strings.
ITEM_CONTROL = "control channel free"
ITEM_SHELL_ID = "shell_id matches"
ITEM_FILES = "crc and length"
ITEM_PAIR = "clearing pairs partial"
ITEM_CLEARING_FITS = "clearing fits"
ITEM_TRANSPORT = "transport"
ITEM_USERCODE = "static_usercode matches"

#: A MISMATCH on one of these means "built for a different board", so the
#: service raises IncompatibleError. A MISMATCH on any other item is a refusal.
IDENTITY_ITEMS = frozenset({ITEM_SHELL_ID, ITEM_USERCODE})


def mismatches(items: Sequence[PreflightItem]) -> list[PreflightItem]:
    return [i for i in items if i.check == Check.MISMATCH]


def mark_identity(items: Sequence[PreflightItem]) -> list[PreflightItem]:
    """Set ``identity=True`` on the identity items (shell_id, static_usercode)."""
    import dataclasses

    return [dataclasses.replace(i, identity=True) if i.name in IDENTITY_ITEMS and not i.identity
            else i for i in items]


def refusal(items: Sequence[PreflightItem], overlay_name: str) -> HarnessError | None:
    """The error to raise for a failed preflight, or None (delegates to the core rule)."""
    from socharness.core.pack import preflight_refusal

    return preflight_refusal(mark_identity(items), overlay_name)


def _same_id(a: str, b: str) -> bool:
    """Compare two hex ids by value ("0x0100001E" == "0x0100001e"); fall back to text."""
    try:
        return int(a, 0) == int(b, 0)
    except (TypeError, ValueError):
        return a.strip().lower() == b.strip().lower()


def _item_dict(item: PreflightItem) -> dict[str, str]:
    return {"name": item.name, "check": Check(item.check).value, "detail": item.detail}


class DeployService:
    """Implements ``socharness.core.services.DeployService``."""

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    # -- plumbing ---------------------------------------------------------------------

    def _adapter(self, session: BoardSession) -> DeployAdapter:
        adapter = getattr(session, "deploy", None)
        if adapter is None:
            raise UnavailableError(
                CAPABILITY, "this session has no link that can program partitions "
                            "(the MPS3 needs its Ethernet link to the shell)")
        store = getattr(self._engine, "store", None)
        use_store = getattr(adapter, "use_store", None)
        if store is not None and callable(use_store):
            use_store(store)
        return adapter

    def _publish(self, session: BoardSession, topic: str, **data: Any) -> None:
        bus = getattr(self._engine, "bus", None)
        if bus is None:
            return
        candidate = getattr(session, "candidate", None)
        bus.publish(Event(topic, getattr(candidate, "board_id", ""), data))

    # -- queries ----------------------------------------------------------------------

    def overlays(self, session: BoardSession) -> Sequence[OverlayRef]:
        return list(self._adapter(session).overlays())

    def preflight(self, session: BoardSession, overlay: OverlayRef) -> Sequence[PreflightItem]:
        return list(self._adapter(session).preflight(overlay))

    def compatible(self, session: BoardSession) -> tuple[list[OverlayRef], dict[str, str]]:
        """(loadable overlays, {overlay name: why it cannot load}).

        Runs the full preflight for every overlay. An overlay is loadable when
        nothing MISMATCHes; UNCHECKED items do not make it incompatible.
        """
        adapter = self._adapter(session)
        loadable: list[OverlayRef] = []
        reasons: dict[str, str] = {}
        for ov in adapter.overlays():
            bad = mismatches(adapter.preflight(ov))
            if not bad:
                loadable.append(ov)
                continue
            key = ov.name if ov.name not in reasons else f"{ov.name} ({ov.static_id}, {ov.source})"
            reasons[key] = "; ".join(f"{i.name}: {i.detail}" for i in bad)
        return loadable, reasons

    # -- actions ----------------------------------------------------------------------

    def deploy(self, session: BoardSession, overlay: OverlayRef) -> DeployResult:
        adapter = self._adapter(session)
        try:
            items = list(adapter.preflight(overlay))
        except HarnessError as exc:
            self._publish(session, "deploy.failed", reason=str(exc), overlay=overlay.name,
                          stage="preflight")
            raise
        err = refusal(items, overlay.name)
        if err is not None:
            self._publish(session, "deploy.failed", reason=str(err), overlay=overlay.name,
                          stage="preflight")
            raise err

        self._publish(session, "deploy.started", overlay=overlay.name, rm_id=overlay.rm_id,
                      preflight=[_item_dict(i) for i in items])

        def progress(phase: str, done: int, total: int) -> None:
            self._publish(session, "deploy.progress", phase=phase, bytes=done, total=total)

        stage = "deploy"
        try:
            result = adapter.deploy(overlay, progress)
            if not result.verified:
                raise ActionFailedError(
                    f"the board did not verify {overlay.name} (verified: false)",
                    hint="the partition may be left decoupled; restore the baseline")
            stage = "confirm"
            ident = session.identity()
            if not _same_id(ident.rm_id, overlay.rm_id):
                raise ActionFailedError(
                    f"after deploying {overlay.name} the board reports rm_id "
                    f"{ident.rm_id or '(none)'}, expected {overlay.rm_id}",
                    hint="read `info` again; if it persists, restore the baseline")
        except Exception as exc:
            self._publish(session, "deploy.failed", reason=str(exc), overlay=overlay.name,
                          stage=stage)
            raise
        self._publish(session, "deploy.done", rm_id=ident.rm_id, verified=True,
                      overlay=overlay.name, seconds=result.seconds, transport=result.transport)
        return result

    def restore_baseline(self, session: BoardSession) -> DeployResult:
        base = self._adapter(session).baseline()
        if base is None:
            raise AbsentError(
                "no baseline overlay (greybox) is known for the running shell",
                hint="add the greybox overlay built for this shell to the overlay directories")
        return self.deploy(session, base)
