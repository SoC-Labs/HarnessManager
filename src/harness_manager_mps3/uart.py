"""MPS3 console rates: what each console runs at, and the harness verb that changes it.

Where each MPS3 console's rate is set (platform repo ``mps3-nanosoc-platform``,
read at d5c4017):

| Console | What carries it | Rate set by | Changeable from the app |
|---|---|---|---|
| ``uart0`` (TCP 6930) | the RP's console AXI-Stream pair, serialised by ``uart_axis_shim`` INSIDE the RM | the loaded design, at build time (``UART_BAUD``) | only through the harness verb ``uart_baud`` (feature ``uart_baud``) |
| ``uart1`` (TCP 6931) | UARTBR U1, whose DUT side the shell ties off | nothing drives it | no |
| ``swo`` (TCP 6932) | the shell's NRZ SWO deserialiser | the harness firmware (divisor 24) | no |
| ``fpga_uart0..3`` (``serial://``) | FT4232H lanes on the Debug USB | the host port | yes: the broker reopens the port (not this module) |
| a hub share (``tcp://``, not a shell console) | fpgahub's share of a host port | the share | no |

The rate on uart0 is fixed INSIDE the design, so the table below maps the
design (``pyverify.rm_id.design_id`` of the board's ``rm_id``) to the
``UART_BAUD`` its wrapper builds ``uart_axis_shim`` with, citing each file.
76800 is not a Linux termios speed, so ``screen`` cannot be told it: for these
consoles the GUI is the baud control and the PTY only carries bytes.

The harness verb (lane L6, frozen 2026-09-23): the harness advertises the
feature ``uart_baud`` in ``version``; ``{"op":"uart_baud","stream":"uart0"}``
answers ``{"ok":true,"stream":"uart0","baud":76800,"mode":"fixed|auto|set",
"settable":true}``, and ``{"op":"uart_baud","stream":"uart0","baud":N}`` sets
it (``baud: 0`` clears the override, back to auto or fixed). It is spoken only
through pyverify's codec (``ShellClient.uart_baud``) on ``Mps3Shell.call_raw``;
the reply line is read from the tap. An installed pyverify without the codec
is a reason, never a hand-rolled request (the rule in ``shell.py``).

``console_baud_info``/``console_set_baud`` implement the optional
``ConsoleAdapter`` methods of the same names (the lead wires them into
``pack.Mps3Consoles``). Neither raises for a board that does not answer: the
row carries the reason instead.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from pyverify import rm_id as rmid
from pyverify.client import ShellProtocolError

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    RefusedError,
    UnavailableError,
    UsageError,
)

from .constants import CONSOLE_PORTS, HOSTIO_CONSOLE, KNOWN_DESIGNS
from .designs import DESIGNS

log = logging.getLogger(__name__)

#: The capability label on a refused rate change (an error label, not a negotiated capability).
BAUD_CAPABILITY = "console_baud"
#: The harness feature and verb (lane L6).
FEATURE = "uart_baud"
#: Consoles the harness's ``uart_baud`` verb covers (the host->DUT UART streams).
VERB_STREAMS = ("uart0", "uart1")
#: The DUT clock every design below was built for: ``UART_CLK_HZ = 50_000_000``
#: (``fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:47``, the shell's clk_wiz_dut CLKOUT1).
DESIGN_CLK_HZ = 50_000_000
#: Rates offered when the harness can set uart0/uart1 (it takes any rate its divisor reaches).
HARNESS_CHOICES = (9600, 19200, 38400, 57600, 76800, 115200, 230400, 460800, 921600)

NO_CODEC = ("the harness reports 'uart_baud' but the installed pyverify has no uart_baud() "
            "(an older harness codec); update pyverify")
HUB_SHARE = "a hub share: the share sets the rate (change it on the hub, not here)"


@dataclass(frozen=True)
class DesignUart:
    """What one design does with uart0. ``baud`` None: no serial line at all (see ``why``)."""

    name: str
    baud: int | None
    source: str                    # the file (and line) that fixes it, platform-repo relative
    why: str = ""                  # for ``baud`` None: why there is no rate


#: design_id -> uart0 rate. A view of the design manifest (designs.py); every row cites the
#: wrapper that fixes it.
DESIGN_UART: dict[int, DesignUart] = {
    d: DesignUart(r.name, r.uart.baud, r.uart.source, r.uart.why)
    for d, r in DESIGNS.items() if r.uart is not None
}

#: uart1: the shell's UARTBR U1 has no partition pins in v0.1; the BD ties its DUT side off.
UART1_WHY = ("nothing drives uart1: the shell ties its DUT side off until a design widens the "
             "partition contract (fpga/shell/ip/uart_bridge/README.md:72-75)")
#: What the console row says (``connected: false``) so the page can mark the console apart.
UART1_NOT_CONNECTED = "DUT uart1: not connected in this shell"
#: Mint 4: with the ``hostio`` feature 6931 is the DUT's hostio channel 1, not the shell's UART1.
HOSTIO_WHY = ("hostio1 passes the DUT's bytes as they are: it has no baud rate of its own "
              "(the DUT sets it) and Harness Manager does not pace what you type")
#: swo: the harness firmware's NRZ deserialiser divisor 24 = a 25-cycle bit period.
SWO_DIVISOR = 24
SWO_BAUD = DESIGN_CLK_HZ // (SWO_DIVISOR + 1)          # 2 000 000 at the 50 MHz dut_clk
SWO_SOURCE = "firmware/uart_over_eth/uart_over_eth.c:30-35 (UART_OVER_ETH_SWO_DIVISOR 24)"


def design_uart(rm_id: Any) -> tuple[int | None, DesignUart | None]:
    """(design_id, its row). ``(None, None)`` when ``rm_id`` does not parse."""
    try:
        did = rmid.design_id(rm_id)
    except (TypeError, ValueError):
        return None, None
    return did, DESIGN_UART.get(did)


def _design_label(did: int | None, row: DesignUart | None) -> str:
    if row is not None:
        return row.name
    if did is None:
        return "the loaded design"
    name = KNOWN_DESIGNS.get(did, "")
    return f"{name} (design 0x{did:04X})" if name else f"design 0x{did:04X}"


# --- rows ---------------------------------------------------------------------------------


def _row(kind: str, baud: int | None, source: str, *, settable: bool = False, reason: str = "",
         choices: tuple[int, ...] = (), **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": kind, "baud": baud, "source": source, "settable": settable,
                           "reason": "" if settable else reason,
                           "choices": list(choices) if choices else ([baud] if baud else [])}
    out.update(extra)
    return out


def design_row(stream: str, rm_id: Any) -> dict[str, Any]:
    """The rate the loaded design fixes on ``stream`` (no harness verb involved)."""
    if stream == HOSTIO_CONSOLE:
        return _row("ethernet", None, "harness", reason=HOSTIO_WHY, carries="hostio1")
    if stream == "uart1":
        return _row("ethernet", None, "design", reason=UART1_WHY, connected=False,
                    connected_reason=UART1_NOT_CONNECTED)
    if stream == "swo":
        return _row("ethernet", SWO_BAUD, "harness",
                    reason=f"the harness firmware fixes the SWO deserialiser at {SWO_BAUD} baud "
                           f"(divisor {SWO_DIVISOR} at the {DESIGN_CLK_HZ // 1_000_000} MHz "
                           f"dut_clk; {SWO_SOURCE})", cite=SWO_SOURCE)
    did, row = design_uart(rm_id)
    label = _design_label(did, row)
    if row is None:
        return _row("ethernet", None, "unknown",
                    reason=f"no uart0 rate is recorded for {label}", design=label)
    if row.baud is None:
        return _row("ethernet", None, "design", reason=row.why, design=row.name, cite=row.source)
    return _row("ethernet", row.baud, "design", design=row.name, cite=row.source,
                reason=f"{row.name} fixes {stream} at {row.baud} baud when it is built "
                       f"({row.source}); changing it at run time needs harness firmware with "
                       f"'{FEATURE}'")


def harness_row(stream: str, reply: Mapping[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """A row from the harness's ``uart_baud`` reply; ``fallback`` (the design row) fills gaps."""
    if reply.get("ok") is not True:
        err = str(reply.get("err") or "no reason given")
        return {**fallback, "settable": False,
                "reason": f"the harness answered uart_baud with ok:false ({err})"}
    baud = reply.get("baud")
    baud = baud if isinstance(baud, int) and not isinstance(baud, bool) and baud > 0 else None
    mode = str(reply.get("mode") or "")
    settable = reply.get("settable") is True
    reason = ""
    if not settable:
        reason = str(reply.get("reason") or reply.get("err") or "") or (
            f"the harness reports {stream}'s rate as {mode or 'not settable'}: the loaded design's "
            "UART has no run-time divisor")
    out = _row("ethernet", baud if baud is not None else fallback.get("baud"), "harness",
               settable=settable, reason=reason, choices=HARNESS_CHOICES if settable else (),
               mode=mode)
    for key in ("design", "cite", "connected", "connected_reason"):
        if key in fallback:
            out[key] = fallback[key]
    return out


