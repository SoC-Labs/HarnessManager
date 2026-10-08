"""Bring a NEW board up from this PC over its Debug USB (lane BRINGUP-USB).

The app's Add dialog ("Over USB") and the bring-up wizard (``js/bringup.js``) drive this
through ``daemon/bringup_api.py``. Everything that WRITES or REBOOTS goes through what
already exists (docs/USER_GUIDE.md §3.1, "First install"): the storage adapter's backup,
install and restore (``Mps3Storage``: journaled, a verified backup first, never an
``.ebf``, never an MCC command file, only the ``V2M-MPS3`` volume), the controller's
witnessed REBOOT, and the harness catalogue's install. This module adds only what the
wizard needs around them:

- ``scan``: the MPS3 Debug USBs this PC sees (the pack's USB probe: ``ProbeHints``
  ``scan_usb`` with no network), each with its MCC serial port, its ``V2M-MPS3`` drive, what
  the drive holds (read only: the board revision, what ``board.txt`` loads), what the MCC
  answers when asked (``ask_mcc``: one ``?`` at its prompt, through the controller adapter,
  which never types during a boot), and whether a harness answers on Ethernet at the
  address a new board comes up on;
- ``check_bundle``: a bundle folder or zip on this PC, validated before anything is written:
  the config-SD tree (``config.txt`` and ``MB/``; or a release bundle's ``config-sd/`` (v2.0:
  the bundle ROOT with ``overlays/`` and ``linux_bundle.json``) or older ``sd/``), the base
  ``.bit`` named with its size, sha256, part and USERID, and refused with the reason for an
  ``.ebf``, an MCC command file, anything outside the config-SD tree, or no bitstream. A
  bundle is UNSIGNED (david 2 Oct, D3a): its sha256 (``bundle_sha256``: the zip's, or the
  folder manifest's) names it, and every write of it needs the typed ``INSTALL UNSIGNED
  <first 8 hex>`` (``require_unsigned``; ``--yes`` never implies it);
- ``witness``: after the REBOOT, wait for the harness to answer at its address (the pack's
  probe of that one host: 6900 ping, else UDP identify, which also finds stage0 RESCUE);
- ``status``: the switches the wizard shows (``bringup.sd_flash``, the network OS door, the
  signing keys) and the default address;
- ``propose_identity``: what the new board should be called (david 2 Oct, D4a: a generic image,
  then the wizard names the board): a name from the MCC's USB serial number (``MPS3-`` and its
  last 4), and (lane IDENTITY, david 2 Oct: a unique IP per board, a RANDOM MAC; this replaces
  the MAC derived from the serial) a random MAC and the next free IP of the pack's pool
  (``services/identity_assign.py``). Only a PROPOSAL: the "Name this board" dialog and the
  identity writer (``board identity`` / net-protocol v0.16 ``identity_set``) set it, with its
  typed phrase.

Nothing here writes a device, mounts a volume or downloads anything.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HarnessError,
    NothingOnTargetError,
    RefusedError,
    UsageError,
)
from harness_manager.core.model import Candidate, LinkKind
from harness_manager.core.pack import ProbeHints
from harness_manager.services import unsigned as _unsigned

#: Where a new board's harness answers: the image's default address (the Linux harness keeps
#: it as a permanent secondary under DHCP, and stage0 RESCUE answers there).
DEFAULT_HOST = "192.168.10.101"
#: This PC's side of that network (USER_GUIDE §3.1 step 5).
PC_ADDRESS_HINT = ("this PC needs an Ethernet port on the board's network: give it an address "
                   "on 192.168.10.0/24 (e.g. 192.168.10.1, netmask 255.255.255.0) and cable it "
                   "to the board's Ethernet port")
#: The volume label of the MCC's configuration SD over USB mass storage.
VOLUME_LABEL = "V2M-MPS3"

SD_FLASH_SETTING = "bringup.sd_flash"
SD_FLASH_ENV = "HARNESS_MANAGER_BRINGUP_SD_FLASH"
SD_FLASH_ON = frozenset({"on", "true", "1", "yes"})
SD_FLASH_OFF_REASON = (f"off: turn on {SD_FLASH_SETTING} in Settings (or set "
                       f"{SD_FLASH_ENV}=on) to write a card in this PC's card reader")
RESCUE_NETWORK_REASON = "comes with Linux v2.1"
RESCUE_NETWORK_NOTE = "stage0 rescue boots an image from RAM; it does not write the card"
#: The Linux OS step writes a WHOLE-CARD image (SD-FLASH kind ``card``): the MBR at LBA 0, the
#: boot-select at LBA 1-2, the slots at LBA 67584 and 198656. A slot image (linux_slot.img,
#: S0LB at its start) written at byte 0 gives a card that never boots.
CARD_IMAGE_HINT = "a whole-card image (.img built with stage0_mkcard.py card --card-img)"
USB_WRITE_WARNING = ("A USB write can take 5 minutes: do not unplug, power off or start a "
                     "second write.")
NONE_FOUND = "No MPS3 Debug USB found on this PC."
NONE_FOUND_HINTS = (
    "the Debug USB cable: the board's DEBUG USB socket to this PC",
    "the board's power: switch it on and wait about 10 s for the MCC to start",
    f"the {VOLUME_LABEL} drive: it must be mounted (Linux: open it in the file manager, or "
    "udisksctl mount -b /dev/sdX1; macOS and Windows mount it themselves)",
)
#: Windows (lane WINDOWS): where a Windows user looks.
WINDOWS_NONE_FOUND_HINTS = (
    "the Debug USB cable: the board's DEBUG USB socket to this PC",
    "the board's power: switch it on and wait about 10 s for the MCC to start",
    f"the {VOLUME_LABEL} drive: Windows gives it a drive letter itself (File Explorer, This "
    "PC); if it is not there, try another USB port or cable",
    "the serial ports: Device Manager, Ports (COM & LPT) lists four 'USB Serial Port (COMn)' "
    "for the board; if they are missing or under Other devices, install the FTDI VCP driver "
    "(Windows Update, or ftdichip.com), then replug the Debug USB",
)
WINDOWS_NO_MCC = (
    "no MCC serial port with it: the board cannot be rebooted from here (power it off and on "
    "by hand after the write). In Device Manager, Ports (COM & LPT) should list four 'USB "
    "Serial Port (COMn)' for the board; if not, install the FTDI VCP driver (Windows Update, "
    "or ftdichip.com), then replug the Debug USB and Scan again")


def _windows(platform: str | None = None) -> bool:
    import sys

    return (platform or sys.platform).startswith("win")


DEFAULT_WITNESS_S = 180.0          # bare metal answers in ~30 s; a cold FPGA load is ~20 s
LINUX_WITNESS_S = 300.0            # the pack's Linux reboot budget (planner.LINUX_REBOOT_WAIT_S)

# The config-SD tree's rules (services/update/bundle.py check_sd_component, the same words).
MCC_COMMAND_FILES = frozenset({"reboot.txt", "reset.txt", "shutdown.txt"})   # TRM 100765 §3.2
#: What a desktop leaves in a folder or zip: never written, listed as ignored.
OS_JUNK = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
OS_JUNK_DIRS = frozenset({"__macosx", ".spotlight-v100", ".trashes", ".fseventsd",
                          "system volume information"})
MPS3_PART = "xcku115"
MAX_TEXT = 64 * 1024               # a board/app file bigger than this is not one

# An unsigned bundle (a folder or zip on this PC; david 2 Oct, D3a): allowed, with this banner and
# a typed phrase naming its sha256 (services/unsigned.py: the one rule for every unsigned write).
# Signed releases (the catalogue, a mirror) never need it.
UNSIGNED_WORDS = _unsigned.WORDS
UNSIGNED_BANNER = _unsigned.BANNER
ZIP_RECIPE = _unsigned.ZIP_RECIPE
MANIFEST_RECIPE = _unsigned.MANIFEST_RECIPE

ProgressFn = Callable[[str, int, int], None]


# --- scan -------------------------------------------------------------------------------------


def _is_lane(detail: str) -> bool:
    """An FPGA UART lane (FT4232H interfaces 01-03), never the MCC (usb.LANE_DETAIL)."""
    return bool(re.search(r"\bif0[1-3]\b", detail or "")) or "FPGA UART" in (detail or "")


def _serial_device(address: str) -> str:
    return address[len("serial://"):] if address.startswith("serial://") else address


def usb_links(cand: Candidate) -> dict[str, Any]:
    """The candidate's Debug USB links: the MCC port, the FPGA lanes, the config SD."""
    mcc = next((lk for lk in cand.links
                if lk.kind == LinkKind.USB_SERIAL and not _is_lane(lk.detail)), None)
    lanes = [lk for lk in cand.links if lk.kind == LinkKind.USB_SERIAL and _is_lane(lk.detail)]
    vol = next((lk for lk in cand.links if lk.kind == LinkKind.USB_MSD), None)
    return {
        "mcc": {"port": _serial_device(mcc.address), "url": mcc.address,
                "detail": mcc.detail} if mcc else None,
        "lanes": [{"port": _serial_device(lk.address), "detail": lk.detail} for lk in lanes],
        "volume": {"path": vol.address, "detail": vol.detail} if vol else None,
    }


