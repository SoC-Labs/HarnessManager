"""``KitService``: DUT build kits in HM's content-addressed cache (KIT-STORE K2, david K1).

A kit enters the cache through ``import_`` (a kit directory, a kit zip, or a directory
of loose mint files the pack can describe: ``fielded/<sid>/``, a mint ``prod/``) or
``fetch`` (the sources, below). Either way the same checks run first: kit.json parses,
every file's size and sha256 match it, and **the CRC-32 of the locked static DCP is the
kit's static_id** (``build_dfx.tcl:725``). A kit that fails is refused whole (exit 15);
nothing half-imported is ever listed.

Storage, in the engine's ``ContentStore`` (``<state_dir>/store``), the way
``import_overlay`` stores overlays:

- each kit file is a blob of kind ``rm_kit_file``, meta ``{static_id, path, role}``;
- kit.json is a blob of kind ``rm_kit``, meta ``{static_id, board_type, vivado,
  usercode, impl, source, imported_at}``, written LAST, so a listed kit has every file.

The zip is only the transport format (a channel asset, a download from the daemon):
Vivado needs plain files, and ``export`` copies them out of the store into a directory.

Sources, tried in this order by ``fetch`` (``source=`` pins one):

1. ``cache``: the store;
2. ``channel``: the signed channel's ``rm-kit`` asset for this static. **Seam for lane
   OTA-C**: ``ChannelSource(resolve=...)`` takes a function ``static_id -> Asset | None``
   that finds the component; until OTA-C wires one, it reports itself unavailable. The
   download goes through ``update.download.Downloader`` (resume, sha256, size, token only
   to GitHub hosts), then through ``import_``;
3. ``hub``: the hub's mint archive, read as a PATH (``$HARNESS_MANAGER_KIT_HUB_DIR``,
   e.g. ``/home/david/mints`` on the hub itself or over a mount): ``<root>/<sid>/kit/``
   (a packed kit, F1) else ``<root>/<sid>/`` (the loose files). No ssh here: fetching
   over ssh is KIT-STORE K8;
4. a path given as the source (``--source DIR|ZIP``).

Cache policy (LRU over a cap, never evicting a registered board's static) needs a
delete in ``ContentStore``; until then kits stay (a kit is 10-40 MB).
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.model import BoardIdentity
from harness_manager.core.pack import BuildProfile, KitAdapter, KitCheck, kit_refusal
from harness_manager.core.registry import GROUP

from .schema import (
    KIT_JSON,
    KitFormatError,
    KitManifest,
    canon_id,
    crc32_file,
    hex32,
    load_kit_json,
    parse_kit,
    parse_u32,
    same_id,
    sha256_file,
)

STORE_KIND = "rm_kit"
STORE_FILE_KIND = "rm_kit_file"
HUB_DIR_ENV = "HARNESS_MANAGER_KIT_HUB_DIR"
CAPABILITY = "build_kit"

SOURCE_CACHE = "cache"
SOURCE_CHANNEL = "channel"
SOURCE_HUB = "hub"
SOURCES = (SOURCE_CACHE, SOURCE_CHANNEL, SOURCE_HUB)

Progress = Callable[[str, int, int], None]


# --- the pack's kit adapter ----------------------------------------------------------------------


def kit_adapter(pack: str) -> KitAdapter:
    """The pack's ``KitAdapter`` (``<pack package>.kit:make_kit_adapter``), found from the
    ``harness_manager.boards`` entry point without instantiating the pack (as the XDC pin
    model is). ``AbsentError`` for no such pack; ``UnavailableError`` for a pack with no kit."""
    import importlib

    eps = [ep for ep in entry_points(group=GROUP) if ep.name == pack]
    if not eps:
        known = ", ".join(sorted(ep.name for ep in entry_points(group=GROUP))) or "none"
        raise AbsentError(f"no board pack named {pack!r}", hint=f"installed packs: {known}")
    package = eps[0].value.split(":", 1)[0].rsplit(".", 1)[0]
    try:
        mod = importlib.import_module(f"{package}.kit")
    except ModuleNotFoundError as exc:
        if exc.name == f"{package}.kit":
            raise UnavailableError(CAPABILITY, f"the {pack} pack has no DUT build kit "
                                               f"({package}.kit)") from exc
        raise
    make = getattr(mod, "make_kit_adapter", None)
    if make is None:
        raise UnavailableError(CAPABILITY, f"{package}.kit has no make_kit_adapter()")
    return make()


# --- the cached kit ------------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedKit:
    manifest: KitManifest
    sha256: str               # of the kit.json blob
    meta: dict[str, str]
    blobs: dict[str, Path]    # kit path -> the store blob

    @property
    def static_id(self) -> str:
        return self.manifest.static_id

    @property
    def source(self) -> str:
        return self.meta.get("source", "")

    def blob(self, role: str) -> Path | None:
        f = self.manifest.file(role)
        return self.blobs.get(f.path) if f else None

    def summary(self) -> dict[str, Any]:
        m = self.manifest
        return {"static_id": hex32(m.static_u32), "kit_id": m.kit_id, "board_type": m.board_type,
                "part": m.part, "vivado": m.vivado.to_json(), "static_usercode": m.static_usercode,
                "harness_impl": m.harness_impl, "access": m.access, "ip_class": m.ip_class,
                "licence_note": m.licence_note, "size": m.size, "files": len(m.files),
                "sha256": self.sha256, "source": self.source,
                "imported_at": self.meta.get("imported_at", "")}


@dataclass
class ImportResult:
    kit: CachedKit
    checks: list[KitCheck] = field(default_factory=list)
    already: bool = False     # the same kit.json was already cached


# --- sources -------------------------------------------------------------------------------------


class ChannelSource:
    """The ``rm-kit`` asset of the static's release in the signed channel.

    THE SEAM FOR LANE OTA-C: ``resolve(static_id) -> Asset | None`` finds the channel
    component (kind ``rm-kit``, target ``host-kit``, the planner never deploys it) for a
    static, in any release. The download is ``update.download.Downloader``'s (resume,
    sha256, size, the GitHub token only to GitHub hosts). Kits are ``access: public``
    (david K2), so no token is needed today; ``token`` is kept for the seam.
    """

    name = SOURCE_CHANNEL

    def __init__(self, resolve: Callable[[str], Any] | None = None, *, base_url: str = "",
                 token: str | None = None) -> None:
        self.resolve = resolve
        self.base_url = base_url
        self.token = token

    @property
    def reason(self) -> str:
        return "" if self.resolve else ("the signed channel's rm-kit component arrives with "
                                         "lane OTA-C; use --source hub or a path meanwhile")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "available": not self.reason, "reason": self.reason}

    def fetch(self, static_id: str, work: Path, progress: Progress | None) -> Path | None:
        if self.resolve is None:
            return None
        asset = self.resolve(static_id)
        if asset is None:
            return None
        from harness_manager.services.update.download import Downloader

        dl = Downloader(work / "download", token=self.token)
        return dl.fetch(asset, base_url=self.base_url, progress=progress)


class HubSource:
    """The hub's mint archive as a path: ``<root>/<sid>/kit/`` (F1) else ``<root>/<sid>/``."""

    name = SOURCE_HUB

    def __init__(self, root: Path | None) -> None:
        self.root = Path(root) if root else None

    @property
    def reason(self) -> str:
        if self.root is None:
            return (f"no hub archive path: set ${HUB_DIR_ENV} (or kits.hub_dir in the "
                    "settings) to the mint archive (e.g. /home/david/mints on the hub, or "
                    "its mount)")
        if not self.root.is_dir():
            return f"{self.root} is not a directory"
        return ""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "available": not self.reason, "reason": self.reason,
                "root": str(self.root) if self.root else None}

    def locate(self, static_id: str) -> Path | None:
        if self.reason:
            return None
        assert self.root is not None
        for spelling in (hex32(parse_u32(static_id)), canon_id(static_id)):
            d = self.root / spelling
            if (d / "kit").is_dir():
                k = d / "kit"
                zips = sorted(k.glob("*.zip"))
                return k if (k / KIT_JSON).is_file() else (zips[0] if zips else k)
            if d.is_dir():
                return d
        return None