def serial_row(url: str) -> dict[str, Any]:
    """A hub share over ``tcp://``: the share sets the rate. Reports ``?baud=`` when the URL says."""
    query = parse_qs(urlparse(url).query)
    try:
        baud: int | None = int(query["baud"][0])
    except (KeyError, IndexError, ValueError):
        baud = None
    return _row("serial", baud, "serial" if baud else "unknown", reason=HUB_SHARE, share=True)


# --- the harness ----------------------------------------------------------------------------


def _uart_baud(client: Any, tap: Any, stream: str, baud: int | None = None) -> dict[str, Any]:
    """pyverify's ``uart_baud`` codec; the reply line from the tap (keys the codec may drop)."""
    ask = getattr(client, "uart_baud", None)
    if not callable(ask):
        raise UnavailableError(BAUD_CAPABILITY, NO_CODEC)
    resp = ask(stream=stream) if baud is None else ask(stream=stream, baud=baud)
    raw = getattr(resp, "raw", None)
    return dict(raw) if isinstance(raw, dict) and raw else dict(tap.last)


def _features(client: Any) -> tuple[str, ...]:
    """``version.features``; () for a harness without ``version`` (net-protocol < v0.8)."""
    try:
        ver = client.version()
    except ShellProtocolError:
        return ()
    return tuple(ver.features) if getattr(ver, "ok", False) else ()


