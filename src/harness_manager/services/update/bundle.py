"""Turn verified downloads into installable parts, and run the domain checks.

Nothing here touches a board. ``prepare_release`` downloads each needed
component (sha256-checked by the downloader), extracts the zips safely, then
checks what the files ARE, not just what they hash to:

- **per-file sha256** against the component's ``files`` list, when the channel gives one;
- **config-SD parts** (target ``mcc-sd``): never an ``.ebf`` (a copied MB BIOS
  silently reflashes the MCC), never an MCC command file at the root
  (``reboot.txt`` and friends act at once, TRM §3.2), only the SD's own tree
  (``config.txt`` and ``MB/``); every ``.bit`` must be a real Xilinx bitstream
  for the channel's ``part`` (``xcku115``) whose header USERID equals the
  release's ``usercode``; the ``MB/HBI0309<rev>/`` tree must be a revision the
  release supports;
- **overlays** (target ``host-store``): every manifest is keyed to the release's
  ``static_id`` and ``usercode`` and passes its own length + CRC-32 check (the
  pack's validator: pyverify for the MPS3). A foreign-implementation partial
  with the right static_id is the one that wiped the FPGA twice in July, so
  the usercode check is not optional when the release declares one. An Arm-IP
  overlay inside a public archive is refused;
- **OS slot image** (target ``ethernet``, kind ``os-slot``): the image hash (and the
  declared size and CRC-32), the stage0 frames (an S0LB v2 boot table stage0 would take,
  whose header CRC, entry point and regions are the ones ``linux_bundle.json`` declared:
  ``check_os_component``), and the static it was provisioned for.

The result is a list of ``PreflightItem`` and the core ``preflight_refusal``
rule turns any MISMATCH into the error: identity mismatches exit 14, the rest 15.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import shutil
import stat
import uuid
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from harness_manager.core.errors import (
    HarnessError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
)
from harness_manager.core.model import Check
from harness_manager.core.pack import PreflightItem, preflight_refusal

from . import s0lb
from .bitheader import BitHeaderError, read_bit_header
from .download import Downloader, Progress
from .schema import (
    IP_ARM,
    KIND_OS_SLOT,
    KIND_OVERLAYS,
    KIND_SD,
    TARGET_MCC_SD,
    Component,
    HarnessRelease,
    os_frames,
    os_provisioned_static,
)

MCC_COMMAND_FILES = frozenset({"reboot.txt", "reset.txt", "shutdown.txt"})   # TRM 100765 §3.2
MAX_ZIP_FILES = 4096
MAX_ZIP_BYTES = 2 << 30          # 2 GiB uncompressed: far above any real bundle (~33 MB)


class OverlayHandler(Protocol):
    """What the update service needs from a board pack's overlay support."""

    def inspect(self, directory: Path) -> dict[str, Any]:
        """{rm_name, rm_id, static_id, static_usercode, ip_class} from the manifest."""
        ...

    def validate(self, directory: Path) -> None:
        """Raise if the payloads fail the manifest's own length/CRC check."""
        ...

    def import_(self, store: Any, directory: Path) -> str:
        """Put the overlay in the content store the deploy service reads; return its key."""
        ...


class PackOverlayHandler:
    """The pack's own overlay module (``harness_manager_<pack>.overlays``), used by name.

    For the MPS3 that is T2's ``load_overlay_dir`` (manifests through pyverify),
    ``Overlay.validate`` (length + CRC-32) and ``import_overlay`` (the content-store
    convention the deploy catalogue reads). See the CCR in the hand-back for a
    pack-level hook that would replace this lookup.
    """

    def __init__(self, pack: str = "mps3") -> None:
        self.pack = pack
        try:
            self._mod = importlib.import_module(f"harness_manager_{pack}.overlays")
        except ModuleNotFoundError:
            self._mod = None

    def _need(self) -> Any:
        if self._mod is None:
            raise UnavailableError("overlay update",
                                   f"the {self.pack} pack in this build has no overlay module")
        return self._mod

    def inspect(self, directory: Path) -> dict[str, Any]:
        overlay, raw = self._need().load_overlay_dir(directory)
        m = overlay.manifest
        return {"rm_name": m.rm_name, "rm_id": f"0x{m.rm_id:08x}", "static_id": f"0x{m.static_id:08x}",
                "static_usercode": (f"0x{m.static_usercode:08x}"
                                    if m.static_usercode is not None else ""),
                "ip_class": str(raw.get("ip_class") or "")}

    def validate(self, directory: Path) -> None:
        overlay, _ = self._need().load_overlay_dir(directory)
        overlay.validate()

    def import_(self, store: Any, directory: Path) -> str:
        return self._need().import_overlay(store, directory)


# --- safe extraction --------------------------------------------------------------------