# --- the service ---------------------------------------------------------------------------------


class KitService:
    """DUT build kits: cache, sources, import, export, verify. Board-agnostic."""

    def __init__(self, store: Any, work_dir: Path, *,
                 adapter_for: Callable[[str], KitAdapter] = kit_adapter,
                 channel: ChannelSource | None = None, hub: HubSource | None = None) -> None:
        self.store = store
        self.work_dir = Path(work_dir)
        self.adapter_for = adapter_for
        self.channel = channel or ChannelSource()
        self._hub = hub

    @property
    def hub(self) -> HubSource:
        """The hub source given, else the setting ``kits.hub_dir`` (``HUB_DIR_ENV``, then the
        settings: lane SET-WIRE), read at each use: the service keeps one KitService, and
        a change applies to the next fetch (a live row)."""
        if self._hub is not None:
            return self._hub
        from harness_manager.settings import runtime

        root = runtime.value("kits.hub_dir")
        return HubSource(Path(root) if root else None)

    @classmethod
    def for_state_dir(cls, state_dir: Path, store: Any = None, **kw: Any) -> KitService:
        from harness_manager.services.store import ContentStore
        from harness_manager.services.update.kits import kit_channel_source

        state_dir = Path(state_dir)
        # OTA-C: the signed channel's rm-kit, through the update service (token, mirrors)
        kw.setdefault("channel", kit_channel_source(state_dir))
        return cls(store if store is not None else ContentStore(state_dir / "store"),
                   state_dir / "kits", **kw)

    # -- reading the cache -------------------------------------------------------------------

    def list(self) -> list[CachedKit]:
        """Every cached kit, newest import first; one row per distinct kit.json."""
        seen: dict[str, CachedKit] = {}
        for sha, meta in self.store.find(STORE_KIND):
            kit = self._load(sha, meta)
            if kit is None:
                continue
            prev = seen.get(sha)
            if prev is None or meta.get("imported_at", "") > prev.meta.get("imported_at", ""):
                seen[sha] = kit
        return sorted(seen.values(), key=lambda k: k.meta.get("imported_at", ""), reverse=True)

    def get(self, static_id: str) -> CachedKit | None:
        """The newest cached kit for ``static_id``, or None."""
        want = canon_id(static_id)
        for kit in self.list():
            if canon_id(kit.static_id) == want:
                return kit
        return None

    def require(self, static_id: str) -> CachedKit:
        kit = self.get(static_id)
        if kit is None:
            raise AbsentError(f"no build kit for static {hex32(parse_u32(static_id))} in the "
                              "cache", hint=f"harness-manager kit fetch --static-id "
                                            f"{hex32(parse_u32(static_id))} (or kit import DIR)")
        return kit

    def _load(self, sha: str, meta: dict[str, str]) -> CachedKit | None:
        try:
            manifest = parse_kit(json.loads(self.store.path(sha).read_text(encoding="utf-8")))
        except (OSError, ValueError, AbsentError, KitFormatError):
            return None
        blobs: dict[str, Path] = {}
        for f in manifest.files:
            try:
                blobs[f.path] = self.store.path(f.sha256)
            except AbsentError:
                return None                      # a file blob went missing: not usable
        return CachedKit(manifest, sha, dict(meta), blobs)

    def verify_cached(self, kit: CachedKit) -> list[KitCheck]:
        """Re-hash every blob and recompute the DCP's CRC (``ContentStore.verify``)."""
        checks = []
        bad = [f.path for f in kit.manifest.files if not self.store.verify(f.sha256)]
        checks.append(KitCheck("files", "mismatch" if bad else "ok",
                               f"corrupt in the cache: {', '.join(bad)}" if bad else
                               f"{len(kit.manifest.files)} files match their sha256"))
        blob = kit.blob("locked_static")
        if blob is not None and not bad:
            checks.append(_crc_check(kit.manifest, blob))
        return checks

    # -- the pack's view ---------------------------------------------------------------------

    def profile(self, pack: str, static_id: str) -> BuildProfile | None:
        kit = self.get(static_id)
        return self.adapter_for(pack).build_profile(static_id, kit.manifest if kit else None)

    def check_against_board(self, kit: KitManifest, identity: BoardIdentity | None,
                            pack: str | None = None) -> list[KitCheck]:
        adapter = self.adapter_for(pack or kit.board_type)
        return list(adapter.check_kit(kit, identity))

    # -- import ---------------------------------------------------------------------------

    def import_(self, path: Path, *, source: str = "path") -> ImportResult:
        """A kit directory, a kit zip, or a directory of loose mint files -> the cache."""
        path = Path(path)
        if not path.exists():
            raise AbsentError(f"no such kit: {path}", hint="a kit directory, a .zip, or a "
                                                         "fielded/<sid>/ directory")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            if path.suffix.lower() != ".zip" and not zipfile.is_zipfile(path):
                raise UsageError(f"{path} is neither a kit directory nor a zip")
            tmp = self.work_dir / f".unzip-{uuid.uuid4().hex}"
            try:
                from harness_manager.services.update.bundle import safe_extract

                safe_extract(path, tmp)
                root = _kit_root(tmp)
                if root is None:
                    raise KitFormatError(f"{path.name} holds no kit.json",
                                         hint="a kit zip has kit.json at its root or one "
                                              "directory down")
                return self._import_kit_dir(root, source)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if (path / KIT_JSON).is_file():
            return self._import_kit_dir(path, source)
        return self._import_loose(path, source)

    def _import_kit_dir(self, root: Path, source: str) -> ImportResult:
        manifest = load_kit_json(root / KIT_JSON)
        checks = verify_dir_files(manifest, root)
        refusal = kit_refusal(checks, f"the kit in {root}")
        if refusal is not None:
            refusal.data = {"checks": [c.__dict__ for c in checks]}  # type: ignore[attr-defined]
            raise refusal
        return self._store_kit(manifest, {f.path: root / f.path for f in manifest.files},
                               source, checks)

    def _import_loose(self, directory: Path, source: str) -> ImportResult:
        """``fielded/<sid>/`` or a mint ``prod/`` dir: the pack writes the kit.json."""
        errors = []
        for pack in _pack_names():
            try:
                adapter = self.adapter_for(pack)
            except (UnavailableError, AbsentError):
                continue
            try:
                got = adapter.kit_from_dir(directory)
            except (UsageError, UnavailableError) as exc:
                errors.append(exc)
                continue
            if got is None:
                continue
            doc, files = got
            manifest = parse_kit(doc)
            checks = verify_files(manifest, files)
            refusal = kit_refusal(checks, f"the mint files in {directory}")
            if refusal is not None:
                raise refusal
            return self._store_kit(manifest, files, source, checks)
        if errors:
            raise errors[0]
        raise UsageError(f"{directory} is not a kit",
                         hint="a kit directory holds kit.json; a fielded/<sid>/ or a mint "
                              "prod/ directory holds static_routed_locked.dcp")

    def _store_kit(self, manifest: KitManifest, files: dict[str, Path], source: str,
                   checks: list[KitCheck]) -> ImportResult:
        sid = canon_id(manifest.static_id)
        for f in manifest.files:
            self.store.put_file(files[f.path], kind=STORE_FILE_KIND,
                                meta={"static_id": sid, "path": f.path, "role": f.role})
        data = manifest.dumps().encode("utf-8")
        already = any(canon_id(k.static_id) == sid and k.sha256 == _sha(data)
                      for k in self.list())
        meta = {"static_id": sid, "board_type": manifest.board_type,
                "vivado": manifest.vivado.release, "usercode": manifest.static_usercode,
                "impl": manifest.harness_impl, "source": source,
                "imported_at": _now()}
        sha = self.store.put_bytes(data, kind=STORE_KIND, meta=meta)
        kit = self._load(sha, meta)
        assert kit is not None
        return ImportResult(kit, checks, already)

    # -- fetch ----------------------------------------------------------------------------

    def sources(self) -> list[dict[str, Any]]:
        return [{"name": SOURCE_CACHE, "available": True, "reason": ""},
                self.channel.describe(), self.hub.describe()]

    def fetch(self, static_id: str, *, source: str | None = None,
              progress: Progress | None = None) -> tuple[CachedKit, str]:
        """The kit for ``static_id``: (kit, the source it came from). ``AbsentError`` (3)
        when no source has it, naming each one tried and why."""
        sid = hex32(parse_u32(static_id))
        tried: list[str] = []
        order = [source] if source else list(SOURCES)
        for src in order:
            if src == SOURCE_CACHE:
                kit = self.get(sid)
                if kit is not None:
                    return kit, SOURCE_CACHE
                tried.append("cache: not cached")
            elif src == SOURCE_CHANNEL:
                if self.channel.reason:
                    tried.append(f"channel: {self.channel.reason}")
                    continue
                got = self.channel.fetch(sid, self.work_dir, progress)
                if got is None:
                    why = getattr(self.channel, "last_error", "")    # OTA-C's source says why
                    tried.append(f"channel: {why or f'no rm-kit component for {sid}'}")
                    continue
                res = self.import_(got, source=SOURCE_CHANNEL)
                _check_is(res.kit, sid)
                with contextlib.suppress(OSError):
                    got.unlink()                 # the store holds the files now
                return res.kit, SOURCE_CHANNEL
            elif src == SOURCE_HUB:
                if self.hub.reason:
                    tried.append(f"hub: {self.hub.reason}")
                    continue
                where = self.hub.locate(sid)
                if where is None:
                    tried.append(f"hub: {self.hub.root} has no {sid}/")
                    continue
                if progress:
                    progress("import", 0, 0)
                res = self.import_(where, source=f"hub:{where}")
                _check_is(res.kit, sid)
                return res.kit, SOURCE_HUB
            else:
                p = Path(src)
                if not p.exists():
                    raise UsageError(f"source {src!r} is not cache, channel, hub or a path")
                res = self.import_(p, source=f"path:{p.resolve()}")
                _check_is(res.kit, sid)
                return res.kit, "path"
        raise AbsentError(f"no source has the build kit for static {sid}",
                          hint="; ".join(tried))

    # -- export ---------------------------------------------------------------------------

    def export(self, kit: CachedKit, out_dir: Path) -> list[Path]:
        """Copy the kit into a plain directory Vivado opens: ``kit.json`` + every file."""
        out_dir = Path(out_dir)
        existing = out_dir / KIT_JSON
        if existing.is_file():
            try:
                other = load_kit_json(existing)
            except KitFormatError:
                other = None
            if other is not None and not same_id(other.static_id, kit.static_id):
                raise RefusedError(f"{out_dir} already holds the kit for {other.static_id}",
                                   hint="export each static's kit to its own directory")
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for f in kit.manifest.files:
            dest = out_dir / f.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
            shutil.copyfile(kit.blobs[f.path], tmp)
            os.replace(tmp, dest)
            written.append(dest)
        existing.write_text(kit.manifest.dumps(), encoding="utf-8")
        written.append(existing)
        return written

    def zip_to(self, kit: CachedKit, dest: Path) -> Path:
        """The kit as one zip (``<sid>/kit.json``, ``<sid>/static/...``): the transport."""
        top = hex32(kit.manifest.static_u32)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:   # a DCP does not compress
            zf.writestr(f"{top}/{KIT_JSON}", kit.manifest.dumps())
            for f in kit.manifest.files:
                zf.write(kit.blobs[f.path], f"{top}/{f.path}")
        return dest

    # -- verify a plain directory ---------------------------------------------------------

    def verify_dir(self, kit_dir: Path, identity: BoardIdentity | None = None,
                   *, pack: str | None = None) -> tuple[KitManifest, list[KitCheck]]:
        """A kit directory on disk (what Vivado will open): files, CRC, and with a board
        its live static (identity items)."""
        manifest = load_kit_json(Path(kit_dir) / KIT_JSON)
        checks = verify_dir_files(manifest, Path(kit_dir))
        if identity is not None or pack:
            try:
                checks += self.check_against_board(manifest, identity, pack)
            except UnavailableError as exc:
                checks.append(KitCheck("board", "unchecked", exc.reason))
        return manifest, checks