def query(shell: Any, streams: tuple[str, ...]) -> dict[str, Any]:
    """ONE control connection: ``ping`` (rm_id), ``version`` (features), then ``uart_baud``
    for each of ``streams`` when the harness has the feature.

    Returns ``{"rm_id", "features", "replies": {stream: reply}, "error"}``; never raises
    for a board that does not answer (``error`` says why).
    """
    def ask(client: Any, tap: Any) -> dict[str, Any]:
        ping = client.ping()
        feats = _features(client)
        out: dict[str, Any] = {"rm_id": ping.rm_id if ping.ok else "", "features": feats,
                               "replies": {}, "error": ""}
        if FEATURE in feats:
            for stream in streams:
                try:
                    out["replies"][stream] = _uart_baud(client, tap, stream)
                except UnavailableError as exc:
                    out["error"] = exc.reason
                    break
        return out

    try:
        return shell.call_raw(ask)
    except HarnessError as exc:
        return {"rm_id": "", "features": (), "replies": {}, "error": f"cannot ask the harness: {exc}"}
    except Exception as exc:  # noqa: BLE001 - a codec surprise is a reason, not a crash
        return {"rm_id": "", "features": (), "replies": {},
                "error": f"unexpected harness reply: {type(exc).__name__}: {exc}"}


# --- the ConsoleAdapter methods ------------------------------------------------------------------


def _shell_names(endpoints: Mapping[str, str], shell: Any) -> list[str]:
    """The consoles the shell serves over Ethernet (the rest are serial ports or shares)."""
    if shell is None:
        return []
    return [n for n in (*CONSOLE_PORTS, HOSTIO_CONSOLE) if n in endpoints]


