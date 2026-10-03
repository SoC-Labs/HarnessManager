"""The MPS3 configuration microSD over USB mass storage. Team T3.

``make_storage_adapter(session)`` is the hook ``pack.py`` calls. It returns an
``Mps3Storage`` (a ``StorageAdapter``) when the session has a ``USB_MSD``
link, else ``None``.

Facts and incidents this module encodes (safety rails are code, not docs):

- **Which volume.** The config SD is the volume labelled ``V2M-MPS3``
  (``constants.MSD_VOLUME_LABEL``; lsblk-verified 2026-07-17,
  scripts/mps3_sd_update.sh). The Debug USB also exposes the on-board
  CMSIS-DAP (DAPLink) drive, labelled ``MBED MPS3`` (64 MB), which once
  enumerated first and took ``/dev/sda`` (2026-08-25, same script). It is
  refused: by label (``MBED*``, ``DAPLINK``, ``MAINTENANCE``) and by content
  (DAPLink's ``DETAILS.TXT`` / ``MBED.HTM``). A volume without ``config.txt``
  or ``MB/`` is refused too.
- **Backup first, always.** ``sd_install`` overwrites in place, and on
  2026-07-16 the previous ``nanosoc.bit`` was overwritten with no backup and is
  gone (pyverify/sd.py). ``install`` refuses unless it is given the
  ``BackupRecord`` of a backup whose archive still matches its sha256, whose
  manifest matches its contents, and which describes the SD as it is NOW.
- **One write at a time; a slow write is not a failed write.** Over the MCC's
  USB-MSC a 12 MB write takes minutes, and a client that times out and retries
  mid-write corrupts the SD (fpgahub sd_install; pyverify/sd.py; the memory
  note "sd_install timeout trap"). This module writes synchronously, has no
  client timeout of its own and NEVER retries. A journal file on the SD root
  (``JOURNAL_NAME``) is the in-flight marker: it is written before the first
  byte and removed only after every file is written and read back. While it
  exists, another install is refused; if its owner is gone, the install was
  interrupted, and ``restore`` is the way back.
- **Never touch ``*.ebf``.** A copied ``mbb_v141.ebf`` (the MB BIOS our
  board.txt names; the MCC boot log reports it missing) would silently reflash
  the MCC. ``install`` refuses an ``.ebf`` destination or source; ``restore``
  never writes or deletes one.
- **Never change MBBIOS** (FIX-PACK-7, G8). ``install`` writes the card's own
  ``MBBIOS:`` line into the bundle's board.txt (``mbbios.keep_mbbios``, read from
  the mandatory backup), and refuses a bundle line that would make the MCC update
  itself (``allow_mcc_update`` overrides). The note is in ``install_notes``.
  FIX-PACK-9: in EVERY revision folder the bundle carries (``MB/HBI0309B`` and
  ``MB/HBI0309C`` for platform v2.0.0), each against the card's board.txt of the
  same revision; other revision folders on the card (an Arm ``HBI0309A`` tree) are
  never written, and stay in the backup. A card with neither a B nor a C folder is
  written with a warning in ``install_notes``.
- **Never delete stock files.** ``install`` only writes. ``restore`` returns
  the SD to the backup: it rewrites files that differ and removes only files
  that were not on the SD when the backup was taken.
- **MCC command files.** With ``USB_REMOTE: TRUE``, a ``reboot.txt`` /
  ``reset.txt`` / ``shutdown.txt`` at the root makes the MCC act at once
  (TRM 100765 §3.2; fpgahub reset_plugins/mps3_msd.py). ``install`` refuses
  them; rebooting is the controller's job.
- **Changes take effect only after a REBOOT or a power-cycle.** A finished
  install means "written and read back", not "running". The read-back may be
  served from the host's cache; the only proof the board runs the new files
  is the board reporting its own identity after a reboot (T7's flow).

Every OS call is injectable (``SdEnv``), so tests need no disks. FAT is
case-insensitive, so destination paths are matched case-insensitively
against what is already on the volume, and both ``/`` and ``\\`` separators are
accepted.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.model import LinkKind
from harness_manager.core.pack import BackupRecord, Progress, report_progress

from .constants import MSD_VOLUME_LABEL

JOURNAL_NAME = ".harness-manager-journal.json"
TMP_SUFFIX = ".harness-manager-tmp"
BACKUP_FORMAT = "harness-manager-sd-backup/1"
MANIFEST_NAME = "MANIFEST.json"
VOLUME_PREFIX = "volume/"
MCC_COMMAND_FILES = frozenset({"reboot.txt", "reset.txt", "shutdown.txt"})   # TRM §3.2
# "MBED MPS3" observed 2026-08-25 (scripts/mps3_sd_update.sh); the others are DAPLink defaults.
DAPLINK_LABEL_PREFIXES = ("MBED", "DAPLINK", "MAINTENANCE")
DAPLINK_MARKERS = frozenset({"details.txt", "mbed.htm"})   # DAPLink virtual-filesystem files
# Host OS metadata, not board configuration: never backed up, compared or removed.
IGNORED_DIRS = frozenset({"system volume information", ".trashes", ".spotlight-v100", ".fseventsd"})
# What a damaged archive can raise on read: a bad zip, a corrupt deflate stream
# (zlib.error), a truncated member (EOFError), a missing member (KeyError), bad JSON.
_ARCHIVE_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, KeyError, ValueError, OSError)
NO_SIDECAR_FLAG = " (no .sha256 sidecar)"   # load_backup's weaker-provenance mark
STALE_AFTER_S = 1800.0      # a foreign-host journal younger than this is assumed live (pyverify/sd.py)
CHUNK = 1 << 20


# --- volume discovery -----------------------------------------------------------------


@dataclass(frozen=True)
class VolumeInfo:
    label: str
    root: str | None          # mount point or drive root ("E:\\"); None when not mounted
    device: str = ""          # "/dev/sdb1", "E:"
    usb_path: str = ""        # USB port path from sysfs ("1-2.3.4.1"), when known


def _unescape_mount(text: str) -> str:
    """/proc/mounts escapes space, tab, newline and backslash as octal (\\040)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _unescape_label(text: str) -> str:
    """udev escapes /dev/disk/by-label names as \\xNN ("MBED\\x20MPS3")."""
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)


