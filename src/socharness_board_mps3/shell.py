"""The MPS3 shell over Ethernet, as seen through pyverify.

This module turns pyverify replies into board-agnostic models and pyverify or
socket failures into ``HarnessError``s with exit codes. The control port
accepts ONE client, so every call opens, asks and closes. Never hold 6900 open
(docs: harness handover §5; fpgahub's poller does the same).

Two harness engines answer the same wire (Linux harness plan §10: the same
firmware service modules run bare-metal and under ``mps3-harnessd``). What the
host sees, and what it means:

| On 6900 | Meaning | Error (exit) | ``Health.control_channel`` |
|---|---|---|---|
| connect refused (RST) | nothing listens: Linux kernel up and harnessd dead or respawning; or bare-metal lwIP up with no listener | ``ShellRefusedError`` (7) | ``offline``, notes from the diagnosis |
| connect times out | no board, a dead CPU, or stage0 rescue (no TCP) | ``ShellSilentError`` (7) | ``offline``, or ``rescue`` when identify says so |
| connected, never a reply | the service loop is hung (a hung harnessd's kernel still accepts) | ``ShellWedgedError`` (7) | ``wedged`` |
| accept then EOF, or RST after accept | another client holds 6900 (single client) | ``HeldError`` (4) | ``busy`` |
| ``{"ok":false,"err":"EBUSY"}`` (A3) | the same, said explicitly | ``HeldError`` (4) | ``busy`` |

The diagnosis after a refused or silent connect asks, cheapest first, whether
anything else on the board answers: UDP identify (6899; ``mode:"rescue"`` means
stage0 rescue), then for non-loopback hosts TCP 22 (the Linux OS) and ICMP.

Reading keys pyverify does not model. pyverify's ``VersionResponse`` (the
installed one) drops ``impl``, ``proto``, ``usercode`` and ``clr_max``, and its
``DiagResponse`` turns an ABSENT key into 0. Every control connection here goes
through ``_TapTransport``, which keeps the last reply line pyverify parsed, so
those additive keys are read from the same line pyverify decoded, never from a
second hand-rolled request. PYVERIFY REQUEST (HOST lane, in progress):
``VersionResponse.impl``/``extra`` and ``DiagResponse.present``; once they land
the tap is only needed for the EBUSY line.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pyverify import client as _pv_client
from pyverify import rm_id as rmid
from pyverify.client import ShellClient, ShellProtocolError, SocketTransport

from socharness.core.errors import (
    ActionFailedError,
    HarnessError,
    HeldError,
    UnreachableError,
    UsageError,
)
from socharness.core.model import BoardIdentity, Check, Health

from . import identify as _identify
from .capabilities import HARNESS_STATES
from .constants import (
    IMPL_BARE_METAL,
    KNOWN_DESIGNS,
    SSH_PORT,
)

T = TypeVar("T")

#: pyverify's HOST lane adds ``ShellChannelClosed(ConnectionError)`` with the same
#: "closed by peer" text; use it when present, match the text otherwise.
_CHANNEL_CLOSED: type[BaseException] | None = getattr(_pv_client, "ShellChannelClosed", None)


def resolve_rm_name(rm_id: str) -> str:
    """Design name from the overlay manifests (T2), falling back to KNOWN_DESIGNS."""
    try:
        from .overlays import resolve_rm_name as from_manifests
    except ImportError:
        return KNOWN_DESIGNS.get(rmid.design_id(rm_id), "")
    return from_manifests(rm_id)


_resolve_rm_name = resolve_rm_name     # the Wave-1 private name; kept for importers


def parse_endpoint(spec: str, default_port: int) -> tuple[str, int]:
    """'host' | 'host:port' -> (host, port). IPv6 literals need brackets."""
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if spec.count(":") == 1:
        host, port = spec.split(":")
        return host, int(port)
    return spec, default_port


def _hex_id(value: Any) -> str:
    """A u32 id as ``"0x"`` + 8 lowercase hex, whatever form it arrived in; else ``""``."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return f"0x{value & 0xFFFFFFFF:08x}"
    try:
        return f"0x{int(str(value), 0) & 0xFFFFFFFF:08x}"
    except ValueError:
        return str(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- errors --------------------------------------------------------------------------


class ShellRefusedError(UnreachableError):
    """The control port refused the connection: nothing listens on 6900."""


class ShellSilentError(UnreachableError):
    """The connect to 6900 timed out: nothing answered at all."""


class ShellWedgedError(UnreachableError):
    """6900 accepted the connection and never replied: the service loop is hung."""


class ShellRescueError(UnreachableError):
    """No control channel because stage0 is in RESCUE (it said so over identify).

    ``identity`` is what stage0 reported (fabric ``shell_id``, no harness), so a
    caller that wants to show the board anyway can; ``reply`` is the identify reply.
    """

    def __init__(self, message: str, *, hint: str = "", identity: BoardIdentity | None = None,
                 reply: Any = None) -> None:
        super().__init__(message, hint=hint)
        self.identity = identity
        self.reply = reply


class _ShellBusy(Exception):
    """The shell answered ``{"ok":false,"err":"EBUSY",...}`` (harness handover A3)."""

    def __init__(self, reply: dict[str, Any]) -> None:
        super().__init__(f"EBUSY: {reply}")
        self.reply = reply


# --- the tap -------------------------------------------------------------------------


def _json_obj(line: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


class _TapTransport:
    """A pyverify ``Transport`` that keeps each reply line pyverify is about to parse.

    pyverify still does all the decoding; this only lets the pack read keys the
    installed pyverify does not model (``impl``, ``proto``, ``usercode``,
    ``clr_max``, the diag keys actually present), and turns the A3 busy line into
    ``HeldError`` before pyverify reads it as a failed ``ping``.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.last: dict[str, Any] = {}

    def send_line(self, payload: bytes) -> None:
        self._inner.send_line(payload)

    def recv_line(self) -> bytes:
        line = self._inner.recv_line()
        self.last = _json_obj(line)
        if self.last.get("ok") is False and str(self.last.get("err", "")).strip().upper() == "EBUSY":
            raise _ShellBusy(self.last)
        return line

    def close(self) -> None:
        self._inner.close()


# --- one live read -------------------------------------------------------------------


@dataclass(frozen=True)
class ShellLive:
    """``ping`` + ``version`` from ONE 6900 connection, with the raw ``version`` line."""

    shell_id: str
    rm_id: str
    version: Any = None                      # pyverify VersionResponse, or None
    raw_version: dict[str, Any] = field(default_factory=dict)
    version_busy: bool = False               # `version` answered EBUSY after `ping` worked

    @property
    def version_ok(self) -> bool:
        return self.version is not None and bool(self.version.ok)

    @property
    def features(self) -> tuple[str, ...]:
        return tuple(self.version.features) if self.version_ok else ()

    @property
    def impl(self) -> str:
        """``"linux"`` | ``"bare-metal"`` (``impl`` absent) | ``""`` (no ``version`` verb)."""
        if not self.version_ok:
            return ""
        impl = self.raw_version.get("impl", getattr(self.version, "impl", None))
        return impl if isinstance(impl, str) and impl else IMPL_BARE_METAL

    @property
    def proto(self) -> str:
        p = self.raw_version.get("proto") if self.version_ok else None
        return str(p) if p is not None and not isinstance(p, (bool, dict, list)) else ""

    @property
    def usercode(self) -> str:
        return _hex_id(self.raw_version.get("usercode")) if self.version_ok else ""

    @property
    def lmb_kb(self) -> int:
        return int(self.version.lmb_kb) if self.version_ok else 0

    @property
    def clr_max(self) -> int | None:
        """The largest clearing this harness can hold (additive ``clr_max``), if it says."""
        v = self.raw_version.get("clr_max") if self.version_ok else None
        return v if _is_int(v) and v > 0 else None

    def identity(self) -> BoardIdentity:
        verdict = self.version.skew_verdict if self.version_ok else "unchecked"
        ver = self.version if self.version_ok else None
        return BoardIdentity(
            board_type="mps3",
            shell_id=self.shell_id,
            rm_id=self.rm_id,
            rm_name=resolve_rm_name(self.rm_id) if self.rm_id else "",
            harness_version=ver.harness if ver else "",
            firmware_sha=ver.sha if ver else "",
            firmware_dirty=bool(ver.dirty) if ver else False,
            features=self.features,
            build_check={"ok": Check.OK, "SKEW": Check.MISMATCH}.get(verdict, Check.UNCHECKED),
            harness_impl=self.impl,
            proto=self.proto,
            usercode=self.usercode,
        )


# --- evidence beyond 6900 ------------------------------------------------------------


def icmp_echo(host: str, timeout: float = 1.0) -> bool:
    """One ICMP echo through the system ``ping`` (no raw sockets needed). False when
    there is no ``ping`` binary or it fails in any way."""
    ping = shutil.which("ping")
    if ping is None:
        return False
    secs = str(max(1, round(timeout)))
    if sys.platform.startswith("win"):
        args = [ping, "-n", "1", "-w", str(int(timeout * 1000)), host]
    elif sys.platform == "darwin":
        args = [ping, "-c", "1", "-t", secs, host]
    else:
        args = [ping, "-c", "1", "-W", secs, host]
    try:
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout + 2.0, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def identify_quietly(host: str, timeout: float = 0.5) -> _identify.IdentifyReply | None:
    try:
        reply = _identify.identify(host, timeout=timeout, retries=1)
    except HarnessError:
        return None
    return reply if reply.ok else None


@dataclass(frozen=True)
class ShellProbes:
    """How the diagnosis gathers evidence. Tests inject fakes; nothing here is required."""

    identify: Callable[[str, float], Any] = identify_quietly
    tcp: Callable[[str, int, float], bool] = tcp_open
    icmp: Callable[[str, float], bool] = icmp_echo


DEFAULT_PROBES = ShellProbes()


@dataclass(frozen=True)
class Diagnosis:
    """Why the control channel does not answer, from the evidence that was cheap to get."""

    state: str                       # "offline" | "service_down" | "rescue" | "wedged"
    notes: tuple[str, ...]
    reply: Any = None                # the IdentifyReply, when identify answered

    def health(self) -> Health:
        channel = {"service_down": "offline"}.get(self.state, self.state)
        # "reachable" = something on the board answered us (TCP accept, identify, SSH, ICMP).
        return Health(reachable=self.state != "offline", control_channel=channel,
                      notes=self.notes)

    @property
    def hint(self) -> str:
        return " ".join(self.notes)


# --- the shell -----------------------------------------------------------------------


class Mps3Shell:
    def __init__(self, host: str, port: int, *, timeout: float = 3.0,
                 probes: ShellProbes | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.probes = probes or DEFAULT_PROBES

    # -- plumbing ---------------------------------------------------------------

    def _connect(self) -> _TapTransport:
        where = f"{self.host}:{self.port}"
        try:
            inner = SocketTransport(self.host, self.port, self.timeout)
        except ConnectionRefusedError as exc:
            raise ShellRefusedError(
                f"shell at {where} refused the connection",
                hint="nothing listens on the control port: is the board powered and the "
                     "harness running?") from exc
        except TimeoutError as exc:
            raise ShellSilentError(
                f"shell at {where} did not answer within {self.timeout}s",
                hint="check the Ethernet link and the board's IP") from exc
        except OSError as exc:
            raise UnreachableError(f"cannot reach {where}: {exc}") from exc
        return _TapTransport(inner)

    def call_raw(self, fn: Callable[[ShellClient, _TapTransport], T]) -> T:
        """Open 6900, run ``fn(client, tap)``, close. Maps failures to exit-coded errors.

        ``tap.last`` is the reply line pyverify parsed last, as a dict: read keys the
        installed pyverify does not model from it, never send a hand-rolled request.
        """
        where = f"{self.host}:{self.port}"
        tap = self._connect()
        try:
            with ShellClient(self.host, self.port, timeout=self.timeout, transport=tap) as client:
                return fn(client, tap)
        except _ShellBusy as exc:
            holder = str(exc.reply.get("holder") or exc.reply.get("peer") or "")
            raise HeldError(f"shell at {where} is busy (EBUSY): another client holds the "
                            "control port", holder=holder,
                            hint=HARNESS_STATES["harness.busy"]) from exc
        except TimeoutError as exc:
            raise ShellWedgedError(
                f"shell at {where} accepted the connection but did not reply within "
                f"{self.timeout}s", hint=HARNESS_STATES["harness.wedged"]) from exc
        except ConnectionResetError as exc:
            # lwIP (bare-metal) or the kernel (Linux) may RST the extra client instead
            # of closing it; a board restarting mid-request looks the same.
            raise HeldError(
                f"shell at {where} reset the connection",
                hint="another client probably holds the control port, or the board is restarting",
            ) from exc
        except ConnectionError as exc:
            # Accept-then-EOF is how the shell turns away a second client (one client at a
            # time). pyverify raises ConnectionError("... closed by peer"), or its
            # ShellChannelClosed subclass once the HOST lane lands.
            if (_CHANNEL_CLOSED is not None and isinstance(exc, _CHANNEL_CLOSED)) \
                    or "closed by peer" in str(exc):
                raise HeldError(
                    f"shell at {where} closed the connection",
                    hint="another client probably holds the control port (one client at a time)",
                ) from exc
            raise UnreachableError(f"cannot reach {where}: {exc}") from exc
        except OSError as exc:
            raise UnreachableError(f"cannot reach {where}: {exc}") from exc
        except ShellProtocolError as exc:
            if "EOF" in str(exc) or "closed" in str(exc).lower():
                raise HeldError(
                    f"shell at {where} closed the connection",
                    hint="another client probably holds the control port (one client at a time)",
                ) from exc
            raise ActionFailedError(f"shell protocol error: {exc}") from exc

    def call(self, fn: Callable[[ShellClient], T]) -> T:
        """Open 6900, run ``fn``, close. Maps failures to exit-coded errors."""
        return self.call_raw(lambda client, _tap: fn(client))

    # -- identity -----------------------------------------------------------------

    def live(self) -> ShellLive:
        """``ping`` then ``version`` on one connection, keeping the raw ``version`` line.

        A harness without ``version`` (net-protocol < v0.8) answers "unknown op": that
        is an older harness, not a fault (``version_ok`` False, ``impl`` ""). A
        ``version`` that answers EBUSY after a good ``ping`` still yields the ping.
        """
        def ask(c: ShellClient, tap: _TapTransport) -> ShellLive:
            ping = c.ping()
            if not ping.ok:
                err = tap.last.get("err", "")
                raise ActionFailedError("shell answered ping with ok:false"
                                        + (f" ({err})" if err else ""))
            try:
                ver = c.version()
            except _ShellBusy:
                return ShellLive(ping.shell_id, ping.rm_id, version_busy=True)
            return ShellLive(ping.shell_id, ping.rm_id, version=ver, raw_version=dict(tap.last))

        return self.call_raw(ask)

    def identity(self) -> BoardIdentity:
        """What the harness says it is.

        - Held (another client, or EBUSY): the identity comes from UDP identify when
          the board answers it (identify is independent of 6900); else ``HeldError``.
        - Refused or silent: ``UnreachableError`` with the diagnosis as its hint; a
          board whose identify says ``mode:"rescue"`` raises ``ShellRescueError``
          carrying stage0's identity (``health()`` says "rescue"). It is an error, not
          an identity: there is no harness to identify, and a probe must not report
          a rescue board as having "answered ping".
        """
        try:
            return self.live().identity()
        except HeldError:
            reply = self.probes.identify(self.host, min(self.timeout, 0.5))
            if reply is not None and not reply.is_rescue:
                return reply.identity()
            raise
        except ShellWedgedError:
            raise
        except UnreachableError as exc:
            diagnosis = self.diagnose(exc)
            if diagnosis.reply is not None and diagnosis.reply.is_rescue:
                reason = diagnosis.reply.reason
                raise ShellRescueError(
                    f"the board at {self.host} is in stage0 RESCUE: no control channel"
                    + (f" ({reason})" if reason else ""),
                    hint=HARNESS_STATES["harness.rescue"],
                    identity=diagnosis.reply.identity(), reply=diagnosis.reply) from exc
            raise type(exc)(exc.message, hint=diagnosis.hint or exc.hint) from exc

    # -- health -------------------------------------------------------------------

    def diagnose(self, exc: UnreachableError) -> Diagnosis:
        """Why 6900 does not answer. Cheapest evidence first; never raises."""
        if isinstance(exc, ShellWedgedError):
            return Diagnosis("wedged", (HARNESS_STATES["harness.wedged"],))
        reply = self.probes.identify(self.host, min(self.timeout, 0.5))
        if reply is not None:
            if reply.is_rescue:
                notes = [HARNESS_STATES["harness.rescue"]]
                if reply.reason:
                    notes.append(f"stage0 says: {reply.reason}")
                return Diagnosis("rescue", tuple(notes), reply)
            return Diagnosis("service_down", (
                f"identify answers ({reply.impl}), the control port does not: "
                + HARNESS_STATES["harness.service_down"],), reply)
        # A loopback host is a test fake or a tunnel end: the local machine answering
        # TCP 22 or ICMP says nothing about the board.
        if not _identify.is_loopback(self.host):
            refused = isinstance(exc, ShellRefusedError)
            if refused and self.probes.tcp(self.host, SSH_PORT, 1.0):
                return Diagnosis("service_down", (
                    "the OS is up (SSH answers on 22) but nothing listens on the control "
                    "port: " + HARNESS_STATES["harness.service_down"],))
            if self.probes.icmp(self.host, 1.0):
                if refused:
                    return Diagnosis("service_down", (
                        "the board answers ICMP but refuses the control port: "
                        + HARNESS_STATES["harness.service_down"],))
                return Diagnosis("service_down", (
                    "the board answers ICMP but the control port does not accept: the "
                    "harness is starting, hung before its listener, or stage0 is in a "
                    "rescue that does not answer identify",))
        return Diagnosis("offline", (HARNESS_STATES["harness.offline"],))

    def health(self) -> Health:
        try:
            diag, raw = self.call_raw(lambda c, tap: (c.diag(), dict(tap.last)))
        except HeldError as exc:
            notes = [HARNESS_STATES["harness.busy"]]
            if exc.holder:
                notes.append(f"held by {exc.holder}")
            return Health(reachable=True, control_channel="busy", notes=tuple(notes))
        except UnreachableError as exc:
            return self.diagnose(exc).health()
        if not diag.ok:
            # A harness without `diag` (e.g. the July Linux v0.7 daemons) is a legitimate
            # older harness, not a fault: reachable, idle, just no counters.
            return Health(reachable=True, control_channel="idle",
                          notes=("this harness does not report diagnostic counters (no `diag` verb)",))
        # Only the keys the harness SENT. pyverify fills an absent key with 0, and
        # mps3-harnessd omits the lwIP/LAN9220 keys it has no source for: a 0 there
        # is not a measurement (TEAM_PLAN principle 7).
        counters = {k: int(v) for k, v in raw.items() if k != "ok" and _is_int(v)}
        notes = []
        if counters.get("svc_skipped"):
            notes.append("superloop skipped services (control channel may be starved)")
        return Health(reachable=True, control_channel="idle", counters=counters, notes=tuple(notes))

    # -- simple verbs -------------------------------------------------------------

    def reset(self, target: str) -> None:
        """``reset`` one target. A target this harness does not know is ``UsageError``."""
        ok, err = self.call_raw(lambda c, tap: (c.reset(target).ok, str(tap.last.get("err", ""))))
        if ok:
            return
        if any(e in err.lower() for e in _BAD_TARGET_ERRS):
            raise UsageError(f"this harness does not accept reset target {target!r} ({err})",
                             hint="list the targets with `reset --help`")
        raise ActionFailedError(f"shell refused reset target {target!r}"
                                + (f": {err}" if err else ""))

    def reset_dut(self) -> None:
        resp = self.call(lambda c: c.reset("dut"))
        if not resp.ok:
            raise ActionFailedError("shell refused reset target 'dut'")


# --- reset targets, read from the harness ---------------------------------------------

#: Every reset target the MPS3 harness vocabulary defines (ARCHITECTURE_SPEC §5,
#: harness handover A6). v0.11 accepts only "dut" (coordinator.c
#: coordinator_handle_reset: anything else is "bad target").
RESET_VOCABULARY: tuple[str, ...] = ("dut", "rp", "dbg")

#: ``version.features`` names that would declare a target, should a harness report
#: them (A6 has not named them; these are the names socharness proposes).
RESET_TARGET_FEATURES: dict[str, str] = {"reset_rp": "rp", "reset_dbg": "dbg"}

#: How the two servers word "no such target": the firmware says "bad target",
#: pyverify's FakeShell "unknown reset target 'x'".
_BAD_TARGET_ERRS = ("bad target", "unknown reset target")


def reset_targets_from(live: ShellLive) -> tuple[str, ...]:
    """The targets a harness declares: an additive ``version.reset_targets`` array
    when it sends one, else "dut" plus any target a feature name declares."""
    declared = live.raw_version.get("reset_targets") if live.version_ok else None
    if isinstance(declared, list) and declared and all(isinstance(t, str) for t in declared):
        return tuple(dict.fromkeys(declared))
    targets = ["dut"]
    targets += [t for f, t in RESET_TARGET_FEATURES.items() if f in live.features]
    return tuple(dict.fromkeys(targets))


class ShellResets:
    """``ResetAdapter`` whose targets come from the harness, not a hard-coded ("dut",).

    No harness can be asked "which targets do you accept?" without resetting
    something (``reset`` has no dry run), so this reads what the harness DECLARES
    (``version.reset_targets`` or feature names) and LEARNS from the answers to
    real requests: a vocabulary target ("rp", "dbg") the harness did not declare
    is still sent when asked for; "ok" adds it, "bad target" removes it, and the
    refusal is a ``UsageError`` that lists what is accepted.
    """

    def __init__(self, shell: Mps3Shell) -> None:
        self._shell = shell
        self._declared: tuple[str, ...] | None = None
        self._accepted: list[str] = []
        self._refused: set[str] = set()

    def refresh(self) -> None:
        """Forget what was read and learned (after a harness change or a reboot)."""
        self._declared = None
        self._accepted.clear()
        self._refused.clear()

    def reset_targets(self) -> Sequence[str]:
        if self._declared is None:
            try:
                self._declared = reset_targets_from(self._shell.live())
            except HarnessError:
                return tuple(dict.fromkeys(["dut", *self._accepted]))   # "dut" is always there
        return tuple(t for t in dict.fromkeys([*self._declared, *self._accepted])
                     if t not in self._refused)

    def reset(self, target: str) -> None:
        known = self.reset_targets()
        if target not in known and (target not in RESET_VOCABULARY or target in self._refused):
            raise UsageError(f"reset target {target!r} is not supported by this harness",
                             hint="targets: " + ", ".join(known))
        try:
            self._shell.reset(target)
        except UsageError as exc:
            self._refused.add(target)
            if target in self._accepted:
                self._accepted.remove(target)
            raise UsageError(exc.message, hint="targets: " + ", ".join(self.reset_targets())) \
                from exc
        if target not in known:
            self._accepted.append(target)


def make_reset_adapter(session: Any) -> ShellResets | None:
    """The reset hook the lead can wire in ``pack.py`` (CCR T12-1). None without a shell."""
    shell = getattr(session, "shell", None)
    return ShellResets(shell) if shell is not None else None
