"""Guarded partition deploy for the MPS3 (Team T2).

``make_deploy_adapter(session)`` is the hook ``pack.py`` calls. It returns an
``Mps3Deploy`` when the session has an Ethernet link to the shell, else None.

The shell protocol is spoken only through pyverify: ``SwapOrchestrator``
sequences ``swap_begin -> push clearing+partial -> swap_await``, and
``BitstreamPusher`` frames and sends. The order is load-bearing; pyverify owns it
(``pyverify/swap.py``, proven on silicon 2026-07-14).

Preflight. Every item is OK, MISMATCH or UNCHECKED, and only MISMATCH blocks.
Item names come from ``harness_manager.services.deploy``.

- control channel free: 6900 answered. It serves one client at a time, and a
  held port raises ``HeldError``.
- (a) shell_id matches: the manifest's ``static_id`` equals ``ping.shell_id``,
  read live.
- (b) crc and length: ``pyverify.overlay.Overlay.validate`` over both files.
- (c) clearing pairs partial: two distinct files named ``<rm>_clear.bin`` /
  ``<rm>.bin``, both framed with one rm_id.
- (d) clearing fits: the clearing is at most the harness's ``clr_max`` (an
  additive key in ``version``, else in ``stats``), default 262144 B, the
  bare-metal firmware's clearing arena. Source: ``firmware/platform/Makefile``
  SWAP_CLEARING_ARENA_BYTES and CLEARING_RAM_BYTES; gate
  ``scripts/harness_gates/check_clearing_fits.py``. A bigger clearing would be
  staged to QSPI, which has never worked on silicon, and the next swap-away
  would fail closed at SWAP_STREAM_CLEARING.
- (e) transport, first rule that applies:

  1. ``version.features`` has "windowed": tcp + windowed on 6910. A windowed
     shell deadlocks against a plain push (OVER_THE_WIRE_DEPLOY_STATUS.md (2)).
  2. ``version.impl == "linux"``: plain tcp on 6910. ``mps3-harnessd`` builds
     config_agent plain (the kernel paces TCP), so "windowed" is absent by
     design (Linux plan §10a S2). Never TFTP: swapping away from a DAP RM streams
     its clearing into the ICAP first, and a partial that arrives meanwhile is
     PARKED on 6910 but REJECTED over TFTP (silicon B1 v4, 2026-09-25).
  3. the link is a TCP tunnel (``is_tunnelled``): plain tcp. TFTP is UDP and
     cannot cross an SSH port forward or a hub WSS tunnel.
  4. otherwise tftp.

  A shell with no ``version`` verb gets tftp (tcp through a tunnel), UNCHECKED.

  The push inactivity limit (``BitstreamPusher.timeout_s``: per TFTP packet, per
  TCP chunk) is pyverify's rule (``pyverify.pusher.choose_push``): a tcp push to
  the Linux harness gets ``LINUX_PUSH_TIMEOUT_S`` (30 s, the harness's own
  ``MPS3_SWAP_AWAIT_IDLE_MS``), because the send blocks while 6910 parks the
  partial; every other push keeps ``DEFAULT_PUSH_TIMEOUT_S`` (2 s).
- (f) static_usercode matches: UNCHECKED ("needs JTAG") unless the running
  static's USERCODE is provided. The shell cannot report it; see
  ``gen_manifest.py`` on why ``static_id`` alone cannot catch a wrong static.

``deploy()`` runs the same preflight again before any push, so the adapter is
safe even when called without the service: a ``static_id`` mismatch raises
``IncompatibleError`` before a single byte leaves the host.

Keep on the card (L1 = option a, david 2026-09-25). pyverify's deploy commits the pair
to the board's user microSD after a verified swap (net-protocol v0.13, D13: ``commit``,
a re-push over 6910 into the card's inactive slot) when ``persist=True``. This adapter
passes ``persist=keep_on_card``, which is False unless the caller asks, so a deploy never
writes the card by default. Asked, the result's ``card`` says what pyverify did: kept
(the slot), or why not (no card, a card without the store, a failed write; a card that
fails never fails the deploy, the swap already stands). ``card_status()`` reads the
card (``version`` for the ``usd`` feature, then ``usd``) without writing anything; the
service refuses a keep from it before a byte is pushed. The commit's push is always TCP
on 6910 (``commit`` takes no TFTP), windowed exactly when the swap's is, and reports
progress as the ``card`` phase.

The shell's own refusals. When the card's image and the running fabric disagree
the shell refuses ``swap`` with a distinct error line (Linux plan §10a S1; the
string is TBD, see ``constants.FABRIC_MISMATCH_ERRS``): that is
``IncompatibleError``. A refusal arrives before the push is expected, so the
push is RESET; the pending reply is then read (1 s) to say why. EBUSY is
``HeldError``.

Push ports. The host is always the session's shell host. The ports resolve in
this order, first hit wins:

1. the ``push_port=`` / ``tftp_port=`` constructor arguments;
2. ``session.push_port`` / ``session.tftp_port``, if the pack ever sets them;
3. ``$HARNESS_MANAGER_MPS3_PUSH_PORT`` / ``$HARNESS_MANAGER_MPS3_TFTP_PORT``, which is
   what tests set, via monkeypatch, to reach a FakeShell on ephemeral ports;
4. the real ports: 6910 (raw/windowed TCP) and 69 (TFTP).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pyverify import rm_id as rmid
from pyverify.client import ShellClient, ShellProtocolError, SocketTransport
from pyverify.overlay import OverlayValidationError
from pyverify.pusher import (
    DEFAULT_ACK_WINDOW,
    DEFAULT_PUSH_TIMEOUT_S,
    HEADER_SIZE,
    LINUX_PUSH_TIMEOUT_S,
    TFTP_PORT,
    BitstreamKind,
    BitstreamPusher,
    PushError,
)
from pyverify.swap import (
    PERSIST_COMMITTED,
    PERSIST_FAILED,
    PERSIST_SKIPPED,
    PersistResult,
    SwapError,
    SwapOrchestrator,
)

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import Check, LinkKind
from harness_manager.core.pack import (
    CardOutcome,
    CardStatus,
    DeployResult,
    OverlayRef,
    PreflightItem,
    Progress,
)
from harness_manager.services.deploy import (
    ITEM_CLEARING_FITS,
    ITEM_CONTROL,
    ITEM_FILES,
    ITEM_PAIR,
    ITEM_SHELL_ID,
    ITEM_TRANSPORT,
    ITEM_USERCODE,
    NO_CARD,
    NO_STORE,
    refusal,
)

from . import constants
from .constants import FABRIC_MISMATCH_ERRS, IMPL_LINUX, PUSH_PORT
from .overlays import CatalogueEntry, OverlayCatalogue
from .shell import Mps3Shell, ShellLive, _ShellBusy, _TapTransport

log = logging.getLogger(__name__)

PUSH_PORT_ENV = "HARNESS_MANAGER_MPS3_PUSH_PORT"
TFTP_PORT_ENV = "HARNESS_MANAGER_MPS3_TFTP_PORT"
#: Set to 1 when 6900/6910 are reached through a TCP-only tunnel that the link
#: detail does not mark (a hand-made ``ssh -L``): TFTP cannot cross it.
TUNNEL_ENV = "HARNESS_MANAGER_MPS3_TUNNELLED"

#: firmware/platform/Makefile:187,193 (SWAP_CLEARING_ARENA_BYTES, CLEARING_RAM_BYTES).
#: The default when the harness reports no ``clr_max``.
CLEARING_ARENA_BYTES = constants.CLEARING_ARENA_BYTES

#: The shell parks 6900 for the whole reconfiguration. The silicon-proven recipe
#: uses 300 s (OVER_THE_WIRE_DEPLOY_STATUS.md (3)); a real swap takes 3–7 s.
SWAP_TIMEOUT_S = 300.0

TRANSPORT_WINDOWED = "tcp+windowed"
TRANSPORT_TCP = "tcp"
TRANSPORT_TFTP = "tftp"
WINDOWED_FEATURE = "windowed"

# Progress phases, in order. ``push`` counts payload bytes (clearing + partial).
PHASE_GUARD = "guard"
PHASE_SWAP = "swap"
PHASE_PUSH = "push"
PHASE_VERIFY = "verify"
PHASE_CARD = "card"         # only when asked to keep the design on the card

#: The ``version.features`` name of the user-microSD store (net-protocol v0.13).
USD_FEATURE = "usd"
#: ``usd`` errors that mean the harness has no store at all (net-protocol v0.13).
USD_NO_STORE_ERRS = frozenset({"no hw", "unavailable"})


def make_deploy_adapter(session: Any) -> Mps3Deploy | None:
    """The ``pack.py`` hook. None when the session has no shell (no Ethernet link)."""
    shell = getattr(session, "shell", None)
    if shell is None:
        return None
    return Mps3Deploy(shell,
                      push_port=getattr(session, "push_port", None),
                      tftp_port=getattr(session, "tftp_port", None),
                      tunnelled=is_tunnelled(session))


def is_tunnelled(session: Any) -> bool:
    """True when the shell is reached through a TCP-only tunnel, so TFTP cannot be used.

    A loopback address alone does NOT mean a tunnel (every test fake is on
    127.0.0.1). The signals, any of which is enough:

    - ``$HARNESS_MANAGER_MPS3_TUNNELLED`` is 1/true/yes/on (0/false/no/off forces False);
    - the candidate has a ``HUB`` link (fpgahub reaches boards over WSS TCP tunnels);
    - the Ethernet link's ``detail`` says "tunnel" (the convention for SSH port
      forwards and hub tunnels until ``Link`` can say so itself; CCR T12-4).
    """
    env = os.environ.get(TUNNEL_ENV, "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    candidate = getattr(session, "candidate", None)
    for link in getattr(candidate, "links", ()) or ():
        if link.kind == LinkKind.HUB or getattr(link, "via", "") in ("ssh", "hub"):
            return True
        if link.kind == LinkKind.ETHERNET and "tunnel" in (link.detail or "").lower():
            return True
    return False


def _env_port(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        port = int(raw, 10)
    except ValueError as exc:
        raise UsageError(f"${name}={raw!r} is not a port number") from exc
    if not 0 < port < 65536:
        raise UsageError(f"${name}={port} is out of range")
    return port


@dataclass(frozen=True)
class _Live:
    """One live read of the shell: ``ping`` and ``version`` over a single 6900 connection."""

    shell_id: str
    rm_id: str
    version_ok: bool
    features: tuple[str, ...]
    impl: str = ""                 # "linux" | "bare-metal" | "" (no `version` verb)
    clr_max: int | None = None     # the harness's own clearing limit, when it reports one


@dataclass(frozen=True)
class _Assessment:
    entry: CatalogueEntry
    items: tuple[PreflightItem, ...]
    transport: str
    #: ``BitstreamPusher.timeout_s`` for this push (``push_timeout_s``)
    push_timeout_s: float = DEFAULT_PUSH_TIMEOUT_S
    #: the harness's ``version.impl`` ("linux", "bare-metal" or "")
    impl: str = ""


class _ReportingPusher(BitstreamPusher):
    """``BitstreamPusher`` reporting each sent frame. Framing, ordering and sockets stay pyverify's.

    ``_send`` is the documented injection seam (pyverify/pusher.py); it is
    called once per frame, clearing then partial.
    """

    def __init__(self, *, on_frame: Callable[[BitstreamKind, int], None], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._on_frame = on_frame

    def _send(self, frame: bytes, *, kind: BitstreamKind):  # type: ignore[override]
        result = super()._send(frame, kind=kind)
        self._on_frame(kind, len(frame) - HEADER_SIZE)
        return result


class _ReportingClient:
    """A ``ShellClient`` proxy that reports when the swap parks and when the reply is awaited."""

    def __init__(self, client: ShellClient, report: Progress, total: int) -> None:
        self._client = client
        self._report = report
        self._total = total
        #: ``swap`` was sent: the harness may now hold a parked swap.
        self.parked = False
        #: The swap's reply was read (or a refusal was): nothing is left parked.
        self.settled = False
        #: ``commit`` (Keep on the card) was sent / its reply was read.
        self.committing = False
        self.committed_reply = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def swap_begin(self, rm: str, src: str = "tftp") -> None:
        self._client.swap_begin(rm, src)
        self.parked = True
        self._report(PHASE_SWAP, 1, 1)
        self._report(PHASE_PUSH, 0, self._total)

    def swap_await(self):
        self._report(PHASE_VERIFY, 0, 1)
        resp = self._client.swap_await()
        self.settled = True
        return resp

    def commit_begin(self, *args: Any, **kwargs: Any) -> None:
        # pyverify commits only after a VERIFIED swap: verify is done, the card is next.
        self._report(PHASE_VERIFY, 1, 1)
        self._client.commit_begin(*args, **kwargs)
        self.committing = True
        self._report(PHASE_CARD, 0, self._total)

    def commit_await(self):
        resp = self._client.commit_await()
        self.committed_reply = True
        return resp


class Mps3Deploy:
    """The MPS3 ``DeployAdapter``."""

    def __init__(self, shell: Mps3Shell, *, catalogue: OverlayCatalogue | None = None,
                 push_port: int | None = None, tftp_port: int | None = None,
                 swap_timeout_s: float = SWAP_TIMEOUT_S,
                 running_usercode: str | int | None = None,
                 tunnelled: bool = False) -> None:
        self._shell = shell
        #: 6900/6910 are reached through a TCP-only tunnel: never choose TFTP.
        self.tunnelled = tunnelled
        self.catalogue = catalogue if catalogue is not None else OverlayCatalogue()
        self._push_port = push_port
        self._tftp_port = tftp_port
        self.swap_timeout_s = swap_timeout_s
        #: The running static's REGISTER.USERCODE, when something read it over JTAG.
        #: None means preflight (f) stays UNCHECKED.
        self.running_usercode = running_usercode
        #: The pusher the last deploy built. Tests read it to prove the transport choice.
        self.last_pusher: BitstreamPusher | None = None
        #: The pusher a kept deploy built for the card's commit (None when not asked).
        self.last_commit_pusher: BitstreamPusher | None = None

    # -- configuration ----------------------------------------------------------------

    def use_store(self, store: Any) -> None:
        """Make overlays in the engine's content store deployable (idempotent)."""
        self.catalogue.use_store(store)

    @property
    def push_port(self) -> int:
        return self._push_port or _env_port(PUSH_PORT_ENV) or PUSH_PORT

    @property
    def tftp_port(self) -> int:
        return self._tftp_port or _env_port(TFTP_PORT_ENV) or TFTP_PORT

    # -- DeployAdapter ----------------------------------------------------------------

    def overlays(self) -> Sequence[OverlayRef]:
        return self.catalogue.refs()

    def rm_name(self, rm_id: object) -> str:
        return self.catalogue.rm_name(rm_id)

    def baseline(self) -> OverlayRef | None:
        """The greybox built for the RUNNING shell (live ping), if the catalogue has one."""
        live = self._live()
        entry = self.catalogue.greybox_for(live.shell_id) if live.shell_id else None
        return entry.ref if entry is not None else None

    def preflight(self, overlay: OverlayRef) -> Sequence[PreflightItem]:
        from harness_manager.services.deploy import mark_identity

        return mark_identity(self._assess(overlay).items)

    def card_status(self) -> CardStatus:
        """The user microSD, read on one 6900 connection (``version`` then ``usd``).
        Writes nothing. ``reason`` is why a deploy cannot keep its design on it now."""
        def ask(c: ShellClient, _tap: _TapTransport) -> CardStatus:
            ver = c.version()
            features = tuple(ver.features) if getattr(ver, "ok", False) else ()
            if USD_FEATURE not in features:
                return CardStatus(store=False, reason=NO_STORE)
            read = getattr(c, "usd", None)
            if not callable(read):
                return CardStatus(store=True, reason="this Harness Manager's pyverify cannot "
                                                      "read the card (no `usd` verb)")
            return card_status_from(read())

        return self._shell.call_raw(ask)

    def commit_pusher(self, *, windowed: bool, impl: str,
                      on_frame: Callable[[BitstreamKind, int], None] | None = None,
                      ) -> BitstreamPusher:
        """The pusher a card ``commit`` sends its pair with: 6910 only, windowed exactly when
        the swap's push is (a WINDOWED shell deadlocks on a plain push), the Linux stall
        limit. "Keep on the card" and ``harness-manager card commit`` (LINUX-SLOTS) share it."""
        return _ReportingPusher(on_frame=on_frame or (lambda kind, n: None),
                                host=self._shell.host, transport="tcp", tcp_port=self.push_port,
                                windowed=windowed, window=DEFAULT_ACK_WINDOW,
                                timeout_s=push_timeout_s(impl, TRANSPORT_TCP))

    def deploy(self, overlay: OverlayRef, progress: Progress | None = None, *,
               keep_on_card: bool = False) -> DeployResult:
        report: Progress = progress or (lambda phase, done, total: None)
        started = time.monotonic()

        report(PHASE_GUARD, 0, 1)
        assessment = self._assess(overlay)
        err = refusal(assessment.items, overlay.name)
        if err is not None:
            raise err
        report(PHASE_GUARD, 1, 1)

        ov = assessment.entry.overlay
        clearing_len = ov.manifest.clearing.len
        total = clearing_len + ov.manifest.partial.len
        sent = {BitstreamKind.CLEARING: 0, BitstreamKind.PARTIAL: 0}

        def on_frame(kind: BitstreamKind, payload_bytes: int) -> None:
            sent[kind] = payload_bytes
            report(PHASE_PUSH, sum(sent.values()), total)

        kept = {BitstreamKind.CLEARING: 0, BitstreamKind.PARTIAL: 0}

        def on_card_frame(kind: BitstreamKind, payload_bytes: int) -> None:
            kept[kind] = payload_bytes
            report(PHASE_CARD, sum(kept.values()), total)

        host = self._shell.host
        timeout_s = assessment.push_timeout_s
        if assessment.transport == TRANSPORT_WINDOWED:
            pusher = _ReportingPusher(on_frame=on_frame, host=host, transport="tcp",
                                      tcp_port=self.push_port, windowed=True,
                                      window=DEFAULT_ACK_WINDOW, timeout_s=timeout_s)
            src = "tcp"
        elif assessment.transport == TRANSPORT_TCP:
            pusher = _ReportingPusher(on_frame=on_frame, host=host, transport="tcp",
                                      tcp_port=self.push_port, windowed=False,
                                      timeout_s=timeout_s)
            src = "tcp"
        else:
            pusher = _ReportingPusher(on_frame=on_frame, host=host, transport="tftp",
                                      tftp_port=self.tftp_port, timeout_s=timeout_s)
            src = "tftp"
        self.last_pusher = pusher
        # Keep on the card: ``commit`` takes its pair over 6910 only, windowed exactly
        # when the swap's push is (a WINDOWED shell deadlocks on a plain push).
        commit_pusher = None
        if keep_on_card:
            commit_pusher = self.commit_pusher(
                windowed=assessment.transport == TRANSPORT_WINDOWED, impl=assessment.impl,
                on_frame=on_card_frame)
        self.last_commit_pusher = commit_pusher

        tap: _TapTransport | None = None
        swap: _ReportingClient | None = None
        try:
            tap = _TapTransport(_TimedSocketTransport(host, self._shell.port, self.swap_timeout_s))
            client = ShellClient(host, port=self._shell.port, timeout=self.swap_timeout_s,
                                 transport=tap)
            with client:
                swap = _ReportingClient(client, report, total)
                orchestrator = SwapOrchestrator(swap, pusher, commit_pusher=commit_pusher)
                try:
                    # persist: pyverify's deploy also commits the pair to the user microSD
                    # after a verified swap when asked (net-protocol v0.13, D1). Only
                    # "Keep on the card" asks; by default the deploy is the swap only.
                    res = orchestrator.deploy(ov, src=src, persist=keep_on_card)
                except PushError as exc:
                    # A shell that REFUSED the swap replied at once and never armed the
                    # push, so the push was reset: read that reply to say why. Only the
                    # DISTINCT refusals (fabric mismatch, EBUSY) replace the push error; a
                    # swap that failed because the push failed is reported as the push.
                    early = _pending_reply(tap)
                    if early is not None:
                        swap.settled = True       # the shell answered: nothing is parked
                    err = str((early or {}).get("err", ""))
                    if early is not None and (_is_fabric_mismatch(err)
                                              or err.strip().upper() == "EBUSY"):
                        raise _refusal_error(early, overlay) from exc
                    said = f"; the shell then said: {err}" if err else ""
                    raise ActionFailedError(
                        f"bitstream push of {overlay.name} failed: {exc}{said}",
                        hint="the swap was parked and will time out with the partition "
                             "decoupled; restore the baseline") from exc
        except _ShellBusy as exc:
            raise HeldError(f"the shell is busy (EBUSY): {overlay.name} was not deployed",
                            hint="another client holds the control port, or a swap is running",
                            holder=str(exc.reply.get("holder") or "")) from exc
        except SwapError as exc:
            raise _swap_error(exc, overlay, tap.last if tap is not None else {}) from exc
        except PushError as exc:
            raise ActionFailedError(
                f"bitstream push of {overlay.name} failed: {exc}",
                hint="the swap was parked and will time out with the partition decoupled; "
                     "restore the baseline") from exc
        except ShellProtocolError as exc:
            raise ActionFailedError(f"shell protocol error during the swap: {exc}") from exc
        except OSError as exc:
            raise UnreachableError(f"cannot reach the shell at {host}:{self._shell.port}: {exc}",
                                   hint="check the Ethernet link and the board's IP") from exc
        finally:
            if swap is not None and ((swap.parked and not swap.settled)
                                     or (swap.committing and not swap.committed_reply)):
                # The push (or the wait for the reply) failed with the swap (or a commit)
                # still parked: the harness turns new 6900 clients away until its 30 s
                # idle timeout fails it. The shell's error mapping reads this (B1 v4).
                self._shell.note_failed_push()

        if not res.verified:
            raise ActionFailedError(
                f"the shell swapped to {overlay.name} but reported verified:false",
                hint="the partition may be left decoupled; restore the baseline")
        if not swap.committing:            # a commit reported verify done before it began
            report(PHASE_VERIFY, 1, 1)
        try:
            rm = rmid.format_rm_id(res.rm_id)
        except (TypeError, ValueError):
            rm = res.rm_id
        card = card_outcome(getattr(res, "persist", None)) if keep_on_card else None
        return DeployResult(rm_id=rm, verified=True, seconds=time.monotonic() - started,
                            transport=assessment.transport, card=card)

    # -- checks -----------------------------------------------------------------------

    def _live(self) -> _Live:
        live: ShellLive = self._shell.live()
        clr_max = live.clr_max
        if clr_max is None and "stats" in live.features:
            clr_max = self._stats_clr_max()
        return _Live(shell_id=live.shell_id, rm_id=live.rm_id, version_ok=live.version_ok,
                     features=live.features, impl=live.impl, clr_max=clr_max)

    def _stats_clr_max(self) -> int | None:
        """``stats.clr_max`` when pyverify can send ``stats`` (v0.11 codec) and the
        harness reports the key. Never a hand-rolled request (one codec)."""
        if not callable(getattr(ShellClient, "stats", None)):
            return None
        try:
            raw = self._shell.call_raw(lambda c, tap: (c.stats(), dict(tap.last))[1])
        except HarnessError:
            return None
        value = raw.get("clr_max")
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 \
            else None

    def _assess(self, overlay: OverlayRef) -> _Assessment:
        entry = self.catalogue.entry_for(overlay)
        live = self._live()
        transport_item, transport = _check_transport(live, self.tunnelled)
        timeout_s = push_timeout_s(live.impl, transport)
        if timeout_s != DEFAULT_PUSH_TIMEOUT_S:
            transport_item = replace(
                transport_item,
                detail=f"{transport_item.detail}; push inactivity limit {timeout_s:.0f} s "
                       "(the send blocks while 6910 parks a partial behind the outgoing "
                       "clearing)")
        items = (
            PreflightItem(ITEM_CONTROL, Check.OK,
                          f"{self._shell.host}:{self._shell.port} answered "
                          "(it serves one client at a time)"),
            _check_shell_id(entry, live),
            _check_files(entry),
            PreflightItem(ITEM_PAIR, entry.pair_check, entry.pair_detail),
            _check_clearing_fits(entry, live.clr_max),
            transport_item,
            _check_usercode(entry, self.running_usercode),
        )
        return _Assessment(entry=entry, items=items, transport=transport,
                           push_timeout_s=timeout_s, impl=live.impl)


