"""The stage side of app self-update (OTA-C; david U3: notify, auto-stage, apply on click).

``app.py`` (lane OTA-L) builds a side-by-side venv from a verified wheel and a lock, and
switches the launcher pointer. This module decides WHAT to stage and hands it over
verified:

- **deps** (HM_SELF_UPDATE §4.1): a release lists the wheels no package index has
  (``mps3-pyverify``) as ``kind: dep`` artifacts. Each is downloaded and sha256-checked
  like the wheel, then the lock's line for that distribution is replaced by one that
  pins the verified local file (``mps3-pyverify @ file:///…whl --hash=sha256:…``), so the
  stage never asks an index, or a host, for it. A lock that pins another version of the
  dep, or a dep that fails its hash, refuses the stage before anything is built;
- **bad versions** (§5.4): a version whose apply failed its health check is marked bad
  (``mark_bad``, by the apply helper, lane OTA-D). It is never offered or auto-staged
  again; a NEWER current version is offered as usual. An operator can ``clear_bad``;
- **stage beside the running version** (``stage_app``): download, verify and build the
  offered version while the app runs and boards are open. It never switches and never
  touches the running venv, so it is not refused while busy; only one stage runs at a
  time (``StageLock``). OTA-D's periodic checker calls it; "Update" is then a restart.

The pointer file's own per-version ``state: "bad"`` (OTA §5.4, written by the apply
helper through ``app.py``) is honoured too, so either place of record works.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import HeldError, RefusedError
from harness_manager.core.events import Event
from harness_manager.core.session import pid_alive

from .channel import VerifiedChannel
from .download import Downloader, Progress
from .schema import CATALOG_APP, AppRelease, Asset, Channel, normalise_dist, wheel_dist
from .state import BadVersions, UpdateState, atomic_write_bytes, safe_name
from .version import compare, same_version

STATE_BAD = "bad"


# --- bad versions --------------------------------------------------------------------------


def _pointer_bad(app: Any, version: str) -> dict[str, Any] | None:
    """``current.json``'s ``versions[V].state == "bad"`` (the apply helper's own record)."""
    try:
        versions = (app.state() or {}).get("versions", {})
    except Exception:  # noqa: BLE001 - an unreadable pointer is "no record", never a crash
        return None
    for v, info in versions.items() if isinstance(versions, dict) else ():
        if isinstance(info, dict) and info.get("state") == STATE_BAD and same_version(v, version):
            return {"reason": info.get("reason") or info.get("error") or "marked bad",
                    "phase": info.get("phase", "health"), "at": info.get("at")}
    return None


def bad_reason(state: UpdateState, version: str, *, app: Any = None,
               catalog: str = CATALOG_APP) -> dict[str, Any] | None:
    """Why ``version`` is never offered again, or None."""
    return BadVersions(state).get(catalog, version) or (
        _pointer_bad(app, version) if app is not None else None)


def mark_bad(state: UpdateState, version: str, reason: str, *, phase: str = "health",
             catalog: str = CATALOG_APP) -> None:
    """Never offer ``version`` again (the apply helper calls this when its health check fails)."""
    BadVersions(state).mark(catalog, version, reason, phase=phase)


def clear_bad(state: UpdateState, version: str, *, catalog: str = CATALOG_APP) -> bool:
    return BadVersions(state).clear(catalog, version)


@dataclass(frozen=True)
class Offer:
    release: AppRelease | None             # what to offer, or None
    skipped_bad: str = ""                  # the channel's current version, skipped as bad
    why: str = ""


def offer_app(ch: Channel, running: str, state: UpdateState, *, app: Any = None) -> Offer:
    """The app release to offer: the channel's current one, when it is newer than what runs
    (``AppUpdater.offer`` decides that, when an updater is given) and not marked bad. A bad
    current version is not replaced by an older one: nothing is offered until the
    channel's current moves past it."""
    if not ch.app_current:
        return Offer(None)
    if app is not None and hasattr(app, "offer"):
        rel = app.offer(ch.app, ch.app_current)
    else:
        rel = ch.app_release(ch.app_current)
        rel = rel if rel is not None and compare(rel.version, running) > 0 else None
    if rel is None:
        return Offer(None)
    bad = bad_reason(state, rel.version, app=app)
    if bad is not None:
        return Offer(None, skipped_bad=rel.version,
                     why=f"harness-manager {rel.version} failed its {bad.get('phase', 'health')} "
                         f"check here ({bad.get('reason', '?')}); not offered again")
    return Offer(rel)


def refuse_if_bad(state: UpdateState, version: str, *, app: Any = None) -> None:
    bad = bad_reason(state, version, app=app)
    if bad is not None:
        raise RefusedError(
            f"harness-manager {version} is marked bad here: it failed its "
            f"{bad.get('phase', 'health')} check ({bad.get('reason', '?')})",
            hint="it is not offered again; a newer release will be. To try it anyway, clear "
                 "the mark (appstage.clear_bad) first")


