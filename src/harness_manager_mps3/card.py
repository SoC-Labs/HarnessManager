"""The MPS3's user microSD (D13 overlay store) for Harness Manager (lane LINUX-SLOTS).

``make_card_adapter(session)`` is the ``pack.py`` hook (``BoardSession.card``, CCR LS-1).
Over pyverify's ``usd`` verb and the v0.13 re-push ``commit`` (net-protocol.md "User
microSD"; docs/planning/HANDOVER_USD_OVERLAY_STORE.md):

- ``status``: card present?, the store's state, its capacity, the DEFAULT overlay the
  board loads at power-on (rm, static, A/B store slot), the power-on decision, and, on a
  Linux harness, the OS boot slots on the same card (``BoardSession.os_slots``);
- ``commit``: make the RUNNING pair the power-on default. The shell never holds a whole
  partial (they stream straight into the ICAP), so persisting is a RE-PUSH of the same
  clearing + partial from this host's overlay store into the card's inactive store slot
  (``SwapOrchestrator.commit``: ``commit`` parks 6900, the pair goes over 6910, the shell
  reads it back and only then flips the header). Only what is running can be committed:
  the running ``rm_id`` and the shell's own ``static_id``;
- ``clear``: invalidate the default, so the next power-on boots the greybox.

**david's hard rule: no card -> the boot is unchanged.** With no card every mutation is
refused before anything is sent (``usd`` status is read, nothing else), and the error
says the board keeps booting as it always has.

**Which harness.** A harness that does not report the ``usd`` feature has no store
(today's fielded bare metal): ``card_reason()`` says so and nothing is sent. D13 runs on
both engines, so a bare-metal image built with it has a card too.
"""

from __future__ import annotations

import logging
from typing import Any

from pyverify import rm_id as rmid
from pyverify.client import IDENTITY_LOCK_PREFIX
from pyverify.pusher import BitstreamPusher
from pyverify.swap import SwapOrchestrator

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HarnessError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
)
from harness_manager.core.pack import CardStatus, Progress

from .constants import IMPL_LINUX

log = logging.getLogger(__name__)

CAPABILITY = "user microSD"
USD_FEATURE = "usd"
#: The control connection parks for the whole commit (write + read-back of ~3 MB on the
#: card): the swap's own budget.
COMMIT_TIMEOUT_S = 300.0
NO_CARD_HINT = ("insert a card in the board's user microSD slot; without one the board boots "
                "exactly as it always has")


def _hex32(value: Any) -> str:
    try:
        return rmid.format_rm_id(value)
    except (TypeError, ValueError):
        return str(value or "")


def _same(a: Any, b: Any) -> bool:
    try:
        return rmid.parse_rm_id(a) == rmid.parse_rm_id(b)
    except (TypeError, ValueError):
        return False


def card_json(st: CardStatus) -> dict[str, Any]:
    """What the CLI and the API show of a ``CardStatus`` (``services.slots``)."""
    from harness_manager.services.slots import card_status_json

    return card_status_json(st)


