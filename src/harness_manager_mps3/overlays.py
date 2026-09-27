"""The MPS3 overlay catalogue: which partition designs can be loaded, and from where.

An overlay is the UltraScale-mandatory triple ``{clearing.bin, partial.bin,
manifest.json}`` for one reconfigurable module (platform repo,
``docs/contracts/overlay-manifest.md``). Manifests are parsed and validated
ONLY through ``pyverify.overlay``. This module adds three things on top:

- the search path (below);
- the board-agnostic ``OverlayRef`` view that the deploy service hands around;
- the rm_id -> name map, keyed by the version-stable design id
  (``rm_id & 0xFFFF``, ``pyverify.rm_id.design_id``). It replaces the scaffold's
  ``constants.KNOWN_DESIGNS`` wherever a manifest is loaded, and falls back to it
  where none is.

Search order. For a duplicate ``(rm_name, rm_id, static_id)``, the first hit wins:

1. directories passed to ``OverlayCatalogue(dirs)``;
2. ``$HARNESS_MANAGER_MPS3_OVERLAY_DIRS`` (``os.pathsep``-separated);
3. the engine content store, when one is attached (``kind="overlay"``). The
   store is optional; the catalogue never requires it.

A directory may be an overlay ROOT (``<root>/<rm>/manifest.json``, the platform's
``fpga/dfx/overlay/`` layout) or ONE overlay (``<dir>/manifest.json``).

Content-store convention (``import_overlay`` writes it; the loader reads it):

- each payload is a blob of kind ``"overlay_payload"``, with meta ``role``
  (``clearing``/``partial``), ``rm_id`` and ``static_id``;
- the manifest is a blob of kind ``"overlay"``, with meta ``static_id``,
  ``rm_name``, ``rm_id``, ``clearing_sha256``, ``partial_sha256`` and, when the
  manifest has one, ``static_usercode``;
- two optional files travel with the pair when the overlay has them, each an
  ``"overlay_payload"`` blob with its own ``role``, listed in the manifest's meta
  by sha256: the ILA probes file the manifest names in ``ltx`` (role ``ltx``,
  meta ``ltx_sha256``; checked against ``ltx_crc32`` when the manifest has one)
  and the build receipt (role ``receipt``, meta ``receipt_sha256``): the file the
  manifest names in ``build_receipt`` (KIT-GUIDE's pack step), else a
  ``<rm>_build.json`` or ``receipt.json`` beside the manifest. A store overlay's
  ``validate()`` re-hashes both, so a damaged copy is caught where the payload
  CRCs are. An overlay without them is stored and listed exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyverify import rm_id as rmid
from pyverify.overlay import (
    Overlay,
    OverlayManifest,
    OverlayManifestError,
    OverlayValidationError,
    compute_crc32,
)

from harness_manager.core.errors import AbsentError, HarnessError, RefusedError
from harness_manager.core.model import Check
from harness_manager.core.pack import OverlayRef

from .constants import KNOWN_DESIGNS

log = logging.getLogger(__name__)

OVERLAY_DIRS_ENV = "HARNESS_MANAGER_MPS3_OVERLAY_DIRS"
STORE_KIND = "overlay"
STORE_PAYLOAD_KIND = "overlay_payload"
STORE_SOURCE_PREFIX = "store:"
DEFAULT_IP_CLASS = "unknown"
ROLE_LTX = "ltx"
ROLE_RECEIPT = "receipt"
OPTIONAL_ROLES = (ROLE_LTX, ROLE_RECEIPT)
#: Where a build receipt is looked for when the manifest names none (DUT_BUILD_GUIDE §3.4).
RECEIPT_NAMES = ("{rm}_build.json", "receipt.json")


def _hex32(value: int) -> str:
    """The canonical rendering: lowercase, zero-padded ("0x3f1a560f"), as the shell reports it."""
    return rmid.format_rm_id(value)


@dataclass(frozen=True, eq=False)
class CatalogueEntry:
    """One loadable overlay: the board-agnostic ref plus the pyverify object behind it."""

    ref: OverlayRef
    overlay: Overlay            # what SwapOrchestrator/BitstreamPusher consume
    origin: str                 # "dir" | "env" | "store"
    pair_check: Check           # preflight (c): are the clearing and the partial one pair?
    pair_detail: str


class _StoredOverlay(Overlay):
    """An overlay whose payloads live in the content store under their sha256.

    ``Overlay.validate`` and ``BitstreamPusher.push_pair`` only reach the files
    through ``clearing_path()``/``partial_path()``, so overriding those two is
    enough to push straight from the store without copying anything.
    """

    def __init__(self, directory: Path, manifest: OverlayManifest,
                 clearing: Path, partial: Path, *, store: Any = None,
                 optional: Mapping[str, str] | None = None) -> None:
        super().__init__(directory=directory, manifest=manifest)
        self._clearing = clearing
        self._partial = partial
        self._store = store
        self.optional_sha256: dict[str, str] = dict(optional or {})   # role -> sha256

    def clearing_path(self) -> Path:
        return self._clearing

    def partial_path(self) -> Path:
        return self._partial

    def optional_path(self, role: str) -> Path | None:
        """Where the store keeps an optional file (``ltx``, ``receipt``); None: not stored."""
        sha = self.optional_sha256.get(role)
        if not sha or self._store is None:
            return None
        try:
            return self._store.path(sha)
        except HarnessError:
            return None

    # The .ltx is served from the store when it was imported with the pair; an overlay
    # imported without one (or before the store kept it) has no probes file, which is
    # honest. The store never carries the _app.bin.
    def ltx_path(self) -> Path | None:
        return self.optional_path(ROLE_LTX)

    def fw_path(self) -> Path | None:
        return None

    def validate(self, *, expected_static_id: int | None = None) -> None:
        """pyverify's checks, plus: each optional file still hashes to the sha it is stored under."""
        errors: list[str] = []
        try:
            super().validate(expected_static_id=expected_static_id)
        except OverlayValidationError as exc:
            errors.append(str(exc))
        for role, sha in sorted(self.optional_sha256.items()):
            if not _store_verify(self._store, sha):
                errors.append(f"stored {role} {sha[:12]} is missing or fails its sha256 "
                              "(re-import the overlay to repair the store copy)")
        if errors:
            raise OverlayValidationError("; ".join(errors))


