"""USB discovery for the MPS3 Debug USB connector. Team T3.

Hooks ``pack.py`` calls:

- ``probe_usb(hints, already_found)`` -> candidates with ``USB_SERIAL`` and
  ``USB_MSD`` links;
- ``serial_console_endpoints(candidate)`` -> the FPGA UART lanes as consoles.

What the Debug USB connector carries (TRM 100765 §2.18; fpgahub mcc.py):

- an **FT4232H** (VID:PID ``0403:6011``, ``constants.FT4232H_VID_PID``) with four
  UARTs. Interface 00 is the MCC ``Cmd>`` console; interface 01 is FPGA UART
  lane 0 or 1, chosen by the MCC's ``UARTMODE`` mux (config.txt; the live boot
  log says ``UART0: MCC, UART1: FPGA0``, i.e. UARTMODE 0,
  docs/evidence/2026-09-w2/pB_mcc_log_20260923.txt); interfaces 02 and 03 are
  lanes 2 and 3, hard-wired (V2M-MPS3 schematic sheet 9). The shell console is
  on lane 2;
- the configuration microSD as USB mass storage, volume label ``V2M-MPS3``;
- the on-board CMSIS-DAP (DAPLink) with its own drive (``MBED MPS3``). That
  drive is never taken for the config SD (see ``sd.py``).

How the FT4232H interface number is found, per OS (pyserial 3.5 ``list_ports``):

- **Linux**: ``location`` is the sysfs interface name, ``1-2.3.4.3:1.0``; the
  number after the last dot is ``bInterfaceNumber``. The part before ``:`` is
  the USB port path, which also groups the four ports of one chip.
- **Windows, usbser/WinUSB**: ``location`` ends ``:x.<MI_xx>``, same rule.
- **Windows, FTDI VCP driver**: the location is hidden and the driver appends
  ``A``-``D`` to the serial number, one letter per channel; A is interface 0.
  Used only when at least two ports share the base serial with distinct
  letters.
- otherwise the interface is unknown and no MCC link is offered (a REBOOT
  typed on an FPGA lane is the historical silent no-op).

Pairing: a USB board is paired with a V2M-MPS3 volume when there is exactly
one of each, or when both sit under the same USB hub. A USB board is paired
with an Ethernet shell only when exactly one of each was found; otherwise
they stay separate and ``evidence`` says why. A paired Ethernet candidate is
REMOVED from ``already_found`` (in place) and replaced by the merged
candidate this returns, so ``pack.probe`` does not list the board twice.

Probing only lists ports and volumes; it never opens a port or writes a file.
Every OS call is injectable (``UsbEnv``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, MutableSequence, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from socharness.core.errors import HarnessError
from socharness.core.model import Candidate, Link, LinkKind
from socharness.core.pack import ProbeHints

from . import sd as sdmod
from .constants import FT4232H_VID_PID, MSD_VOLUME_LABEL

log = logging.getLogger(__name__)

MCC_INTERFACE = 0
LANE_DETAIL = {
    0: "MCC console",
    1: "FPGA UART lane 0 or 1 (MCC UARTMODE mux)",
    2: "FPGA UART lane 2 (hard-wired; the shell console)",
    3: "FPGA UART lane 3 (hard-wired)",
}
_LANE_RE = re.compile(r"\bif0([1-3])\b")
_LOC_IF_RE = re.compile(r":(?:\d+|x)\.(\d+)$")


# --- port model -----------------------------------------------------------------------


@dataclass(frozen=True)
class UsbSerialPort:
    """The fields of a pyserial ``ListPortInfo`` this module uses."""

    device: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str = ""
    location: str = ""
    product: str = ""
    interface_name: str = ""

    @classmethod
    def from_info(cls, info: Any) -> UsbSerialPort:
        if isinstance(info, UsbSerialPort):
            return info
        return cls(
            device=str(getattr(info, "device", "")),
            vid=getattr(info, "vid", None),
            pid=getattr(info, "pid", None),
            serial_number=getattr(info, "serial_number", None) or "",
            location=getattr(info, "location", None) or "",
            product=getattr(info, "product", None) or "",
            interface_name=getattr(info, "interface", None) or "",
        )


@dataclass(frozen=True)
class Ft4232Board:
    """The four UARTs of one FT4232H."""

    key: str
    serial: str
    usb_path: str                        # USB port path of the chip ("1-2.3.4.3"), "" if hidden
    interfaces: dict[int, str]           # interface number -> device
    how: str                             # how the interface numbers were found
    unassigned: tuple[str, ...] = ()     # ports whose interface number is unknown


def interface_from_location(location: str) -> int | None:
    m = _LOC_IF_RE.search(location or "")
    return int(m.group(1)) if m else None


def _port_path(location: str) -> str:
    return (location or "").split(":", 1)[0]


def _hub_of(usb_path: str) -> str:
    """The hub a USB port path hangs off: '1-2.3.4.3' -> '1-2.3.4'."""
    return usb_path.rsplit(".", 1)[0] if "." in usb_path else ""


def group_ft4232(ports: Sequence[Any]) -> list[Ft4232Board]:
    """Group FT4232H ports into chips and number their interfaces."""
    fts = [p for p in (UsbSerialPort.from_info(i) for i in ports)
           if (p.vid, p.pid) == FT4232H_VID_PID]
    by_location: dict[str, dict[str, Any]] = {}
    leftovers: list[UsbSerialPort] = []
    for p in fts:
        iface = interface_from_location(p.location)
        if iface is None:
            leftovers.append(p)
            continue
        g = by_location.setdefault(_port_path(p.location), {
            "serial": p.serial_number, "ifs": {}, "extra": []})
        if iface in g["ifs"]:
            g["extra"].append(p.device)
        else:
            g["ifs"][iface] = p.device
    boards = [
        Ft4232Board(key=f"usb:{path}", serial=g["serial"], usb_path=path, interfaces=g["ifs"],
                    how="interface numbers from the USB location", unassigned=tuple(g["extra"]))
        for path, g in sorted(by_location.items())
    ]
    # FTDI VCP (Windows): no location; the serial carries a channel letter A-D.
    by_base: dict[str, list[tuple[int, UsbSerialPort]]] = {}
    unknown: list[UsbSerialPort] = []
    for p in leftovers:
        m = re.fullmatch(r"(.+?)([A-D])", p.serial_number)
        if m:
            by_base.setdefault(m.group(1), []).append(("ABCD".index(m.group(2)), p))
        else:
            unknown.append(p)
    for base, members in sorted(by_base.items()):
        letters = [n for n, _ in members]
        if len(members) >= 2 and len(set(letters)) == len(letters):
            boards.append(Ft4232Board(
                key=f"ser:{base}", serial=base, usb_path="",
                interfaces={n: p.device for n, p in members},
                how="interface numbers from the FTDI channel letter (A=0 … D=3)"))
        else:
            unknown.extend(p for _, p in members)
    by_serial: dict[str, list[UsbSerialPort]] = {}
    for p in unknown:
        by_serial.setdefault(p.serial_number, []).append(p)
    for serial, members in sorted(by_serial.items()):
        boards.append(Ft4232Board(
            key=f"ser:{serial or '?'}", serial=serial, usb_path="", interfaces={},
            how="interface numbers unknown (no USB location, no FTDI channel letter)",
            unassigned=tuple(p.device for p in members)))
    return boards


# --- environment ----------------------------------------------------------------------


def _default_list_ports() -> list[Any]:
    from socharness.transports import direct

    return direct.comports()


@dataclass
class UsbEnv:
    list_ports: Callable[[], list[Any]] = _default_list_ports
    list_volumes: Callable[[], list[sdmod.VolumeInfo]] = sdmod.list_volumes


DEFAULT_ENV: UsbEnv | None = None   # tests may swap this; read on every probe


# --- probe ----------------------------------------------------------------------------


def _serial_url(device: str) -> str:
    return device if "://" in device else f"serial://{device}"


def _board_links(board: Ft4232Board) -> list[Link]:
    links: list[Link] = []
    serial = board.serial or "?"
    for n in (MCC_INTERFACE, 1, 2, 3):          # the MCC first: session.link() takes the first
        dev = board.interfaces.get(n)
        if dev:
            links.append(Link(LinkKind.USB_SERIAL, _serial_url(dev),
                              f"FT4232H {serial} if0{n}: {LANE_DETAIL[n]}"))
    return links


@dataclass
class _UsbBoard:
    serial_links: list[Link] = field(default_factory=list)
    volume: Link | None = None
    evidence: list[str] = field(default_factory=list)


def _explicit(hints: ProbeHints, env: UsbEnv) -> list[_UsbBoard]:
    boards: list[_UsbBoard] = []
    serials = [_UsbBoard(serial_links=[Link(LinkKind.USB_SERIAL, _serial_url(url),
                                            "MCC console (given explicitly)")],
                         evidence=[f"MCC console {url} given explicitly"])
               for url in hints.serial_ports]
    vols = []
    for spec in hints.volumes:
        # Explicit input gets an explicit refusal (the DAPLink drive, a non-SD folder).
        root = sdmod.Mps3Storage(spec, env=sdmod.SdEnv(list_volumes=env.list_volumes)).locate()
        vols.append(_UsbBoard(volume=Link(LinkKind.USB_MSD, root, f"{MSD_VOLUME_LABEL} volume (given explicitly)"),
                              evidence=[f"config SD {root} given explicitly"]))
    if len(serials) == 1 and len(vols) == 1:
        s, v = serials[0], vols[0]
        s.volume = v.volume
        s.evidence += v.evidence + ["paired: one serial port and one volume were given"]
        return [s]
    boards.extend(serials)
    boards.extend(vols)
    return boards


def _scanned(env: UsbEnv) -> list[_UsbBoard]:
    notes: list[str] = []
    try:
        ports = env.list_ports()
    except HarnessError as exc:            # pyserial missing: still look for the SD
        ports = []
        notes.append(f"serial ports not scanned ({exc.message})")
    try:
        volumes = env.list_volumes()
    except OSError as exc:
        volumes = []
        notes.append(f"volumes not scanned ({exc})")
    fts = group_ft4232(ports)
    sds = [v for v in volumes if v.label.upper() == MSD_VOLUME_LABEL.upper()]
    boards: list[_UsbBoard] = []
    for ft in fts:
        b = _UsbBoard(serial_links=_board_links(ft))
        vid, pid = FT4232H_VID_PID
        where = f" at USB {ft.usb_path}" if ft.usb_path else ""
        b.evidence.append(f"FT4232H {vid:04x}:{pid:04x} serial {ft.serial or '?'}{where} ({ft.how})")
        if MCC_INTERFACE not in ft.interfaces:
            b.evidence.append("no MCC link: interface 00 could not be identified")
        boards.append(b)
    unmatched_sd = []
    for vol in sds:
        if vol.root is None:
            notes.append(f"{vol.label} volume {vol.device} is attached but not mounted")
            continue
        unmatched_sd.append(vol)
    # Pair SD volumes with FT4232H chips.
    if len(boards) == 1 and len(unmatched_sd) == 1:
        vol = unmatched_sd.pop()
        boards[0].volume = _volume_link(vol)
        boards[0].evidence.append(f"config SD {vol.root}: the only FT4232H and the only "
                                  f"{MSD_VOLUME_LABEL} volume")
    else:
        for b, ft in zip(boards, fts, strict=True):
            hub = _hub_of(ft.usb_path)
            same = [v for v in unmatched_sd if hub and _hub_of(v.usb_path) == hub]
            if len(same) == 1:
                vol = same[0]
                unmatched_sd.remove(vol)
                b.volume = _volume_link(vol)
                b.evidence.append(f"config SD {vol.root}: on the same USB hub {hub}")
        for vol in unmatched_sd:
            boards.append(_UsbBoard(volume=_volume_link(vol),
                                    evidence=[f"{MSD_VOLUME_LABEL} volume at {vol.root}; its FT4232H "
                                              "could not be told apart"]))
    for b in boards:
        b.evidence.extend(notes)
    return boards


def _volume_link(vol: sdmod.VolumeInfo) -> Link:
    dev = f" on {vol.device}" if vol.device else ""
    return Link(LinkKind.USB_MSD, str(vol.root), f"{MSD_VOLUME_LABEL} volume{dev}")


def _candidate(b: _UsbBoard) -> Candidate:
    links = tuple(b.serial_links) + ((b.volume,) if b.volume else ())
    parts = []
    if b.serial_links:
        parts.append("MCC console" if any(not _LANE_RE.search(lk.detail) for lk in b.serial_links)
                     else "FPGA UARTs")
    if b.volume:
        parts.append("config SD")
    # board_id = "mps3@usb:<first link address>", the same rule the CLI uses for a
    # USB-only target, so one board has one session lock however it was found.
    return Candidate(pack="mps3", board_id=f"mps3@usb:{links[0].address}", links=links,
                     label=f"MPS3 on Debug USB ({', '.join(parts)})",
                     evidence="; ".join(b.evidence))


def probe_usb(hints: ProbeHints, already_found: MutableSequence[Candidate] | Sequence[Candidate],
              env: UsbEnv | None = None) -> list[Candidate]:
    """MPS3 boards on USB. Explicit ``hints.serial_ports``/``hints.volumes`` bypass scanning."""
    env = env or DEFAULT_ENV or UsbEnv()
    if hints.serial_ports or hints.volumes:
        boards = _explicit(hints, env)
    elif hints.scan_usb:
        boards = _scanned(env)
    else:
        return []
    cands = [_candidate(b) for b in boards if b.serial_links or b.volume]
    return _pair_with_ethernet(cands, already_found)


def _pair_with_ethernet(usb: list[Candidate],
                        already_found: MutableSequence[Candidate] | Sequence[Candidate]) -> list[Candidate]:
    usb_kinds = (LinkKind.USB_SERIAL, LinkKind.USB_MSD)
    eth = [c for c in already_found
           if c.pack == "mps3" and any(lk.kind == LinkKind.ETHERNET for lk in c.links)
           and not any(lk.kind in usb_kinds for lk in c.links)]
    if not usb:
        return []
    if len(eth) == 1 and len(usb) == 1:
        e, u = eth[0], usb[0]
        merged = Candidate(
            pack="mps3", board_id=e.board_id, links=e.links + u.links,
            label=f"{e.label} + Debug USB",
            evidence=f"{e.evidence}; {u.evidence}; paired: the only MPS3 shell on Ethernet "
                     "and the only MPS3 on USB")
        if isinstance(already_found, MutableSequence):
            already_found.remove(e)          # superseded by the merged candidate
        return [merged]
    if not eth:
        why = "not paired with an Ethernet shell: none was found"
    else:
        why = (f"not paired with an Ethernet shell: {len(eth)} MPS3 shell(s) on Ethernet and "
               f"{len(usb)} on USB; which is which cannot be told until the harness reports a "
               "board serial (firmware A0)")
    return [Candidate(pack=c.pack, board_id=c.board_id, links=c.links, label=c.label,
                      evidence=f"{c.evidence}; {why}" if c.evidence else why) for c in usb]


# --- console endpoints ----------------------------------------------------------------


def uartmode_of(volume_root: str) -> int | None:
    """``UARTMODE:`` from the SD's config.txt (TRM §3.5.2), or None if unreadable."""
    root = Path(volume_root)
    try:
        cfg = next((p for p in root.iterdir() if p.name.lower() == "config.txt"), None)
        if cfg is None:
            return None
        text = cfg.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        key, sep, value = line.partition(":")
        if sep and key.strip().upper() == "UARTMODE":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def serial_console_endpoints(candidate: Candidate) -> dict[str, str]:
    """FPGA UART lanes as console endpoints: ``fpga_uart0..3`` -> ``serial://…``.

    Interfaces 02/03 are lanes 2/3 (hard-wired). Interface 01 is lane 0 when the
    SD's config.txt says ``UARTMODE: 0`` and lane 1 for 1 or 2; when the mode
    cannot be read, interface 01 is left out rather than guessed. The MCC
    console (interface 00) is not listed: the controller adapter owns it.
    """
    lanes: dict[int, str] = {}
    for link in candidate.links:
        if link.kind != LinkKind.USB_SERIAL:
            continue
        m = _LANE_RE.search(link.detail)
        if m:
            lanes.setdefault(int(m.group(1)), link.address)
    if not lanes:
        return {}
    out: dict[str, str] = {}
    if 1 in lanes:
        msd = next((lk for lk in candidate.links if lk.kind == LinkKind.USB_MSD), None)
        mode = uartmode_of(msd.address) if msd is not None else None
        if mode == 0:
            out["fpga_uart0"] = lanes[1]
        elif mode in (1, 2):
            out["fpga_uart1"] = lanes[1]
    for n in (2, 3):
        if n in lanes:
            out[f"fpga_uart{n}"] = lanes[n]
    return out


def is_lane_link(link: Link) -> bool:
    """True for an FT4232H FPGA-lane link (interfaces 01-03), which is never the MCC."""
    return link.kind == LinkKind.USB_SERIAL and bool(_LANE_RE.search(link.detail))