def _usb_port_path(device: str, sys_block: Path) -> str:
    """The USB port path ("1-2.3.4.1") above a block device, from sysfs."""
    try:
        real = os.path.realpath(sys_block / os.path.basename(device))
    except OSError:
        return ""
    ports = [p for p in Path(real).parts if re.fullmatch(r"\d+-[\d.]+", p)]
    return ports[-1] if ports else ""


def linux_volumes(
    *,
    by_label: Path = Path("/dev/disk/by-label"),
    mounts: Path = Path("/proc/mounts"),
    sys_block: Path = Path("/sys/class/block"),
) -> list[VolumeInfo]:
    """Labelled block devices (udev by-label links) with their mount points, if mounted."""
    labels: dict[str, str] = {}
    try:
        entries = sorted(by_label.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        labels[os.path.realpath(entry)] = _unescape_label(entry.name)
    mount_of: dict[str, str] = {}
    try:
        text = mounts.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            dev = os.path.realpath(_unescape_mount(parts[0])) if parts[0].startswith("/") else parts[0]
            mount_of.setdefault(dev, _unescape_mount(parts[1]))
    return [VolumeInfo(label=label, root=mount_of.get(dev), device=dev,
                       usb_path=_usb_port_path(dev, sys_block))
            for dev, label in labels.items()]


class _Win32Volumes:
    """Drive letters and volume labels through kernel32 (ctypes; Windows only)."""

    def __init__(self) -> None:
        import ctypes

        self._ct = ctypes
        self._k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    def drive_roots(self) -> list[str]:
        mask = self._k32.GetLogicalDrives()
        return [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]

    def label(self, root: str) -> str | None:
        ct = self._ct
        name = ct.create_unicode_buffer(261)
        fs = ct.create_unicode_buffer(261)
        old = self._k32.SetErrorMode(1)       # SEM_FAILCRITICALERRORS: no "insert a disk" dialog
        try:
            ok = self._k32.GetVolumeInformationW(ct.c_wchar_p(root), name, 261, None, None, None,
                                                 fs, 261)
        finally:
            self._k32.SetErrorMode(old)
        return name.value if ok else None


def windows_volumes(api: Any = None) -> list[VolumeInfo]:
    """Every drive letter with a readable volume label. ``api`` is injectable for tests."""
    api = api if api is not None else _Win32Volumes()
    out = []
    for root in api.drive_roots():
        label = api.label(root)
        if label is not None:
            out.append(VolumeInfo(label=label, root=root, device=root[:2]))
    return out


def mac_volumes(volumes_dir: Path = Path("/Volumes")) -> list[VolumeInfo]:
    try:
        return [VolumeInfo(label=p.name, root=str(p)) for p in sorted(volumes_dir.iterdir()) if p.is_dir()]
    except OSError:
        return []


def list_volumes(platform: str | None = None) -> list[VolumeInfo]:
    platform = platform or sys.platform
    if platform.startswith("win"):
        return windows_volumes()
    if platform == "darwin":
        return mac_volumes()
    return linux_volumes()


def is_daplink_label(label: str) -> bool:
    return label.strip().upper().startswith(DAPLINK_LABEL_PREFIXES)


# --- the injectable environment -------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Is process ``pid`` alive on this host? Never signals it (os.kill on Windows kills)."""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return k32.GetLastError() == 5               # ERROR_ACCESS_DENIED: it exists
        code = ctypes.c_ulong()
        try:
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        finally:
            k32.CloseHandle(handle)
        return bool(ok) and code.value == 259            # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


@dataclass
class SdEnv:
    list_volumes: Callable[[], list[VolumeInfo]] = list_volumes
    pid_alive: Callable[[int], bool] = pid_alive
    hostname: Callable[[], str] = socket.gethostname
    now: Callable[[], float] = time.time
    disk_free: Callable[[Path], int] = _disk_free


DEFAULT_ENV: SdEnv | None = None   # tests may swap this; read when the adapter is made


# --- file helpers ---------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    if sys.platform.startswith("win"):
        return                                       # not supported; os.replace is durable enough
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _walk(root: Path) -> tuple[list[str], list[str]]:
    """Every file and directory under ``root`` as POSIX-style relative paths, sorted."""
    files: list[str] = []
    dirs: list[str] = []

    def fail(exc: OSError) -> None:
        raise ActionFailedError(f"cannot read {exc.filename} on the SD: {exc.strerror}",
                                hint="an incomplete copy is not a backup; check the volume") from exc

    for dirpath, dirnames, filenames in os.walk(root, onerror=fail):
        base = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in IGNORED_DIRS)
        for d in dirnames:
            dirs.append((base / d).relative_to(root).as_posix())
        for name in sorted(filenames):
            rel = (base / name).relative_to(root).as_posix()
            if rel == JOURNAL_NAME:
                continue
            files.append(rel)
    return sorted(files), sorted(dirs)