# --- loading --------------------------------------------------------------------------


def load_overlay_dir(directory: Path) -> tuple[Overlay, dict[str, Any]]:
    """Parse ``<directory>/manifest.json`` through pyverify. Returns (overlay, raw JSON).

    Raises ``OverlayManifestError`` for a missing, unparsable or incomplete manifest.
    """
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise OverlayManifestError(f"no manifest at {manifest_path}")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OverlayManifestError(f"invalid JSON in {manifest_path}: {exc}") from exc
    manifest = OverlayManifest.from_dict(raw)
    return Overlay(directory=directory, manifest=manifest), raw


def _ref(overlay: Overlay, raw: Mapping[str, Any], source: str,
         optional: Mapping[str, str] | None = None) -> OverlayRef:
    """``optional``: role -> sha256 of the optional files (``ltx``, ``receipt``) it carries."""
    m = overlay.manifest
    ip_class = raw.get("ip_class")
    optional = optional or {}
    return OverlayRef(
        name=m.rm_name,
        rm_id=_hex32(m.rm_id),
        static_id=_hex32(m.static_id),
        static_usercode=_hex32(m.static_usercode) if m.static_usercode is not None else "",
        source=source,
        size_bytes=m.clearing.len + m.partial.len,   # bytes pushed, without the 24-byte headers
        ip_class=ip_class if isinstance(ip_class, str) and ip_class else DEFAULT_IP_CLASS,
        ltx_sha256=optional.get(ROLE_LTX, ""),
        receipt_sha256=optional.get(ROLE_RECEIPT, ""),
    )


# --- the optional files beside the pair (.ltx, build receipt) ---------------------------


def _plain_name(name: object) -> bool:
    """A file name beside the manifest: no directory part, no '.'/'..'."""
    return isinstance(name, str) and name not in ("", ".", "..") and \
        Path(name).name == name and "\\" not in name


def _named_receipt(raw: Mapping[str, Any]) -> str | None:
    named = raw.get("build_receipt")
    return named if isinstance(named, str) and named else None


def optional_files(overlay: Overlay, raw: Mapping[str, Any]) -> dict[str, Path]:
    """The optional files of an overlay DIRECTORY that exist, by role: the ``.ltx`` its
    manifest names, and its build receipt (``build_receipt``, else ``RECEIPT_NAMES``)."""
    out: dict[str, Path] = {}
    directory = Path(overlay.directory)
    ltx = overlay.manifest.ltx
    if _plain_name(ltx) and (directory / ltx).is_file():
        out[ROLE_LTX] = directory / ltx
    named = _named_receipt(raw)
    names = [named] if named is not None else [n.format(rm=overlay.manifest.rm_name)
                                                for n in RECEIPT_NAMES]
    for name in names:
        if _plain_name(name) and (directory / name).is_file():
            out[ROLE_RECEIPT] = directory / name
            break
    return out