def card_status_from(st: Any) -> CardStatus:
    """A pyverify ``UsdResponse`` (net-protocol v0.13 ``usd``) as a ``CardStatus``."""
    if not getattr(st, "ok", False):
        err = str(getattr(st, "err", "") or "")
        if err in USD_NO_STORE_ERRS:
            return CardStatus(store=False, reason=f"{NO_STORE} (usd: {err})")
        return CardStatus(store=True, reason=f"the card could not be read (usd: {err or 'no reason'})")
    state, text = str(st.state or ""), str(st.text or "")
    more = _card_facts(st)
    if state == "no_hw":
        return CardStatus(store=False, state=state, text=text, reason=NO_STORE, **more)
    if not st.present:
        return CardStatus(store=True, present=False, state=state, text=text, reason=NO_CARD,
                          **more)
    if st.committable:
        return CardStatus(store=True, present=True, state=state, text=text, **more)
    why = {
        "foreign": "the card in the USER microSD slot holds no harness store (a foreign card); "
                   "format it for the harness first",
        "init": "the card in the USER microSD slot is still starting; try again in a few "
                "seconds",
        "unsupported": "the card in the USER microSD slot is not a kind the harness supports",
        "error": f"the card in the USER microSD slot reports an error ({text or 'no code'})",
    }.get(state, f"the card in the USER microSD slot cannot take a design (state {state!r})")
    return CardStatus(store=True, present=True, state=state, text=text, reason=why, **more)


