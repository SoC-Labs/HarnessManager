"""The MPS3 config SD as A/B by pointer (lane HUB-SD; U8; HARNESS-DIST §6.1, D3a).

**Behind ``updates.sd_ab``, off** until the 10-minute board check proves the MCC loads an
``F0FILE`` other than ``nanosoc.bit``. With it off, T7's in-place install (``sd.py``) runs
exactly as before; this module is only ``session.ab_storage``, which nothing uses then.

How the MCC finds the bitstream: ``MB/HBI0309C/board.txt`` ``APPFILE: Nanosoc\\nanosoc.txt``,
then that file's ``F0FILE: nanosoc.bit`` (the chain ``tests/fakes/t7_board._sd_bit``
follows, as the MCC does). A/B keeps two images, ``nanosoca.bit`` and ``nanosocb.bit``
(8.3 and lowercase, as fpgahub's ``mps3_msd.py`` requires), beside the legacy
``nanosoc.bit``:

- **install** writes the image ``F0FILE`` does NOT name, reads it back, then rewrites the
  ~200-byte ``nanosoc.txt`` so ``F0FILE`` names it. The running image is never touched,
  so an interrupted 12 MB write cannot darken the board: only the pointer write is risky,
  and it is one small temp-file-and-rename;
- **the backup** is the pointer itself: the ``nanosoc.txt`` it replaces, and the name and
  sha256 of the image that file names (kilobytes, not the 12 MB zip);
- **rollback** is the flip back: the recorded ``nanosoc.txt`` is written again, after
  checking the image it names is still there with the recorded sha. No 12 MB write.

The A/B view writes only the image and the pointer: a release whose other SD files differ
from the card's (``config.txt``, ``board.txt``, the rest of ``nanosoc.txt``) is refused,
with the hint to install it in place. The rails are ``sd.py``'s: the volume is the
``V2M-MPS3`` config SD (never the DAPLink drive), one write at a time under the SD's
journal, never an ``.ebf`` or an MCC command file, never a delete.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import ActionFailedError, RefusedError
from harness_manager.core.pack import BackupRecord, Progress

from .sd import JOURNAL_NAME, Mps3Storage, _copy_stream, _resolve_ci, file_sha256

VIA_AB = "ab"
AB_FORMAT = "harness-manager-sd-ab/1"
MB_DIR = "MB/HBI0309C"
BOARD_TXT = f"{MB_DIR}/board.txt"
LEGACY_IMAGE = "nanosoc.bit"
IMAGES = ("nanosoca.bit", "nanosocb.bit")
POINTER_NAME = "POINTER.json"
_APPFILE = re.compile(r"^\s*APPFILE\s*:\s*(\S+)", re.I | re.M)
_F0FILE = re.compile(r"^(\s*F0FILE\s*:\s*)(\S+)(.*)$", re.I | re.M)
_83 = re.compile(r"^[a-z0-9_~-]{1,8}\.[a-z0-9]{1,3}$")


@dataclass(frozen=True)
class Pointer:
    """The MCC's chain on this SD: ``board.txt`` -> the app note -> ``F0FILE``."""

    note_rel: str                 # "MB/HBI0309C/Nanosoc/nanosoc.txt" (the card's spelling)
    image_name: str               # "nanosoc.bit", "nanosoca.bit", ...
    image_rel: str                # "MB/HBI0309C/Nanosoc/nanosoc.bit"
    note_text: str

    @property
    def inactive(self) -> str:
        """The image an install writes: the one ``F0FILE`` does not name."""
        return IMAGES[1] if self.image_name.lower() == IMAGES[0] else IMAGES[0]

    @property
    def dir_rel(self) -> str:
        return self.note_rel.rsplit("/", 1)[0]