# --- helpers -----------------------------------------------------------------------------------


def verify_files(manifest: KitManifest, files: dict[str, Path]) -> list[KitCheck]:
    """Every file present with its size and sha256; the DCP's CRC-32 is the static_id."""
    bad: list[str] = []
    for f in manifest.files:
        p = files.get(f.path)
        if p is None or not p.is_file():
            bad.append(f"{f.path} is missing")
            continue
        size = p.stat().st_size
        if size != f.size:
            bad.append(f"{f.path} is {size} B, kit.json says {f.size}")
        elif sha256_file(p) != f.sha256:
            bad.append(f"{f.path} fails its sha256")
    checks = [KitCheck("files", "mismatch" if bad else "ok",
                       "; ".join(bad) if bad else
                       f"{len(manifest.files)} files match kit.json (size and sha256)")]
    dcp = files.get(manifest.locked_static.path)
    if dcp is not None and dcp.is_file():
        checks.append(_crc_check(manifest, dcp))
    return checks


def verify_dir_files(manifest: KitManifest, root: Path) -> list[KitCheck]:
    return verify_files(manifest, {f.path: root / f.path for f in manifest.files})


def _crc_check(manifest: KitManifest, dcp: Path) -> KitCheck:
    crc = crc32_file(dcp)
    ok = crc == manifest.static_u32
    return KitCheck("static_id", "ok" if ok else "mismatch",
                    f"CRC-32 of {Path(manifest.locked_static.path).name} is {hex32(crc)}"
                    + ("" if ok else f", the kit says {manifest.static_id}: this DCP is not "
                                     "that static (a wrong, stale or corrupt copy)")
                    + (" = the kit's static_id" if ok else ""))


def _check_is(kit: CachedKit, sid: str) -> None:
    if not same_id(kit.static_id, sid):
        raise RefusedError(f"the source holds the kit for {kit.static_id}, not {sid}",
                           hint="check the source; it was cached under its own static_id")


def _kit_root(tmp: Path) -> Path | None:
    if (tmp / KIT_JSON).is_file():
        return tmp
    subs = [p for p in tmp.iterdir() if p.is_dir() and (p / KIT_JSON).is_file()]
    return subs[0] if len(subs) == 1 else None


def _pack_names() -> Iterable[str]:
    return sorted({ep.name for ep in entry_points(group=GROUP)})


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def temp_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="kit-", dir=base))
