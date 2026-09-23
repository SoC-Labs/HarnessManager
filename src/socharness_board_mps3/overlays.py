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
2. ``$SOCHARNESS_MPS3_OVERLAY_DIRS`` (``os.pathsep``-separated);
3. the engine content store, when one is attached (``kind="overlay"``). The
   store is optional; the catalogue never requires it.

A directory may be an overlay ROOT (``<root>/<rm>/manifest.json``, the platform's
``fpga/dfx/overlay/`` layout) or ONE overlay (``<dir>/manifest.json``).

Content-store convention (``import_overlay`` writes it; the loader reads it):

- each payload is a blob of kind ``"overlay_payload"``, with meta ``role``
  (``clearing``/``partial``), ``rm_id`` and ``static_id``;
- the manifest is a blob of kind ``"overlay"``, with meta ``static_id``,
  ``rm_name``, ``rm_id``, ``clearing_sha256``, ``partial_sha256`` and, when the
  manifest has one, ``static_usercode``.
"""

from __future__ import annotations

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
)

from socharness.core.errors import AbsentError, RefusedError
from socharness.core.model import Check
from socharness.core.pack import OverlayRef

from .constants import KNOWN_DESIGNS

log = logging.getLogger(__name__)

OVERLAY_DIRS_ENV = "SOCHARNESS_MPS3_OVERLAY_DIRS"
STORE_KIND = "overlay"
STORE_PAYLOAD_KIND = "overlay_payload"
STORE_SOURCE_PREFIX = "store:"
DEFAULT_IP_CLASS = "unknown"


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
                 clearing: Path, partial: Path) -> None:
        super().__init__(directory=directory, manifest=manifest)
        self._clearing = clearing
        self._partial = partial

    def clearing_path(self) -> Path:
        return self._clearing

    def partial_path(self) -> Path:
        return self._partial

    # The store does not carry the optional .ltx/_app.bin; do not let validate()
    # fail on them. The re-attach plan then has no probes file, which is honest.
    def ltx_path(self) -> Path | None:
        return None

    def fw_path(self) -> Path | None:
        return None


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


def _ref(overlay: Overlay, raw: Mapping[str, Any], source: str) -> OverlayRef:
    m = overlay.manifest
    ip_class = raw.get("ip_class")
    return OverlayRef(
        name=m.rm_name,
        rm_id=_hex32(m.rm_id),
        static_id=_hex32(m.static_id),
        static_usercode=_hex32(m.static_usercode) if m.static_usercode is not None else "",
        source=source,
        size_bytes=m.clearing.len + m.partial.len,   # bytes pushed, without the 24-byte headers
        ip_class=ip_class if isinstance(ip_class, str) and ip_class else DEFAULT_IP_CLASS,
    )


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
    raw = os.environ.get(OVERLAY_DIRS_ENV, "")
    return [Path(p) for p in raw.split(os.pathsep) if p.strip()]


# --- the catalogue --------------------------------------------------------------------


@dataclass
class OverlayCatalogue:
    """Loads lazily on first use. Call ``reload()`` after the directories change."""

    dirs: tuple[Path, ...] = ()
    use_env: bool = True
    store: Any = None                           # a socharness ContentStore, optional
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
                add(CatalogueEntry(_ref(overlay, raw, str(manifest_path)), overlay, origin,
                                   check, detail))

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
            overlay = _StoredOverlay(store.path(sha).parent, manifest,
                                     store.path(clearing_sha), store.path(partial_sha))
            check, detail = _pair_in_store(store, manifest, clearing_sha, partial_sha)
            yield CatalogueEntry(_ref(overlay, raw, source), overlay, "store", check, detail)


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
    triple that failed its own manifest's length and CRC check.
    """
    overlay, raw = load_overlay_dir(Path(directory))
    try:
        overlay.validate()
    except OverlayValidationError as exc:
        raise RefusedError(f"refusing to import {directory}: {exc}",
                           hint="rebuild the overlay or fix its manifest.json") from exc
    m = overlay.manifest
    ids = {"rm_id": _hex32(m.rm_id), "static_id": _hex32(m.static_id)}
    clearing = store.put_file(overlay.clearing_path(), kind=STORE_PAYLOAD_KIND,
                              meta={"role": "clearing", **ids})
    partial = store.put_file(overlay.partial_path(), kind=STORE_PAYLOAD_KIND,
                             meta={"role": "partial", **ids})
    meta = {"rm_name": m.rm_name, "clearing_sha256": clearing, "partial_sha256": partial, **ids}
    if m.static_usercode is not None:
        meta["static_usercode"] = _hex32(m.static_usercode)
    data = json.dumps(raw, indent=2, sort_keys=True).encode("utf-8")
    return store.put_bytes(data, kind=STORE_KIND, meta=meta)


# --- rm_id -> name, for identity ------------------------------------------------------

_default: tuple[str, OverlayCatalogue] | None = None


def default_catalogue() -> OverlayCatalogue:
    """A process-wide catalogue over ``$SOCHARNESS_MPS3_OVERLAY_DIRS``, rebuilt when it changes."""
    global _default
    key = os.environ.get(OVERLAY_DIRS_ENV, "")
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