def _optional_problems(overlay: Overlay, raw: Mapping[str, Any]) -> list[str]:
    """Why the optional files cannot be stored with the pair (pyverify has already
    refused an ``ltx`` the manifest names but the directory lacks)."""
    problems: list[str] = []
    ltx = overlay.manifest.ltx
    if ltx is not None and not _plain_name(ltx):
        problems.append(f"ltx {ltx!r} is not a file name beside the manifest")
    elif ltx is not None and raw.get("ltx_crc32") is not None:
        path = Path(overlay.directory) / ltx
        want = _crc_value(raw["ltx_crc32"])
        have = compute_crc32(path) if path.is_file() else None
        if want is None:
            problems.append(f"ltx_crc32 {raw['ltx_crc32']!r} is not a CRC-32")
        elif have is not None and have != want:
            problems.append(f"ltx crc32 mismatch: manifest=0x{want:08x} "
                            f"actual=0x{have:08x} ({path}): a stale probes file")
    named = _named_receipt(raw)
    if named is not None and not _plain_name(named):
        problems.append(f"build_receipt {named!r} is not a file name beside the manifest")
    elif named is not None and not (Path(overlay.directory) / named).is_file():
        problems.append(f"build_receipt referenced but missing: {Path(overlay.directory) / named}")
    return problems


def _crc_value(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value & 0xFFFFFFFF
    try:
        return int(str(value).strip(), 16) & 0xFFFFFFFF
    except ValueError:
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _store_verify(store: Any, sha: str) -> bool:
    """The store's own re-hash when it has one (``ContentStore.verify``); else hash the blob."""
    if store is None:
        return False
    verify = getattr(store, "verify", None)
    try:
        if callable(verify):
            return bool(verify(sha))
        return _sha256_file(store.path(sha)) == sha
    except (OSError, HarnessError, ValueError):
        return False


def _pair_by_name(manifest: OverlayManifest) -> tuple[Check, str]:
    """Preflight (c) for an overlay directory.

    A manifest carries ONE rm_id, and the pusher frames both files with it, so
    the frames always agree. What can go wrong is the manifest pointing at the
    wrong files. gen_manifest.py names the pair ``<rm>.bin`` / ``<rm>_clear.bin``
    (fpga/dfx/gen_manifest.py, build_manifest), so check that shape.
    """
    clearing, partial = Path(manifest.clearing.file), Path(manifest.partial.file)
    if clearing == partial:
        return Check.MISMATCH, f"clearing and partial are the same file ({clearing})"
    if clearing.parent != partial.parent:
        return Check.MISMATCH, f"clearing {clearing} and partial {partial} are in different directories"
    expected = f"{partial.stem}_clear{partial.suffix}"
    if clearing.name != expected:
        return (Check.MISMATCH,
                f"clearing {clearing.name} does not pair with partial {partial.name} "
                f"(expected {expected}, gen_manifest.py naming)")
    return (Check.OK,
            f"{clearing.name} + {partial.name}, both framed with rm_id {_hex32(manifest.rm_id)}")


def _overlay_dirs(directory: Path) -> list[Path]:
    """A single overlay dir, or the overlay dirs under a root (sorted by name)."""
    if (directory / "manifest.json").is_file():
        return [directory]
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if (p / "manifest.json").is_file())


def env_overlay_dirs() -> list[Path]:
    """The setting ``mps3.overlay_dirs``, read at each load: ``OVERLAY_DIRS_ENV`` (split on
    ``os.pathsep``), else the Settings menu / ``settings.toml`` (lane SET-WIRE)."""
    from .settings import value

    return [Path(p) for p in value("mps3.overlay_dirs") if str(p).strip()]   # OVERLAY_DIRS_ENV


# --- the catalogue --------------------------------------------------------------------