def _read_text(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > MAX_TEXT:
            return ""
        return path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return ""


def _ci(root: Path, *parts: str) -> Path | None:
    """``root/parts`` matched case-insensitively (FAT), or None."""
    here: Path | None = root
    for part in parts:
        if here is None:
            return None
        try:
            here = next((p for p in here.iterdir() if p.name.lower() == part.lower()), None)
        except OSError:
            return None
    return here


def _key(text: str, name: str) -> str:
    """The value of ``NAME:`` in an MCC config file (the part before ``;``)."""
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        k, sep, v = line.partition(":")
        if sep and k.strip().upper() == name.upper():
            return v.strip()
    return ""


def board_files(root: Path) -> dict[str, Any]:
    """What an MPS3 config-SD tree loads: ``MB/<rev>/board.txt`` -> ``APPFILE`` ->
    ``F0FILE``, as the MCC reads them. Every path SD-relative (POSIX); "" when not found."""
    out: dict[str, Any] = {"revisions": [], "board_file": "", "app_file": "", "fpga_file": "",
                           "mb_bios": ""}
    mb = _ci(root, "MB")
    if mb is None or not mb.is_dir():
        return out
    try:
        revs = sorted(p.name for p in mb.iterdir() if p.is_dir() and
                      p.name.upper().startswith("HBI"))
    except OSError:
        revs = []
    out["revisions"] = revs
    for rev in revs:
        board = _ci(mb, rev, "board.txt")
        if board is None:
            continue
        text = _read_text(board)
        out["board_file"] = f"MB/{rev}/{board.name}"
        out["mb_bios"] = _key(text, "MBBIOS")
        app = _key(text, "APPFILE").replace("\\", "/").strip("/")
        if not app:
            continue
        app_path = _ci(mb, rev, *app.split("/"))
        if app_path is None:
            out["app_file"] = f"MB/{rev}/{app}"
            continue
        out["app_file"] = app_path.relative_to(root).as_posix()
        f0 = _key(_read_text(app_path), "F0FILE").replace("\\", "/").strip("/")
        if f0:
            bit = _ci(app_path.parent, *f0.split("/"))
            out["fpga_file"] = (bit.relative_to(root).as_posix() if bit is not None
                                else (app_path.parent / f0).relative_to(root).as_posix())
        break
    return out


def drive_facts(path: str) -> dict[str, Any]:
    """What a V2M-MPS3 drive holds, read only (no file is opened for writing)."""
    root = Path(path)
    if not root.is_dir():
        return {"readable": False, "why": f"{path} is not a mounted folder"}
    facts: dict[str, Any] = board_files(root)
    facts["readable"] = True
    facts["config_txt"] = _ci(root, "config.txt") is not None
    facts["journal"] = _ci(root, ".harness-manager-journal.json") is not None
    return facts


def drive_board_facts(path: str) -> dict[str, Any] | None:
    """The board's revision and the MCC's firmware, from the V2M-MPS3 drive's LOG.TXT (and its
    one MB/HBI0309* folder); None when neither is there. Reads only; never raises."""
    try:
        from harness_manager.services import board_facts
        from harness_manager_mps3.sd import board_revision_of, mcc_firmware_of

        root = Path(path)
        rev, rev_from = board_revision_of(root)
        fw, fw_from = mcc_firmware_of(root)
        return board_facts.build(rev, rev_from, fw, fw_from)
    except Exception:  # noqa: BLE001 - a fact that cannot be read is unknown
        return None


@dataclass
class ScanBoard:
    candidate: Candidate
    links: dict[str, Any]
    open_here: bool = False
    holder: str = ""
    drive: dict[str, Any] | None = None
    facts: dict[str, Any] | None = None     # QUICKWINS G2: revision, MCC firmware (read only)
    mcc_answer: dict[str, Any] = field(default_factory=lambda: {"state": "not-asked", "text": ""})
    ethernet: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        from harness_manager.cli.output import jsonable

        return {"board_id": self.candidate.board_id, "candidate": jsonable(self.candidate),
                **self.links, "open": self.open_here, "holder": self.holder,
                "drive": self.drive, "facts": self.facts, "mcc_answer": self.mcc_answer,
                "ethernet": self.ethernet, "problems": list(self.problems),
                "evidence": self.candidate.evidence}


def _problems(links: dict[str, Any], platform: str | None = None) -> list[str]:
    out = []
    if links["volume"] is None:
        out.append(f"no {VOLUME_LABEL} drive with it: the configuration SD cannot be backed up "
                   "or written over USB. Check the drive is mounted, then Scan again")
    if links["mcc"] is None and _windows(platform):
        out.append(WINDOWS_NO_MCC)
    elif links["mcc"] is None:
        out.append("no MCC serial port with it: the board cannot be rebooted from here (power "
                   "it off and on by hand after the write). Check the Debug USB cable, and on "
                   "Windows the FTDI driver")
    return out


def _gate_none(_bid: str) -> Any:
    return contextlib.nullcontext()


def ask_mcc(engine: Any, cand: Candidate, *,
            gate: Callable[[str], Any] = _gate_none) -> dict[str, Any]:
    """One ``?`` at the MCC's prompt, through the board's controller adapter.

    A board open here is asked through its own session (under ``gate``, the daemon's board
    gate); a board nobody holds is opened for the question and closed again; a board another
    program holds is not asked. The adapter listens first and never types while the MCC
    boots (a keypress in its auto-boot window stops the boot)."""
    bid = cand.board_id
    opened = False
    try:
        if bid in engine.open_boards():
            session = engine.session(bid)
        else:
            owner = engine.lock_owner(bid)
            if owner is not None:
                who = owner.describe() if hasattr(owner, "describe") else str(owner)
                return {"state": "held", "text": f"not asked: {who} has this board open"}
            session = engine.open(cand, note="harness-manager bring-up: asking the MCC")
            opened = True
        ctl = getattr(session, "controller", None)
        if ctl is None:
            return {"state": "none", "text": "no MCC console on this board's links"}
        with gate(bid):
            reply = ctl.command("?")
        out: dict[str, Any] = {"state": "answers", "text": "answers at its Cmd> prompt"}
        lines = [ln.strip() for ln in str(reply or "").splitlines() if ln.strip()]
        if lines:
            out["reply"] = lines[:4]
        out.update(_boot_facts(getattr(ctl, "last_transcript", b"")))
        return out
    except NothingOnTargetError as exc:
        return {"state": "silent", "text": exc.message,
                "hint": exc.hint or "is the board on? the MCC is FT4232H interface 00"}
    except HarnessError as exc:
        return {"state": "error", "text": exc.message, "hint": exc.hint or ""}
    finally:
        if opened:
            with contextlib.suppress(HarnessError):
                engine.close(bid)


def _boot_facts(transcript: Any) -> dict[str, Any]:
    """The MCC's firmware and board from a boot banner it printed while we listened."""
    if not isinstance(transcript, (bytes, bytearray)) or not transcript:
        return {}
    try:
        from harness_manager_mps3.mcc import parse_boot_log
    except ImportError:
        return {}
    boots = parse_boot_log(bytes(transcript).decode("ascii", "replace"))
    if not boots:
        return {}
    b = boots[-1]
    return {k: v for k, v in (("firmware", b.firmware), ("board", b.board),
                              ("hbi_build", b.hbi_build)) if v}


def _at(address: str, host: str) -> bool:
    """Whether a link ``address`` (``host:port``) is ``host`` (a bare host: any port)."""
    text = address.split("@", 1)[-1].strip().lower()
    want = host.strip().lower()
    if want.count(":") == 1:
        return text == want
    return (text.rsplit(":", 1)[0] if text.count(":") == 1 else text) == want


def network_check(host: str) -> dict[str, Any] | None:
    """Lane WINDOWS: this PC's network check for a board at ``host`` (Windows only: an
    address on the board's /24, a Public profile, a firewall block; None elsewhere)."""
    from harness_manager.services import netcheck

    return netcheck.check(host)


def ethernet(engine: Any, host: str = DEFAULT_HOST, *, timeout_s: float = 1.5,
             check_network: bool = False) -> dict[str, Any]:
    """Whether a harness answers at ``host``: the pack's probe of that one address (6900
    ping, then UDP identify, which also finds stage0 RESCUE). Never broadcasts.
    ``check_network``: when nothing answers, add this PC's network check (``network``,
    Windows only)."""
    from harness_manager.cli.output import jsonable

    hints = ProbeHints(hosts=(host,), scan_usb=False, scan_network=True, timeout_s=timeout_s)
    try:
        found = engine.probe(hints)
    except HarnessError as exc:
        return {"state": "error", "host": host, "text": exc.message}
    for cand in found:
        eth = [lk for lk in cand.links if lk.kind == LinkKind.ETHERNET]
        if not any(_at(lk.address, host) for lk in eth):
            continue
        rescue = any("rescue" in lk.detail.lower() for lk in eth) or \
            "RESCUE" in (cand.evidence or "")
        ident = getattr(cand, "identity", None)
        out: dict[str, Any] = {
            "state": "rescue" if rescue else "running", "host": host,
            "board_id": cand.board_id, "label": cand.label, "evidence": cand.evidence,
            "impl": getattr(ident, "harness_impl", "") or "",
            "harness": getattr(ident, "harness_version", "") or "",
            "shell_id": getattr(ident, "shell_id", "") or "",
            "candidate": jsonable(cand),
        }
        what = " ".join(x for x in (out["impl"], out["harness"]) if x)
        out["text"] = (f"stage0 RESCUE answers at {host}: no bootable OS slot (TFTP and "
                       "identify only)" if rescue else
                       f"a harness answers at {host}" + (f": {what}" if what else ""))
        return out
    out = {"state": "none", "host": host, "text": f"nothing answers at {host}"}
    if check_network:
        found = network_check(host)
        if found is not None:
            out["network"] = found
    return out


def scan(engine: Any, *, host: str = DEFAULT_HOST, ask: bool = False,
         gate: Callable[[str], Any] = _gate_none, timeout_s: float = 1.5) -> dict[str, Any]:
    """The MPS3 Debug USBs this PC sees, what each says, and whether a harness answers."""
    found = engine.probe(ProbeHints(scan_usb=True, scan_network=False, timeout_s=timeout_s))
    usb = [c for c in found if any(lk.kind in (LinkKind.USB_SERIAL, LinkKind.USB_MSD)
                                   for lk in c.links)
           and not any(lk.kind == LinkKind.ETHERNET for lk in c.links)]
    open_ids = set(engine.open_boards())
    boards: list[ScanBoard] = []
    for cand in usb:
        links = usb_links(cand)
        b = ScanBoard(candidate=cand, links=links, open_here=cand.board_id in open_ids)
        owner = None if b.open_here else engine.lock_owner(cand.board_id)
        if owner is not None:
            b.holder = owner.describe() if hasattr(owner, "describe") else str(owner)
        if links["volume"] is not None:
            b.drive = drive_facts(links["volume"]["path"])
            b.facts = drive_board_facts(links["volume"]["path"])
        b.problems = _problems(links)
        if ask and links["mcc"] is not None:
            b.mcc_answer = ask_mcc(engine, cand, gate=gate)
        boards.append(b)
    eth = ethernet(engine, host, timeout_s=timeout_s, check_network=True)
    notes: list[str] = []
    if len(boards) == 1:
        boards[0].ethernet = eth
    elif boards and eth["state"] in ("running", "rescue"):
        notes.append(f"{eth['text']}, but {len(boards)} boards are on USB: which one it is "
                     "cannot be told until the harness reports a board serial")
    if len(boards) > 1:
        notes.append(f"{len(boards)} Debug USBs: add the one you are bringing up (unplug the "
                     "others to be sure which is which)")
    out: dict[str, Any] = {"boards": [b.as_dict() for b in boards], "ethernet": eth,
                           "host": host, "notes": notes, "candidates": usb}
    if not boards:
        out["empty"] = {"text": NONE_FOUND, "check": list(
            WINDOWS_NONE_FOUND_HINTS if _windows() else NONE_FOUND_HINTS)}
    return out


# --- a bundle folder or zip ---------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


unsigned_phrase = _unsigned.phrase
folder_manifest = _unsigned.folder_manifest


def require_unsigned(chk: BundleCheck, typed: Any) -> None:
    """The typed ``INSTALL UNSIGNED <sha8>`` for THIS bundle, as it is now (checked again just
    before a write, so a bundle changed since it was shown is refused): else REFUSED (15),
    ``error.data.unsigned`` the phrase and the sha256. Never implied by ``--yes``."""
    info = chk.unsigned_info()
    if info is None:
        raise RefusedError(f"{chk.path} has no sha256 to confirm", hint="check the bundle again")
    _unsigned.require(info, typed)


def _junk(rel: str) -> bool:
    parts = rel.lower().split("/")
    return parts[-1] in OS_JUNK or any(p in OS_JUNK_DIRS for p in parts[:-1]) or \
        parts[-1].startswith("._")


@dataclass
class BundleCheck:
    path: str
    kind: str = "dir"                         # dir | zip
    layout: str = ""                          # sd-tree | release-bundle
    sd_root: str = ""
    impl: str = ""                            # bare-metal | linux | "" (the bundle does not say)
    version: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
    total_bytes: int = 0
    base_bit: dict[str, Any] | None = None
    board: dict[str, Any] = field(default_factory=dict)
    os_image: dict[str, Any] | None = None
    overlays: dict[str, Any] | None = None      # BUNDLE/overlays/open: {path, count, names}
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    install_files: dict[str, str] = field(default_factory=dict)
    sha256: str = ""                          # the bundle's: the zip's, or its manifest's
    sha256_of: str = ""                       # zip | manifest
    manifest_files: int = 0                   # a folder: the files its manifest lists

    @property
    def refused(self) -> bool:
        return bool(self.problems)

    @property
    def unsigned_phrase(self) -> str:
        return unsigned_phrase(self.sha256) if self.sha256 else ""

    def unsigned_info(self) -> _unsigned.Unsigned | None:
        """What the typed phrase names (services/unsigned.py); None before a sha256."""
        if not self.sha256:
            return None
        return _unsigned.Unsigned(self.path, self.sha256, self.sha256_of,
                                  self.manifest_files if self.sha256_of == "manifest" else None)

    def unsigned(self) -> dict[str, Any] | None:
        """What the app and the CLI show for the typed phrase; None before a sha256."""
        info = self.unsigned_info()
        return info.as_dict() if info is not None else None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "layout": self.layout,
                "sd_root": self.sd_root, "impl": self.impl, "version": self.version,
                "files": self.files, "count": len(self.files), "total_bytes": self.total_bytes,
                "base_bit": self.base_bit, "board": self.board, "os_image": self.os_image,
                "overlays": self.overlays, "problems": self.problems, "warnings": self.warnings, "ignored": self.ignored,
                "refused": self.refused, "linux": self.impl == "linux", "sha256": self.sha256,
                "unsigned": self.unsigned()}