def _member_parts(name: str) -> tuple[str, ...]:
    if "\\" in name:
        name = name.replace("\\", "/")
    p = PurePosixPath(name)
    if p.is_absolute() or (p.parts and ":" in p.parts[0]):
        raise RefusedError(f"archive member {name!r} has an absolute path")
    if any(part in ("..", "") for part in p.parts):
        raise RefusedError(f"archive member {name!r} escapes the archive")
    return p.parts


def safe_extract(archive: Path, dest: Path) -> dict[str, Path]:
    """Extract ``archive`` into ``dest`` (created fresh). Returns relative path -> file.

    Refuses absolute paths, ``..``, symlinks, duplicate names (case-insensitive:
    the SD is FAT) and archives that expand beyond ``MAX_ZIP_BYTES``.
    """
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise RefusedError(f"{archive.name} is not a readable zip: {exc}") from None
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    out: dict[str, Path] = {}
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_FILES:
            raise RefusedError(f"{archive.name} holds {len(infos)} entries; refusing")
        total = sum(i.file_size for i in infos)
        if total > MAX_ZIP_BYTES:
            raise RefusedError(f"{archive.name} expands to {total} bytes; refusing")
        seen: set[str] = set()
        tmp.mkdir(parents=True)
        try:
            for info in infos:
                parts = _member_parts(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise RefusedError(f"archive member {info.filename!r} is a symlink")
                if info.is_dir():
                    continue
                rel = "/".join(parts)
                if rel.lower() in seen:
                    raise RefusedError(f"{archive.name} lists {rel!r} twice (FAT is case-insensitive)")
                seen.add(rel.lower())
                target = tmp.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                out[rel] = dest.joinpath(*parts)
        except (zipfile.BadZipFile, OSError, EOFError) as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RefusedError(f"{archive.name} could not be extracted: {exc}") from None
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- prepared parts ---------------------------------------------------------------------


@dataclass
class PreparedComponent:
    component: Component
    blob: Path                                    # the verified download
    files: dict[str, Path] = field(default_factory=dict)   # extracted: relative path -> file


@dataclass
class PreparedRelease:
    release: HarnessRelease
    parts: dict[str, PreparedComponent] = field(default_factory=dict)
    checks: list[PreflightItem] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)   # component -> why it was not fetched

    @property
    def sd_files(self) -> dict[str, Path]:
        for p in self.parts.values():
            if p.component.kind == KIND_SD:
                return dict(p.files)
        return {}

    @property
    def os_image(self) -> PreparedComponent | None:
        return next((p for p in self.parts.values() if p.component.kind == KIND_OS_SLOT), None)

    def overlay_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for p in self.parts.values():
            if p.component.kind != KIND_OVERLAYS:
                continue
            for rel, path in sorted(p.files.items()):
                if rel.rsplit("/", 1)[-1] == "manifest.json":
                    dirs.append(path.parent)
        return dirs

    def refusal(self) -> HarnessError | None:
        return bundle_refusal(self.checks, self.release.version)


def bundle_refusal(items: list[PreflightItem], version: str) -> HarnessError | None:
    """The core rule (``preflight_refusal``): identity MISMATCH -> 14, other MISMATCH -> 15,
    UNCHECKED never blocks; with messages about a bundle rather than an overlay deploy."""
    rule = preflight_refusal(items, f"harness {version}")
    if rule is None:
        return None
    bad = "; ".join(f"{i.name}: {i.detail}" for i in items if i.check == Check.MISMATCH)
    if isinstance(rule, IncompatibleError):
        return IncompatibleError(
            f"harness {version} is not consistent with the identity it was signed for ({bad})",
            hint="nothing was written to the board; report the release to its publisher")
    return RefusedError(f"harness {version} failed its checks ({bad})",
                        hint="nothing was written to the board; the downloads were verified "
                             "but their content is refused")


def _item(name: str, ok: bool | None, detail: str, *, identity: bool = False) -> PreflightItem:
    check = Check.UNCHECKED if ok is None else (Check.OK if ok else Check.MISMATCH)
    return PreflightItem(name=name, check=check, detail=detail, identity=identity)


def _same_u32(a: str, b: str) -> bool:
    try:
        return int(a, 16) == int(b, 16)
    except (TypeError, ValueError):
        return a.lower() == b.lower()


# --- the checks -------------------------------------------------------------------------


def check_file_list(comp: Component, files: dict[str, Path]) -> list[PreflightItem]:
    if not comp.files:
        return []
    listed = {k.replace("\\", "/"): v for k, v in comp.files.items()}
    missing = sorted(set(listed) - set(files))
    extra = sorted(set(files) - set(listed))
    bad = sorted(rel for rel in set(listed) & set(files) if _sha256(files[rel]) != listed[rel])
    problems = []
    if missing:
        problems.append(f"missing {', '.join(missing[:3])}")
    if extra:
        problems.append(f"not listed {', '.join(extra[:3])}")
    if bad:
        problems.append(f"sha256 differs for {', '.join(bad[:3])}")
    return [_item(f"{comp.name}: file list", not problems,
                  "; ".join(problems) or f"{len(files)} files match the signed list")]


