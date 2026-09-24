"""Shared pieces of the release tool: errors, hashing, git, deterministic zips, the layout."""

from __future__ import annotations

import hashlib
import io
import posixpath
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Exit codes: the same numbers as Harness Manager's own table (core/errors.py).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ACTION_FAILED = 6
EXIT_MISMATCH = 14
EXIT_REFUSED = 15

ZIP_DATE = (2026, 1, 1, 0, 0, 0)
EXPIRES_DAYS = 180

#: A command runner: argv (no shell), optional cwd and extra env -> CompletedProcess.
Runner = Callable[..., subprocess.CompletedProcess]


class ReleaseError(Exception):
    """A release step refused or failed. ``hint`` says what to do next."""

    def __init__(self, message: str, *, hint: str = "", code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code

    def __str__(self) -> str:
        return f"{self.message}\n  hint: {self.hint}" if self.hint else self.message


def run(argv: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        check: bool = False, capture: bool = True, stdin_tty: bool = False,
        ) -> subprocess.CompletedProcess:
    """The default runner. ``stdin_tty`` leaves stdin attached (a passphrase prompt)."""
    import os

    full_env = {**os.environ, **(env or {})}
    return subprocess.run(list(argv), cwd=cwd, env=full_env, check=check, text=True,
                          stdin=None if stdin_tty else subprocess.DEVNULL,
                          capture_output=capture)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def deterministic_zip(files: dict[str, bytes]) -> bytes:
    """Same members in, same bytes out: sorted names, a fixed date, fixed modes, deflate."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[name])
    return buf.getvalue()


def tree_files(root: Path) -> dict[str, Path]:
    """Every regular file under ``root``: relative POSIX path -> path. Symlinks are refused."""
    out: dict[str, Path] = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise ReleaseError(f"{p} is a symlink: a release carries only regular files")
        if p.is_file():
            out[p.relative_to(root).as_posix()] = p
    return out


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def in_temp_dir(path: Path) -> bool:
    """True when ``path`` is under the system temp dir or /tmp (where throwaway keys live)."""
    p = Path(path).resolve()
    roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
    return any(p == r or r in p.parents for r in roots)


def write_once(path: Path, data: bytes) -> None:
    """Write a release asset. A published asset is never rewritten with different bytes."""
    if path.exists():
        if path.read_bytes() == data:
            return
        raise ReleaseError(f"{path} exists with different bytes",
                           hint="a released file is never rewritten: bump the version")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


# --- git ---------------------------------------------------------------------------------


def git(repo: Path, *args: str, runner: Runner = run) -> str:
    res = runner(["git", "-C", str(repo), *args])
    if res.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {(res.stderr or '').strip()[:200]}",
                           code=EXIT_ACTION_FAILED)
    return (res.stdout or "").strip()


def git_dirty(repo: Path, runner: Runner = run) -> list[str]:
    """Changed, staged and untracked (not ignored) paths: anything a build could pick up."""
    out = git(repo, "status", "--porcelain", "--untracked-files=all", runner=runner)
    return [ln[3:] for ln in out.splitlines() if ln.strip()]


def git_tag_commit(repo: Path, tag: str, runner: Runner = run) -> str:
    res = runner(["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"])
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def git_export(repo: Path, ref: str, dest: Path, runner: Runner = run) -> None:
    """``git archive REF`` into ``dest``: the release is built from committed files only."""
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    tar = dest.parent / f".{dest.name}.tar"
    res = runner(["git", "-C", str(repo), "archive", "--format=tar", "-o", str(tar), ref])
    if res.returncode != 0:
        raise ReleaseError(f"git archive {ref} failed: {(res.stderr or '').strip()[:200]}",
                           code=EXIT_ACTION_FAILED)
    with tarfile.open(tar) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:  # pragma: no cover - Python < 3.10.12 (git archive output is trusted anyway)
            tf.extractall(dest)
    tar.unlink()


# --- the release tree --------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Where things live, in GitHub's own URL space, so one signed channel works everywhere.

    GitHub serves a release asset at ``<owner>/<repo>/releases/download/<tag>/<file>``. The
    dry-run tree, the hub mirror and GitHub all use that layout, so the asset URLs in a
    channel are RELATIVE to ``channel.json`` (``../v0.2.0/<wheel>``) and resolve the same on
    each host. A rolling release ``channel-<catalog>-<channel>`` holds the channel files;
    per-version releases hold the assets.
    """

    root: Path                                 # dist/release, or a mirror dir
    repo: str = "SoC-Labs/HarnessManager"      # U1: the private repo whose Releases host it
    aaa_repo: str = "SoC-Labs/mps3-harness-aaa"   # U1: Arm-IP overlays, a separate private repo

    @staticmethod
    def channel_tag(catalog: str, channel: str) -> str:
        return f"channel-{catalog}-{channel}"

    def release_dir(self, tag: str, repo: str = "") -> Path:
        return self.root / (repo or self.repo) / "releases" / "download" / tag

    def channel_dir(self, catalog: str, channel: str) -> Path:
        return self.release_dir(self.channel_tag(catalog, channel))

    def channel_file(self, catalog: str, channel: str) -> Path:
        return self.channel_dir(catalog, channel) / "channel.json"

    def asset_path(self, tag: str, name: str, repo: str = "") -> Path:
        return self.release_dir(tag, repo) / name

    def rel_url(self, tag: str, name: str, repo: str = "") -> str:
        """The asset's URL relative to any channel.json in ``self.repo``."""
        start = f"{self.repo}/releases/download/_channel_"
        target = f"{repo or self.repo}/releases/download/{tag}/{name}"
        return posixpath.relpath(target, start)

    def source_template(self, catalog: str, base: str = "https://github.com/") -> str:
        """What a client passes as ``--source`` (``{channel}`` is filled in by the client)."""
        return f"{base}{self.repo}/releases/download/{self.channel_tag(catalog, '{channel}')}/channel.json"