def _unpack(path: Path, work: Path, sha256: str) -> Path:
    """A zip's contents under ``work`` (safe: no absolute paths, ``..``, symlinks or
    oversize; ``bundle.safe_extract``), reused when the same zip (``sha256``) was checked
    before."""
    from harness_manager.services.update.bundle import safe_extract

    digest = sha256[:16]
    dest = work / f"{path.stem[:40]}-{digest}"
    if (dest / ".complete").is_file():
        return dest
    work.mkdir(parents=True, exist_ok=True)
    safe_extract(path, dest)
    (dest / ".complete").write_text(digest + "\n", encoding="ascii")
    return dest


def _single_top(root: Path) -> Path:
    """A zip of a folder: its one top folder, when the zip holds nothing else."""
    try:
        entries = [p for p in root.iterdir() if p.name != ".complete" and not _junk(p.name)
                   and p.name.lower() not in OS_JUNK_DIRS]
    except OSError:
        return root
    if len(entries) == 1 and entries[0].is_dir() and entries[0].name.lower() not in ("mb", "sd", "config-sd"):
        return entries[0]
    return root


def _manifest_impl(root: Path) -> tuple[str, str]:
    """(impl, version) from a release bundle's ``linux_bundle.json`` or ``mint.json``."""
    import json

    for name, impl in (("linux_bundle.json", "linux"), ("mint.json", "bare-metal")):
        p = root / name
        if p.is_file():
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
            version = str(doc.get("version") or doc.get("harness_version") or "") \
                if isinstance(doc, dict) else ""
            return impl, version
    return "", ""