@dataclass
class OverlayCatalogue:
    """Loads lazily on first use. Call ``reload()`` after the directories change."""

    dirs: tuple[Path, ...] = ()
    use_env: bool = True
    store: Any = None                           # a harness-manager ContentStore, optional
    _entries: list[CatalogueEntry] | None = field(default=None, init=False, repr=False)
    _rejects: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.dirs = tuple(Path(d) for d in self.dirs)

    # -- configuration --------------------------------------------------------------

    def use_store(self, store: Any) -> None:
        """Attach the engine's content store. Idempotent."""
        if store is not self.store:
            self.store = store
            self._entries = None

    def search_dirs(self) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = [("dir", d) for d in self.dirs]
        if self.use_env:
            out.extend(("env", d) for d in env_overlay_dirs())
        return out

    def reload(self) -> None:
        self._entries = None

    # -- queries --------------------------------------------------------------------

    def entries(self) -> list[CatalogueEntry]:
        if self._entries is None:
            self._entries = self._load()
        return list(self._entries)

    def refs(self) -> list[OverlayRef]:
        return [e.ref for e in self.entries()]

    @property
    def rejects(self) -> dict[str, str]:
        """{source: why its manifest could not be loaded}. Loaded entries are not listed."""
        self.entries()
        return dict(self._rejects)

    def entry_for(self, ref: OverlayRef) -> CatalogueEntry:
        """The entry behind ``ref``: by source first, then by (name, rm_id, static_id)."""
        entries = self.entries()
        for e in entries:
            if ref.source and e.ref.source == ref.source:
                return e
        for e in entries:
            if (e.ref.name == ref.name
                    and _same_u32(e.ref.rm_id, ref.rm_id)
                    and _same_u32(e.ref.static_id, ref.static_id)):
                return e
        raise AbsentError(
            f"overlay {ref.name!r} ({ref.rm_id} for shell {ref.static_id}) is not in the catalogue",
            hint=f"add its directory to {OVERLAY_DIRS_ENV}",
        )

    def rm_name(self, rm_id: object) -> str:
        """The design's name from the loaded manifests, keyed by ``rm_id & 0xFFFF``; "" if unknown."""
        try:
            design = rmid.design_id(rm_id)
        except (TypeError, ValueError):
            return ""
        for e in self.entries():
            if rmid.design_id(e.overlay.manifest.rm_id) == design:
                return e.ref.name
        return ""

    def greybox_for(self, static_id: str) -> CatalogueEntry | None:
        """The greybox (rm_id 0) keyed to ``static_id``, if the catalogue has one."""
        for e in self.entries():
            if rmid.is_greybox(e.overlay.manifest.rm_id) and _same_u32(e.ref.static_id, static_id):
                return e
        return None

    # -- loading --------------------------------------------------------------------

    def _load(self) -> list[CatalogueEntry]:
        self._rejects = {}
        seen: set[tuple[str, int, int]] = set()
        entries: list[CatalogueEntry] = []

        def add(entry: CatalogueEntry) -> None:
            m = entry.overlay.manifest
            key = (m.rm_name, m.rm_id, m.static_id)
            if key in seen:
                log.info("overlay %s from %s shadowed by an earlier search path",
                         m.rm_name, entry.ref.source)
                return
            seen.add(key)
            entries.append(entry)

        for origin, root in self.search_dirs():
            for d in _overlay_dirs(root):
                manifest_path = d / "manifest.json"
                try:
                    overlay, raw = load_overlay_dir(d)
                except OverlayManifestError as exc:
                    self._rejects[str(manifest_path)] = str(exc)
                    log.warning("skipping overlay %s: %s", manifest_path, exc)
                    continue
                check, detail = _pair_by_name(overlay.manifest)
                optional = {role: _sha256_file(p)
                            for role, p in optional_files(overlay, raw).items()}
                add(CatalogueEntry(_ref(overlay, raw, str(manifest_path), optional), overlay,
                                   origin, check, detail))

        for entry in self._load_store():
            add(entry)
        return entries

    def _load_store(self) -> Iterable[CatalogueEntry]:
        store = self.store
        if store is None:
            return
        for sha, meta in store.find(STORE_KIND):
            source = STORE_SOURCE_PREFIX + sha
            # Meta first: a record without payload pointers is not an overlay manifest
            # (e.g. a bare partial put as kind="overlay"), so never read its blob as JSON.
            missing = [k for k in ("clearing_sha256", "partial_sha256") if not meta.get(k)]
            if missing:
                self._rejects[source] = (f"store entry lacks meta {missing[0]!r} "
                                         "(not written by import_overlay)")
                continue
            clearing_sha, partial_sha = meta["clearing_sha256"], meta["partial_sha256"]
            try:
                raw = json.loads(store.path(sha).read_text(encoding="utf-8"))
                manifest = OverlayManifest.from_dict(raw)
            except (OSError, ValueError, OverlayManifestError) as exc:
                self._rejects[source] = f"store manifest unreadable: {exc}"
                continue
            optional = {role: meta[f"{role}_sha256"] for role in OPTIONAL_ROLES
                        if meta.get(f"{role}_sha256")}
            overlay = _StoredOverlay(store.path(sha).parent, manifest,
                                     store.path(clearing_sha), store.path(partial_sha),
                                     store=store, optional=optional)
            check, detail = _pair_in_store(store, manifest, clearing_sha, partial_sha)
            yield CatalogueEntry(_ref(overlay, raw, source, optional), overlay, "store", check,
                                 detail)