def read_pointer(root: Path) -> Pointer:
    """Follow ``board.txt`` -> ``APPFILE`` -> ``F0FILE`` (case-blind, as FAT is)."""
    board = _resolve_ci(root, BOARD_TXT.split("/"))
    try:
        text = board.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RefusedError(f"cannot read {BOARD_TXT} on the config SD: {exc}") from exc
    m = _APPFILE.search(text)
    if not m:
        raise RefusedError(f"{BOARD_TXT} names no APPFILE: the MCC would load nothing")
    note = _resolve_ci(root, [*MB_DIR.split("/"), *m.group(1).replace("\\", "/").split("/")])
    try:
        note_text = note.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RefusedError(f"cannot read {note} (board.txt's APPFILE): {exc}") from exc
    f0 = _F0FILE.search(note_text)
    if not f0:
        raise RefusedError(f"{note.name} names no F0FILE: the MCC would load no bitstream")
    note_rel = note.relative_to(root).as_posix()
    image = _resolve_ci(root, [*note_rel.split("/")[:-1], f0.group(2)])
    return Pointer(note_rel=note_rel, image_name=f0.group(2), note_text=note_text,
                   image_rel=image.relative_to(root).as_posix())


def patch_f0file(text: str, name: str) -> str:
    """``text`` with its ``F0FILE`` value set to ``name`` (everything else, CRLF included,
    kept byte for byte)."""
    if not _83.match(name):
        raise RefusedError(f"{name!r} is not an 8.3 lowercase file name (the MCC reads 8.3)")
    out, n = _F0FILE.subn(lambda m: f"{m.group(1)}{name}{m.group(3)}", text, count=1)
    if n != 1:
        raise RefusedError("the app note has no F0FILE line to point")
    return out


def _without_f0(text: str) -> str:
    return _F0FILE.sub(lambda m: f"{m.group(1)}*{m.group(3)}", text.replace("\r\n", "\n"))


@dataclass
class AbInstallReport:
    image: str = ""               # what was written ("MB/.../nanosocb.bit")
    pointer_from: str = ""        # the F0FILE before
    pointer_to: str = ""          # the F0FILE after
    verified: bool = False
    written: list[str] = field(default_factory=list)