def check_bundle(path: str | Path, work: Path) -> BundleCheck:
    """Validate a bundle folder or zip; never writes anything but a zip's extraction."""
    if not str(path).strip():
        raise UsageError("give the bundle's folder or zip")
    given = Path(str(path)).expanduser()
    if not given.is_absolute():
        raise UsageError(f"{path} is not an absolute path",
                         hint="paths are on the machine running harness-manager-daemon")
    if not given.exists():
        raise AbsentError(f"nothing at {given}", hint="give a bundle folder, or its .zip")
    chk = BundleCheck(path=str(given))
    if given.is_file():
        if given.suffix.lower() == ".ebf":
            chk.problems.append(f"{given.name} is board-controller firmware (.ebf): it is never "
                                "written")
            return chk
        if given.suffix.lower() != ".zip":
            raise UsageError(f"{given.name} is neither a folder nor a .zip",
                             hint="give the bundle's folder, or its .zip")
        chk.kind = "zip"
        chk.sha256, chk.sha256_of = file_sha256(given), "zip"
        try:
            root = _single_top(_unpack(given, work, chk.sha256))
        except RefusedError as exc:
            chk.problems.append(exc.message)
            return chk
        sums: dict[str, str] = {}
    else:
        root = given
        manifest, rel_sums, links = folder_manifest(given)
        chk.sha256, chk.sha256_of = hashlib.sha256(manifest).hexdigest(), "manifest"
        chk.manifest_files = len(rel_sums)
        sums = {str(given / rel): h for rel, h in rel_sums.items()}
        if links:
            more = f" and {len(links) - 5} more" if len(links) > 5 else ""
            chk.problems.append(f"{', '.join(links[:5])}{more}: a symbolic link; a bundle "
                                "carries only regular files (its sha256 covers the files, not "
                                "what a link points to)")
    sd_name = next((n for n in ("sd", "config-sd") if (root / n).is_dir()), "")
    outer = root.parent if (not sd_name and root.name.lower() in ("sd", "config-sd")
                            and _is_bundle_root(root.parent)) else None
    if sd_name or outer is not None:
        # A release bundle: v2.0's config-sd/ (+ overlays/ + linux_bundle.json at its root),
        # the older sd/ (+ overlays/open/), or the bare SD folder of either, which then looks
        # for its bundle one folder up.
        chk.layout = "release-bundle"
        sd = root / sd_name if sd_name else root
        top = root if sd_name else outer
        chk.impl, chk.version = _manifest_impl(top)
        img = top / "linux_slot.img"
        if img.is_file():
            # an OS SLOT image (S0LB at its start): pushed into slot A/B over Ethernet, never
            # written to a card at byte 0 (that card never boots); not the whole-card image
            chk.os_image = {"path": str(img), "size": img.stat().st_size,
                            "sha256": sums.get(str(img)) or file_sha256(img), "kind": "slot"}
            chk.impl = chk.impl or "linux"
        _bundle_overlays(chk, top)
        if (top / "overlays" / "aaa").is_dir():
            chk.warnings.append("overlays/aaa (Arm Academic Access RMs) is left out: it is "
                                "private; Program finds it once you add its folder yourself")
        for p in (sorted(top.rglob("*")) if outer is None else ()):
            if p.is_file() and p.suffix.lower() == ".ebf" and sd not in p.parents:
                chk.problems.append(f"{p.relative_to(root).as_posix()}: an .ebf (board-"
                                    "controller firmware) is never written; remove it")
    elif _ci(root, "config.txt") is not None or _ci(root, "MB") is not None:
        chk.layout = "sd-tree"
        sd = root
    else:
        chk.problems.append(f"{root} holds no config-SD tree: expected config.txt and MB/ "
                            "(or a release bundle's config-sd/ or sd/ with them)")
        return chk
    chk.sd_root = str(sd)
    files: dict[str, Path] = {}
    for p in sorted(sd.rglob("*")):
        rel = p.relative_to(sd).as_posix()
        if rel == ".complete":
            continue
        if p.is_symlink():
            chk.problems.append(f"{rel}: a symlink; refusing")
            continue
        if not p.is_file():
            continue
        if _junk(rel):
            chk.ignored.append(rel)
            continue
        files[rel] = p
    ebf = [r for r in files if r.lower().endswith(".ebf")]
    if ebf:
        chk.problems.append(f"{', '.join(ebf)}: an .ebf (board-controller firmware) is never "
                            "written; a copied MB BIOS silently reflashes the MCC")
    cmd = [r for r in files if "/" not in r and r.lower() in MCC_COMMAND_FILES]
    if cmd:
        chk.problems.append(f"{', '.join(cmd)}: an MCC command file makes the MCC act at once "
                            "(TRM §3.2); refusing")
    outside = [r for r in files if not (r.lower() == "config.txt" or r.lower().startswith("mb/"))
               and r not in cmd]
    if outside:
        more = f" and {len(outside) - 5} more" if len(outside) > 5 else ""
        chk.problems.append(f"outside the config-SD tree (config.txt and MB/): "
                            f"{', '.join(outside[:5])}{more}")
    if not files:
        chk.problems.append("the config-SD tree is empty")
        return chk
    chk.files = [{"path": r, "size": p.stat().st_size} for r, p in sorted(files.items())]
    chk.total_bytes = sum(f["size"] for f in chk.files)
    chk.board = board_files(sd)
    bits = [r for r in files if r.lower().endswith(".bit")]
    if not bits:
        chk.problems.append("no .bit: the config SD's base bitstream is missing")
    else:
        named = chk.board.get("fpga_file") or ""
        base = next((r for r in bits if r.lower() == named.lower()), None)
        if base is None:
            base = bits[0] if len(bits) == 1 else None
            if named:
                chk.problems.append(f"the board file names {named}, which the bundle does not "
                                    "hold")
            elif base is None:
                chk.warnings.append(f"{len(bits)} .bit files and no board file names one: "
                                    f"{', '.join(bits)}")
        if base is not None:
            chk.base_bit = _bit_facts(base, files[base], chk, sums.get(str(files[base])))
    revs = chk.board.get("revisions") or []
    if revs and not any(r.upper().startswith("HBI0309") for r in revs):
        chk.warnings.append(f"MB/{', MB/'.join(revs)}: not an MPS3 (HBI0309) tree")
    if not chk.problems:
        chk.install_files = {r: str(p) for r, p in files.items()}
    return chk


