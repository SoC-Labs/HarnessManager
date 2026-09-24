"""Shared data model. Frozen dataclasses only, so every layer can pass them freely.

Two rules the whole app relies on:

1. **Every reading carries its provenance.** ``Reading.source`` says where a
   value came from, and a value that could not be obtained is
   ``Reading.unavailable(reason)``, never 0. (The shell's telemetry verb was
   redesigned for exactly this reason; see pyverify ``TelemetryResponse``.)
2. **Three-state checks stay three-state.** ``Check.UNCHECKED`` is not a pass.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class LinkKind(str, Enum):
    """The physical/logical routes the engine can have to a board."""

    ETHERNET = "ethernet"      # shell data plane (control 6900, push, consoles, debug)
    USB_SERIAL = "usb_serial"  # board-controller console and FPGA UART lanes
    USB_MSD = "usb_msd"        # board configuration storage as a mass-storage volume
    USB_DEBUG = "usb_debug"    # on-board debug probe (e.g. MPS3 CMSIS-DAP)
    JTAG = "jtag"              # FPGA JTAG cable (hw_server / openocd)
    HUB = "hub"                # everything above, reached through fpgahub
    SMART_POWER = "smart_power"  # networked metered plug / PDU on the board's supply
    SSH = "ssh"                # SSH into a Linux harness (console, logs, forwards, file copy)


@dataclass(frozen=True)
class Link:
    kind: LinkKind
    address: str               # "192.168.10.101", "/dev/ttyUSB10", "E:\\", "hub://mapstone-dev/mps3_01"
    detail: str = ""           # human-readable: "FT4232H if00 (MCC)", "V2M-MPS3 volume"
    via: str = ""              # "" direct | "ssh" (SSH port-forward) | "hub" (fpgahub tunnel): TCP-only


class Check(str, Enum):
    OK = "ok"
    MISMATCH = "mismatch"
    UNCHECKED = "unchecked"    # could not compare, which is NOT a pass


@dataclass(frozen=True)
class Reading:
    """One telemetry value with provenance. ``value`` is None when unavailable."""

    name: str                  # "fpga_die_temp", "vccint", "mcc_temp", "board_power"
    value: float | None
    unit: str                  # "degC", "V", "W", "MHz"
    source: str                # "sysmon-jtag", "mcc-console", "stmpe811", "estimate:vivado"
    observed_at: float = field(default_factory=time.time)
    reason: str = ""           # why unavailable, or a caveat ("unverified sensor")

    @classmethod
    def unavailable(cls, name: str, unit: str, reason: str, source: str = "") -> Reading:
        return cls(name=name, value=None, unit=unit, source=source, reason=reason)

    @property
    def available(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class BoardIdentity:
    """What the harness running on the board says about itself.

    Board-agnostic field names. The MPS3 pack fills these from the shell's
    ``ping`` and ``version`` verbs.
    """

    board_type: str            # "mps3"
    shell_id: str = ""         # static-region compatibility key, "0x3f1a560f"
    rm_id: str = ""            # loaded reconfigurable module, "0x01000001"
    rm_name: str = ""          # resolved from the overlay store when known
    harness_version: str = ""  # "1.0.0"
    firmware_sha: str = ""
    firmware_dirty: bool = False
    features: tuple[str, ...] = ()
    build_check: Check = Check.UNCHECKED   # firmware vs fabric skew verdict
    unit_id: str = ""          # per-board serial/DNA when the harness can report it
    harness_impl: str = ""     # "bare-metal" | "linux" | "" (unknown: harness predates `version`)
    proto: str = ""            # net-protocol version the harness speaks, when it says
    usercode: str = ""         # implementation-run identity (static_usercode), when it says
    name: str = ""             # the board's own name, when the harness reports one (CCR N1-1)
    # OTA-C (H1): the packed HARNESS_VER32 the firmware reports ("0x01000000"), when it says.
    # Names the release once VERSION is stamped with its tag (HARNESS-DIST §3.2 rule 4).
    ver32: str = ""


@dataclass(frozen=True)
class Health:
    reachable: bool
    control_channel: str = "unknown"   # "idle" | "busy" | "wedged" | "offline" | "rescue" | "unknown"
    counters: dict[str, int] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    """A board a pack believes is present, before any session is opened."""

    pack: str                  # board-pack name, "mps3"
    board_id: str              # stable local id, "mps3@192.168.10.101" or a USB serial
    links: tuple[Link, ...]
    label: str = ""            # what the selection dialog shows
    evidence: str = ""         # how it was found ("answered ping", "FT4232H 0403:6011")
    identity: BoardIdentity | None = None   # what the board said while being probed, if anything
    # CCR N1-1: the board's display name ("mps3-01") and where it came from:
    # "config" (boards.toml name) | "harness" | "hub" | "hub-target" | "" (no name: show
    # the address). Display only: a name never keys anything (board_id does).
    # harness_manager.naming holds the resolution order.
    name: str = ""
    name_source: str = ""


@dataclass(frozen=True)
class BoardInfo:
    candidate: Candidate
    identity: BoardIdentity
    health: Health
    capabilities: frozenset[str]
    unavailable: dict[str, str] = field(default_factory=dict)   # capability -> reason