def console_baud_info(endpoints: Mapping[str, str], shell: Any,
                      on_features: Any = None) -> dict[str, dict[str, Any]]:
    """``ConsoleAdapter.console_baud_info()`` for the MPS3.

    One row per console this module knows the rate of: the shell's consoles
    (one control connection; the ``uart_baud`` verb when the harness has it) and
    ``tcp://`` hub shares. Host serial ports (``serial://``) are left out: the
    broker owns their rate.
    """
    rows: dict[str, dict[str, Any]] = {}
    for name, url in endpoints.items():
        if name not in CONSOLE_PORTS and name != HOSTIO_CONSOLE and urlparse(url).scheme == "tcp":
            rows[name] = serial_row(url)
    names = _shell_names(endpoints, shell)
    if not names:
        return rows
    got = query(shell, tuple(n for n in names if n in VERB_STREAMS))
    if on_features is not None and got["features"]:
        on_features(tuple(got["features"]))      # mint 4: the same connection told us 6931's name
    for name in names:
        base = design_row(name, got["rm_id"]) if got["rm_id"] else _row(
            "ethernet", None, "unknown", reason=got["error"] or "the harness did not report rm_id")
        reply = got["replies"].get(name)
        if reply is not None:
            rows[name] = harness_row(name, reply, base)
        elif FEATURE in got["features"] and name in VERB_STREAMS and got["error"]:
            rows[name] = {**base, "settable": False, "reason": got["error"]}
        else:
            rows[name] = base
    return rows


def console_set_baud(endpoints: Mapping[str, str], shell: Any, name: str,
                     baud: int) -> dict[str, Any]:
    """``ConsoleAdapter.console_set_baud(name, baud)``: the ``uart_baud`` verb.

    ``baud`` 0 clears the override (back to auto or fixed). Refuses with
    ``UnavailableError`` (and the reason) when the harness cannot set it.
    """
    if isinstance(baud, bool) or not isinstance(baud, int) or baud < 0:
        raise UsageError(f"baud must be a whole number >= 0, not {baud!r}")
    if name not in _shell_names(endpoints, shell) or name not in VERB_STREAMS:
        row = console_baud_info(endpoints, shell).get(name)
        why = row["reason"] if row else f"{name} is not a harness console"
        raise UnavailableError(BAUD_CAPABILITY, why)

    def ask(client: Any, tap: Any) -> dict[str, Any]:
        ping = client.ping()
        feats = _features(client)
        base = design_row(name, ping.rm_id if ping.ok else "")
        if FEATURE not in feats:
            raise UnavailableError(BAUD_CAPABILITY, base["reason"])
        current = harness_row(name, _uart_baud(client, tap, name), base)
        if not current["settable"]:
            raise UnavailableError(BAUD_CAPABILITY, current["reason"])
        reply = _uart_baud(client, tap, name, baud)
        if reply.get("ok") is not True:
            raise ActionFailedError(
                f"the harness refused {name} at {baud} baud: {reply.get('err') or 'no reason'}",
                hint="pick another rate; `harness-manager baud TARGET NAME` shows the choices")
        return harness_row(name, reply, base)

    return shell.call_raw(ask)


# --- hostio (mint 4, net-protocol v0.20): the mode of the DUT's console pins ------------------
#
# With the engine feature NAME ``hostio`` the DUT's P1 pins carry either its console (the reset
# value) or hostio4. ``{"op":"hostio"}`` reads the mode; ``act:"set"`` changes what the NEXT
# release of the DUT's reset latches (claim-locked; refused ``not_held`` while a design runs).

HOSTIO_CAPABILITY = "hostio"
HOSTIO_MODES = ("console", "hostio4")

#: One plain sentence per refusal ``code`` (the contract's table). ``not_held`` is the one a
#: user meets: the board names a hex design id that means nothing to them.
HOSTIO_NOT_HELD = ("Load the greybox or hold the DUT first; the change applies at the next "
                   "design load.")
_HOSTIO_CODE_WORDS = {
    "not_supported": "This board's software has no hostio support.",
    "no_hw": "This board's hardware has no hostio block (it needs the mint-4 static).",
    "locked": "The board is claimed: the change has to go through its SSH.",
    "busy": "The board is busy with something else; try again in a moment.",
    "timeout": "The board did not answer in time; try again.",
    "not_held": HOSTIO_NOT_HELD,
}