def _bit_facts(rel: str, path: Path, chk: BundleCheck, sha256: str | None = None
               ) -> dict[str, Any]:
    from harness_manager.services.update.bitheader import BitHeaderError, read_bit_header

    out: dict[str, Any] = {"path": rel, "size": path.stat().st_size,
                           "sha256": sha256 or file_sha256(path)}
    try:
        hdr = read_bit_header(path)
    except (BitHeaderError, OSError) as exc:
        chk.problems.append(f"{rel} is not a Xilinx bitstream: {exc}")
        return out
    out.update(part=hdr.part, userid=hdr.userid, design=hdr.design.split(";", 1)[0],
               date=f"{hdr.date} {hdr.time}".strip())
    if hdr.part and not hdr.part_matches(MPS3_PART):
        chk.problems.append(f"{rel} is built for {hdr.part}; the MPS3 is {MPS3_PART}")
    if not hdr.stamped:
        chk.warnings.append(f"{rel} carries no USERID: no implementation run names it")
    return out


def _is_bundle_root(path: Path) -> bool:
    """A folder that holds a release bundle's ``linux_bundle.json`` / ``mint.json``."""
    return any((path / n).is_file() for n in ("linux_bundle.json", "mint.json"))


def _read_json(path: Path) -> dict[str, Any]:
    import json

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _u32(value: Any) -> int | None:
    try:
        return int(str(value), 0) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None