def _card_facts(st: Any) -> dict[str, Any]:
    """The rest of the ``usd`` reply (LINUX-SLOTS ``card status``): the capacity, the
    power-on default, the power-on decision, whether a commit could land."""
    d = getattr(st, "default", None)
    default = ({"rm_id": str(d.rm_id), "static_id": str(d.static_id), "slot": str(d.slot)}
               if d is not None else None)
    return {"card_mb": getattr(st, "card_mb", None), "default": default,
            "boot": str(getattr(st, "boot", "") or ""),
            "committable": bool(getattr(st, "committable", False))}


def card_outcome(persist: PersistResult | None) -> CardOutcome:
    """What pyverify's persist step did, as the deploy result's ``card``."""
    if persist is None:
        return CardOutcome(kept=False, why="pyverify reported nothing about the card")
    if persist.status == PERSIST_COMMITTED:
        return CardOutcome(kept=True, slot=persist.slot)
    if persist.status == PERSIST_FAILED:
        return CardOutcome(kept=False, why=f"the card write failed ({persist.err}); the card "
                                           "keeps the design it had")
    if persist.status == PERSIST_SKIPPED:
        return CardOutcome(kept=False, why=persist.reason or "skipped")
    return CardOutcome(kept=False, why=persist.reason or persist.status)


