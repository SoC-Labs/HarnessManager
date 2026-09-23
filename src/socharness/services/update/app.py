"""App self-update: side-by-side versioned venvs, built by ``uv``, switched by a pointer.

Layout under ``<state_dir>/update/app/``::

    versions/<version>/          one venv per version (never modified once staged)
    wheels/<wheel file name>     the verified wheel under its PEP 427 name (pip needs it)
    reqs/<version>.txt           the hashed requirements the venv was built from
    current.json                 {"current": "0.2.0", "previous": "0.1.0", "versions": {...}}

The launcher shims (T11's installer writes them) read ``current.json`` and run
``versions/<current>/bin/socharness``. So:

- **staging** a new version builds a NEW venv next to the running one; the
  running process and its venv are never touched;
- **switching** rewrites ``current.json`` atomically, and only when nothing is
  busy (no board session lock held by a live process, no harness update
  journal open, no job/lease reported by an extra probe such as socharnessd's
  job table or a hub lease). The previous version stays for **rollback**;
- ``pip install git+…@branch`` remains the developer path; it bypasses all of this.

Commands (``uv`` 0.4+)::

    uv venv --python <X.Y> <versions/V>
    uv pip install --python <versions/V/bin/python> --require-hashes -r <reqs/V.txt>
    <versions/V/bin/python> -c "import socharness; print(socharness.__version__)"

The requirements pin the wheel by its signed sha256
(``socharness @ file:///…/socharness-V-py3-none-any.whl --hash=sha256:…``) plus
the release's hashed lock file when it ships one. Without a lock file the wheel
is still hash-pinned, and its dependencies come from the index (a warning).

Every subprocess goes through an injectable ``runner`` (argv list, no shell),
so the tests drive the state machine with a fake ``uv`` and no network.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from socharness import __version__
from socharness.core.errors import (
    ActionFailedError,
    AlreadyError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
)
from socharness.core.session import SessionLock, pid_alive

from .schema import AppRelease
from .state import atomic_write_bytes, atomic_write_json, read_json, safe_name
from .version import at_least, is_version, parse_version

UV_ENV = "SOCHARNESS_UV"
DIST_NAME = "socharness"
STATE_STAGING = "staging"
STATE_STAGED = "staged"
STATE_FAILED = "failed"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=1800)


class BusyProbe(Protocol):
    def reasons(self) -> list[str]:
        """Why the app cannot switch now (empty: it can)."""
        ...


@dataclass
class LocalBusyProbe:
    """Busy = a live process holds a board lock, or a harness update journal is open.

    ``extra`` probes add more reasons: socharnessd's running jobs, a hub lease.
    """

    state_dir: Path
    extra: tuple[Callable[[], list[str]], ...] = ()

    def reasons(self) -> list[str]:
        out: list[str] = []
        locks = Path(self.state_dir) / "locks"
        if locks.is_dir():
            for path in sorted(locks.glob("*.lock")):
                owner = SessionLock(path.stem, lock_dir=locks).owner()
                if owner is None:
                    continue
                local = owner.host == socket.gethostname()
                if owner.pid == os.getpid() and local:
                    out.append(f"this process holds {path.stem}")
                elif not local or pid_alive(owner.pid):
                    out.append(f"{path.stem} is in use by {owner.describe()}")
        journal_dir = Path(self.state_dir) / "update" / "journal"
        if journal_dir.is_dir():
            for j in sorted(journal_dir.glob("*.json")):
                out.append(f"a harness update of {j.stem} is unfinished "
                           "(finish it or roll it back first)")
        for probe in self.extra:
            out.extend(probe())
        return out


@dataclass(frozen=True)
class AppLayout:
    root: Path

    @property
    def pointer(self) -> Path:
        return self.root / "current.json"

    def venv(self, version: str) -> Path:
        return self.root / "versions" / safe_name(version)

    def wheel(self, name: str) -> Path:
        return self.root / "wheels" / safe_name(name)

    def reqs(self, version: str) -> Path:
        return self.root / "reqs" / f"{safe_name(version)}.txt"

    def python(self, version: str, *, windows: bool | None = None) -> Path:
        win = os.name == "nt" if windows is None else windows
        return self.venv(version) / ("Scripts/python.exe" if win else "bin/python")


_WHEEL_RE = re.compile(r"^(?P<dist>[A-Za-z0-9_.]+)-(?P<ver>[^-]+)(-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$")


def check_wheel_name(name: str, release: AppRelease) -> None:
    """The wheel is ``socharness-<release version>-…whl`` (PEP 427 file name)."""
    m = _WHEEL_RE.match(name)
    if not m:
        raise RefusedError(f"{name} is not a wheel file name")
    dist = m.group("dist").replace("-", "_").lower()
    if dist != DIST_NAME:
        raise IncompatibleError(f"{name} is a {m.group('dist')} wheel, not {DIST_NAME}")
    if parse_version(m.group("ver")) != parse_version(release.version):
        raise IncompatibleError(f"{name} is version {m.group('ver')}, the channel says "
                                f"{release.version}", hint="the release is inconsistent; not installing")


def python_satisfies(spec: str, version: tuple[int, int]) -> bool:
    """A minimal ``requires_python`` check: comma-joined ``>=``, ``>``, ``<``, ``<=``, ``==``."""
    if not spec.strip():
        return True
    have = version
    for clause in spec.split(","):
        m = re.match(r"^\s*(>=|<=|==|>|<|~=)\s*(\d+)(?:\.(\d+))?", clause)
        if not m:
            continue
        want = (int(m.group(2)), int(m.group(3) or 0))
        op = m.group(1)
        ok = {">=": have >= want, ">": have > want, "<=": have <= want, "<": have < want,
              "==": have[:2] == want, "~=": have >= want}[op]
        if not ok:
            return False
    return True


@dataclass
class AppUpdater:
    layout: AppLayout
    busy: BusyProbe
    uv: str | None = None
    runner: Runner = default_runner
    python_version: str = f"{sys.version_info.major}.{sys.version_info.minor}"
    running_version: str = __version__
    windows: bool = field(default_factory=lambda: os.name == "nt")
    now: Callable[[], float] = time.time

    # -- state --

    def state(self) -> dict[str, Any]:
        data = read_json(self.layout.pointer, {}) or {}
        return {"current": data.get("current", ""), "previous": data.get("previous", ""),
                "versions": dict(data.get("versions", {}))}

    def _save(self, st: dict[str, Any]) -> None:
        atomic_write_json(self.layout.pointer, st)

    def _mark(self, version: str, state: str, **extra: Any) -> None:
        st = self.state()
        st["versions"][version] = {**st["versions"].get(version, {}), "state": state,
                                   "at": self.now(), **extra}
        self._save(st)

    def find_uv(self) -> str:
        cand = self.uv or os.environ.get(UV_ENV, "").strip() or shutil.which("uv")
        if not cand:
            raise UnavailableError("app self-update",
                                   "needs `uv` (https://docs.astral.sh/uv/) on PATH, or $SOCHARNESS_UV")
        return cand

    # -- commands --

    def requirements(self, release: AppRelease, wheel: Path, lock: Path | None) -> str:
        lines = []
        if lock is not None:
            lines += [ln for ln in lock.read_text(encoding="utf-8").splitlines()
                      if ln.strip() and not ln.lstrip().startswith(DIST_NAME)]
        lines.append(f"{DIST_NAME} @ {Path(wheel).resolve().as_uri()} "
                     f"--hash=sha256:{release.wheel.sha256}")
        return "\n".join(lines) + "\n"

    def commands(self, release: AppRelease, reqs: Path) -> list[list[str]]:
        uv = self.find_uv()
        venv = self.layout.venv(release.version)
        py = self.layout.python(release.version, windows=self.windows)
        return [
            [uv, "venv", "--python", self.python_version, str(venv)],
            [uv, "pip", "install", "--python", str(py), "--require-hashes", "-r", str(reqs)],
            [str(py), "-c", "import socharness,sys; sys.stdout.write(socharness.__version__)"],
        ]

    # -- the state machine --

    def stage(self, release: AppRelease, wheel: Path, lock: Path | None = None) -> dict[str, Any]:
        """Build ``versions/<v>`` from the verified wheel. Never touches the running venv."""
        check_wheel_name(release.wheel.name, release)
        if release.requires_python:
            major, minor = (int(x) for x in self.python_version.split(".")[:2])
            if not python_satisfies(release.requires_python, (major, minor)):
                raise IncompatibleError(f"socharness {release.version} needs Python "
                                        f"{release.requires_python}; this is {self.python_version}")
        st = self.state()
        if st["versions"].get(release.version, {}).get("state") == STATE_STAGED and \
                self.layout.python(release.version, windows=self.windows).exists():
            return st["versions"][release.version]
        venv = self.layout.venv(release.version)
        if venv.exists() and release.version in (st["current"], st["previous"]):
            raise RefusedError(f"version {release.version} is in use; not rebuilding it")
        shutil.rmtree(venv, ignore_errors=True)
        # pip/uv recognise a wheel by its PEP 427 file name, and the download cache names
        # blobs by hash: give the verified blob its real name before pointing at it.
        named = self.layout.wheel(release.wheel.name)
        named.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wheel, named)
        reqs = self.layout.reqs(release.version)
        atomic_write_bytes(reqs, self.requirements(release, named, lock).encode("utf-8"))
        self._mark(release.version, STATE_STAGING)
        cmds = self.commands(release, reqs)
        for argv in cmds[:-1]:
            res = self.runner(argv)
            if res.returncode != 0:
                self._fail(release.version, argv, res)
        res = self.runner(cmds[-1])
        got = (res.stdout or "").strip()
        if res.returncode != 0 or not is_version(got) or \
                parse_version(got) != parse_version(release.version):
            self._fail(release.version, cmds[-1], res,
                       why=f"the new venv reports version {got or '?'}, expected {release.version}")
        self._mark(release.version, STATE_STAGED, wheel_sha256=release.wheel.sha256,
                   wheel=release.wheel.name, locked=lock is not None)
        return self.state()["versions"][release.version]

    def _fail(self, version: str, argv: Sequence[str], res: subprocess.CompletedProcess,
              why: str = "") -> None:
        shutil.rmtree(self.layout.venv(version), ignore_errors=True)
        tail = ((res.stderr or res.stdout or "").strip().splitlines() or [""])[-1][:200]
        self._mark(version, STATE_FAILED, error=why or tail)
        raise ActionFailedError(
            f"building socharness {version} failed at `{' '.join(argv[:3])} …`: {why or tail}",
            hint="the running version is untouched; retry, or report the release")

    def switch(self, version: str) -> dict[str, Any]:
        """Point the launcher at a staged version. Refused while anything is busy."""
        st = self.state()
        if st["versions"].get(version, {}).get("state") != STATE_STAGED or \
                not self.layout.python(version, windows=self.windows).exists():
            raise RefusedError(f"socharness {version} is not staged",
                               hint="run `socharness update app` to download and stage it")
        if st["current"] == version:
            raise AlreadyError(f"socharness {version} is already the current version")
        self._refuse_if_busy(f"switch to {version}")
        st["previous"], st["current"] = st["current"], version
        st["switched_at"] = self.now()
        self._save(st)
        return st

    def rollback(self) -> dict[str, Any]:
        """Switch back to the previous version (kept on disk for exactly this)."""
        st = self.state()
        prev = st["previous"]
        if not prev:
            raise RefusedError("there is no previous version to roll back to")
        if st["versions"].get(prev, {}).get("state") != STATE_STAGED or \
                not self.layout.python(prev, windows=self.windows).exists():
            raise RefusedError(f"the previous version {prev} is no longer on disk")
        self._refuse_if_busy(f"roll back to {prev}")
        st["previous"], st["current"] = st["current"], prev
        st["switched_at"] = self.now()
        self._save(st)
        return st

    def _refuse_if_busy(self, what: str) -> None:
        reasons = self.busy.reasons()
        if reasons:
            raise HeldError(f"cannot {what} now: {'; '.join(reasons)}",
                            hint="finish or close those sessions first; the new version stays staged")

    def prune(self, keep: int = 3) -> list[str]:
        """Remove old staged versions, never the current or the previous one."""
        st = self.state()
        protected = {st["current"], st["previous"]}
        staged = [v for v, info in st["versions"].items()
                  if v not in protected and info.get("state") in (STATE_STAGED, STATE_FAILED)]
        staged.sort(key=parse_version, reverse=True)
        removed = []
        for v in staged[max(0, keep - len(protected - {''})):]:
            shutil.rmtree(self.layout.venv(v), ignore_errors=True)
            wheel_name = st["versions"][v].get("wheel", "")
            if wheel_name:
                with contextlib.suppress(FileNotFoundError):
                    self.layout.wheel(wheel_name).unlink()
            with contextlib.suppress(FileNotFoundError):
                self.layout.reqs(v).unlink()
            st["versions"].pop(v, None)
            removed.append(v)
        self._save(st)
        return removed

    def offer(self, releases: Iterable[AppRelease], current: str) -> AppRelease | None:
        """The release to offer: the channel's current one, when it is newer than we run."""
        rel = next((r for r in releases if r.version == current), None)
        if rel is None or not at_least(rel.version, self.running_version) or \
                parse_version(rel.version) == parse_version(self.running_version):
            return None
        return rel