def _bundle_overlays(chk: BundleCheck, top: Path) -> None:
    """The bundle's overlays: ``overlays/open/`` (the older layout) when it is there, else
    ``overlays/`` itself (v2.0: one folder per design). Each is checked the way Program
    checks one: its ``ip_class`` (anything but ``open`` is left out) and its static / usercode
    keying against the bundle's shell (``linux_bundle.json`` / ``mint.json``)."""
    base = top / "overlays"
    path = base / "open" if (base / "open").is_dir() else base
    shell: dict[str, Any] = {}
    for name in ("linux_bundle.json", "mint.json"):
        if (top / name).is_file():
            shell = _read_json(top / name)
            break
    chk.overlays = overlay_set(path, shell=shell, warnings=chk.warnings)


def overlay_set(path: Path, *, shell: dict[str, Any] | None = None,
                warnings: list[str] | None = None) -> dict[str, Any] | None:
    """A bundle's overlay dirs under ``path`` (each with a manifest.json), or ``path`` itself
    when it is one. ``excluded``: dirs whose manifest says an ``ip_class`` other than
    ``open`` (not joined). Keying against ``shell`` is a warning, never a refusal: the same
    check Program makes before it swaps."""
    if not path.is_dir():
        return None
    dirs = [path] if (path / "manifest.json").is_file() else \
        sorted(p for p in path.iterdir() if (p / "manifest.json").is_file())
    names: list[str] = []
    excluded: list[str] = []
    for d in dirs:
        man = _read_json(d / "manifest.json")
        klass = man.get("ip_class")
        if isinstance(klass, str) and klass and klass != "open":
            excluded.append(d.name)
            if warnings is not None:
                warnings.append(f"overlay {d.name} is ip_class {klass}, not open: left out")
            continue
        names.append(d.name)
        if shell and warnings is not None:
            for key in ("static_id", "static_usercode"):
                want, got = _u32(shell.get(key)), _u32(man.get(key))
                if want is not None and got is not None and want != got:
                    warnings.append(f"overlay {d.name} is keyed to {key} {man.get(key)}, the "
                                    f"bundle's shell is {shell.get(key)}: Program will refuse it")
    if not names:
        return None
    out: dict[str, Any] = {"path": str(path), "count": len(names), "names": names}
    if excluded:
        out["excluded"] = excluded
    return out


OVERLAY_DIRS_SETTING = "mps3.overlay_dirs"


def keep_overlays(chk: BundleCheck, keep_root: Path) -> Path | None:
    """Where the bundle's open overlays will be found from now on: the bundle's own folder,
    or (a zip, unpacked in the service's work area that is cleared) a copy under
    ``keep_root``. None when the bundle has none."""
    if not chk.overlays:
        return None
    src = Path(chk.overlays["path"])
    filtered = bool(chk.overlays.get("excluded"))
    if chk.kind != "zip" and not filtered:
        return src
    digest = hashlib.sha256(chk.path.encode()).hexdigest()[:12]
    dest = keep_root / f"{Path(chk.path).stem[:40]}-{digest}"
    if not dest.is_dir():
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        if filtered:       # only the open ones: the rest never join the catalogue
            tmp.mkdir(parents=True)
            for name in chk.overlays["names"]:
                shutil.copytree(src / name, tmp / name)
        else:
            shutil.copytree(src, tmp)
        tmp.rename(dest)
    return dest