class Mps3Card:
    """``core.pack.CardAdapter`` for an MPS3 session (module docstring)."""

    def __init__(self, session: Any, *, commit_timeout_s: float = COMMIT_TIMEOUT_S) -> None:
        self._session = session
        self.commit_timeout_s = commit_timeout_s
        #: The pusher the last commit built (tests read its transport).
        self.last_pusher: BitstreamPusher | None = None

    # -- plumbing -------------------------------------------------------------------------------

    def _shell(self) -> Any:
        shell = getattr(self._session, "shell", None)
        if shell is None:
            raise UnavailableError(CAPABILITY, "no Ethernet link to the harness")
        return shell

    def _catalogue(self) -> Any:
        deploy = getattr(self._session, "deploy", None)
        cat = getattr(deploy, "catalogue", None)
        if cat is None:
            from .overlays import default_catalogue

            cat = default_catalogue()
        return cat

    def _rm_name(self, rm_id: Any) -> str:
        from .overlays import resolve_rm_name

        try:
            return resolve_rm_name(rm_id, self._catalogue())
        except Exception:  # noqa: BLE001 - a name is never worth failing a status for
            return ""

    def card_reason(self) -> str:
        try:
            live = self._shell().live()
        except HarnessError as exc:
            return f"the harness did not answer: {exc.message}"
        if not live.version_ok:
            return "the harness does not answer 'version': it predates the user-microSD store"
        if USD_FEATURE not in live.features:
            return (f"this {live.impl or 'bare-metal'} harness has no user-microSD store (it "
                    "does not report the 'usd' feature; net-protocol v0.13, D13)")
        return ""

    def _need(self) -> None:
        reason = self.card_reason()
        if reason:
            raise UnavailableError(CAPABILITY, reason)

    def _usd(self) -> Any:
        resp = self._shell().call(lambda c: c.usd())
        if not resp.ok:
            raise _usd_error("usd status", resp.err)
        return resp

    # -- status ---------------------------------------------------------------------------------

    def status(self) -> CardStatus:
        self._need()
        resp = self._usd()
        return self._status_from(resp)

    def _status_from(self, resp: Any) -> CardStatus:
        notes: list[str] = []
        default = None
        if resp.default is not None:
            d = resp.default
            default = {"rm_id": _hex32(d.rm_id), "rm_name": self._rm_name(d.rm_id),
                       "static_id": _hex32(d.static_id) if d.static_id else "", "slot": d.slot}
        if resp.state == "stale" and default is not None:
            notes.append(f"the default was committed for static {default['static_id'] or '?'}, "
                         "not this shell's: it is skipped at power-on (commit one built for "
                         "this shell)")
        if resp.state == "foreign":
            notes.append("the card holds no harness store (a PC card?): it is never written; "
                         "`pyverify usd format` makes it a harness card")
        if not resp.present:
            notes.append("no card: the board boots exactly as it always has")
        os_st = None
        slots = getattr(self._session, "os_slots", None)
        if resp.present and slots is not None:
            try:
                live = self._shell().live()
                if live.impl == IMPL_LINUX and not slots.slots_reason():
                    os_st = slots.status()
            except HarnessError as exc:
                notes.append(f"the OS slots could not be read: {exc.message}")
        return CardStatus(present=bool(resp.present), state=resp.state, text=resp.text,
                          card_mb=resp.card_mb, default=default, boot=resp.boot,
                          committable=bool(resp.committable), os_slots=os_st,
                          notes=tuple(notes), raw=dict(resp.raw))

    # -- clear ----------------------------------------------------------------------------------

    def clear(self) -> CardStatus:
        self._need()
        st = self._usd()
        if not st.present:
            raise RefusedError("no card in the user microSD slot: nothing was changed",
                               hint=NO_CARD_HINT)
        if st.state == "foreign":
            raise RefusedError("the card holds no harness store (foreign): it is never "
                               "written, and has no default to clear")
        resp = self._shell().call(lambda c: c.usd_clear())
        if not resp.ok:
            raise _usd_error("card clear", resp.err)
        return self.status()

    # -- commit ---------------------------------------------------------------------------------

    def commit(self, progress: Progress | None = None) -> dict[str, Any]:
        report: Progress = progress or (lambda p, d, t: None)
        self._need()
        st = self._usd()
        if not st.present:
            raise RefusedError("no card in the user microSD slot: nothing was written",
                               hint=NO_CARD_HINT)
        if not st.committable:
            hint = ("a card with no harness store: format it first (`pyverify usd format`)"
                    if st.state == "foreign" else "`harness-manager card status TARGET` says why")
            raise RefusedError(f"the card cannot take a commit now (state {st.state!r}: "
                               f"{st.text or 'no text'})", hint=hint)
        shell = self._shell()
        live = shell.live()
        if not live.rm_id or rmid.is_greybox(live.rm_id):
            raise RefusedError("nothing to persist: the partition runs the greybox",
                               hint="deploy an overlay first; `harness-manager card clear "
                                    "TARGET` makes the greybox the power-on default")
        entry = self._entry(live.rm_id, live.shell_id)
        name = entry.ref.name
        deploy = getattr(self._session, "deploy", None)
        port = getattr(deploy, "push_port", None) or getattr(self._session, "push_port", None)
        from .constants import PUSH_PORT
        from .deploy import TRANSPORT_TCP, push_timeout_s

        pusher = BitstreamPusher(host=shell.host, transport="tcp", tcp_port=port or PUSH_PORT,
                                 windowed="windowed" in live.features,
                                 timeout_s=push_timeout_s(live.impl, TRANSPORT_TCP))
        self.last_pusher = pusher
        total = entry.overlay.manifest.clearing.len + entry.overlay.manifest.partial.len
        report("commit", 0, total)
        from .shell import Mps3Shell

        parked = Mps3Shell(shell.host, shell.port, timeout=self.commit_timeout_s)
        reply = parked.call(lambda c: SwapOrchestrator(c, pusher, commit_pusher=pusher).commit(
            entry.overlay, rm_id=rmid.parse_rm_id(live.rm_id),
            static_id=rmid.parse_rm_id(live.shell_id), features=tuple(live.features)))
        if not reply.ok:
            raise _usd_error(f"card commit of {name}", reply.err)
        report("commit", total, total)
        return {"slot": reply.slot, "rm_name": name, "rm_id": _hex32(live.rm_id),
                "static_id": _hex32(live.shell_id), "bytes": total}

    def _entry(self, rm_id: str, shell_id: str) -> Any:
        """The running pair in this host's overlay store: same rm_id, keyed to this shell."""
        cat = self._catalogue()
        for e in cat.entries():
            m = e.overlay.manifest
            if _same(m.rm_id, rm_id) and _same(m.static_id, shell_id):
                return e
        name = self._rm_name(rm_id) or "the running RM"
        raise AbsentError(
            f"{name} ({_hex32(rm_id)} for shell {_hex32(shell_id)}) is not in this host's "
            "overlay store, so its pair cannot be re-pushed to the card",
            hint="import or deploy it from Harness Manager first (the card commit re-pushes "
                 "the running pair; the shell keeps no copy)")