def check_sd_component(comp: Component, files: dict[str, Path], release: HarnessRelease,
                       part: str) -> list[PreflightItem]:
    items: list[PreflightItem] = []
    ebf = [r for r in files if r.lower().endswith(".ebf")]
    items.append(_item(f"{comp.name}: no .ebf", not ebf,
                       f"refusing {', '.join(ebf)}: an .ebf reflashes the MCC" if ebf
                       else "no MCC firmware image in the bundle"))
    cmd = [r for r in files if "/" not in r and r.lower() in MCC_COMMAND_FILES]
    items.append(_item(f"{comp.name}: no MCC command files", not cmd,
                       f"refusing {', '.join(cmd)}: the MCC acts on it at once" if cmd
                       else "no reboot.txt/reset.txt/shutdown.txt"))
    outside = [r for r in files if not (r.lower() == "config.txt" or r.lower().startswith("mb/"))]
    items.append(_item(f"{comp.name}: SD tree", not outside,
                       f"outside config.txt and MB/: {', '.join(outside[:3])}" if outside
                       else "config.txt and MB/ only"))
    revs = sorted({r.split("/")[1] for r in files if r.lower().startswith("mb/") and r.count("/") >= 2})
    if release.compat.board_revs:
        wrong = [r for r in revs if r.upper() not in {b.upper() for b in release.compat.board_revs}]
        items.append(_item(f"{comp.name}: board revision", not wrong,
                           f"tree {', '.join(wrong)} is not in {list(release.compat.board_revs)}"
                           if wrong else f"{', '.join(revs) or 'no MB/ tree'}"))
    bits = [r for r in files if r.lower().endswith(".bit")]
    if not bits:
        items.append(_item(f"{comp.name}: bitstream", False, "the SD part carries no .bit"))
    for rel in bits:
        try:
            hdr = read_bit_header(files[rel])
        except (BitHeaderError, OSError) as exc:
            items.append(_item(f"{rel}: bitstream header", False, str(exc)))
            continue
        if part:
            items.append(_item(f"{rel}: part", hdr.part_matches(part),
                               f"built for {hdr.part or '?'}, the board is {part}", identity=True))
        want = release.identity.usercode
        if want:
            ok = hdr.stamped and _same_u32(hdr.userid, want)
            items.append(_item(f"{rel}: USERID", ok,
                               f"header USERID {hdr.userid or 'none'}, release usercode {want}",
                               identity=True))
        else:
            items.append(_item(f"{rel}: USERID", None,
                               f"the release declares no usercode (header says {hdr.userid or 'none'})"))
    return items


def check_overlays(comp: Component, dirs: list[Path], release: HarnessRelease,
                   handler: OverlayHandler) -> list[PreflightItem]:
    items: list[PreflightItem] = []
    if not dirs:
        return [_item(f"{comp.name}: overlays", False, "the archive holds no overlay manifest")]
    for d in dirs:
        label = f"{comp.name}/{d.name}"
        try:
            info = handler.inspect(d)
        except Exception as exc:  # noqa: BLE001 - any unreadable manifest is a refusal
            items.append(_item(f"{label}: manifest", False, f"unreadable: {exc}"))
            continue
        items.append(_item(f"{label}: static_id", _same_u32(info["static_id"],
                                                             release.identity.static_id),
                           f"keyed to {info['static_id']}, release shell is "
                           f"{release.identity.static_id}", identity=True))
        want, have = release.identity.usercode, info.get("static_usercode", "")
        if want and have:
            items.append(_item(f"{label}: static_usercode", _same_u32(have, want),
                               f"keyed to {have}, release usercode {want}", identity=True))
        elif want:
            items.append(_item(f"{label}: static_usercode", False,
                               f"the manifest has no static_usercode; the release is {want}",
                               identity=True))
        declared = str(info.get("ip_class") or "").replace("_", "-")
        if declared == IP_ARM and comp.ip_class != IP_ARM:
            items.append(_item(f"{label}: ip_class", False,
                               "an arm-aaa overlay inside a public component"))
        try:
            handler.validate(d)
            items.append(_item(f"{label}: length + CRC", True, "payloads match the manifest"))
        except Exception as exc:  # noqa: BLE001 - pyverify raises its own types
            items.append(_item(f"{label}: length + CRC", False, str(exc)))
    return items