def _check_shell_id(entry: CatalogueEntry, live: _Live) -> PreflightItem:
    want = entry.overlay.manifest.static_id
    if not live.shell_id:
        return PreflightItem(ITEM_SHELL_ID, Check.UNCHECKED,
                             "the shell reported no shell_id, so the static cannot be compared")
    try:
        have = int(live.shell_id, 0)
    except ValueError:
        return PreflightItem(ITEM_SHELL_ID, Check.MISMATCH,
                             f"the shell reported an unparseable shell_id {live.shell_id!r}")
    if have != want:
        return PreflightItem(
            ITEM_SHELL_ID, Check.MISMATCH,
            f"overlay built for shell 0x{want:08x}, the board runs 0x{have:08x} "
            "(a shell rebuild invalidates every partial; overlay-manifest.md)")
    return PreflightItem(ITEM_SHELL_ID, Check.OK, f"0x{have:08x}")


def _check_files(entry: CatalogueEntry) -> PreflightItem:
    m = entry.overlay.manifest
    try:
        entry.overlay.validate()
    except OverlayValidationError as exc:
        return PreflightItem(ITEM_FILES, Check.MISMATCH, str(exc))
    return PreflightItem(
        ITEM_FILES, Check.OK,
        f"clearing {m.clearing.len} B crc 0x{m.clearing.crc32:08x}, "
        f"partial {m.partial.len} B crc 0x{m.partial.crc32:08x}")