def _usd_error(what: str, err: str) -> HarnessError:
    """A ``usd``/``commit`` refusal (contract error NAMES) -> the error class it means."""
    e = (err or "").strip()
    if e.startswith("unknown op"):
        return UnavailableError(CAPABILITY, f"this harness does not have the usd verb ({e})")
    if e in ("no card", "no sd card"):
        return RefusedError(f"{what}: no card in the user microSD slot; nothing was written",
                            hint=NO_CARD_HINT)
    if e == "no hw":
        return UnavailableError(CAPABILITY, "the fabric has no user-microSD controller (usd_spi)")
    if e == "foreign":
        return RefusedError(f"{what}: the card holds no harness store (foreign); it is never "
                            "written", hint="`pyverify usd format` makes it a harness card")
    if e == "stale key":
        return IncompatibleError(f"{what}: the pair is not keyed to this shell (stale key)")
    if e == "rm mismatch":
        return IncompatibleError(f"{what}: that is not the RM running now (rm mismatch)",
                                 hint="only the running pair can be committed")
    if e == "store busy":
        return HeldError(f"{what}: the card store is busy", hint="try again in a moment")
    if e.startswith(IDENTITY_LOCK_PREFIX):
        return RefusedError(f"{what}: {e}",
                            hint="the harness's identity is locked; `harness-manager info "
                                 "TARGET` shows why")
    if e == "unavailable":
        return UnavailableError(CAPABILITY, "the harness's card store is unavailable")
    return ActionFailedError(f"{what} failed: {e or 'no reason given'}",
                             hint="the card keeps its previous default")


def make_card_adapter(session: Any) -> Mps3Card | None:
    """The pack hook (CCR LS-1): an adapter for any session with an Ethernet shell. Whether
    the harness has a store is ``card_reason()``."""
    if getattr(session, "shell", None) is None:
        return None
    return Mps3Card(session)
