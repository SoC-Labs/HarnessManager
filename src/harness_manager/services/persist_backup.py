"""Save a board's card contents (/persist) to this PC, and put them back (lane L3).

A Linux-harness board keeps its claim keys, identity, sensor calibration and keep-on-card
state in /persist on its user microSD. Making the card again (layout v2) wipes it. The Linux
lead's rule: HM goes over the board's own SSH, ``tar`` the folder to this PC, make the card,
``tar`` it back. The harness has no backup verb of its own.

- ``backup``: ``ssh ... tar -C /persist -czf - .`` streamed into a file under the state dir
  (``persist-backups/<board>/``) with a manifest beside it: board id, shell id, UTC time,
  sha256 of the archive and the file list with sizes.
- ``list``: the manifests for one board, newest first.
- ``restore``: check the archive against its manifest, upload it (``tar -C /persist -xzf -``),
  then list /persist again over SSH and compare with the manifest.
- ``has_recent_backup``: the guard a card re-make asks.

It uses the claim adapter's own ``ssh_argv`` (the pinned host key, the claiming key, ``-J``
the hub): nothing here edits the claim. A board this PC has not claimed is refused before
anything is sent. File contents are never read, printed or logged: only names, sizes and the
archive's sha256.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    RefusedError,
    UnavailableError,
    UnreachableError,
)
from harness_manager.core.proc import no_window

CAPABILITY = "ssh_claim"
REMOTE_DIR = "/persist"
DIR_NAME = "persist-backups"
#: A backup newer than this satisfies the re-make guard (the card changes between backups:
#: keys, calibration), so an old one asks for a fresh one.
RECENT_S = 7 * 24 * 3600
#: Time budgets for one ssh stream.
STREAM_TIMEOUT_S = 900.0
CHUNK = 64 * 1024

ProgressFn = Callable[..., None]
#: ``runner(argv, stdin_path=, sink=, on_bytes=, timeout=) -> (returncode, stderr_text)``.
#: ``stdin_path``: a file sent to the command's stdin; ``sink``: where its stdout goes;
#: ``on_bytes(n)``: called with the running count of bytes moved.
Runner = Callable[..., "tuple[int, str]"]

NOT_CLAIMED = ("this PC has not claimed the board's SSH, and backing up the card contents goes "
               "over it")
CLAIM_HINT = "claim the board first: `harness-manager board claim TARGET`"


def run_stream(argv: Sequence[str], *, stdin_path: Path | None = None,
               sink: BinaryIO | None = None, on_bytes: Callable[[int], None] | None = None,
               timeout: float = STREAM_TIMEOUT_S) -> tuple[int, str]:
    """Run one command moving bytes (a file in, or the output into ``sink``). The default
    runner; tests replace it. ``stderr`` comes back as text (the last 4 KiB)."""
    proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE if stdin_path else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **no_window())
    err: list[bytes] = []

    def drain_err() -> None:
        assert proc.stderr is not None
        for chunk in iter(lambda: proc.stderr.read(4096), b""):  # type: ignore[union-attr]
            err.append(chunk)
            del err[:-1024]                              # a bounded tail

    def feed() -> None:
        assert proc.stdin is not None
        sent = 0
        try:
            with open(stdin_path, "rb") as f:      # type: ignore[arg-type]
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    proc.stdin.write(chunk)
                    sent += len(chunk)
                    if on_bytes:
                        on_bytes(sent)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                proc.stdin.close()

    threads = [threading.Thread(target=drain_err, daemon=True)]
    if stdin_path is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        got = 0
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(CHUNK), b""):
            if sink is not None:
                sink.write(chunk)
                got += len(chunk)
                if on_bytes:
                    on_bytes(got)
        rc = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
    for t in threads:
        t.join(2.0)
    if rc < 0:
        raise UnreachableError(f"ssh did not finish within {timeout:.0f}s")
    return rc, b"".join(err).decode("utf-8", "replace")


def _safe(board_id: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", board_id).strip("._") or "board"
    return f"{stem[:40]}-{hashlib.sha256(board_id.encode('utf-8')).hexdigest()[:8]}"


def _bid(board: Any) -> str:
    if isinstance(board, str):
        return board
    cand = getattr(board, "candidate", None)
    return str(getattr(cand, "board_id", "") or "")


def _tidy(path: str) -> str:
    return path[2:] if path.startswith("./") else path.lstrip("/")


def archive_files(source: Any, mode: str = "r:*") -> list[dict[str, Any]]:
    """The regular files and links of a tar (a path or a stream-readable file) as
    ``[{path, size}]``, sorted. Only the headers are read, never a file's bytes."""
    kw: dict[str, Any] = {"name": str(source)} if isinstance(source, (str, Path)) else \
        {"fileobj": source}
    try:
        with tarfile.open(mode=mode, **kw) as tf:
            out = [{"path": _tidy(m.name), "size": int(m.size) if m.isreg() else 0}
                   for m in tf if (m.isreg() or m.issym() or m.islnk()) and _tidy(m.name)]
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ActionFailedError(f"the archive could not be read: {exc}",
                                hint="run the backup again") from exc
    return sorted(out, key=lambda e: e["path"])