def _check_clearing_fits(entry: CatalogueEntry, clr_max: int | None = None) -> PreflightItem:
    declared = entry.overlay.manifest.clearing.len
    path = entry.overlay.clearing_path()
    actual = path.stat().st_size if path.is_file() else 0
    size = max(declared, actual)
    if clr_max is not None:
        limit, source = clr_max, "the harness's clr_max"
    else:
        limit, source = CLEARING_ARENA_BYTES, "clearing arena"
    if size > limit:
        why = ("the harness reports it cannot hold a bigger clearing" if clr_max is not None
               else "firmware/platform/Makefile SWAP_CLEARING_ARENA_BYTES, CLEARING_RAM_BYTES; "
                    "scripts/harness_gates/check_clearing_fits.py")
        return PreflightItem(
            ITEM_CLEARING_FITS, Check.MISMATCH,
            f"clearing is {size} B, over the {limit} B {source} ({why}): it would be staged "
            "to QSPI and the next swap-away would fail closed")
    return PreflightItem(ITEM_CLEARING_FITS, Check.OK, f"{size} B of the {limit} B {source}")


def _check_transport(live: _Live, tunnelled: bool = False) -> tuple[PreflightItem, str]:
    if not live.version_ok:
        if tunnelled:
            return (PreflightItem(
                ITEM_TRANSPORT, Check.UNCHECKED,
                "tcp: the link is a TCP tunnel (TFTP cannot cross it) and the shell did not "
                "answer 'version', so its push mode is unknown; a shell built WINDOWED=1 "
                "would stall a plain push"), TRANSPORT_TCP)
        return (PreflightItem(
            ITEM_TRANSPORT, Check.UNCHECKED,
            "tftp: the shell did not answer 'version', so its push mode is unknown; "
            "a shell built WINDOWED=1 would stall a non-windowed push"), TRANSPORT_TFTP)
    if WINDOWED_FEATURE in live.features:
        return (PreflightItem(
            ITEM_TRANSPORT, Check.OK,
            "tcp+windowed: the firmware reports 'windowed' (a plain push deadlocks it)"),
            TRANSPORT_WINDOWED)
    if live.impl == IMPL_LINUX:
        return (PreflightItem(
            ITEM_TRANSPORT, Check.OK,
            "tcp: mps3-harnessd takes a plain push on 6910 (the kernel paces TCP; "
            "'windowed' is absent by design)"), TRANSPORT_TCP)
    if tunnelled:
        return (PreflightItem(
            ITEM_TRANSPORT, Check.OK,
            "tcp: the link is a TCP tunnel and TFTP (UDP) cannot cross it; the firmware "
            "does not report 'windowed', so a plain push is safe"), TRANSPORT_TCP)
    return (PreflightItem(ITEM_TRANSPORT, Check.OK,
                          "tftp: the firmware does not report 'windowed'"), TRANSPORT_TFTP)