def _pair_in_store(store: Any, manifest: OverlayManifest,
                   clearing_sha: str, partial_sha: str) -> tuple[Check, str]:
    """Preflight (c) for a store overlay: both payloads were imported under this rm_id."""
    if clearing_sha == partial_sha:
        return Check.MISMATCH, "clearing and partial are the same blob"
    rm = _hex32(manifest.rm_id)
    roles = {sha: m for sha, m in store.find(STORE_PAYLOAD_KIND, rm_id=rm)}
    problems = []
    for role, sha in (("clearing", clearing_sha), ("partial", partial_sha)):
        meta = roles.get(sha)
        if meta is None:
            problems.append(f"the {role} blob {sha[:12]} was not imported for rm_id {rm}")
        elif meta.get("role") != role:
            problems.append(f"blob {sha[:12]} was imported as a {meta.get('role')!r}, not a {role}")
    if problems:
        return Check.MISMATCH, "; ".join(problems)
    return Check.OK, f"both payload blobs were imported for rm_id {rm}"


def _same_u32(a: object, b: object) -> bool:
    """Compare two ids by u32 value (overlay-manifest.md: "compare the u32, never the string")."""
    try:
        return rmid.parse_rm_id(a) == rmid.parse_rm_id(b)
    except (TypeError, ValueError):
        return str(a).lower() == str(b).lower()


# --- import into the content store ----------------------------------------------------


def import_overlay(store: Any, directory: Path) -> str:
    """Validate an overlay directory, then copy it into ``store``. Returns the manifest's sha256.

    A corrupt overlay is refused (``RefusedError``). The store never holds a
    triple that failed its own manifest's length and CRC check, nor an ``.ltx``
    that fails the manifest's ``ltx_crc32``. The optional ``.ltx`` and build
    receipt are stored beside the pair (module docstring).
    """
    overlay, raw = load_overlay_dir(Path(directory))
    try:
        overlay.validate()
    except OverlayValidationError as exc:
        raise RefusedError(f"refusing to import {directory}: {exc}",
                           hint="rebuild the overlay or fix its manifest.json") from exc
    problems = _optional_problems(overlay, raw)
    if problems:
        raise RefusedError(f"refusing to import {directory}: {'; '.join(problems)}",
                           hint="rebuild the overlay or fix its manifest.json")
    m = overlay.manifest
    ids = {"rm_id": _hex32(m.rm_id), "static_id": _hex32(m.static_id)}
    clearing = store.put_file(overlay.clearing_path(), kind=STORE_PAYLOAD_KIND,
                              meta={"role": "clearing", **ids})
    partial = store.put_file(overlay.partial_path(), kind=STORE_PAYLOAD_KIND,
                             meta={"role": "partial", **ids})
    meta = {"rm_name": m.rm_name, "clearing_sha256": clearing, "partial_sha256": partial, **ids}
    for role, path in sorted(optional_files(overlay, raw).items()):
        meta[f"{role}_sha256"] = store.put_file(path, kind=STORE_PAYLOAD_KIND,
                                                meta={"role": role, **ids})
    if m.static_usercode is not None:
        meta["static_usercode"] = _hex32(m.static_usercode)
    data = json.dumps(raw, indent=2, sort_keys=True).encode("utf-8")
    return store.put_bytes(data, kind=STORE_KIND, meta=meta)


# --- rm_id -> name, for identity ------------------------------------------------------

_default: tuple[str, OverlayCatalogue] | None = None


def default_catalogue() -> OverlayCatalogue:
    """A process-wide catalogue over the setting ``mps3.overlay_dirs``
    (``$HARNESS_MANAGER_MPS3_OVERLAY_DIRS``, then the settings), rebuilt when it changes."""
    global _default
    key = os.pathsep.join(str(d) for d in env_overlay_dirs())      # OVERLAY_DIRS_ENV
    if _default is None or _default[0] != key:
        _default = (key, OverlayCatalogue())
    return _default[1]


def resolve_rm_name(rm_id: object, catalogue: OverlayCatalogue | None = None) -> str:
    """The loaded design's name: manifests first, then ``KNOWN_DESIGNS``; "" when unknown.

    Keyed on the design id, so ``nanosoc`` v1.0 (0x01000001) and v1.1
    (0x01010001) both resolve to ``nanosoc`` (pyverify/rm_id.py).
    """
    cat = catalogue if catalogue is not None else default_catalogue()
    name = cat.rm_name(rm_id)
    if name:
        return name
    try:
        return KNOWN_DESIGNS.get(rmid.design_id(rm_id), "")
    except (TypeError, ValueError):
        return ""
