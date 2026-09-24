"""App self-update: side-by-side versioned venvs, built by ``uv``, switched by a pointer.

Layout under the install root (``harness_manager._launch``; lane OTA-L moved it out of
``<state_dir>/update/app/``, and the installer migrates an older one)::

    install.json                 what the installer installed: its venv, version, extras, uv
    venv/                        the installer's venv: the version ``current: ""`` runs
    versions/<version>/          one venv per version (never modified once staged)
    wheels/<wheel file name>     the verified wheel under its PEP 427 name (pip needs it)
    reqs/<version>.txt           the hashed requirements the venv was built from
    current.json                 {"current": "0.2.0", "previous": "", "versions": {...},
                                  "installer": {"version": "0.1.0", "venv": "…/venv"}}

The installer's command (``harness_manager._launch``) reads ``current.json`` and runs
``versions/<current>``, or its own venv when ``current`` is ``""``. So:

- **staging** a new version builds a NEW venv next to the running one; the
  running process and its venv are never touched;
- **switching** rewrites ``current.json`` atomically, and only when nothing is
  busy (no board session lock held by a live process, no harness update
  journal open, no job/lease reported by an extra probe such as harness-manager-daemon's
  job table or a hub lease). The previous version stays for **rollback**, and that may be
  the installer's venv (``""``, M2);
- a developer install (``pip install -e``, a venv no installer made) never stages, switches
  or rolls back (``_launch.dev_install``); neither does an install whose administrator's
  policy turned self-update off (rollback stays allowed there);
- ``pip install git+…@branch`` remains the developer path; it bypasses all of this.

Commands (``uv`` 0.4+)::

    uv venv --python <X.Y> <versions/V>
    uv pip install --python <versions/V/bin/python> --require-hashes -r <reqs/V.txt>
    <versions/V/bin/python> -c "import harness_manager; print(harness_manager.__version__)"

The requirements pin the wheel by its signed sha256
(``harness-manager @ file:///…/harness_manager-V-py3-none-any.whl --hash=sha256:…``) plus
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
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness_manager import __version__, _launch
from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
)
from harness_manager.core.session import SessionLock, pid_alive

from .schema import CATALOG_APP, AppRelease
from .selfupdate import venv_in_use
from .state import (
    BadVersions,
    UpdateState,
    atomic_write_bytes,
    atomic_write_json,
    read_json,
    safe_name,
)
from .version import at_least, is_version, parse_version

UV_ENV = "HARNESS_MANAGER_UV"
DIST_NAME = "harness-manager"
WHEEL_DIST = DIST_NAME.replace("-", "_")   # how PEP 427 wheel file names spell it
STATE_STAGING = "staging"
STATE_STAGED = "staged"
STATE_FAILED = "failed"
#: A version whose apply failed its health check (lane OTA-D): never offered, staged or
#: switched to again. The mark also lives in OTA-C's catalogue store (``state.BadVersions``,
#: ``<state_dir>/update/bad_versions.json``), which outlives prune.
STATE_BAD = "bad"

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

    ``extra`` probes add more reasons: harness-manager-daemon's running jobs, a hub lease.
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
    """The wheel is ``harness-manager-<release version>-…whl`` (PEP 427 file name)."""
    m = _WHEEL_RE.match(name)
    if not m:
        raise RefusedError(f"{name} is not a wheel file name")
    dist = m.group("dist").replace("-", "_").lower()
    if dist != WHEEL_DIST:
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


def _norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_extras(wheel: Path) -> dict[str, set[str]]:
    """extra -> the distributions it adds, from the wheel's METADATA (``Requires-Dist``)."""
    out: dict[str, set[str]] = {}
    try:
        with zipfile.ZipFile(wheel) as zf:
            meta = next((n for n in zf.namelist() if n.endswith(".dist-info/METADATA")), "")
            text = zf.read(meta).decode("utf-8", "replace") if meta else ""
    except (OSError, zipfile.BadZipFile, KeyError):
        return out
    for line in text.splitlines():
        m = re.match(r"^Requires-Dist:\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        extra = re.search(r"""extra\s*==\s*["']([^"']+)["']""", line)
        if m and extra:
            out.setdefault(_norm_name(extra.group(1)), set()).add(_norm_name(m.group(1)))
    return out


def lock_names(lock_text: str) -> set[str]:
    """The distributions a hashed lock pins (``name==1.0 \\`` lines; hashes and options skipped)."""
    names = set()
    for line in lock_text.splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^]]*\])?\s*(==|@)", line)
        if m:
            names.add(_norm_name(m.group(1)))
    return names


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
    # M6: the extras the installer installed (install.json), kept by every update
    extras: tuple[str, ...] = ()
    # why this copy never changes the pointer: a developer install ("": it may)
    dev_install: str = ""
    # why the administrator's policy file turned self-update off ("": it did not)
    policy_off: str = ""
    # the state dir whose update/bad_versions.json (OTA-C) records bad versions (lane OTA-D)
    state_dir: Path | None = None

    @classmethod
    def for_install(cls, state_dir: Path, *, prefix: str | None = None, policy_off: str = "",
                    **kw: Any) -> AppUpdater:
        """The updater of the copy that runs: its install root (M4, M7), the installer's uv
        (M5) and extras (M6). A developer install gets the old state-dir layout, read-only."""
        dev = _launch.dev_install(prefix)
        root = None if dev else _launch.install_root(prefix)
        if root is None:
            return cls(AppLayout(UpdateState.under(state_dir).app), LocalBusyProbe(state_dir),
                       dev_install=dev or "not an installed copy", policy_off=policy_off,
                       state_dir=Path(state_dir), **kw)
        info = _launch.read_json(root / _launch.INSTALL_JSON) or {}
        uv = str(info.get("uv") or "")
        if os.environ.get(UV_ENV, "").strip() or not uv or not Path(uv).exists():
            uv = ""
        extras = tuple(e for e in info.get("extras") or () if isinstance(e, str))
        return cls(AppLayout(root), LocalBusyProbe(state_dir), uv=uv or None, extras=extras,
                   policy_off=policy_off, state_dir=Path(state_dir), **kw)

    # -- state --

    def state(self) -> dict[str, Any]:
        data = read_json(self.layout.pointer, {}) or {}
        # every other key (the installer's registration, switched_at, …) survives a save
        return {**data, "current": data.get("current", ""), "previous": data.get("previous", ""),
                "versions": dict(data.get("versions", {}))}

    def blocked(self) -> str:
        """Why this copy may not stage or switch a new version ("": it may)."""
        return self.dev_install or self.policy_off

    def guard(self, what: str, *, rollback: bool = False) -> None:
        if self.dev_install:
            raise RefusedError(f"cannot {what}: {self.dev_install}",
                               hint="developer installs never self-update")
        if self.policy_off and not rollback:
            raise RefusedError(f"cannot {what}: {self.policy_off}",
                               hint="ask this machine's administrator")

    def installer_python(self, st: dict[str, Any]) -> Path | None:
        """The installer's venv Python (M2), when the installer registered one."""
        venv = str((st.get("installer") or {}).get("venv") or "")
        return _launch.python_in(Path(venv), windows=self.windows) if venv else None

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
                                   "needs `uv` (https://docs.astral.sh/uv/) on PATH, or $HARNESS_MANAGER_UV")
        return cand

    # -- commands --

    def requirements(self, release: AppRelease, wheel: Path, lock: Path | None,
                     extras: Sequence[str] = ()) -> str:
        lines = []
        if lock is not None:
            lines += [ln for ln in lock.read_text(encoding="utf-8").splitlines()
                      if ln.strip() and not ln.lstrip().startswith(DIST_NAME)]
        spec = f"{DIST_NAME}[{','.join(extras)}]" if extras else DIST_NAME
        lines.append(f"{spec} @ {Path(wheel).resolve().as_uri()} "
                     f"--hash=sha256:{release.wheel.sha256}")
        return "\n".join(lines) + "\n"

    def kept_extras(self, wheel: Path, lock: Path | None) -> tuple[list[str], list[str]]:
        """M6: (the recorded extras the release's lock covers, the ones it does not).

        Under ``--require-hashes`` every package an extra adds must be in the lock. An extra
        the lock lacks would fail the whole stage, so it is left out and reported.
        """
        if not self.extras:
            return [], []
        wanted = sorted({_norm_name(e) for e in self.extras})
        per_extra = wheel_extras(wheel)
        pinned = lock_names(lock.read_text(encoding="utf-8")) if lock is not None else set()
        kept = [e for e in wanted if e in per_extra and per_extra[e] <= pinned]
        return kept, [e for e in wanted if e not in kept]

    def commands(self, release: AppRelease, reqs: Path) -> list[list[str]]:
        uv = self.find_uv()
        venv = self.layout.venv(release.version)
        py = self.layout.python(release.version, windows=self.windows)
        return [
            [uv, "venv", "--python", self.python_version, str(venv)],
            [uv, "pip", "install", "--python", str(py), "--require-hashes", "-r", str(reqs)],
            [str(py), "-c", "import harness_manager,sys; sys.stdout.write(harness_manager.__version__)"],
        ]

    # -- the state machine --

    def stage(self, release: AppRelease, wheel: Path, lock: Path | None = None) -> dict[str, Any]:
        """Build ``versions/<v>`` from the verified wheel. Never touches the running venv."""
        self.guard(f"stage harness-manager {release.version}")
        self._refuse_if_bad(release.version, "stage")
        check_wheel_name(release.wheel.name, release)
        if release.requires_python:
            major, minor = (int(x) for x in self.python_version.split(".")[:2])
            if not python_satisfies(release.requires_python, (major, minor)):
                raise IncompatibleError(f"harness-manager {release.version} needs Python "
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
        extras, missing = self.kept_extras(named, lock)
        atomic_write_bytes(reqs, self.requirements(release, named, lock, extras).encode("utf-8"))
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
                   wheel=release.wheel.name, locked=lock is not None, extras=extras,
                   extras_missing=missing)
        return self.state()["versions"][release.version]

    def _fail(self, version: str, argv: Sequence[str], res: subprocess.CompletedProcess,
              why: str = "") -> None:
        shutil.rmtree(self.layout.venv(version), ignore_errors=True)
        tail = ((res.stderr or res.stdout or "").strip().splitlines() or [""])[-1][:200]
        self._mark(version, STATE_FAILED, error=why or tail)
        raise ActionFailedError(
            f"building harness-manager {version} failed at `{' '.join(argv[:3])} …`: {why or tail}",
            hint="the running version is untouched; retry, or report the release")

    def switch(self, version: str) -> dict[str, Any]:
        """Point the launcher at a staged version. Refused while anything is busy."""
        self.guard(f"switch to harness-manager {version}")
        self._refuse_if_bad(version, "switch to")
        st = self.state()
        if st["versions"].get(version, {}).get("state") != STATE_STAGED or \
                not self.layout.python(version, windows=self.windows).exists():
            raise RefusedError(f"harness-manager {version} is not staged",
                               hint="run `harness-manager update app` to download and stage it")
        if st["current"] == version:
            raise AlreadyError(f"harness-manager {version} is already the current version")
        self._refuse_if_busy(f"switch to {version}")
        st["previous"], st["current"] = st["current"], version
        st["switched_at"] = self.now()
        self._save(st)
        return st

    def rollback(self) -> dict[str, Any]:
        """Switch back to the previous version (kept on disk for exactly this).

        The previous version may be the installer's venv (``""``, M2): after the first
        self-update, rollback returns to what the installer installed.
        """
        self.guard("roll the app back", rollback=True)
        st = self.state()
        prev = st["previous"]
        if not prev:
            py = self.installer_python(st)
            if not st["current"] or py is None:
                raise RefusedError("there is no previous version to roll back to")
            if not py.exists():
                raise RefusedError(f"the installed version's venv {py.parent.parent} is gone",
                                   hint="re-run the installer")
            prev_name = f"{(st.get('installer') or {}).get('version') or '?'} (installed)"
        elif self.bad(prev) is not None:
            raise RefusedError(f"the previous version {prev} is marked bad: "
                               f"{self.bad(prev).get('reason') or 'it failed its health check'}",
                               hint="update to a newer release instead")
        elif st["versions"].get(prev, {}).get("state") != STATE_STAGED or \
                not self.layout.python(prev, windows=self.windows).exists():
            raise RefusedError(f"the previous version {prev} is no longer on disk")
        else:
            prev_name = prev
        self._refuse_if_busy(f"roll back to {prev_name}")
        st["previous"], st["current"] = st["current"], prev
        st["switched_at"] = self.now()
        self._save(st)
        return st

    def _refuse_if_busy(self, what: str) -> None:
        reasons = self.busy.reasons()
        if reasons:
            raise HeldError(f"cannot {what} now: {'; '.join(reasons)}",
                            hint="finish or close those sessions first; the new version stays staged")

    def prune(self, keep: int = 3, *, state_dir: Path | None = None) -> list[str]:
        """Remove old staged versions, never the current or the previous one, and never a
        version whose venv a running process uses (a daemon, the apply helper, a CLI: lane
        OTA-D; ``selfupdate.venv_in_use``). A bad version's venv goes; its mark stays."""
        st = self.state()
        protected = {st["current"], st["previous"]}
        staged = [v for v, info in st["versions"].items()
                  if v not in protected and info.get("state") in (STATE_STAGED, STATE_FAILED,
                                                                   STATE_BAD)]
        staged.sort(key=parse_version, reverse=True)
        removed = []
        self.pruned_skipped: dict[str, str] = {}
        for v in staged[max(0, keep - len(protected - {''})):]:
            busy = venv_in_use(self.layout.venv(v), v, state_dir=state_dir,
                               running_version=self.running_version)
            if busy:
                self.pruned_skipped[v] = busy
                continue
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

    # -- bad versions (lane OTA-D) --

    def bad(self, version: str) -> dict[str, Any] | None:
        """Why ``version`` is marked bad (``{reason, phase, at}``), or None."""
        if not version:
            return None
        if self.state_dir is not None:
            mark = BadVersions(UpdateState.under(self.state_dir)).get(CATALOG_APP, version)
            if mark is not None:
                return mark
        info = self.state()["versions"].get(version) or {}
        if info.get("state") == STATE_BAD:
            return {"reason": info.get("reason") or info.get("error") or "marked bad",
                    "phase": info.get("phase", ""), "at": info.get("at")}
        return None

    def mark_bad(self, version: str, reason: str, *, phase: str = "health") -> None:
        """Never offer, stage or switch to ``version`` again (its apply failed): the pointer's
        record, and OTA-C's catalogue store when the state dir is known."""
        if self.state_dir is not None:
            BadVersions(UpdateState.under(self.state_dir)).mark(CATALOG_APP, version, reason,
                                                               phase=phase)
        if version in self.state()["versions"]:
            self._mark(version, STATE_BAD, reason=reason, phase=phase)

    def _refuse_if_bad(self, version: str, what: str) -> None:
        mark = self.bad(version)
        if mark is not None:
            raise RefusedError(f"cannot {what} harness-manager {version}: it is marked bad "
                               f"({mark.get('reason') or 'it failed its health check'})",
                               hint="a newer release will be offered when there is one")

    def offer(self, releases: Iterable[AppRelease], current: str) -> AppRelease | None:
        """The release to offer: the channel's current one, when it is newer than we run."""
        rel = next((r for r in releases if r.version == current), None)
        if rel is None or not at_least(rel.version, self.running_version) or \
                parse_version(rel.version) == parse_version(self.running_version):
            return None
        return rel