class AbStorage:
    """``session.ab_storage``: the StorageAdapter view of the config SD as A/B by pointer."""

    via = VIA_AB

    def __init__(self, storage: Mps3Storage) -> None:
        self.storage = storage
        self.last_install: AbInstallReport | None = None

    # -- the plain parts --

    def locate(self) -> str:
        return self.storage.locate()

    def pending(self) -> dict[str, Any] | None:
        return self.storage.pending()

    def pointer(self) -> Pointer:
        return read_pointer(Path(self.locate()))

    # -- backup: the pointer --

    def backup(self, dest_dir: Path, progress: Progress | None = None) -> BackupRecord:
        emit: Progress = progress or (lambda phase, done, total: None)
        root = Path(self.locate())
        self.storage._refuse_if_pending(root, "back up")
        ptr = read_pointer(root)
        image = root / ptr.image_rel
        if not image.is_file():
            raise RefusedError(f"F0FILE names {ptr.image_name}, which is not on the SD: the "
                               "board would not boot it; fix the SD first")
        emit("backup", 0, 1)
        now = self.storage.env.now()
        doc = {"format": AB_FORMAT, "note_rel": ptr.note_rel, "note_text": ptr.note_text,
               "note_sha256": hashlib.sha256(ptr.note_text.encode("utf-8")).hexdigest(),
               "image_name": ptr.image_name, "image_rel": ptr.image_rel,
               "image_sha256": file_sha256(image), "created_at": now}
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        final = dest_dir / f"sd-ab-pointer-{stamp}.zip"
        n = 1
        while final.exists():
            final = dest_dir / f"sd-ab-pointer-{stamp}-{n}.zip"
            n += 1
        with zipfile.ZipFile(final, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(POINTER_NAME, json.dumps(doc, indent=1, sort_keys=True))
        sha = file_sha256(final)
        final.with_name(final.name + ".sha256").write_text(f"{sha}  {final.name}\n",
                                                           encoding="utf-8")
        emit("backup", 1, 1)
        return BackupRecord(path=str(final), sha256=sha, created_at=now, files=1,
                            volume_label=f"sd-ab:{ptr.image_name}")

    @staticmethod
    def read_backup(record: BackupRecord) -> dict[str, Any]:
        path = Path(record.path)
        if not path.is_file() or file_sha256(path) != record.sha256:
            raise RefusedError(f"pointer backup {path} is missing or fails its sha256 check")
        try:
            with zipfile.ZipFile(path) as zf:
                doc = json.loads(zf.read(POINTER_NAME))
        except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
            raise RefusedError(f"pointer backup {path} is unreadable: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("format") != AB_FORMAT:
            raise RefusedError(f"{path} is not an A/B pointer backup ({AB_FORMAT})")
        if hashlib.sha256(str(doc.get("note_text", "")).encode("utf-8")).hexdigest() != \
                doc.get("note_sha256"):
            raise RefusedError(f"pointer backup {path}: the note does not match its sha256")
        return doc

    def load_backup(self, path: Path) -> BackupRecord:
        path = Path(path)
        sha = file_sha256(path)
        sidecar = path.with_name(path.name + ".sha256")
        if sidecar.is_file():
            words = sidecar.read_text(encoding="utf-8", errors="replace").split()
            if not words or words[0].lower() != sha:
                raise RefusedError(f"pointer backup {path} does not match its .sha256 sidecar")
        rec = BackupRecord(path=str(path), sha256=sha, created_at=0.0, files=1,
                           volume_label="sd-ab")
        doc = self.read_backup(rec)
        return BackupRecord(path=str(path), sha256=sha, created_at=float(doc.get("created_at", 0)),
                            files=1, volume_label=f"sd-ab:{doc.get('image_name', '?')}")

    # -- install: the inactive image, then the pointer --

    def install(self, files: Mapping[str, Path], *, backup: BackupRecord | None,
                progress: Progress | None = None) -> None:
        emit: Progress = progress or (lambda phase, done, total: None)
        if backup is None:
            raise RefusedError("writing the configuration SD needs a verified backup of it first")
        doc = self.read_backup(backup)
        root = Path(self.locate())
        self.storage._refuse_if_pending(root, "install")
        ptr = read_pointer(root)
        if ptr.note_text != doc["note_text"] or ptr.note_rel != doc["note_rel"]:
            raise RefusedError(f"the SD's pointer changed since backup {backup.path} was taken",
                               hint="take a fresh backup, then install")
        new_bit = self._release_bit(files, ptr)
        self._refuse_other_changes(root, files, ptr)
        target_rel = f"{ptr.dir_rel}/{ptr.inactive}"
        dest = _resolve_ci(root, target_rel.split("/"))
        target_rel = dest.relative_to(root).as_posix()
        if target_rel.lower() == ptr.image_rel.lower():
            raise RefusedError("the inactive image is the running one: refusing to overwrite it")
        want = file_sha256(new_bit)
        note_new = patch_f0file(ptr.note_text, dest.name)
        report = AbInstallReport(image=target_rel, pointer_from=ptr.image_name,
                                 pointer_to=dest.name)
        journal = self.storage._new_journal("ab-install", backup, [target_rel, ptr.note_rel])
        size = new_bit.stat().st_size
        with self.storage._active(root):
            self.storage._write_journal(root, journal)
            done = 0

            def tick(n: int) -> None:
                nonlocal done
                done += n
                emit("install", done, size)

            try:
                journal["current"] = target_rel
                self.storage._write_journal(root, journal)
                with open(new_bit, "rb") as fin:
                    _copy_stream(fin.read, dest, tick)
                report.written.append(target_rel)
                if file_sha256(dest) != want:
                    # The pointer still names the running image: nothing the MCC loads changed.
                    self.storage._clear_journal(root)
                    raise ActionFailedError(f"read-back mismatch on {target_rel}: the pointer was "
                                            "NOT flipped; the board still boots its old image")
                emit("verify", size, size)
                journal["done"].append(target_rel)
                journal["current"] = ptr.note_rel
                self.storage._write_journal(root, journal)
                data = note_new.encode("utf-8")
                pos = 0

                def read(n: int) -> bytes:
                    nonlocal pos
                    chunk = data[pos:pos + n]
                    pos += len(chunk)
                    return chunk

                _copy_stream(read, root / ptr.note_rel, lambda n: None)
                report.written.append(ptr.note_rel)
                if (root / ptr.note_rel).read_bytes() != data:
                    journal["state"] = "verify-failed"
                    self.storage._write_journal(root, journal)
                    raise ActionFailedError(f"read-back mismatch on {ptr.note_rel}",
                                            hint=f"restore the pointer backup {backup.path}")
            except ActionFailedError:
                raise
            except BaseException as exc:
                journal["state"] = "interrupted"
                journal["error"] = f"{type(exc).__name__}: {exc}"
                with contextlib.suppress(OSError):
                    self.storage._write_journal(root, journal)
                raise
            report.verified = True
            self.storage._clear_journal(root)
        self.last_install = report

    @staticmethod
    def _release_bit(files: Mapping[str, Path], ptr: Pointer) -> Path:
        bits = {rel: Path(p) for rel, p in files.items() if rel.lower().endswith(".bit")}
        if len(bits) != 1:
            raise RefusedError(f"the release's SD part has {len(bits)} bitstreams; the A/B "
                               "install takes exactly one")
        return next(iter(bits.values()))

    @staticmethod
    def _refuse_other_changes(root: Path, files: Mapping[str, Path], ptr: Pointer) -> None:
        """Only the image and the pointer move: any other SD file the release changes would
        be left behind, so it is refused (install it in place instead)."""
        changed = []
        for rel, src in files.items():
            low = rel.lower()
            if low.endswith(".bit"):
                continue
            card = _resolve_ci(root, rel.replace("\\", "/").split("/"))
            if low == ptr.note_rel.lower():
                if not card.is_file() or _without_f0(card.read_text(errors="replace")) != \
                        _without_f0(Path(src).read_text(errors="replace")):
                    changed.append(rel)
                continue
            if not card.is_file() or file_sha256(card) != file_sha256(Path(src)):
                changed.append(rel)
        if changed:
            raise RefusedError(
                f"this release changes more than the bitstream ({', '.join(sorted(changed)[:4])}): "
                "the A/B install moves only the image and its pointer",
                hint="turn updates.sd_ab off for this install (it then rewrites the SD in place, "
                     "with a full backup first)")

    # -- restore: the flip back --

    def restore(self, backup: BackupRecord, progress: Progress | None = None) -> None:
        emit: Progress = progress or (lambda phase, done, total: None)
        doc = self.read_backup(backup)
        root = Path(self.locate())
        journal = self.storage._read_journal(root)
        if journal is not None and self.storage._owner_alive(root, journal):
            raise RefusedError("an SD write is in flight: never interrupt a write",
                               hint="wait for it to finish, then roll back")
        image = _resolve_ci(root, str(doc["image_rel"]).split("/"))
        if not image.is_file() or file_sha256(image) != doc["image_sha256"]:
            raise RefusedError(f"the image the pointer backup names ({doc['image_rel']}) is not on "
                               "the SD as it was: flipping back would boot something else",
                               hint="restore a full SD backup instead (the Debug USB)")
        note = _resolve_ci(root, str(doc["note_rel"]).split("/"))
        data = str(doc["note_text"]).encode("utf-8")
        pos = 0

        def read(n: int) -> bytes:
            nonlocal pos
            chunk = data[pos:pos + n]
            pos += len(chunk)
            return chunk

        emit("restore", 0, 1)
        with self.storage._active(root):
            _copy_stream(read, note, lambda n: None)
            if note.read_bytes() != data:
                raise ActionFailedError(f"read-back mismatch on {doc['note_rel']}",
                                        hint="run the rollback again")
            with contextlib.suppress(FileNotFoundError):
                (root / JOURNAL_NAME).unlink()
        emit("restore", 1, 1)


def make_ab_storage_adapter(session: Any) -> AbStorage | None:
    """The ``pack.py`` hook: the A/B view of ``session.storage`` (None without one). The
    executor uses it only when ``updates.sd_ab`` is on."""
    storage = getattr(session, "storage", None)
    return AbStorage(storage) if isinstance(storage, Mps3Storage) else None


__all__ = ["AB_FORMAT", "IMAGES", "AbInstallReport", "AbStorage", "Pointer", "VIA_AB",
           "make_ab_storage_adapter", "patch_f0file", "read_pointer"]
