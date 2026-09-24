"""A local content-addressed store (sha256).

Layout under ``root`` (the engine uses ``<state_dir>/store``)::

    blobs/<aa>/<sha256>                 the content, named by its own hash
    index/<sha256>/<record>.json        one index record: {kind, meta, size, added_at}
    tmp/                                partial writes; renamed into place when complete

Rules:

- **Content is deduplicated.** Identical bytes are stored once, however many
  times they are added.
- **The index is a set of records.** A record is ``(sha256, kind, meta)``.
  Adding the same record twice is a no-op. Adding the same content under a
  different kind or meta adds a second record for the same blob, so no import
  ever overwrites another's metadata.
- **Every write is write-then-rename.** A blob or record is first written in
  full to ``tmp/``, then moved into place with ``os.replace``. A reader never
  sees a partial file, and two processes adding at once need no lock: both
  renames put the same content under the same name. (Windows: the second
  rename does not replace the first writer's file; see ``_replace``.)
- **Corruption is detected, never trusted.** ``verify`` re-hashes the blob.
  Adding content whose blob exists but fails verification replaces the bad
  blob with the good one.

Typical use (overlays): do not store bare partials as ``kind="overlay"``.
Use ``harness_manager_mps3.overlays.import_overlay(store, overlay_dir)``,
which stores the manifest as ``kind="overlay"`` (meta includes
``clearing_sha256``/``partial_sha256``) and each payload as
``kind="overlay_payload"``. Then::

    store.find("overlay", static_id="0x3f1a560f")   # -> [(sha, meta), ...]
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, TypeVar

from harness_manager.core.errors import AbsentError, UsageError

log = logging.getLogger(__name__)
T = TypeVar("T")

CHUNK = 1 << 20                      # streamed hashing: 1 MiB at a time
TMP_MAX_AGE_S = 24 * 3600            # partial writes older than this are debris
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS = os.name == "nt"
# Windows only: how long to retry a PermissionError while another writer's rename of the
# same name is in flight (or a virus scanner holds the file). About 1 s in all.
_WIN_RETRY_S = (0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.3)


def _check_sha(sha256: str) -> str:
    if not isinstance(sha256, str) or not _SHA_RE.match(sha256):
        raise UsageError(f"not a sha256 digest: {sha256!r}",
                         hint="expected 64 lowercase hex characters")
    return sha256


def _check_record(kind: str, meta: dict[str, str]) -> dict[str, str]:
    if not isinstance(kind, str) or not kind:
        raise UsageError("a store entry needs a non-empty kind", hint="e.g. kind='overlay'")
    if not isinstance(meta, dict):
        raise UsageError(f"meta must be a dict of strings, got {type(meta).__name__}")
    bad = [k for k, v in meta.items() if not isinstance(k, str) or not isinstance(v, str)]
    if bad:
        raise UsageError(f"meta values must be strings; not strings: {', '.join(map(str, bad))}",
                         hint="convert numbers with str() or hex() before storing")
    return dict(meta)


def _record_key(kind: str, meta: dict[str, str]) -> str:
    canon = json.dumps({"kind": kind, "meta": meta}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


class ContentStore:
    """sha256 content-addressed store. Safe for concurrent threads and processes."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._blobs = self.root / "blobs"
        self._index = self.root / "index"
        self._tmp = self.root / "tmp"
        for d in (self._blobs, self._index, self._tmp):
            d.mkdir(parents=True, exist_ok=True)
        self._sweep_tmp()

    # -- adding --------------------------------------------------------------------

    def put_bytes(self, data: bytes, *, kind: str, meta: dict[str, str]) -> str:
        """Store ``data``; return its sha256. Idempotent for identical content."""
        meta = _check_record(kind, meta)
        with self._temp() as (tmp, fh):
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        sha = hashlib.sha256(data).hexdigest()
        self._commit_blob(tmp, sha)
        self._add_record(sha, kind, meta, len(data))
        return sha

    def put_file(self, path: Path, *, kind: str, meta: dict[str, str]) -> str:
        """Store a file's content, hashing it while copying (never read whole into memory)."""
        meta = _check_record(kind, meta)
        src = Path(path)
        if not src.is_file():
            raise AbsentError(f"no such file: {src}")
        digest = hashlib.sha256()
        size = 0
        with self._temp() as (tmp, fh), src.open("rb") as rd:
            while chunk := rd.read(CHUNK):
                digest.update(chunk)
                fh.write(chunk)
                size += len(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        sha = digest.hexdigest()
        self._commit_blob(tmp, sha)
        self._add_record(sha, kind, meta, size)
        return sha

    # -- reading -------------------------------------------------------------------

    def path(self, sha256: str) -> Path:
        """Where the blob lives. ``AbsentError`` if it is not in the store."""
        blob = self._blob_path(_check_sha(sha256))
        if not blob.is_file():
            raise AbsentError(f"blob {sha256[:12]}… is not in the store",
                              hint="add it again with put_file or put_bytes")
        return blob

    def verify(self, sha256: str) -> bool:
        """Re-hash the stored blob. False means corrupted, or missing."""
        blob = self._blob_path(_check_sha(sha256))
        try:
            return _hash_file(blob) == sha256
        except FileNotFoundError:
            return False

    def find(self, kind: str, **meta: str) -> list[tuple[str, dict[str, str]]]:
        """Records of ``kind`` whose meta has every given key=value. Blobs must exist.

        Sorted by sha256 then meta, so the order is stable.
        """
        out: list[tuple[str, dict[str, str]]] = []
        for rec in self._records():
            if rec.get("kind") != kind:
                continue
            rmeta = rec.get("meta") or {}
            if all(rmeta.get(k) == v for k, v in meta.items()):
                sha = rec.get("sha256", "")
                if _SHA_RE.match(sha) and self._blob_path(sha).is_file():
                    out.append((sha, dict(rmeta)))
        out.sort(key=lambda item: (item[0], json.dumps(item[1], sort_keys=True)))
        return out

    # -- internals -----------------------------------------------------------------

    def _blob_path(self, sha: str) -> Path:
        return self._blobs / sha[:2] / sha

    @contextmanager
    def _temp(self) -> Iterator[tuple[Path, BinaryIO]]:
        """A fresh file in tmp/. Removed if the block fails."""
        tmp = self._tmp / f"{uuid.uuid4().hex}.part"
        fh = tmp.open("xb")
        try:
            with fh:
                yield tmp, fh
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _commit_blob(self, tmp: Path, sha: str) -> None:
        dest = self._blob_path(sha)
        try:
            present = dest.is_file()
            if present and self.verify(sha):
                return                                  # deduplicated
            dest.parent.mkdir(parents=True, exist_ok=True)
            _replace(tmp, dest, repair=present)         # present but bad: replace it
        finally:
            tmp.unlink(missing_ok=True)
        if not self.verify(sha):
            # Another writer's rename can only put identical bytes here, so this is
            # real damage (disk, or someone editing the store by hand).
            raise OSError(f"blob {sha[:12]}… failed verification right after it was written")

    def _add_record(self, sha: str, kind: str, meta: dict[str, str], size: int) -> None:
        rec_dir = self._index / sha
        rec_path = rec_dir / f"{_record_key(kind, meta)}.json"
        if rec_path.is_file():
            return                                      # same record: idempotent
        rec_dir.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"sha256": sha, "kind": kind, "meta": meta, "size": size,
                           "added_at": time.time()}, sort_keys=True, indent=1)
        with self._temp() as (tmp, fh):
            fh.write(body.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            _replace(tmp, rec_path)
        finally:
            tmp.unlink(missing_ok=True)

    def _records(self) -> Iterator[dict]:
        for rec_path in sorted(self._index.glob("*/*.json")):
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("skipping unreadable store index record %s", rec_path)
                continue
            if isinstance(rec, dict):
                yield rec

    def _sweep_tmp(self) -> None:
        cutoff = time.time() - TMP_MAX_AGE_S
        for leftover in self._tmp.glob("*.part"):
            try:
                if leftover.stat().st_mtime < cutoff:
                    leftover.unlink()
            except OSError:
                pass


def _windows_retry(op: Callable[[], T]) -> T:
    """Run ``op``. On Windows, retry a ``PermissionError`` for about a second.

    Windows refuses to open a file while another writer's rename onto it is in flight,
    and refuses the rename while a reader has the file open (the concurrent-put tests hit
    both on the Windows CI runner). Both clear in milliseconds. Elsewhere a
    PermissionError is a real permission problem and is raised at once.
    """
    for delay in _WIN_RETRY_S if _WINDOWS else ():
        try:
            return op()
        except PermissionError:
            time.sleep(delay)
    return op()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _windows_retry(lambda: path.open("rb")) as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _replace(src: Path, dest: Path, *, repair: bool = False) -> None:
    """Move a finished temp file to ``dest``, tolerating a race to an identical writer.

    Every writer of ``dest`` writes the same bytes (the name is the content hash, or
    the hash of the record), so an existing ``dest`` is another writer's finished copy.

    POSIX: ``os.replace``; a reader keeps the file it opened. Windows: replacing a file
    breaks the readers of that name (see ``_windows_retry``), so an existing ``dest`` is
    left alone: ``os.rename`` there refuses to overwrite (``FileExistsError``), which
    means the other writer got there first. Only ``repair`` (``dest`` is known bad)
    replaces it.
    """
    move = os.rename if _WINDOWS and not repair else os.replace
    try:
        _windows_retry(lambda: move(src, dest))
    except FileExistsError:
        pass                            # Windows: another writer's identical copy is in place
    except PermissionError:
        if not dest.is_file():
            raise