def hostio_failure(reply: Mapping[str, Any]) -> HarnessError:
    """A refused ``hostio`` reply as the error shown to the user (plain words)."""
    code = str(reply.get("code") or "")
    err = str(reply.get("err") or "")
    if not code and "unknown op" in err.lower():
        code = "not_supported"
    if code == "not_held":
        return RefusedError(HOSTIO_NOT_HELD, hint="the DUT keeps its current pins until then")
    if code in ("not_supported", "no_hw"):
        return UnavailableError(HOSTIO_CAPABILITY, _HOSTIO_CODE_WORDS[code])
    words = _HOSTIO_CODE_WORDS.get(code)
    return ActionFailedError(f"{words} ({err})" if words and err else words or
                             f"the board refused the hostio request: {err or 'no reason'}")


def hostio_view(reply: Mapping[str, Any]) -> dict[str, Any]:
    """The ``hostio`` ``get`` body as the console panel shows it. ``changing`` is True when
    ``pending`` differs from ``mode`` (a change waits for the next design load)."""
    mode = str(reply.get("mode") or "console")
    pending = str(reply.get("pending") or mode)
    drops = reply.get("drops") if isinstance(reply.get("drops"), Mapping) else {}
    out: dict[str, Any] = {
        "mode": mode, "pending": pending, "changing": pending != mode,
        "held": bool(reply.get("held")), "target_rst": bool(reply.get("target_rst")),
        "ch0": dict(reply["ch0"]) if isinstance(reply.get("ch0"), Mapping) else {},
        "ch1": dict(reply["ch1"]) if isinstance(reply.get("ch1"), Mapping) else {},
        "drops": {"ch0": drops.get("ch0", 0), "ch1": drops.get("ch1", 0)},
        "note": (f"Changes to {pending} at the next design load." if pending != mode else ""),
    }
    return out


def hostio_status(shell: Any) -> dict[str, Any]:
    """The hostio mode and counters (any peer may read). ``UnavailableError`` if the board
    has no hostio."""
    reply = shell.call_raw(lambda client, _tap: client.hostio())
    if reply.get("ok") is not True:
        raise hostio_failure(reply)
    return hostio_view(reply)


def hostio_set_mode(session: Any, mode: str) -> dict[str, Any]:
    """Ask the DUT to use ``mode`` (``console`` or ``hostio4``) from its next reset release.

    Claim-locked: goes through the board's SSH when the claim is ours, else direct and again
    through the SSH if the board says it is claimed (the identity setter's way). A board
    running a design answers ``not_held``, shown as ``HOSTIO_NOT_HELD``."""
    if mode not in HOSTIO_MODES:
        raise UsageError(f"hostio mode must be console or hostio4, not {mode!r}")
    shell = getattr(session, "shell", None)
    if shell is None:
        raise UnavailableError(HOSTIO_CAPABILITY, "no Ethernet link to the harness")
    what = "changing the console pins' mode"

    def route(claimed: bool | None = None) -> str:
        claim = getattr(session, "claim", None)
        if claim is None or not callable(getattr(claim, "lock_route", None)):
            return "board-ssh" if claimed else ""
        from .constants import IMPL_LINUX

        return claim.lock_route(what, impl=IMPL_LINUX, claimed=claimed)

    def via_board() -> dict[str, Any]:
        from .card import _forwarded, _gate_key
        from .os_slots import claim_forward
        from .shell import Mps3Shell

        with claim_forward(session, "hostio", what) as (host, ctl, _push):
            fwd = _forwarded(Mps3Shell(host, ctl, gate_key=_gate_key(shell)))
            return fwd.call_raw(lambda client, _tap: client.hostio_set_mode(mode))

    how = route()
    if how == "board-ssh":
        reply = via_board()
    else:
        reply = shell.call_raw(lambda client, _tap: client.hostio_set_mode(mode))
        if reply.get("ok") is not True and str(reply.get("code") or "") == "locked":
            route(claimed=True)
            reply, how = via_board(), "board-ssh"
    if reply.get("ok") is not True:
        raise hostio_failure(reply)
    return {"mode": reply.get("mode"), "pending": reply.get("pending"),
            "applies": reply.get("applies") or "rp_release", "route": how or "direct"}
