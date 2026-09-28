"""The MPS3's live display mirror as ``session.display`` (lane LM2, DISPLAY-MPS3).

``docs/design/LCD_MIRROR.md`` §7 is the design; ``harness_manager.core.display`` the model and
the ``DisplayAdapter`` protocol; ``harness_manager.services.display`` the compositor that drives
this adapter. david's decisions of 2026-09-26: the hybrid (D1, the harnessd software tap now,
the snooper at mint 4: nothing here changes between them), only the lease holder sees the
live picture (D3), no touch pass-through (D4).

**The gate** (net-protocol v0.15, "LCD mirror (TCP 6940)"). ``version.features`` carries the
ENGINE name ``lcd_mirror``: no feature bit, so the NAME is matched and a bit number never
counts. It comes with ``version.lcd_mirror = {port, mode, proto}`` (the port, default 6940)
and ``stats.lcd_mirror``; all three appear only while the mirror is configured (not
``--lcdmirror none``). Only the Linux harness serves it. The reasons (``display_reason``),
in the order a user fixes them:

1. bare metal: ``needs the Linux harness with lcd_mirror``;
2. Linux without the engine: ``the Live display needs a harness image with lcd_mirror (this
   image has none...)``;
3. a board behind a hub whose lease is not yours (D3): the holder is named;
4. an unclaimed board, or no SSH: the claim hint.

1-2 are the gate (``display_gate``): the board can never show it as it is, so the routes
answer them 422 UNAVAILABLE before they look at the lease (a bare-metal board someone else
leases says "needs the Linux harness", not "alice holds it").

**The reach** is the one XVC and the on-board GDB server use: ``claim.open_forward`` =
``ssh -J HUB -l root <pinned host key, claimed key> -N -T -L 127.0.0.1:<p>:127.0.0.1:6940
BOARD`` (a real ``tunnel.SshTunnel``: ``ControlPath=none``, ``BatchMode``,
``ExitOnForwardFailure``, ``StrictHostKeyChecking=yes``, and its supervisor restarts ssh on
the SAME local port). The mirror binds the board's loopback only, so the claimed key is the
authentication (S12). One forward per session, opened by the first ``display_connect`` and
shared by every connection after it; each ``display_connect`` is a NEW socket to its local
port. ``display_release`` (the compositor's grace ran out, or ``close``) drops it; closing
the session drops it for good, so it never outlives the board (FINDINGS_TRIAGE #20: a
lingering forward to the board's loopback passes the claim lock for anyone on this host).

**D3, the lease.** A board behind a hub: the forward is opened only for the lease holder
(``LeaseService.view``, asked fresh before every forward is opened, as XVC does; the cached
view on a reconnect over an open forward). Someone else's lease, or nobody's, refuses with
``HeldError`` naming the holder, so the compositor stops. A hub that cannot be asked is
``DisplayUnavailable`` with a retry, and still nothing opens. A board with no hub has no
lease: allowed. Lease loss or release closes the display at once: ``DisplayService`` hears
``lease.state`` on the bus (the XVC wiring), and its close calls ``display_release``.

**Failures.** A forward that does not come up is an ``UnreachableError`` (the compositor
retries with back-off); after ``OPEN_FAILURES_MAX`` in a row it is an ``ActionFailedError``
and the compositor stops with ssh's reason (the design's "stops after 3 restarts"). A
changed host key is the claim's loud ``HostKeyChangedError`` at once. A stream that closes
before HELLO is diagnosed from ssh's "open failed" lines (``display_diagnose``): "no
lcd_mirror service on the board".

**Stale facts (FIX-PACK-1).** On 2026-09-28 the board rebooted from an image with
``lcd_mirror`` into one without it, and the Live display kept its cached facts and spun on
"connecting". Now: every identity read (``engine.info``) is noted here
(``display_note_identity``); a reboot, a power cycle or a changed SSH host key
(``display_forget``, from the daemon's events) drops the facts AND the forward, and until a
fresh read nothing is seeded from the open's (old) identity, so the next open re-gates on a
live ``version``; and a stream that closes before HELLO re-gates at once
(``display_regate``): an image without the engine ends the display with ``NO_ENGINE``.

Nothing here reads REGS or MODE: the picture's panel registers are the model's
(``DisplayFrame.mode_regs``: MODE after RESETS changes, REGS being a raw log never reset).

Test seams: ``tunnel.DEFAULT_LAUNCHER``/``DEFAULT_SSH_G`` (FakeSsh), ``leases=``/``use_leases``,
``clock=``.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harness_manager.core.capabilities import DISPLAY_MIRROR
from harness_manager.core.display import DisplayUnavailable
from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    HeldError,
    UnavailableError,
    UnreachableError,
    UsageError,
)

from .claim import changed_back_words, pin_fingerprint
from .constants import IMPL_BARE_METAL, IMPL_LINUX, LCD_MIRROR_PORT

log = logging.getLogger(__name__)

#: The ENGINE name in ``version.features`` (net-protocol v0.15): matched by name, never a bit.
LCD_MIRROR_FEATURE = "lcd_mirror"
#: The forward's name (``SshTunnel.local_port(FORWARD)``).
FORWARD = "lcd_mirror"
#: How long the harness's features and ``version.lcd_mirror`` are trusted (they change only
#: with the image or its configuration); a forward is opened on a fresh read.
FACTS_TTL_S = 300.0
#: The design's "stops after 3 restarts": forwards that fail to come up, in a row.
OPEN_FAILURES_MAX = 3
#: A hub that cannot be asked about the lease: try again after this long (never opened meanwhile).
LEASE_RETRY_S = 30.0
#: Connecting to the forward's local port (it is ours, on loopback).
CONNECT_TIMEOUT_S = 5.0
#: How long ssh's "open failed" line gets to arrive after the close (Q2; ``pack.py``).
OPEN_FAILURE_GRACE_S = 0.5

NEEDS_LINUX = "needs the Linux harness with lcd_mirror (this board runs the {impl} harness)"
NO_ENGINE = ("the Live display needs a harness image with lcd_mirror (this image has none: its "
             "version.features does not name it); update the board's Linux image")
CLAIM_HINT = ("needs a claimed board: the live display is reached over SSH with your claimed "
              "key (`harness-manager board claim TARGET`, or `--adopt` for a claim made "
              "elsewhere)")
NO_SSH = "this session has no SSH to the board; " + CLAIM_HINT
CLOSED = "the board session is closed"


def _port(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        return None
    return value


@dataclass(frozen=True)
class MirrorFacts:
    """What the harness says about its mirror: ``impl``, ``features`` (by name), and from
    ``version.lcd_mirror`` the ``port`` (default 6940), ``mode`` (``sw``|``hw``; "" unread)
    and ``proto``. ``source``: ``version`` (read live) or ``identity`` (a cached identity)."""

    impl: str
    features: frozenset[str]
    port: int = LCD_MIRROR_PORT
    mode: str = ""
    proto: int | None = None
    source: str = "identity"

    @property
    def engine(self) -> bool:
        """The image serves the mirror: the NAME is in ``version.features``."""
        return LCD_MIRROR_FEATURE in self.features

    @classmethod
    def of_identity(cls, identity: Any, keep: MirrorFacts | None = None) -> MirrorFacts:
        """From a ``BoardIdentity`` (no ``version.lcd_mirror`` in it: ``keep``'s, else the default)."""
        impl = str(getattr(identity, "harness_impl", "") or "")
        feats = frozenset(str(f) for f in (getattr(identity, "features", ()) or ()))
        if keep is not None and keep.source == "version":
            return cls(impl, feats, keep.port, keep.mode, keep.proto, "identity")
        return cls(impl, feats)

    @classmethod
    def of_live(cls, live: Any) -> MirrorFacts:
        """From a ``shell.ShellLive`` (``ping`` + ``version`` with the raw reply)."""
        raw = getattr(live, "raw_version", None) or {}
        block = raw.get(LCD_MIRROR_FEATURE)
        port, mode, proto = LCD_MIRROR_PORT, "", None
        if isinstance(block, dict):
            port = _port(block.get("port")) or LCD_MIRROR_PORT
            mode = block.get("mode") if isinstance(block.get("mode"), str) else ""
            p = block.get("proto")
            proto = p if isinstance(p, int) and not isinstance(p, bool) else None
        return cls(str(live.impl or ""), frozenset(str(f) for f in live.features), port, mode,
                   proto, "version")

    def to_json(self) -> dict[str, Any]:
        return {"impl": self.impl or IMPL_BARE_METAL, "engine": self.engine, "port": self.port,
                "mode": self.mode, "proto": self.proto, "source": self.source}


class Mps3Display:
    """``core.display.DisplayAdapter`` for an MPS3 session (the module docstring).

    ``leases``: an object with ``view(hub) -> {lease: {mine, holder}}`` and optionally
    ``forget(hub)`` (the daemon's ``LeaseService``, given by ``use_leases`` or the display
    service); by default one is made on the config dir when the board has a hub.
    """

    def __init__(self, session: Any, *, leases: Any = None,
                 clock: Callable[[], float] = time.monotonic,
                 facts_ttl_s: float = FACTS_TTL_S) -> None:
        self._session = session
        self._leases = leases
        self._clock = clock
        self._ttl = facts_ttl_s
        self._mu = threading.RLock()
        self._facts: tuple[float, MirrorFacts] | None = None
        self._tunnel: Any = None
        self._closed = False
        self._failures = 0
        #: FIX-PACK-1: the facts were dropped (a reboot, a new host key): nothing is seeded
        #: from the open's identity (it predates the change) until a fresh read or a new
        #: identity is noted.
        self._forgotten = False
        self.opens = 0                       # forwards opened (tests, status)
        self.forgets = 0                     # facts dropped (tests)

    @property
    def board_id(self) -> str:
        return str(self._session.candidate.board_id)

    # -- the harness's facts ----------------------------------------------------------------

    def display_note_identity(self, identity: Any) -> None:
        """Someone read the board's identity: use its features and impl. FIX-PACK-1: an
        identity without features says nothing about the engine (UDP identify while another
        client holds 6900, a ``version`` that answered EBUSY): it is ignored, never read as
        "this image has no lcd_mirror" (not known is not never)."""
        if identity is None or not tuple(getattr(identity, "features", ()) or ()):
            return
        with self._mu:
            keep = self._facts[1] if self._facts is not None else None
            self._facts = (self._clock(), MirrorFacts.of_identity(identity, keep))
            self._forgotten = False

    def display_forget(self, why: str = "") -> None:
        """FIX-PACK-1: the board may run another image now (a reboot, a power cycle, a changed
        SSH host key): drop the cached facts and the forward. The next open re-gates on a
        live ``version`` read; until then the gate is "not known" (never the old answer)."""
        with self._mu:
            self._facts = None
            self._forgotten = True
            self._failures = 0
            self.forgets += 1
        log.info("the live display of %s forgets what it knew%s", self.board_id,
                 f": {why}" if why else "")
        self.display_release()

    def display_regate(self) -> str:
        """FIX-PACK-1: the stream closed before HELLO, over this session's own forward (the
        lease holder's): read ``version`` again and return the gate ("" when the image may
        still show it, or it cannot be read now). An image that lost the engine answers
        ``NO_ENGINE``: the compositor ends the display with it instead of reconnecting."""
        if self._closed:
            return ""
        try:
            f = self.facts(fresh=True)
        except HarnessError:
            return ""
        return self._gate_reason(f)

    def known_facts(self) -> MirrorFacts | None:
        """What is known WITHOUT asking the board: the cached facts (of any age; a stale
        cache takes the open board's identity when it has one), else the identity's, else
        None. REVIEW-W5 4: the refusal path (``display_gate``, ``display_reason``, the status)
        uses only this; not known is not never."""
        now = self._clock()
        with self._mu:
            cached = self._facts[1] if self._facts is not None else None
            age = now - self._facts[0] if self._facts is not None else None
        if cached is not None and age is not None and age < self._ttl:
            return cached
        with self._mu:
            if self._forgotten:
                return cached                # dropped: the open's identity is older (None)
        seed = getattr(self._session.candidate, "identity", None)
        seeded = MirrorFacts.of_identity(seed, cached) if seed is not None and (
            getattr(seed, "features", ()) or getattr(seed, "harness_impl", "")) else None
        if seeded is not None:
            with self._mu:
                self._facts = (now, seeded)
            return seeded
        return cached

    def facts(self, *, fresh: bool = False) -> MirrorFacts:
        """The harness's mirror facts. Not ``fresh``: ``known_facts`` only, never a read of
        the board (``UnavailableError`` when nothing is known yet). ``fresh``: ``version``
        read now (a harness that does not answer falls back to what is known); only
        ``_open_forward`` asks for that, after the lease check."""
        if not fresh:
            known = self.known_facts()
            if known is None:
                raise UnavailableError(DISPLAY_MIRROR, "the harness's features are not known "
                                                       "yet (its version is read when the "
                                                       "live display opens)")
            return known
        now = self._clock()
        with self._mu:
            cached = self._facts[1] if self._facts is not None else None
            forgotten = self._forgotten
        seed = getattr(self._session.candidate, "identity", None)
        seeded = MirrorFacts.of_identity(seed) if seed is not None and not forgotten and (
            getattr(seed, "features", ()) or getattr(seed, "harness_impl", "")) else None
        try:
            got = self._read_live()
        except HarnessError:
            fallback = cached if cached is not None else seeded
            if fallback is None:
                raise
            return fallback
        with self._mu:
            self._facts = (now, got)
            self._forgotten = False
        return got

    def _read_live(self) -> MirrorFacts:
        shell = getattr(self._session, "shell", None)
        if shell is None:
            raise UnreachableError("this session has no Ethernet link to the harness")
        live = shell.live()
        if getattr(live, "version_busy", False):
            raise HeldError("the harness answered 'version' with EBUSY")
        return MirrorFacts.of_live(live)

    # -- why not ----------------------------------------------------------------------------

    @staticmethod
    def _gate_reason(f: MirrorFacts) -> str:
        if f.impl != IMPL_LINUX:
            return NEEDS_LINUX.format(impl=f.impl or IMPL_BARE_METAL)
        if not f.engine:
            return NO_ENGINE
        return ""

    def use_leases(self, leases: Any) -> None:
        """Share the daemon's lease service (one cache, one view of "mine")."""
        if leases is not None:
            self._leases = leases

    def _lease_service(self) -> Any:
        if self._leases is None:
            from harness_manager.services.lease import LeaseService
            from harness_manager.settings.files import config_dir  # the one rule (SET-WIRE)

            self._leases = LeaseService(config_dir())
        return self._leases

    def _lease(self, *, fresh: bool) -> tuple[str, str, str]:
        """``(reason, kind, holder)``: kind "" (yours, or no hub), ``held`` (someone else's),
        ``nobody`` or ``unknown`` (the hub did not answer)."""
        hub = getattr(self._session, "hub", None)
        if hub is None:
            return "", "", ""
        target = getattr(hub, "target", "") or "the board"
        leases = self._lease_service()
        if fresh:
            forget = getattr(leases, "forget", None)
            if callable(forget):
                forget(hub)                  # a fresh answer: a lease taken a moment ago counts
        try:
            view = leases.view(hub)
        except HarnessError as exc:
            return (f"cannot confirm you hold the lease on {target}: {exc.message}", "unknown",
                    "")
        lease = (view or {}).get("lease")
        if not lease:
            return (f"the live display is for the lease holder only, and nobody holds {target} "
                    "(take it: `harness-manager lease acquire TARGET`)", "nobody", "nobody")
        if not lease.get("mine"):
            who = str(lease.get("holder") or "someone else")
            return (f"the live display is for the lease holder only: {who} holds {target} "
                    "(ask for it: `harness-manager lease request TARGET`)", "held", who)
        return "", "", str(lease.get("holder") or "")

    def _require_lease(self, *, fresh: bool) -> None:
        why, kind, holder = self._lease(fresh=fresh)
        if not why:
            return
        if kind == "unknown":
            raise DisplayUnavailable(why, retry_s=LEASE_RETRY_S)
        raise HeldError(why, holder=holder, hint="the live display opens for the lease holder "
                                                 "only (the same rule as XVC)")

    def _claim_reason(self) -> str:
        """"" when the board can be reached over SSH with a pinned host key; else the hint."""
        claim = getattr(self._session, "claim", None)
        if claim is None:
            return NO_SSH
        try:
            pin = claim.config()["host_key"]
        except UsageError as exc:
            return exc.message
        if not pin:
            return CLAIM_HINT
        if pin.startswith("SHA256:"):
            return (f"{self.board_id}'s SSH pin is a fingerprint only; "
                    "`harness-manager board claim TARGET --adopt` fetches the key")
        if not claim.board_host():
            return f"no board address for SSH to {self.board_id}; " + CLAIM_HINT
        try:
            obs = claim.observe()
        except HarnessError:
            return ""                        # not known now: the connect will say
        if obs.claimed is False:
            return "the board is unclaimed now; " + CLAIM_HINT
        if obs.host_key and obs.host_key != pin_fingerprint(pin):
            seen = getattr(claim, "seen_before", None)
            seen_at = seen(obs.host_key) if callable(seen) else ""
            if seen_at:                      # a key pinned here before: /persist, said plainly
                return changed_back_words(obs.host_key, pin_fingerprint(pin), seen_at,
                                          unclaimed=obs.claimed is False)
            return (f"THE BOARD'S SSH HOST KEY CHANGED: pinned {pin_fingerprint(pin)}, the board "
                    f"reports {obs.host_key} (re-claim only if it was re-provisioned: "
                    "`harness-manager board claim TARGET --replace-host-key`)")
        return ""

    def display_gate(self) -> str:
        """Why this board can NEVER show the mirror as it is now (reasons 1-2: the bare-metal
        harness, an image without the engine), else "". A harness that cannot be asked now is
        "" (not known is not never; ``display_reason`` says it did not answer). The routes
        answer a gate 422 before they look at the lease: taking the lease would not help."""
        if self._closed:
            return ""
        f = self.known_facts()                 # never a read of the board (REVIEW-W5 4)
        return self._gate_reason(f) if f is not None else ""

    def display_reason(self) -> str:
        """"" when the mirror can be opened now; else why not (the capability line). Never
        reads the board: the gate from what is known (``known_facts``; nothing known skips
        it, and ``_open_forward`` reads ``version`` after the lease check), the lease view,
        the claim on record."""
        if self._closed:
            return CLOSED
        f = self.known_facts()
        gate = self._gate_reason(f) if f is not None else ""
        return gate or self._lease(fresh=False)[0] or self._claim_reason()

    def display_facts(self) -> dict[str, Any]:
        """What a status view shows about the reach (additive, for the daemon's status).
        What is known only: never a read of the board."""
        with self._mu:
            tunnel = self._tunnel
        known = self.known_facts()
        facts: dict[str, Any] = known.to_json() if known is not None else {
            "error": "the harness's features are not known yet"}
        return {**facts, "reach": "board-ssh", "lease_holder_only": True,
                "forward": tunnel.status() if tunnel is not None else None}

    # -- the forward --------------------------------------------------------------------------

    def display_connect(self) -> socket.socket:
        """A NEW connected socket to the board's lcd_mirror, through this session's forward
        (opened here the first time, for the lease holder only)."""
        with self._mu:
            if self._closed:
                raise DisplayUnavailable(CLOSED, retry_s=None)
            tunnel = self._tunnel
        if tunnel is None:
            tunnel = self._open_forward()
        else:
            self._require_lease(fresh=False)
            self._check_forward(tunnel)
        return socket.create_connection(("127.0.0.1", tunnel.local_port(FORWARD)),
                                        timeout=CONNECT_TIMEOUT_S)

    def _open_forward(self) -> Any:
        known = self.known_facts()
        why = self._gate_reason(known) if known is not None else ""
        if why:
            raise DisplayUnavailable(why, retry_s=None)
        self._require_lease(fresh=True)      # D3: never a forward for anyone but the holder
        # The live ``version`` read (the port, the mode) only now, after the lease check:
        # nobody else's board is read on the way to a refusal (REVIEW-W5 4).
        f = self.facts(fresh=True)
        why = self._gate_reason(f)
        if why:
            raise DisplayUnavailable(why, retry_s=None)
        why = self._claim_reason()
        if why:
            raise DisplayUnavailable(why, retry_s=None)
        claim = self._session.claim
        try:
            tunnel = claim.open_forward({FORWARD: f.port}, label=f"{self.board_id} lcd_mirror")
        except UnreachableError as exc:
            with self._mu:
                self._failures += 1
                n = self._failures
                if n >= OPEN_FAILURES_MAX:
                    self._failures = 0
            if n >= OPEN_FAILURES_MAX:
                raise ActionFailedError(
                    f"the SSH forward to {self.board_id}'s lcd_mirror did not come up "
                    f"({n} tries): {exc.message}", hint=exc.hint or CLAIM_HINT) from exc
            raise
        with self._mu:
            self._failures = 0
            if self._closed or self._tunnel is not None:
                spare, tunnel = tunnel, self._tunnel         # closed, or another won the race
            else:
                spare, self._tunnel = None, tunnel
                self.opens += 1
        if spare is not None:
            spare.close()
        if tunnel is None:
            raise DisplayUnavailable(CLOSED, retry_s=None)
        return tunnel

    def _check_forward(self, tunnel: Any) -> None:
        """The forward's ssh is restarting (its supervisor keeps the local port): say why."""
        state = getattr(tunnel, "state", "up")
        if state == "up":
            return
        detail = str(getattr(tunnel, "detail", "") or "")
        exc = UnreachableError(f"the SSH forward to the board's lcd_mirror is {state}"
                               + (f" ({detail})" if detail else ""))
        claim = getattr(self._session, "claim", None)
        mapped = claim.map_ssh_failure(exc) if claim is not None else exc
        raise mapped

    def display_diagnose(self, since: float) -> str:
        """After a stream that closed before HELLO: ssh's "open failed" lines since ``since``."""
        with self._mu:
            tunnel = self._tunnel
        if tunnel is None:
            return ""
        fails = tunnel.open_failures_since(since, wait_s=OPEN_FAILURE_GRACE_S)
        return f"no lcd_mirror service on the board ({fails[-1]})" if fails else ""

    def display_release(self) -> None:
        """The compositor closed this board's upstream: drop the forward (a later connect
        opens a new one, for the lease holder only)."""
        with self._mu:
            tunnel, self._tunnel = self._tunnel, None
        if tunnel is not None:
            try:
                tunnel.close()
            except Exception:  # noqa: BLE001 - releasing never raises into the compositor
                log.exception("closing the lcd_mirror forward of %s failed", self.board_id)

    def tunnel_status(self) -> dict[str, Any] | None:
        with self._mu:
            tunnel = self._tunnel
        return tunnel.status() if tunnel is not None else None

    def close(self) -> None:
        """The session is closing: drop the forward and refuse any later connect. Never raises."""
        with self._mu:
            self._closed = True
        self.display_release()


def make_display_adapter(session: Any) -> Mps3Display | None:
    """``pack.py``'s table factory (``session.display``): an adapter for a session with an
    Ethernet shell (the harness to ask), else None."""
    if getattr(session, "shell", None) is None:
        return None
    return Mps3Display(session)


def display_adapter(session: Any) -> Mps3Display | None:
    """THE pack hook ``display_adapter(session) -> DisplayAdapter | None`` (lane LM3 calls it,
    through ``Mps3Pack.display_adapter``): the session's adapter, made once. One per session,
    because the adapter holds the board's forward."""
    current = getattr(session, "display", None)
    if current is not None:
        return current
    made = make_display_adapter(session)
    if made is not None:
        try:
            session.display = made
        except AttributeError:               # a session that cannot hold it: still one per call
            pass
    return made


__all__ = ["CLAIM_HINT", "FORWARD", "LCD_MIRROR_FEATURE", "MirrorFacts", "Mps3Display",
           "NEEDS_LINUX", "NO_ENGINE", "NO_SSH", "display_adapter", "make_display_adapter"]