def push_timeout_s(impl: str, transport: str) -> float:
    """``BitstreamPusher.timeout_s`` for one push: pyverify's rule (``choose_push``).

    A tcp push (plain or windowed) to the Linux harness waits up to
    ``LINUX_PUSH_TIMEOUT_S`` (30 s) for each chunk: config_agent parks a partial that
    arrives while the outgoing clearing still streams into the ICAP, with the TCP
    window closed, so the send blocks for that whole stream. 30 s is the harness's
    own ``MPS3_SWAP_AWAIT_IDLE_MS``. Everything else keeps ``DEFAULT_PUSH_TIMEOUT_S``.
    """
    if impl == IMPL_LINUX and transport in (TRANSPORT_TCP, TRANSPORT_WINDOWED):
        return LINUX_PUSH_TIMEOUT_S
    return DEFAULT_PUSH_TIMEOUT_S


def _check_usercode(entry: CatalogueEntry, running: str | int | None) -> PreflightItem:
    want = entry.overlay.manifest.static_usercode
    if running is None or running == "":
        expect = f" (the manifest expects 0x{want:08x})" if want is not None else ""
        return PreflightItem(
            ITEM_USERCODE, Check.UNCHECKED,
            "needs JTAG: the running static's USERCODE is only readable over JTAG "
            f"(REGISTER.USERCODE); the shell cannot report it{expect}")
    if want is None:
        return PreflightItem(ITEM_USERCODE, Check.UNCHECKED,
                             "the manifest records no static_usercode to compare against")
    try:
        have = rmid.parse_rm_id(running)
    except (TypeError, ValueError):
        return PreflightItem(ITEM_USERCODE, Check.MISMATCH,
                             f"the provided USERCODE {running!r} is not a 32-bit value")
    if have != want:
        return PreflightItem(
            ITEM_USERCODE, Check.MISMATCH,
            f"overlay built against static implementation 0x{want:08x}, the board runs "
            f"0x{have:08x}; loading it would destroy the FPGA configuration (gen_manifest.py)")
    return PreflightItem(ITEM_USERCODE, Check.OK, f"0x{have:08x}")