def add_overlay_dir(settings: Any, path: Path) -> dict[str, Any]:
    """Append ``path`` to ``mps3.overlay_dirs`` (the user's settings.toml, ``apply: live``), so
    Program and Restore find the bundle's overlays. Kept when already there. ``shadowed``:
    ``$HARNESS_MANAGER_MPS3_OVERLAY_DIRS`` in the service's environment hides the setting."""
    from harness_manager.settings import ops

    resolver = settings.resolver()
    user = resolver.layer.values.get(OVERLAY_DIRS_SETTING)      # the user's own list, if any
    current = [str(x) for x in user] if isinstance(user, (list, tuple)) else []
    out: dict[str, Any] = {"setting": OVERLAY_DIRS_SETTING, "path": str(path), "added": False}
    if str(path) not in current:
        result = ops.set_values(settings, {OVERLAY_DIRS_SETTING: [*current, str(path)]})
        out["added"] = True
        out["result"] = result
    got = settings.resolver().resolve(OVERLAY_DIRS_SETTING)
    out["dirs"] = [str(x) for x in got.value or []]
    out["shadowed"] = got.source == "env" and str(path) not in out["dirs"]
    out["where"] = got.where or got.source
    return out


# --- the witness ---------------------------------------------------------------------------------


def witness(engine: Any, host: str = DEFAULT_HOST, *, wait_s: float = DEFAULT_WITNESS_S,
            poll_s: float = 3.0, progress: ProgressFn | None = None,
            now: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep, timeout_s: float = 1.5) -> dict[str, Any]:
    """Wait for the harness to answer at ``host`` after the REBOOT; a dark board after
    ``wait_s`` is ``ActionFailedError`` (its ``data`` says ``timeout``)."""
    from harness_manager.cli.output import with_data

    if wait_s <= 0:
        raise UsageError("wait_s must be positive")
    emit = progress or (lambda phase, done, total: None)
    start = now()
    tries = 0
    while True:
        tries += 1
        ans = ethernet(engine, host, timeout_s=timeout_s)
        took = now() - start
        if ans["state"] in ("running", "rescue"):
            emit(ans["state"], int(took), int(wait_s))
            return {**ans, "took_s": round(took, 1), "tries": tries}
        if took >= wait_s:
            err = with_data(ActionFailedError(
                f"nothing answered at {host} within {wait_s:.0f} s of the reboot",
                hint=f"{PC_ADDRESS_HINT}. If the board stays dark, restore the backup "
                     "(the SD goes back to what it held)"),
                timeout=True, host=host, waited_s=round(took, 1), tries=tries)
            found = network_check(host)          # Windows: the address, the profile, a block
            if found is not None:
                with_data(err, network=found)
            raise err
        emit("waiting", int(took), int(wait_s))
        sleep(min(poll_s, max(wait_s - took, 0.1)))


# --- the identity a new board is given (a proposal; the identity writer sets it) ------------------

#: Lane IDENTITY (david 2 Oct): every board gets a unique IP and a random MAC.
IP_NOTE = ("every board gets its own IP: a free address of the pool, searched from its MAC (the image default "
           "192.168.10.101 is never given)")
NO_SERIAL = ("the MCC's USB serial number is not known (the Debug USB did not report one): give "
             "the board a name yourself")
#: The bare-metal harness: nothing to set (harness_manager_mps3.net_identity's words).
NO_IDENTITY_STORE = ("the bare-metal harness has no identity store: its label, IP and MAC are "
                     "compiled into the firmware; the proposal is for the Linux harness "
                     "(net-protocol v0.16 identity_set)")
#: A Linux image before net-protocol v0.16 has no identity_set: the writer says so.
IDENTITY_SET_NOTE = ("an image before net-protocol v0.16 has no identity_set: `board identity` "
                     "says so and changes nothing")
_FT_SERIAL = re.compile(r"\bFT4232H\s+(\S+)\s+if00\b")


def mcc_serial(cand: Candidate | None, *,
               list_ports: Callable[[], list[Any]] | None = None) -> str:
    """The MCC's USB serial number: the Debug USB's FT4232H serial (its MCC console,
    interface 00), as the pack's USB probe recorded it on the link, else as this PC lists the
    MCC port (``list_ports``: the pack's port listing, for a port given by hand). ``""`` when
    nothing says."""
    if cand is None:
        return ""
    for lk in cand.links:
        m = _FT_SERIAL.search(lk.detail or "")
        if m and m.group(1) != "?":
            return m.group(1)
    m = re.search(r"\bFT4232H [0-9a-f]{4}:[0-9a-f]{4} serial (\S+)", cand.evidence or "")
    if m and m.group(1) != "?":
        return m.group(1)
    mcc = next((lk for lk in cand.links if lk.kind == LinkKind.USB_SERIAL
                and not _is_lane(lk.detail)), None)
    if mcc is None:
        return ""
    try:
        from harness_manager_mps3 import usb as usbmod
    except ImportError:
        return ""
    try:
        ports = (list_ports or usbmod.DEFAULT_ENV.list_ports)()
        boards = usbmod.group_ft4232(ports)      # the pack's own grouping (and Windows' A-D)
    except (HarnessError, OSError):
        return ""
    want = _serial_device(mcc.address)
    for ft in boards:
        if ft.interfaces.get(usbmod.MCC_INTERFACE) == want and ft.serial:
            return ft.serial
    return ""


def identity_inputs(engine: Any) -> tuple[Any, dict[str, list[str]], dict[str, list[str]]]:
    """The pack's identity policy and this Harness Manager's registry (every MAC and IP it
    assigned or saw: ``<state>/identity/seen.json``), for a proposal."""
    from harness_manager.services import identity_assign as IA
    from harness_manager.services.board_identity import SeenIdentities

    svc = getattr(engine, "board_identity", None)
    seen = getattr(svc, "seen", None)
    if not isinstance(seen, SeenIdentities):
        state = getattr(engine, "state_dir", None)
        seen = SeenIdentities(Path(state) / "identity") if state else None
    macs = seen.taken("mac") if seen is not None else {}
    ips = seen.taken("ip") if seen is not None else {}
    return IA.policy_for(engine), macs, ips