def compare_files(want: Sequence[dict[str, Any]],
                  have: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Differences between the manifest's file list and what the board lists now."""
    w = {e["path"]: e["size"] for e in want}
    h = {e["path"]: e["size"] for e in have}
    missing = sorted(p for p in w if p not in h)
    changed = sorted(f"{p} (backup {w[p]} bytes, board {h[p]} bytes)"
                     for p in w if p in h and w[p] != h[p])
    extra = sorted(p for p in h if p not in w)
    return {"ok": not missing and not changed, "missing": missing, "changed": changed,
            "extra": extra}


def differences_words(diff: dict[str, Any]) -> str:
    """The comparison in plain words ("" when nothing differs)."""
    parts = []
    if diff["missing"]:
        parts.append(f"{len(diff['missing'])} file(s) from the backup are not on the board: "
                     + ", ".join(diff["missing"][:5]) + (" ..." if len(diff["missing"]) > 5 else ""))
    if diff["changed"]:
        parts.append(f"{len(diff['changed'])} file(s) differ in size: "
                     + ", ".join(diff["changed"][:5]) + (" ..." if len(diff["changed"]) > 5 else ""))
    if diff["extra"]:
        parts.append(f"{len(diff['extra'])} file(s) on the board are not in the backup "
                     "(made since): " + ", ".join(diff["extra"][:5])
                     + (" ..." if len(diff["extra"]) > 5 else ""))
    return "; ".join(parts)


class PersistBackupService:
    """``PersistBackupService(root=None, runner=run_stream)``. ``root``: the folder the
    backups live in (default: the state dir's ``persist-backups/``)."""

    def __init__(self, *, root: Path | str | None = None, runner: Runner | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._root = Path(root) if root is not None else None
        self.runner: Runner = runner or run_stream
        self._clock = clock

    # -- where ---------------------------------------------------------------------------

    @property
    def root(self) -> Path:
        if self._root is not None:
            return self._root
        from harness_manager.settings.files import config_dir  # the one state-dir rule

        return config_dir() / DIR_NAME

    def board_dir(self, board_id: str) -> Path:
        return self.root / _safe(board_id)

    # -- the claim SSH ---------------------------------------------------------------------

    @staticmethod
    def _adapter(session: Any, what: str) -> Any:
        """The claim adapter, once this PC can enter the board over its own SSH."""
        claim = getattr(session, "claim", None)
        if claim is None:
            raise UnavailableError(CAPABILITY, "this board has no SSH claim (the card contents "
                                               "backup is for the Linux harness)")
        why = claim.claimable() if hasattr(claim, "claimable") else ""
        if why:
            raise UnavailableError(CAPABILITY, why)
        from harness_manager_mps3.claim import ROUTE_BOARD_SSH

        route = claim.lock_route(what)              # raises ClaimLockedError (a refusal)
        if route != ROUTE_BOARD_SSH:
            raise RefusedError(f"{what}: {NOT_CLAIMED}; nothing was sent", hint=CLAIM_HINT)
        return claim

    @staticmethod
    def _shell_id(session: Any) -> str:
        with contextlib.suppress(Exception):
            return str(session.identity().shell_id or "")
        return ""

    def _remote(self, claim: Any, *tar_args: str) -> list[str]:
        return claim.ssh_argv(["tar", "-C", REMOTE_DIR, *tar_args])

    def _remote_files(self, claim: Any, tmp: Path) -> list[dict[str, Any]]:
        """/persist's files now (names and sizes), over the same SSH."""
        listing = tmp / "listing.tar"
        with open(listing, "wb") as out:
            rc, err = self.runner(self._remote(claim, "-cf", "-", "."), sink=out,
                                  timeout=STREAM_TIMEOUT_S)
        if rc != 0:
            raise ActionFailedError(f"the board's card contents could not be listed: "
                                    f"{_last(err)}", hint="check the board is up and claimed")
        return archive_files(listing, "r:")

    # -- backup ----------------------------------------------------------------------------

    def backup(self, session: Any, *, progress: ProgressFn | None = None) -> dict[str, Any]:
        bid = _bid(session)
        claim = self._adapter(session, "backing up the card contents")
        say = progress or (lambda *a, **k: None)
        folder = self.board_dir(bid)
        _private_dir(folder)
        now = datetime.fromtimestamp(self._clock(), timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        n = 1
        backup_id = stamp
        while (folder / f"persist-{backup_id}.tar.gz").exists():
            n += 1
            backup_id = f"{stamp}-{n}"
        archive = folder / f"persist-{backup_id}.tar.gz"
        part = archive.with_suffix(".gz.part")
        sha = hashlib.sha256()
        say("backup", 0, 0)

        class Tee:
            def __init__(self, f: BinaryIO) -> None:
                self.f = f

            def write(self, b: bytes) -> int:
                sha.update(b)
                return self.f.write(b)

        try:
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                rc, err = self.runner(
                    self._remote(claim, "-czf", "-", "."), sink=Tee(f),
                    on_bytes=lambda got: say("backup", got, 0), timeout=STREAM_TIMEOUT_S)
            if rc != 0:
                raise ActionFailedError(
                    f"the board did not hand over its card contents (ssh status {rc}): "
                    f"{_last(err)}", hint="nothing was saved; check the board is up and the "
                                          "claim works, then run the backup again")
            size = part.stat().st_size
            files = archive_files(part, "r:gz")
            os.replace(part, archive)
        finally:
            with contextlib.suppress(OSError):
                part.unlink()
        manifest = {
            "id": backup_id, "board_id": bid, "shell_id": self._shell_id(session),
            "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "created_at": now.timestamp(),
            "archive": archive.name, "size": size, "sha256": sha.hexdigest(),
            "file_count": len(files), "files": files,
        }
        _write_json(folder / f"persist-{backup_id}.json", manifest)
        say("backup", size, size)
        return manifest

    # -- list ------------------------------------------------------------------------------

    def list(self, board: Any) -> list[dict[str, Any]]:
        """This board's backups, newest first. A manifest without its archive is left out."""
        folder = self.board_dir(_bid(board))
        out: list[dict[str, Any]] = []
        for path in sorted(folder.glob("persist-*.json")) if folder.is_dir() else ():
            try:
                m = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(m, dict) and m.get("id") and (folder / str(m.get("archive"))).is_file():
                out.append(m)
        out.sort(key=lambda m: float(m.get("created_at") or 0), reverse=True)
        return out

    def get(self, board: Any, backup_id: str) -> dict[str, Any]:
        for m in self.list(board):
            if m["id"] == backup_id:
                return m
        raise AbsentError(f"no backup {backup_id!r} for this board",
                          hint="`harness-manager card backups TARGET` lists them")

    def has_recent_backup(self, board: Any, max_age_s: float = RECENT_S) -> bool:
        now = self._clock()
        return any(now - float(m.get("created_at") or 0) <= max_age_s for m in self.list(board))

    # -- restore ---------------------------------------------------------------------------

    def restore(self, session: Any, backup_id: str, *,
                progress: ProgressFn | None = None) -> dict[str, Any]:
        bid = _bid(session)
        claim = self._adapter(session, "restoring the card contents")
        say = progress or (lambda *a, **k: None)
        m = self.get(bid, backup_id)
        archive = self.board_dir(bid) / str(m["archive"])
        say("check backup", 0, 0)
        if _sha256(archive) != m.get("sha256"):
            raise ActionFailedError("the saved backup no longer matches its record "
                                    "(the file changed or is damaged); nothing was sent",
                                    hint="use another backup")
        size = archive.stat().st_size
        say("restore", 0, size)
        rc, err = self.runner(self._remote(claim, "-xzf", "-"), stdin_path=archive,
                              on_bytes=lambda sent: say("restore", sent, size),
                              timeout=STREAM_TIMEOUT_S)
        if rc != 0:
            raise ActionFailedError(
                f"the board did not take the card contents (ssh status {rc}): {_last(err)}",
                hint="the card may hold part of them; run the restore again")
        say("compare", 0, 0)
        with tempfile.TemporaryDirectory(prefix="hm-persist-") as tmp:
            have = self._remote_files(claim, Path(tmp))
        diff = compare_files(m["files"], have)
        words = differences_words(diff)
        note = (f"restored {m['file_count']} file(s); the board's list matches the backup"
                + (f" ({words})" if words else "") if diff["ok"]
                else f"restored, but the board's list does not match the backup: {words}")
        return {"board_id": bid, "backup_id": m["id"], "verified": diff["ok"],
                "differences": diff, "file_count": m["file_count"], "note": note}


def _last(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "no message"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, path)


def summary(m: dict[str, Any]) -> dict[str, Any]:
    """A manifest without its file list (the list view)."""
    return {k: v for k, v in m.items() if k != "files"}