class _TimedSocketTransport(SocketTransport):
    """pyverify's socket transport, plus a way to shorten the read timeout (to read a
    pending refusal without waiting out the swap timeout)."""

    def settimeout(self, seconds: float) -> None:
        self._sock.settimeout(seconds)


def _pending_reply(tap: _TapTransport, wait_s: float = 1.0) -> dict | None:
    """The control reply already sent (a refused swap), or None if none arrives soon."""
    try:
        tap._inner.settimeout(wait_s)
        tap.recv_line()
    except _ShellBusy as busy:
        return busy.reply
    except (OSError, AttributeError):
        return None
    return tap.last or None


def _is_fabric_mismatch(err: str) -> bool:
    low = err.lower()
    return any(marker in low for marker in FABRIC_MISMATCH_ERRS)


def _refusal_error(reply: dict, overlay: OverlayRef) -> HarnessError:
    """Map the shell's own refusal of a swap (a reply line) onto the taxonomy."""
    err = str(reply.get("err", ""))
    if err.strip().upper() == "EBUSY":
        return HeldError(f"the shell is busy (EBUSY): {overlay.name} was not deployed",
                         hint="another client holds the control port, or a swap is running")
    if _is_fabric_mismatch(err):
        return IncompatibleError(
            f"the shell refused {overlay.name}: the running fabric does not match the "
            f"harness image ({err})",
            hint="nothing was loaded; `info` shows the skew. Re-provision the image for "
                 "this fabric, or restore the base bitstream it was built for")
    if reply.get("ok") is True:
        return ActionFailedError(f"the push of {overlay.name} was reset although the shell "
                                 "accepted the swap", hint="restore the baseline")
    return ActionFailedError(f"the shell refused the swap to {overlay.name}: {err or reply}",
                             hint="nothing was loaded; the shell refused before the push")