def check_os_component(comp: Component, blob: Path, release: HarnessRelease) -> list[PreflightItem]:
    """The OS slot image against ``linux_bundle.json``'s declarations (lane LINUX-SLOTS).

    - **image hash**: the sha256 the channel signed (the downloader already refused any
      other), plus the file's size and CRC-32 when the channel copies them;
    - **stage0 frames**: the file is an S0LB v2 boot table whose table CRC holds and whose
      every region is inside the file and the DDR window with a good CRC (what stage0 and
      the board check), AND its frames (header CRC, entry point, each region's dst, len
      and CRC) are the ones the bundle declared (``s0lb``). A release that declares no
      frames is UNCHECKED on the comparison (never a pass), never on the self-check;
    - **provisioned static**: the static the image was provisioned for is the release's
      (``provisioned.static_id``); a board refuses an image for another fabric.
    """
    name = comp.name
    data = blob.read_bytes()
    items: list[PreflightItem] = []
    size_ok = "bytes" not in comp.extra or comp.extra.get("bytes") == len(data)
    crc = f"0x{zlib.crc32(data) & 0xFFFFFFFF:08x}"
    crc_ok = "crc32" not in comp.extra or _same_u32(str(comp.extra.get("crc32")), crc)
    items.append(_item(f"{name}: image hash", size_ok and crc_ok,
                       f"sha256 {comp.asset.sha256[:12]}… as signed, {len(data)} B, crc32 {crc}"
                       if size_ok and crc_ok else
                       f"{len(data)} B crc32 {crc}; the release declares "
                       f"{comp.extra.get('bytes', '?')} B crc32 {comp.extra.get('crc32', '?')}"))
    try:
        table = s0lb.parse(data)
    except s0lb.S0lbError as exc:
        items.append(_item(f"{name}: stage0 frames", False, f"not a boot image stage0 would "
                                                            f"take: {exc}"))
        return items
    if not table.ok:
        items.append(_item(f"{name}: stage0 frames", False, "; ".join(table.problems)))
        return items
    declared = os_frames(comp)
    hdr = f"0x{table.header_crc32:08x}"
    if declared is None:
        items.append(_item(f"{name}: stage0 frames", None,
                           f"a good S0LB v2 table (hdr_crc {hdr}, {table.num_entries} region(s)), "
                           "but the release declares no frames to compare (linux_bundle.json "
                           "slot_image.s0lb): not a pass"))
    else:
        diff = s0lb.compare(table, declared)
        items.append(_item(f"{name}: stage0 frames", not diff,
                           "; ".join(diff) if diff else
                           f"hdr_crc {hdr}, {table.num_entries} region(s): as the bundle "
                           "declares"))
    prov = os_provisioned_static(comp, release)
    items.append(_item(f"{name}: provisioned static", _same_u32(prov, release.identity.static_id),
                       f"provisioned for {prov}, the release is {release.identity.static_id}",
                       identity=True))
    return items


# --- preparation ------------------------------------------------------------------------


def prepare_release(release: HarnessRelease, names: list[str], *, downloader: Downloader,
                    base_url: str, work: Path, part: str = "",
                    overlay_handler: OverlayHandler | None = None,
                    progress: Progress | None = None,
                    skip: Callable[[Component], str] | None = None) -> PreparedRelease:
    """Download, verify and check the named components. Raises on any refusal."""
    prepared = PreparedRelease(release)
    handler = overlay_handler
    work.mkdir(parents=True, exist_ok=True)
    for name in names:
        comp = release.component(name)
        if comp is None:
            raise RefusedError(f"harness {release.version} has no component {name!r}")
        why = skip(comp) if skip else ""
        if why:
            prepared.skipped[name] = why
            continue
        try:
            blob = downloader.fetch(comp.asset, base_url=base_url, progress=progress)
        except UnavailableError as exc:
            if comp.optional or comp.needs_token:
                prepared.skipped[name] = exc.reason
                continue
            raise
        pc = PreparedComponent(comp, blob)
        if comp.fmt == "zip":
            pc.files = safe_extract(blob, work / comp.name)
            prepared.checks += check_file_list(comp, pc.files)
        prepared.parts[name] = pc
        if comp.target == TARGET_MCC_SD:
            prepared.checks += check_sd_component(comp, pc.files, release, part)
        elif comp.kind == KIND_OVERLAYS:
            if handler is None:
                raise UnavailableError("overlay update", "no overlay handler for this board pack")
            dirs = sorted({p.parent for rel, p in pc.files.items()
                           if rel.rsplit("/", 1)[-1] == "manifest.json"})
            prepared.checks += check_overlays(comp, dirs, release, handler)
        elif comp.kind == KIND_OS_SLOT:
            prepared.checks += check_os_component(comp, blob, release)
    refusal = prepared.refusal()
    if refusal is not None:
        raise refusal
    return prepared


def discard(work: Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(work)