def _dest(root: Path, rel: str) -> Path:
    return root.joinpath(*rel.split("/"))


def _resolve_ci(root: Path, parts: list[str]) -> Path:
    """FAT semantics: reuse the on-volume spelling of each existing path component."""
    cur = root
    for part in parts:
        match = None
        if cur.is_dir():
            with contextlib.suppress(OSError):
                match = next((e.name for e in cur.iterdir() if e.name.lower() == part.lower()), None)
        cur = cur / (match or part)
    return cur


def _normalise_rel(key: str) -> list[str]:
    """'MB\\HBI0309C\\x.bit' or 'MB/HBI0309C/x.bit' -> ['MB', 'HBI0309C', 'x.bit']. Refuses escapes."""
    text = str(key).strip()
    if not text:
        raise UsageError("empty destination path")
    if re.match(r"^[A-Za-z]:", text) or text.startswith(("/", "\\")):
        raise UsageError(f"destination {key!r} must be relative to the SD root")
    parts = text.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UsageError(f"destination {key!r} has an empty, '.' or '..' component")
    return parts


def _copy_stream(read: Callable[[int], bytes], dest: Path, tick: Callable[[int], None]) -> None:
    """temp file + fsync + rename. The destination is either old or new, never half."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + TMP_SUFFIX)
    try:
        with open(tmp, "wb") as out:
            for chunk in iter(lambda: read(CHUNK), b""):
                out.write(chunk)
                tick(len(chunk))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dest)
        _fsync_dir(dest.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# --- reports --------------------------------------------------------------------------


@dataclass
class InstallReport:
    written: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    verified: bool = False
    note: str = ("written and read back; the board runs it only after a REBOOT or power-cycle, "
                 "and only the board reporting its identity proves it")


@dataclass
class RestoreReport:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    ebf_left_alone: list[str] = field(default_factory=list)


_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


# --- the card in a backup (FIX-PACK-7, MBBIOS) ------------------------------------------


def boards_of_backup(storage: Any, backup: BackupRecord) -> tuple[dict[str, bytes], list[str]]:
    """FIX-PACK-9: every ``MB/HBI*/board.txt`` on the card (SD path -> bytes; one per
    revision folder) and every file on it, from a verified backup
    (``storage.verify_backup``): the card as it was read before writing."""
    from .mbbios import board_rev

    manifest = storage.verify_backup(backup)
    files = [str(e["path"]) for e in manifest.get("files", [])]
    hits = [f for f in files if board_rev(f)]
    try:
        with zipfile.ZipFile(backup.path) as zf:
            return {f: zf.read(VOLUME_PREFIX + f) for f in hits}, files
    except _ARCHIVE_ERRORS as exc:
        raise RefusedError(f"backup {backup.path} is unreadable: {exc}",
                           hint="take a fresh backup") from exc


def card_of_backup(storage: Any, backup: BackupRecord) -> tuple[bytes | None, list[str]]:
    """The card's ``MB/HBI0309C/board.txt`` (None: none) and every file on it, from a
    verified backup (the pre-FIX-PACK-9 view; ``boards_of_backup`` has every revision)."""
    from .mbbios import BOARD_TXT, board_rev

    boards, files = boards_of_backup(storage, backup)
    want = board_rev(BOARD_TXT)
    return next((v for k, v in boards.items() if board_rev(k) == want), None), files


# --- the board's revision (FIX-PACK-9) --------------------------------------------------

#: What the MCC says it found: its console's "Configuring motherboard (rev C, var A)..."
#: (the boot witness) and the card's LOG.TXT "MotherBoard Revision C Variant A" (board 1).
_REV_LINE = re.compile(r"mother\s*board\s*(?:\(\s*)?rev(?:ision)?\.?\s*:?\s*([A-Z])\b", re.I)
#: Where the MCC leaves its log on the card (case-blind); the newest mention wins.
MCC_LOGS = ("LOG.TXT",)
_LOG_TAIL = 256 * 1024


def revision_of_log(text: str) -> str:
    """``HBI0309C`` from the LAST motherboard-revision line of an MCC log; "" none."""
    hits = _REV_LINE.findall(text or "")
    return f"HBI0309{hits[-1].upper()}" if hits else ""


#: The MCC's firmware banner line in LOG.TXT: "ARM V2M-MPS3 Firmware v1.3.2". The file is CRLF
#: and rewritten at each boot; the LAST such line wins.
_FW_LINE = re.compile(r"^[ \t]*ARM[ \t]+V2M-MPS3[ \t]+Firmware[ \t]+(v?\d[\w.\-]*)[ \t]*\r?$",
                      re.I | re.M)


def firmware_of_log(text: str) -> str:
    """``v1.3.2`` from the LAST "ARM V2M-MPS3 Firmware vX.Y.Z" line of an MCC log; "" none
    (a number with no ``v`` gets one)."""
    hits = _FW_LINE.findall(text or "")
    if not hits:
        return ""
    fw = hits[-1]
    return fw if fw[:1] in "vV" else f"v{fw}"


def mcc_firmware_of(root: Path, *, boot_fw: str = "") -> tuple[str, str]:
    """``(firmware, how it is known)`` of the MCC for the config SD at ``root``: the MCC boot
    witness's ``firmware`` first, else the card's ``LOG.TXT``; ("", "") unknown. Reads only."""
    if boot_fw:
        return (boot_fw if boot_fw[:1] in "vV" else f"v{boot_fw}"), "the MCC boot log"
    for name in MCC_LOGS:
        log = _resolve_ci(root, name.split("/"))
        try:
            if log.is_file():
                with open(log, "rb") as fh:
                    size = fh.seek(0, os.SEEK_END)
                    fh.seek(max(0, size - _LOG_TAIL))
                    fw = firmware_of_log(fh.read().decode("latin-1"))
                if fw:
                    return fw, f"{log.name} on its config SD"
        except OSError:
            continue
    return "", ""


def board_revision_of(root: Path, *, boot_board: str = "") -> tuple[str, str]:
    """``(revision, how it is known)`` for the board whose config SD is at ``root``; ("", "")
    unknown. In order: the MCC boot witness's ``board`` ("rev C, var A"), the card's
    ``LOG.TXT``, a card with exactly one ``MB/HBI0309*`` folder (it serves that revision only).
    A card with several folders (Arm's stock A/B/C, or platform v2.0.0's B and C) says
    nothing about the board."""
    if boot_board and (rev := revision_of_log(f"motherboard ({boot_board})")):
        return rev, f"the MCC boot log: rev {rev[-1]}"
    for name in MCC_LOGS:
        log = _resolve_ci(root, name.split("/"))
        try:
            if log.is_file():
                with open(log, "rb") as fh:
                    size = fh.seek(0, os.SEEK_END)
                    fh.seek(max(0, size - _LOG_TAIL))
                    rev = revision_of_log(fh.read().decode("latin-1"))
                if rev:
                    return rev, f"{log.name} on its config SD"
        except OSError:
            continue
    mb = _resolve_ci(root, ["MB"])
    try:
        dirs = sorted(e.name for e in mb.iterdir()
                      if e.is_dir() and e.name.upper().startswith("HBI0309"))
    except OSError:
        dirs = []
    if len(dirs) == 1:
        return dirs[0].upper(), "the config SD's only revision folder"
    return "", ""


# --- the adapter ----------------------------------------------------------------------


class Mps3Storage:
    """``StorageAdapter`` for the MPS3 configuration SD."""

    def __init__(self, address: str = "", *, env: SdEnv | None = None,
                 label: str = MSD_VOLUME_LABEL) -> None:
        self.address = address
        self.env = env or SdEnv()
        self.label = label
        self.last_install: InstallReport | None = None
        self.last_restore: RestoreReport | None = None
        #: FIX-PACK-7: what the last install said beside its files ("MBBIOS kept: …")
        self.install_notes: list[str] = []

    # -- locate --

    def board_revision(self, *, boot_board: str = "") -> tuple[str, str]:
        """FIX-PACK-9: ``(revision, how it is known)`` (``board_revision_of``) for the
        planner's ``BoardView.board_rev``; ("", "") unknown. Reads only."""
        return board_revision_of(Path(self.locate()), boot_board=boot_board)

    def mcc_firmware(self, *, boot_fw: str = "") -> tuple[str, str]:
        """``(MCC firmware, how it is known)`` (``mcc_firmware_of``); ("", "") unknown."""
        return mcc_firmware_of(Path(self.locate()), boot_fw=boot_fw)

    def locate(self) -> str:
        """The config SD's mount point / drive root. Refuses the DAPLink drive."""
        root = self._find_root()
        self._check_is_config_sd(root, self._label_of(root))
        return str(root)

    def _find_root(self) -> Path:
        addr = self.address.strip()
        if addr.startswith("file://"):
            addr = unquote(urlparse(addr).path)
            if re.match(r"^/[A-Za-z]:", addr):          # file:///E:/ -> E:/
                addr = addr[1:]
        if addr and not addr.startswith("label:"):
            path = Path(addr)
            if path.is_dir():
                return path
            vols = [v for v in self.env.list_volumes()
                    if v.device and os.path.realpath(v.device) == os.path.realpath(addr)]
            if vols and vols[0].root:
                return Path(vols[0].root)
            if vols:
                raise UnavailableError(
                    "storage", f"the volume on {addr} ({vols[0].label}) is not mounted; "
                               f"mount it first (e.g. `udisksctl mount -b {addr}`)")
            raise AbsentError(f"{addr} is not a mounted volume",
                              hint="connect the MPS3 Debug USB, or pass the SD's mount point")
        label = addr[len("label:"):] if addr.startswith("label:") else self.label
        vols = [v for v in self.env.list_volumes() if v.label.upper() == label.upper()]
        mounted = [v for v in vols if v.root]
        if not mounted:
            if vols:
                raise UnavailableError(
                    "storage", f"the {label} volume ({vols[0].device or 'unknown device'}) is not "
                               f"mounted; mount it first (e.g. `udisksctl mount -b {vols[0].device}`)")
            raise AbsentError(f"no volume labelled {label} is attached",
                              hint="connect the MPS3 Debug USB: the config SD appears as USB mass storage")
        if len(mounted) > 1:
            where = ", ".join(str(v.root) for v in mounted)
            raise UsageError(f"{len(mounted)} volumes are labelled {label} ({where})",
                             hint="name the one you mean explicitly")
        return Path(str(mounted[0].root))

    def _label_of(self, root: Path) -> str | None:
        """The volume label when ``root`` is a volume root; None for a plain directory."""
        try:
            vols = self.env.list_volumes()
        except OSError:
            return None
        want = os.path.normcase(os.path.abspath(root))
        for v in vols:
            if v.root and os.path.normcase(os.path.abspath(v.root)) == want:
                return v.label
        return None

    def _check_is_config_sd(self, root: Path, label: str | None) -> None:
        if label is not None:
            if is_daplink_label(label):
                raise RefusedError(
                    f"{root} is the on-board CMSIS-DAP (DAPLink) drive (label {label!r}), "
                    "not the configuration SD",
                    hint=f"the configuration SD is the volume labelled {self.label}")
            if label.upper() != self.label.upper():
                raise RefusedError(f"{root} is labelled {label!r}, not {self.label}: "
                                   "it is not the MPS3 configuration SD")
        try:
            names = {e.name.lower() for e in root.iterdir()}
        except OSError as exc:
            raise ActionFailedError(f"cannot list {root}: {exc}") from exc
        if names & DAPLINK_MARKERS:
            raise RefusedError(
                f"{root} holds DAPLink files ({', '.join(sorted(names & DAPLINK_MARKERS))}): it is "
                "the on-board CMSIS-DAP drive, not the configuration SD",
                hint=f"the configuration SD is the volume labelled {self.label}")
        if "config.txt" not in names and "mb" not in names:
            raise RefusedError(f"{root} has neither config.txt nor MB/: it does not look like a "
                               f"{self.label} configuration SD")

    # -- the journal (in-flight marker) --

    def pending(self) -> dict[str, Any] | None:
        """The journal of an install/restore in flight or interrupted on this SD, else None."""
        return self._read_journal(Path(self.locate()))

    @staticmethod
    def _read_journal(root: Path) -> dict[str, Any] | None:
        path = root / JOURNAL_NAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"op": "unknown", "state": "unreadable"}
        return data if isinstance(data, dict) else {"op": "unknown", "state": "unreadable"}

    @staticmethod
    def _write_journal(root: Path, journal: dict[str, Any]) -> None:
        data = json.dumps(journal, indent=1, sort_keys=True).encode()
        pos = 0

        def read(n: int) -> bytes:
            nonlocal pos
            chunk = data[pos:pos + n]
            pos += len(chunk)
            return chunk

        _copy_stream(read, root / JOURNAL_NAME, lambda n: None)

    @staticmethod
    def _clear_journal(root: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            (root / JOURNAL_NAME).unlink()
        _fsync_dir(root)

    def _owner_alive(self, root: Path, journal: dict[str, Any]) -> bool:
        if journal.get("host") != self.env.hostname():
            return self.env.now() - float(journal.get("started_at", 0)) < STALE_AFTER_S
        pid = int(journal.get("pid", -1))
        if pid == os.getpid():
            return _key(root) in _ACTIVE
        return self.env.pid_alive(pid)

    def _refuse_if_pending(self, root: Path, op: str) -> None:
        journal = self._read_journal(root)
        if journal is None:
            return
        if self._owner_alive(root, journal):
            raise HeldError(
                f"an SD {journal.get('op', 'write')} is in flight on {root} "
                f"(pid {journal.get('pid')} on {journal.get('host')}); a second write now corrupts the SD",
                hint="wait for it to finish: a slow write is still a write, never retry it")
        backup = (journal.get("backup") or {}).get("path", "?")
        raise RefusedError(
            f"cannot {op}: the SD holds an interrupted {journal.get('op', 'write')} "
            f"({JOURNAL_NAME}, state {journal.get('state')}, "
            f"{len(journal.get('done', []))}/{len(journal.get('planned', []))} files written)",
            hint=f"restore the backup it names ({backup}) first")

    @contextlib.contextmanager
    def _active(self, root: Path) -> Iterator[None]:
        key = _key(root)
        with _ACTIVE_LOCK:
            if key in _ACTIVE:
                raise HeldError(f"this process is already writing {root}")
            _ACTIVE.add(key)
        try:
            yield
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(key)

    def _new_journal(self, op: str, backup: BackupRecord, planned: list[str]) -> dict[str, Any]:
        return {
            "op": op, "state": "in-flight", "pid": os.getpid(), "host": self.env.hostname(),
            "started_at": self.env.now(), "planned": planned, "done": [], "created": [],
            "current": None, "backup": {"path": backup.path, "sha256": backup.sha256},
        }

    # -- backup --

    def backup(self, dest_dir: Path, progress: Progress | None = None) -> BackupRecord:
        """Zip the whole volume with a sha256 manifest; return the record ``install`` needs."""
        emit: Progress = progress or (lambda phase, done, total: None)
        root = Path(self.locate())
        self._refuse_if_pending(root, "back up")
        label = self._label_of(root) or self.label
        files, dirs = _walk(root)
        total = sum((root / Path(rel)).stat().st_size for rel in files)
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        created = self.env.now()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(created))
        safe_label = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
        final = dest_dir / f"{safe_label}-{stamp}.zip"
        n = 1
        while final.exists():
            final = dest_dir / f"{safe_label}-{stamp}-{n}.zip"
            n += 1
        part = final.with_name(final.name + ".part")
        entries: list[dict[str, Any]] = []
        done = 0
        emit("backup", 0, total)
        try:
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for rel in dirs:
                    zf.writestr(zipfile.ZipInfo(VOLUME_PREFIX + rel + "/"), b"")
                for rel in files:
                    src = _dest(root, rel)
                    st = src.stat()
                    info = zipfile.ZipInfo(VOLUME_PREFIX + rel,
                                           date_time=time.gmtime(max(st.st_mtime, 315532800))[:6])
                    info.compress_type = zipfile.ZIP_DEFLATED
                    h = hashlib.sha256()
                    size = 0
                    with open(src, "rb") as fin, zf.open(info, "w") as fout:
                        for chunk in iter(lambda f=fin: f.read(CHUNK), b""):
                            h.update(chunk)
                            fout.write(chunk)
                            size += len(chunk)
                            done += len(chunk)
                            emit("backup", done, total)
                    entries.append({"path": rel, "size": size, "sha256": h.hexdigest(),
                                    "mtime_ns": st.st_mtime_ns})
                manifest = {"format": BACKUP_FORMAT, "label": label, "source": str(root),
                            "created_at": created, "files": entries, "dirs": dirs}
                zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=1, sort_keys=True))
            os.replace(part, final)
        except BaseException:
            with contextlib.suppress(OSError):
                part.unlink()
            raise
        sha = file_sha256(final)
        final.with_name(final.name + ".sha256").write_text(f"{sha}  {final.name}\n", encoding="utf-8")
        record = BackupRecord(path=str(final), sha256=sha, created_at=created, files=len(entries),
                              volume_label=label)
        self.verify_backup(record)       # a backup that cannot be read back is not a backup
        return record

    def verify_backup(self, record: BackupRecord | None) -> dict[str, Any]:
        """Check a backup archive against its record and its own manifest. Returns the manifest."""
        if not isinstance(record, BackupRecord):
            raise RefusedError("writing the configuration SD needs a verified backup of it first",
                               hint="run backup() (`harness-manager sd TARGET backup DIR`) "
                                    "and pass its record")
        path = Path(record.path)
        if not path.is_file():
            raise RefusedError(f"backup {path} does not exist", hint="take a fresh backup")
        actual = file_sha256(path)
        if actual != record.sha256:
            raise RefusedError(f"backup {path} fails its sha256 check (recorded {record.sha256[:12]}…, "
                               f"now {actual[:12]}…): it is damaged or not the backup that was taken",
                               hint="take a fresh backup")
        try:
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read(MANIFEST_NAME))
                if manifest.get("format") != BACKUP_FORMAT:
                    raise RefusedError(f"backup {path} is not a {BACKUP_FORMAT} archive")
                entries = manifest.get("files", [])
                if len(entries) != record.files:
                    raise RefusedError(f"backup {path} lists {len(entries)} files, its record says "
                                       f"{record.files}")
                for entry in entries:
                    h = hashlib.sha256()
                    with zf.open(VOLUME_PREFIX + entry["path"]) as f:
                        for chunk in iter(lambda f=f: f.read(CHUNK), b""):
                            h.update(chunk)
                    if h.hexdigest() != entry["sha256"]:
                        raise RefusedError(f"backup {path}: {entry['path']} does not match its manifest")
        except _ARCHIVE_ERRORS as exc:
            raise RefusedError(f"backup {path} is unreadable or incomplete: {exc}",
                               hint="take a fresh backup") from exc
        return manifest

    def load_backup(self, path: Path) -> BackupRecord:
        """Rebuild a ``BackupRecord`` from an archive on disk, then verify it fully.

        The ``<zip>.sha256`` sidecar written by ``backup()`` is the tamper
        witness. If it is present, the archive must match it; if it is missing,
        the archive is verified against its own manifest only, which is weaker,
        and that is noted in the record's label.
        (Contract addition CCR-2 from T5; added by the lead after the T3 merge.)
        """
        path = Path(path)
        if not path.is_file():
            raise AbsentError(f"backup {path} does not exist", hint="check the path")
        actual = file_sha256(path)
        sidecar = path.with_name(path.name + ".sha256")
        recorded = actual
        if sidecar.is_file():
            words = sidecar.read_text(encoding="utf-8", errors="replace").split()
            recorded = words[0].strip().lower() if words else ""      # T3: an empty sidecar
            if recorded != actual:
                raise RefusedError(
                    f"backup {path} does not match its .sha256 sidecar: it was changed after the backup",
                    hint="take a fresh backup")
        try:
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read(MANIFEST_NAME))
        except _ARCHIVE_ERRORS as exc:                 # T3: + zlib.error / EOFError
            raise RefusedError(f"backup {path} is unreadable or incomplete: {exc}",
                               hint="take a fresh backup") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            raise RefusedError(f"backup {path} has no valid {MANIFEST_NAME}")
        label = str(manifest.get("label", ""))
        try:
            created = float(manifest.get("created_at", 0.0))
        except (TypeError, ValueError):
            created = 0.0
        record = BackupRecord(
            path=str(path), sha256=recorded, created_at=created,
            files=len(manifest["files"]),
            volume_label=label if sidecar.is_file() else f"{label}{NO_SIDECAR_FLAG}",
        )
        self.verify_backup(record)
        return record

    # -- install --

    def install(self, files: Mapping[str, Path], *, backup: BackupRecord | None,
                progress: Progress | None = None, allow_mcc_update: bool = False) -> None:
        """Write ``files`` (SD-relative path -> local file), journaled, then read back.

        Refused without a verified backup of the SD as it is now. Never writes
        ``.ebf`` or MCC command files, never deletes, never retries. The bundle's
        board.txt carries the card's MBBIOS line (``mbbios.keep_mbbios``, from the
        backup); a line that would make the MCC update itself is refused (15) unless
        ``allow_mcc_update``.
        """
        from .mbbios import keep_mbbios, notes_of

        self.install_notes = []
        if backup is None:
            raise RefusedError("writing the configuration SD needs a verified backup of it first",
                               hint="run backup() (`harness-manager sd TARGET backup DIR`) "
                                    "and pass its record")
        boards, card_files = boards_of_backup(self, backup)
        with tempfile.TemporaryDirectory(prefix="hm-mbbios-") as tmp:
            files, kept = keep_mbbios(files, card_boards=boards, card_files=card_files,
                                      workdir=Path(tmp), allow_mcc_update=allow_mcc_update)
            for note in notes_of(kept):
                self.install_notes.append(note)
                report_progress(progress, "mbbios", 1, 1, {"text": note})
            self._install(files, backup=backup, progress=progress)

    def _install(self, files: Mapping[str, Path], *, backup: BackupRecord,
                 progress: Progress | None = None) -> None:
        emit: Progress = progress or (lambda phase, done, total: None)
        root = Path(self.locate())
        self._refuse_if_pending(root, "install")
        plan = self._plan(root, files)
        manifest = self.verify_backup(backup)
        self._check_label(root, manifest, backup)
        self._check_backup_is_current(root, manifest, plan, backup)
        total = sum(size for _, _, size in plan)
        overwritten = sum(dest.stat().st_size for dest, _, _ in plan if dest.is_file())
        free = self.env.disk_free(root)
        if total - overwritten > free:
            raise RefusedError(f"the SD has {free} bytes free; this install needs "
                               f"{total - overwritten} more", hint="nothing was written")

        report = InstallReport()
        rels = [dest.relative_to(root).as_posix() for dest, _, _ in plan]
        journal = self._new_journal("install", backup, rels)
        with self._active(root):
            self._write_journal(root, journal)
            done = 0
            current = ""

            def tick(n: int) -> None:
                nonlocal done
                done += n
                emit("install", done, total)

            try:
                emit("install", 0, total)
                for (dest, src, _), rel in zip(plan, rels, strict=True):
                    current = rel
                    if not dest.exists():
                        journal["created"].append(rel)
                        report.created.append(rel)
                    journal["current"] = rel
                    self._write_journal(root, journal)
                    with open(src, "rb") as fin:
                        _copy_stream(fin.read, dest, tick)
                    journal["done"].append(rel)
                    journal["current"] = None
                    self._write_journal(root, journal)
                    report.written.append(rel)
            except BaseException as exc:
                journal["state"] = "interrupted"
                journal["error"] = f"{type(exc).__name__}: {exc}"
                with contextlib.suppress(OSError):
                    self._write_journal(root, journal)
                self.last_install = report
                if isinstance(exc, Exception):
                    raise ActionFailedError(
                        f"install interrupted while writing {current}: {exc}",
                        hint=f"the SD's {JOURNAL_NAME} marks it; restore the backup {backup.path}",
                    ) from exc
                raise

            # Read back what was written.
            checked = 0
            emit("verify", 0, total)
            bad = []
            for dest, src, size in plan:
                if file_sha256(dest) != file_sha256(src):
                    bad.append(dest.relative_to(root).as_posix())
                checked += size
                emit("verify", checked, total)
            if bad:
                journal["state"] = "verify-failed"
                journal["mismatched"] = bad
                self._write_journal(root, journal)
                self.last_install = report
                raise ActionFailedError(f"read-back mismatch after install: {', '.join(bad)}",
                                        hint=f"restore the backup {backup.path}")
            report.verified = True
            self._clear_journal(root)
        self.last_install = report

    def _plan(self, root: Path, files: Mapping[str, Path]) -> list[tuple[Path, Path, int]]:
        if not files:
            raise UsageError("nothing to install")
        plan: list[tuple[Path, Path, int]] = []
        seen: dict[str, str] = {}
        for key, source in files.items():
            parts = _normalise_rel(key)
            name = parts[-1].lower()
            if name.endswith(".ebf"):
                raise RefusedError(f"{key}: .ebf files are never written (a copied mbb_v141.ebf "
                                   "silently reflashes the MCC)", hint="nothing was written")
            if len(parts) == 1 and name in MCC_COMMAND_FILES:
                raise RefusedError(f"{key} is an MCC command file (TRM §3.2): writing it makes the "
                                   "MCC act at once", hint="use the controller's reboot()")
            if name == JOURNAL_NAME or name.endswith(TMP_SUFFIX) or \
                    any(p.lower() in IGNORED_DIRS for p in parts):
                raise UsageError(f"{key} is reserved and cannot be installed")
            src = Path(source)
            if src.suffix.lower() == ".ebf":
                raise RefusedError(f"source {src} is an .ebf (MCC firmware); it is never installed",
                                   hint="nothing was written")
            if not src.is_file():
                raise AbsentError(f"install source {src} does not exist")
            dest = _resolve_ci(root, parts)
            folded = dest.relative_to(root).as_posix().lower()
            if folded in seen:
                raise UsageError(f"{key} and {seen[folded]} are the same file on a FAT volume")
            seen[folded] = key
            if dest.is_dir():
                raise UsageError(f"{key} is a directory on the SD")
            plan.append((dest, src, src.stat().st_size))
        return plan

    def _check_label(self, root: Path, manifest: dict[str, Any], backup: BackupRecord) -> None:
        """The backup must be of this volume: compare the verified manifest's label."""
        current = self._label_of(root)
        taken = str(manifest.get("label", ""))
        if current and taken and current.upper() != taken.upper():
            raise RefusedError(f"backup {backup.path} is of volume {taken!r}, this SD is {current!r}")

    def _check_backup_is_current(self, root: Path, manifest: dict[str, Any],
                                 plan: list[tuple[Path, Path, int]], backup: BackupRecord) -> None:
        files, _ = _walk(root)
        backed = {e["path"]: e for e in manifest["files"]}
        current = set(files)
        missing = sorted(set(backed) - current)
        added = sorted(current - set(backed))
        changed = []
        for rel in sorted(current & set(backed)):
            st = _dest(root, rel).stat()
            if st.st_size != backed[rel]["size"] or st.st_mtime_ns != backed[rel]["mtime_ns"]:
                changed.append(rel)
        # The files about to be overwritten must be byte-identical to the backup's copy.
        for dest, _, _ in plan:
            rel = dest.relative_to(root).as_posix()
            if rel in backed and rel not in changed and file_sha256(dest) != backed[rel]["sha256"]:
                changed.append(rel)
        if missing or added or changed:
            what = "; ".join(
                f"{label}: {', '.join(items[:3])}{' …' if len(items) > 3 else ''}"
                for label, items in (("missing", missing), ("added", added), ("changed", changed))
                if items)
            raise RefusedError(f"the SD has changed since backup {backup.path} was taken ({what})",
                               hint="take a fresh backup, then install")

    # -- restore --

    def restore(self, backup: BackupRecord, progress: Progress | None = None) -> None:
        """Return the SD to ``backup``. Also the recovery for an interrupted install."""
        emit: Progress = progress or (lambda phase, done, total: None)
        manifest = self.verify_backup(backup)
        root = Path(self.locate())
        self._check_label(root, manifest, backup)
        journal = self._read_journal(root)
        if journal is not None and self._owner_alive(root, journal):
            raise HeldError(
                f"an SD {journal.get('op', 'write')} is in flight on {root} (pid {journal.get('pid')} "
                f"on {journal.get('host')}); never interrupt a write",
                hint="wait for it to finish, then restore")
        entries = manifest["files"]
        report = RestoreReport()
        total = sum(e["size"] for e in entries if not e["path"].lower().endswith(".ebf"))
        done = 0

        def tick(n: int) -> None:
            nonlocal done
            done += n
            emit("restore", done, total)

        with self._active(root):
            self._write_journal(root, self._new_journal("restore", backup,
                                                        [e["path"] for e in entries]))
            emit("restore", 0, total)
            with zipfile.ZipFile(backup.path) as zf:
                for entry in entries:
                    rel = entry["path"]
                    dest = _dest(root, rel)
                    if rel.lower().endswith(".ebf"):
                        if not dest.is_file() or file_sha256(dest) != entry["sha256"]:
                            report.ebf_left_alone.append(rel)
                        continue
                    if dest.is_file() and dest.stat().st_size == entry["size"] \
                            and file_sha256(dest) == entry["sha256"]:
                        report.unchanged.append(rel)
                        tick(entry["size"])
                        continue
                    with zf.open(VOLUME_PREFIX + rel) as fin:
                        _copy_stream(fin.read, dest, tick)
                    report.written.append(rel)
            for rel in manifest.get("dirs", []):
                _dest(root, rel).mkdir(parents=True, exist_ok=True)
            # Remove what was not on the SD when the backup was taken (never an .ebf).
            backed = {e["path"] for e in entries}
            files, dirs = _walk(root)
            for rel in files:
                if rel in backed:
                    continue
                if rel.lower().endswith(".ebf"):
                    report.ebf_left_alone.append(rel)
                    continue
                _dest(root, rel).unlink()
                report.removed.append(rel)
            backed_dirs = set(manifest.get("dirs", []))
            for rel in sorted(dirs, key=lambda d: d.count("/"), reverse=True):
                path = _dest(root, rel)
                if rel not in backed_dirs and path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
                    report.removed.append(rel + "/")
            bad = [e["path"] for e in entries
                   if not e["path"].lower().endswith(".ebf")
                   and file_sha256(_dest(root, e["path"])) != e["sha256"]]
            if bad:
                raise ActionFailedError(f"restore read-back mismatch: {', '.join(bad)}",
                                        hint="run restore again; the journal stays until it succeeds")
            self._clear_journal(root)
        self.last_restore = report


def _key(root: Path) -> str:
    return os.path.normcase(os.path.abspath(root))


# --- the pack hook --------------------------------------------------------------------


def make_storage_adapter(session: Any) -> Mps3Storage | None:
    """The ``pack.py`` hook. ``None`` when the session has no USB mass-storage link."""
    link_of = getattr(session, "link", None)
    link = link_of(LinkKind.USB_MSD) if callable(link_of) else next(
        (lk for lk in session.candidate.links if lk.kind == LinkKind.USB_MSD), None)
    if link is None:
        return None
    return Mps3Storage(link.address, env=DEFAULT_ENV)