# --- deps and the lock -------------------------------------------------------------------------


@dataclass
class PreparedApp:
    """A release's files, downloaded and verified, ready for ``AppUpdater.stage``."""

    release: AppRelease
    wheel: Path                            # the verified wheel (cache blob)
    lock: Path | None                      # the lock to build from (deps pinned locally)
    deps: dict[str, Path] = field(default_factory=dict)   # dist -> verified, PEP 427-named
    lock_name: str = ""                    # which of the release's locks it is

    def summary(self) -> dict[str, Any]:
        return {"version": self.release.version, "locked": self.lock is not None,
                "lock": self.lock_name, "deps": {d: p.name for d, p in self.deps.items()}}


_REQ_HEAD = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(==\s*([^\s;\\]+)|@)?")


def _logical_lines(text: str) -> list[list[str]]:
    """The lock as requirement blocks: a line plus its ``\\`` continuations (hash lines)."""
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in text.splitlines():
        cur.append(ln)
        if not ln.rstrip().endswith("\\"):
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def pin_deps(lock_text: str, deps: dict[str, tuple[str, Path, str]]) -> str:
    """Replace each dep's requirement in a hashed lock with a pin to the verified local wheel.

    ``deps``: normalised dist -> (version, local wheel path, sha256). A lock line that pins
    another version of the dist refuses it (the release is inconsistent). A dep the lock
    does not name is added (the lock needs it anyway under ``--require-hashes``).
    """
    out: list[str] = []
    done: set[str] = set()
    for block in _logical_lines(lock_text):
        head = block[0]
        m = _REQ_HEAD.match(head) if head and not head[0].isspace() else None
        dist = normalise_dist(m.group(1)) if m else ""
        if dist in deps:
            version, path, sha = deps[dist]
            pinned = m.group(4) if m else None
            if pinned and not same_version(pinned, version):
                raise RefusedError(
                    f"the release's lock pins {dist}=={pinned}, but its dep wheel is {version}",
                    hint="the release is inconsistent; not staging it (report it)")
            out.append(_pin_line(dist, path, sha))
            done.add(dist)
            continue
        out.extend(block)
    for dist, (_version, path, sha) in sorted(deps.items()):
        if dist not in done:
            out.append(_pin_line(dist, path, sha))
    return "\n".join(out).rstrip("\n") + "\n"


def _pin_line(dist: str, path: Path, sha: str) -> str:
    return f"{dist} @ {Path(path).resolve().as_uri()} --hash=sha256:{sha}"


def prepare_app_release(downloader: Downloader, verified: VerifiedChannel, rel: AppRelease, *,
                        wheels_dir: Path, locks_dir: Path, extras: Iterable[str] = (),
                        progress: Progress | None = None) -> PreparedApp:
    """Download and verify the wheel, the lock and every dep; pin the deps in the lock.

    ``extras``: what the install recorded (M6); the lock that covers them is used
    (``AppRelease.lock_for``). Every file is sha256-checked by the downloader before
    anything is written here; a tampered dep refuses the whole release (nothing staged).
    """
    base = verified.url
    wheel = downloader.fetch(rel.wheel, base_url=base, progress=progress)
    lock_asset = rel.lock_for(extras)
    lock = downloader.fetch(lock_asset, base_url=base, progress=progress) if lock_asset else None
    fetched: dict[str, tuple[Asset, Path]] = {}
    for dep in rel.deps:
        parsed = wheel_dist(dep.name)
        if parsed is None:                         # the schema refuses it; belt and braces
            raise RefusedError(f"dep {dep.name} is not a wheel file name")
        fetched[parsed[0]] = (dep, downloader.fetch(dep, base_url=base, progress=progress))
    if not fetched:
        return PreparedApp(rel, wheel, lock, lock_name=lock_asset.name if lock_asset else "")
    # pip/uv recognise a wheel by its PEP 427 name; the cache names blobs by hash.
    named: dict[str, Path] = {}
    pins: dict[str, tuple[str, Path, str]] = {}
    for dist, (dep, blob) in fetched.items():
        dest = Path(wheels_dir) / safe_name(dep.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file() or dest.stat().st_size != dep.size:
            tmp = dest.with_name(f".{dest.name}.tmp")
            shutil.copyfile(blob, tmp)
            tmp.replace(dest)
        named[dist] = dest
        pins[dist] = (wheel_dist(dep.name)[1], dest, dep.sha256)   # type: ignore[index]
    text = lock.read_text(encoding="utf-8") if lock is not None else ""
    pinned = Path(locks_dir) / f"{safe_name(rel.version)}.lock.txt"
    atomic_write_bytes(pinned, pin_deps(text, pins).encode("utf-8"))
    return PreparedApp(rel, wheel, pinned, named,
                       lock_name=lock_asset.name if lock_asset else "")


# --- one stage at a time -------------------------------------------------------------------


class StageLock:
    """A single-flight lock for staging (``update/app-stage.lock``): pid + host; a dead
    holder's lock is taken over."""

    def __init__(self, state: UpdateState) -> None:
        self.path = state.root / "app-stage.lock"
        self._held = False

    def holder(self) -> dict[str, Any] | None:
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return info if isinstance(info, dict) else None

    def acquire(self, what: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                info = self.holder() or {}
                pid, host = int(info.get("pid", -1) or -1), info.get("host", "")
                if host == socket.gethostname() and pid > 0 and not pid_alive(pid):
                    with contextlib.suppress(FileNotFoundError):
                        self.path.unlink()         # a dead stager: take it over
                    continue
                raise HeldError(f"cannot {what}: another stage is running "
                                f"({info.get('what', '?')}, pid {pid} on {host or '?'})",
                                hint="wait for it; it never touches the running version") \
                    from None
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "host": socket.gethostname(), "what": what,
                           "at": time.time()}, fh)
            self._held = True
            return
        raise HeldError(f"cannot {what}: the stage lock is contended")

    def release(self) -> None:
        if self._held:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            self._held = False


