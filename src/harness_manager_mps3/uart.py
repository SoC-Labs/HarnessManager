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
    UnavailableError,
    UsageError,
)

from .constants import CONSOLE_PORTS, KNOWN_DESIGNS

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


#: design_id -> uart0 rate. Every row cites the wrapper that fixes it.
DESIGN_UART: dict[int, DesignUart] = {
    0x0001: DesignUart("nanosoc", 76800,
                       "fpga/rp/nanosoc/rp_nanosoc_wrapper.sv:54 (UART_BAUD -> uart_axis_shim "
                       ".BAUD, 382-384)"),
    0x0003: DesignUart("nanosoc_multicore", 76800,
                       "fpga/rp/nanosoc_multicore/rp_nanosoc_multicore_wrapper.sv:49 (UART_BAUD -> "
                       "uart_axis_shim .BAUD, 229-231)"),
    0x0005: DesignUart("nanosoc_upy", 76800,
                       "fpga/rp/nanosoc_upy/rp_nanosoc_upy_wrapper.sv:62 (passed to "
                       "rp_nanosoc_wrapper, 164)"),
    0x0008: DesignUart("nanosoc_iice", 76800,
                       "fpga/rp/nanosoc_iice/rp_nanosoc_iice_core.sv:165-182 (forwards only NGPIO, "
                       "so rp_nanosoc_wrapper.sv:54's default applies)"),
    0x000A: DesignUart("nanosoc_ila", 76800,
                       "fpga/rp/nanosoc_ila/rp_nanosoc_ila_wrapper.sv:40 (passed to "
                       "rp_nanosoc_wrapper, 107)"),
    # Designs that put nothing on the console pair.
    0x0000: DesignUart("greybox", None, "fpga/dfx/rms/rm_greybox/rm_greybox.sv",
                       "no design is loaded (greybox): nothing drives uart0"),
    0x0002: DesignUart("eth_ss", None, "fpga/rp/eth_ss/rp_eth_ss_wrapper.sv:193-195",
                       "eth_ss ties its console off (rp_eth_ss_wrapper.sv:193-195)"),
    0x0004: DesignUart("uart_echo", None, "fpga/dfx/rms/rm_uart_echo/rm_uart_echo.sv:273-274",
                       "uart_echo echoes bytes on the AXI-Stream itself: there is no serial line, "
                       "so there is no rate (rm_uart_echo.sv:273-274)"),
    0x0007: DesignUart("clcd_demo", None, "fpga/rp/clcd_demo/rp_clcd_demo_wrapper.sv:331-333",
                       "clcd_demo ties its console off (rp_clcd_demo_wrapper.sv:331-333)"),
    0x0009: DesignUart("dbg_demo", None, "fpga/rp/dbg_demo/rp_dbg_demo_wrapper.sv:148-150",
                       "dbg_demo ties its console off (rp_dbg_demo_wrapper.sv:148-150)"),
}

#: uart1: the shell's UARTBR U1 has no partition pins in v0.1; the BD ties its DUT side off.
UART1_WHY = ("nothing drives uart1: the shell ties its DUT side off until a design widens the "
             "partition contract (fpga/shell/ip/uart_bridge/README.md:72-75)")
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
    if stream == "uart1":
        return _row("ethernet", None, "design", reason=UART1_WHY)
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
    for key in ("design", "cite"):
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
    return [n for n in CONSOLE_PORTS if n in endpoints]


def console_baud_info(endpoints: Mapping[str, str], shell: Any) -> dict[str, dict[str, Any]]:
    """``ConsoleAdapter.console_baud_info()`` for the MPS3.

    One row per console this module knows the rate of: the shell's consoles
    (one control connection; the ``uart_baud`` verb when the harness has it) and
    ``tcp://`` hub shares. Host serial ports (``serial://``) are left out: the
    broker owns their rate.
    """
    rows: dict[str, dict[str, Any]] = {}
    for name, url in endpoints.items():
        if name not in CONSOLE_PORTS and urlparse(url).scheme == "tcp":
            rows[name] = serial_row(url)
    names = _shell_names(endpoints, shell)
    if not names:
        return rows
    got = query(shell, tuple(n for n in names if n in VERB_STREAMS))
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