def propose_identity(serial: str, *, policy: Any = None, taken_macs: Any = (),
                     taken_ips: Any = (), urandom: Callable[[int], bytes] | None = None,
                     ) -> dict[str, Any]:
    """What the wizard and the CLI propose for a new board (editable; nothing is set, written
    or reserved here): ``{serial, label, hostname, mac, mac_how, ip, ip_how, ip_error,
    same_net, notes}``. The name is ``MPS3-`` and the serial's last 4 letters or digits
    (upper case: the panel takes A-Z, 0-9 and -); the MAC is random (byte 0 the pack's,
    never a reserved range or a MAC in the registry); the IP is the first free address of the
    pack's pool (``ip_error`` says why there is none)."""
    from harness_manager.core.errors import HarnessError
    from harness_manager.services import identity_assign as IA

    policy = policy or IA.GENERIC_POLICY
    key = re.sub(r"[^A-Z0-9]", "", serial.strip().upper())
    tail = key[-4:]
    label = IA.normalize_name(f"MPS3-{tail}") if tail else ""
    notes = [IP_NOTE]
    if not key:
        notes.insert(0, NO_SERIAL)
    if policy.rescue_note:
        notes.append(policy.rescue_note)
    mac = IA.random_mac(policy, taken_macs, **({"urandom": urandom} if urandom else {}))
    ip, ip_error = "", ""
    try:
        ip = IA.allocate_ip(policy, taken_ips, mac=mac)        # starts at mac[5] mod pool
    except HarnessError as exc:
        ip_error = exc.message + (f" ({exc.hint})" if exc.hint else "")
    return {"serial": serial.strip(), "label": label, "hostname": label.lower(), "mac": mac,
            "mac_how": IA.MAC_RANDOM, "ip": ip, "ip_how": IA.IP_AUTO if ip else "",
            "ip_error": ip_error, "same_net": IA.same_net_note(ip), "pool": policy.ip_pool,
            "ip_note": IP_NOTE, "notes": notes}


def identity_command(host: str, proposal: dict[str, Any]) -> str:
    """The ``board identity`` command that sets the proposal (its typed phrase is the label):
    the proposal's own MAC and IP, so what was shown is what is set."""
    parts = ["harness-manager board identity", host]
    for key in ("label", "ip", "mac"):
        value = proposal.get(key)
        if value:
            parts.append(f"--{key} {str(value).split('/', 1)[0] if key == 'ip' else value}")
    if proposal.get("label"):
        parts.append(f"--consent {proposal['label']}")
    return " ".join(parts)


# --- the switches ---------------------------------------------------------------------------------


def sd_flash(state_dir: Any = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    """``bringup.sd_flash``: the settings row when this build has it (lane SD-FLASH), else its
    variable; ``off`` by default."""
    environ = os.environ if env is None else env
    value, where = "off", "default"
    try:
        from harness_manager.settings import runtime

        r = runtime.resolved(SD_FLASH_SETTING, state_dir=state_dir, env=env, strict=False)
        raw = str(r.value).strip().lower() if r.value is not None else "off"
        value = "on" if raw in SD_FLASH_ON else "off"
        where = r.where or r.source or "default"
    except UsageError:                    # no row yet: the SD-FLASH lane has not landed
        raw = str(environ.get(SD_FLASH_ENV, "")).strip().lower()
        if raw:
            value, where = ("on" if raw in SD_FLASH_ON else "off"), f"${SD_FLASH_ENV}"
    on = value == "on"
    return {"setting": SD_FLASH_SETTING, "value": value, "where": where, "enabled": on,
            "reason": "" if on else SD_FLASH_OFF_REASON}


def signing(engine: Any) -> dict[str, Any]:
    """Whether a signed release can be verified here (docs/KEYS.md): the update service's
    trusted keys. None pinned means every real channel is refused, by design; ``reason`` is
    then the trust store's own refusal, word for word (what Read the list will answer)."""
    svc = getattr(engine, "update", None)
    if svc is None:
        return {"keys": 0, "refused": True, "name": "UNAVAILABLE", "hint": "",
                "reason": "this build has no harness catalogue (no update service)"}
    trust = getattr(svc, "trust", None)
    try:
        keys = len(trust.all_keys()) if trust is not None else 0
    except Exception:  # noqa: BLE001 - a trust store that cannot list keys trusts none
        keys = 0
    if keys:
        return {"keys": keys, "refused": False, "name": "", "hint": "", "reason": ""}
    reason, hint = "cannot verify channel.json: this build has no pinned update-signing keys", ""
    try:
        if trust is not None:
            trust.verify_for_channel(b"", b"", "stable")
    except RefusedError as exc:                  # the store's own words (trust.py)
        reason, hint = exc.message, exc.hint or ""
    except Exception:  # noqa: BLE001 - any other failure: keep the documented words
        pass
    return {"keys": 0, "refused": True, "name": "REFUSED", "reason": reason, "hint": hint}


def status(engine: Any, state_dir: Any = None) -> dict[str, Any]:
    """What the wizard shows before it starts: the address, the switches, the examples."""
    examples = getattr(engine, "bringup_examples", None)
    card = getattr(engine, "bringup_card_image", "")
    import sys

    return {"default_host": DEFAULT_HOST, "pc_hint": PC_ADDRESS_HINT, "platform": sys.platform,
            "usb_write_warning": USB_WRITE_WARNING, "unsigned": {
                "banner": UNSIGNED_BANNER, "words": UNSIGNED_WORDS, "zip": ZIP_RECIPE,
                "manifest": MANIFEST_RECIPE},
            "sd_flash": sd_flash(state_dir),
            "rescue_network": {"available": False, "reason": RESCUE_NETWORK_REASON,
                               "note": RESCUE_NETWORK_NOTE},
            "card_image": {"hint": CARD_IMAGE_HINT, "example": card if isinstance(card, str)
                           else ""},
            "signing": signing(engine), "witness_s": {"bare-metal": DEFAULT_WITNESS_S,
                                                      "linux": LINUX_WITNESS_S},
            "examples": list(examples() if callable(examples) else examples or [])}


def clear_work(work: Path, keep: int = 8) -> None:
    """Keep the newest ``keep`` extracted zips under ``work``."""
    try:
        dirs = sorted((p for p in work.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for p in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(p, ignore_errors=True)