def _swap_error(exc: SwapError, overlay: OverlayRef, raw: dict | None = None) -> HarnessError:
    """Map pyverify's one SwapError onto the exit-code taxonomy.

    ``raw`` is the last reply line (the swap reply, when there was one): pyverify's
    ``SwapResponse`` drops its ``err``, which is where the shell says why.
    """
    text = str(exc)
    cause = exc.__cause__
    err = str((raw or {}).get("err", "")) if (raw or {}).get("ok") is False else ""
    if err and _is_fabric_mismatch(err):
        return _refusal_error(raw or {}, overlay)
    if "static_id mismatch" in text or "unparseable shell_id" in text:
        return IncompatibleError(f"{overlay.name} does not match the running shell: {text}")
    if isinstance(cause, OverlayValidationError):
        return RefusedError(f"refusing to deploy {overlay.name}: {text}",
                            hint="nothing was pushed; rebuild or re-import the overlay")
    if isinstance(cause, OSError):
        return ActionFailedError(
            f"the swap to {overlay.name} did not complete: {text}",
            hint="the control connection timed out or dropped mid-swap; "
                 "read `info`, then restore the baseline if needed")
    detail = f"{err} ({text})" if err else text
    return ActionFailedError(f"the shell refused the swap to {overlay.name}: {detail}",
                             hint="the partition is left decoupled; restore the baseline")