# --- stage beside the running version (U3) ---------------------------------------------------


def app_dirs(app: Any, state: UpdateState) -> tuple[Path, Path]:
    """Where dep wheels and pinned locks go: next to the app's own wheels (its layout,
    wherever OTA-L's installer puts it), else under the update state."""
    layout = getattr(app, "layout", None)
    if layout is not None and hasattr(layout, "wheel") and hasattr(layout, "root"):
        return Path(layout.wheel("x")).parent, Path(layout.root) / "locks"
    return state.root / "app-deps", state.root / "app-locks"


def stage_app(svc: Any, *, channel: str | None = None, source: str | None = None,
              catalog: str | None = None, version: str | None = None, auto: bool = True,
              verified: VerifiedChannel | None = None, progress: Progress | None = None,
              now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Download, verify and stage the offered app release beside the running one. NEVER
    switches. ``svc``: an ``UpdateService`` (its channel client, downloader, app updater,
    state, bus and the administrator's policy).

    ``auto`` (the background checker): stages only under the policy's ``stage`` mode
    (lane OTA-L's ``policy.py``: ``notify`` offers without staging, ``off`` does
    neither); a user's click passes ``auto=False``. ``version`` stages that release even
    if it is not current (never a bad one); otherwise the offer decides. Returns
    ``{staged, version, ...}``; ``staged: False`` with ``why`` when nothing is staged.
    """
    policy = getattr(svc, "policy", None)
    mode = getattr(policy, "self_update", "stage")
    if auto and mode != "stage":
        why = (policy.off_reason() if mode == "off" else
               f"the administrator's policy {getattr(policy, 'path', '')} says notify: "
               "an update is staged only when a user asks")
        return {"staged": False, "version": "", "channel": channel or "", "why": why}
    verified = verified or svc.fetch_channel(channel, source, catalog=catalog)
    ch = verified.channel
    app = svc.app()
    state: UpdateState = svc.state
    if version is not None:
        rel = ch.app_release(version)
        if rel is None:
            raise RefusedError(f"the {ch.channel!r} channel has no app release {version}")
        refuse_if_bad(state, rel.version, app=app)
    else:
        offer = offer_app(ch, svc.app_version, state, app=app)
        if offer.release is None:
            out: dict[str, Any] = {"staged": False, "version": "", "channel": ch.channel,
                                   "why": offer.why or f"harness-manager {svc.app_version} is "
                                                       f"current on the {ch.channel!r} channel"}
            if offer.skipped_bad:
                out["skipped_bad"] = offer.skipped_bad
            return out
        rel = offer.release
    if rel.status == "withdrawn":
        raise RefusedError(f"harness-manager {rel.version} is withdrawn by its publisher")
    lock = StageLock(state)
    lock.acquire(f"stage harness-manager {rel.version}")
    try:
        wheels_dir, locks_dir = app_dirs(app, state)
        prepared = prepare_app_release(svc.downloader, verified, rel, wheels_dir=wheels_dir,
                                       locks_dir=locks_dir, progress=progress,
                                       extras=getattr(app, "extras", ()) or ())
        info = app.stage(rel, prepared.wheel, prepared.lock)
    finally:
        lock.release()
    bus = getattr(svc, "bus", None)
    if bus is not None:
        bus.publish(Event("update.app.staged", "", {"version": rel.version,
                                                    "channel": ch.channel,
                                                    "notes": rel.notes}))
    return {"staged": True, "version": rel.version, "channel": ch.channel,
            "info": info, "prepared": prepared.summary(), "notes": rel.notes,
            "at": now()}
