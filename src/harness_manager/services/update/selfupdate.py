"""The app self-update's own small records, for the daemon's apply step and checker (lane OTA-D).

The design is docs/design/HM_SELF_UPDATE.md §5. This module is stdlib-only and board-agnostic:
the daemon (``daemon/update_api.py``, ``daemon/update_checker.py``), the apply helper
(``daemon/update_apply.py``, run by the OLD interpreter) and the CLI (``update status``) read
the same files.

Under ``<state_dir>/update/``::

    settings.json       the user's settings {channel, auto}: the admin policy may only tighten them
    last_check.json     what the periodic checker last saw (and what it announced)
    resume.json         0600. What a restarting daemon hands its successor: port, listen, token,
                        open boards and their PTYs. It never travels in argv (``ps`` shows argv)
    apply.json          the apply helper while it runs: {pid, from, to, phase, started_at}
    apply.log           the apply helper's log
    last_apply.json     how the last apply ended: {id, from, to, result, phase, reason, seconds}

A version whose apply failed its self-test or health check is marked bad in OTA-C's
catalogue-keyed store (``state.BadVersions``, ``<state_dir>/update/bad_versions.json``,
catalogue ``hm-app``) AND in the pointer (``current.json`` ``versions[V].state = "bad"``):
``appstage.offer_app``/``refuse_if_bad`` honour either, and the store outlives ``prune``.

The effective self-update mode is the stricter of the admin policy (``policy.py``) and the
user's ``auto`` setting, in the order ``off < notify < stage``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from harness_manager.core.errors import RefusedError, UsageError

MODES = ("off", "notify", "stage")
DEFAULT_MODE = "stage"
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


# --- small file helpers (stdlib only: the apply helper imports this) ---------------------------


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict[str, Any], *, private: bool = False) -> None:
    """Write-then-rename; ``private`` makes it 0600 before it has any content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600 if private else 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if private and os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def update_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "update"


def settings_path(state_dir: Path) -> Path:
    return update_dir(state_dir) / "settings.json"


def last_check_path(state_dir: Path) -> Path:
    return update_dir(state_dir) / "last_check.json"


def resume_path(state_dir: Path) -> Path:
    return update_dir(state_dir) / "resume.json"


def apply_record_path(state_dir: Path) -> Path:
    return update_dir(state_dir) / "apply.json"


def apply_log_path(state_dir: Path) -> Path:
    return update_dir(state_dir) / "apply.log"


def last_apply_path(state_dir: Path) -> Path:
    return update_dir(state_dir) / "last_apply.json"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# --- the user's settings, and the effective mode ----------------------------------------------


def mode_rank(mode: str) -> int:
    return MODES.index(mode) if mode in MODES else 0


@dataclass(frozen=True)
class Settings:
    channel: str = ""            # "": $HARNESS_MANAGER_UPDATE_CHANNEL, else stable
    auto: str = ""               # "": the default (stage, U3)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_settings(state_dir: Path) -> Settings:
    data = read_json(settings_path(state_dir)) or {}
    channel = data.get("channel") if isinstance(data.get("channel"), str) else ""
    auto = data.get("auto") if data.get("auto") in MODES else ""
    return Settings(channel=channel if not channel or _CHANNEL_RE.match(channel) else "",
                    auto=auto)


def save_settings(state_dir: Path, *, channel: str | None = None, auto: str | None = None,
                  policy: Any = None) -> Settings:
    """Change the user's settings. ``UsageError`` for a bad value; ``RefusedError`` for a
    channel the administrator's policy does not allow (a mode above the policy's is kept:
    it is simply capped by it)."""
    now = load_settings(state_dir)
    new_channel, new_auto = now.channel, now.auto
    if channel is not None:
        if not isinstance(channel, str) or (channel and not _CHANNEL_RE.match(channel)):
            raise UsageError(f"channel must be a channel name such as stable, beta or dev, "
                             f"not {channel!r}")
        pinned = getattr(policy, "channel", "") or ""
        if channel and pinned and channel != pinned:
            raise RefusedError(f"the administrator's policy {policy.path} pins the {pinned!r} "
                               f"channel; {channel!r} is not allowed",
                               hint=f"use {pinned}, or leave the channel empty")
        new_channel = channel
    if auto is not None:
        if auto not in ("", *MODES):
            raise UsageError(f"auto must be one of {', '.join(MODES)}, not {auto!r}")
        new_auto = auto
    out = Settings(channel=new_channel, auto=new_auto)
    write_json(settings_path(state_dir), out.as_dict())
    return out


def effective(policy: Any, settings: Settings, *, blocked: str = "") -> dict[str, Any]:
    """``{auto, channel, check_interval_s, why}``: what the checker and the UI go by.

    ``blocked`` is the updater's own reason to stay off (a developer install)."""
    admin = getattr(policy, "self_update", DEFAULT_MODE) or DEFAULT_MODE
    user = settings.auto or DEFAULT_MODE
    auto = admin if mode_rank(admin) < mode_rank(user) else user
    why = ""
    if blocked:
        auto, why = "off", blocked
    elif auto != user:
        why = (policy.off_reason() if admin == "off" else
               f"the administrator's policy {policy.path} allows at most {admin!r}")
    elif auto == "off":
        why = "your settings turn self-update off"
    return {"auto": auto, "channel": getattr(policy, "channel", "") or settings.channel,
            "check_interval_s": int(getattr(policy, "interval_s", 6 * 3600)), "why": why}


# --- is a venv in use by a running process? ---------------------------------------------------


def _proc_uses(venv: str, proc: Path, me: int) -> str:
    """A process of this user whose argv or mapped files are inside ``venv`` (Linux /proc)."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    prefix = venv.rstrip(os.sep) + os.sep
    try:
        entries = list(os.scandir(proc))
    except OSError:
        return ""
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            if uid is not None and entry.stat(follow_symlinks=False).st_uid != uid:
                continue
            raw = Path(entry.path, "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        if any(a.startswith(prefix) for a in argv):
            return f"pid {entry.name} runs {argv[0]}"
        try:
            with open(Path(entry.path, "maps"), encoding="utf-8", errors="replace") as fh:
                if any(prefix in line for line in fh):
                    return f"pid {entry.name} has files of {venv} open"
        except OSError:
            continue
    return ""


def venv_in_use(venv: Path, version: str, *, state_dir: Path | None = None,
                proc: str = "/proc", running_version: str | None = None,
                cmdline: Any = None, alive: Any = None) -> str:
    """Why ``venv`` (the venv of ``version``) must not be deleted now ("": it may be).

    Linux: any of this user's processes whose argv or memory maps name a file in it. Other
    systems (no ``/proc``): the processes this state dir knows (the daemon in ``daemon.json``,
    the apply helper in ``apply.json``), by their argv (``control._cmdline``) or, where even
    that cannot tell (Windows), by the version they run. The running process's own version
    is always in use.
    """
    venv_s = os.path.abspath(str(venv))
    here = os.path.abspath(sys.prefix)
    if venv_s == here or here.startswith(venv_s.rstrip(os.sep) + os.sep):
        return "this process runs from it"
    if running_version is not None and version and version == running_version:
        return "this process runs it"
    if os.path.isdir(proc):
        return _proc_uses(venv_s, Path(proc), os.getpid())
    if state_dir is None:
        return ""
    if cmdline is None or alive is None:
        from harness_manager.core.session import pid_alive
        from harness_manager.daemon.control import _cmdline

        cmdline = cmdline or _cmdline
        alive = alive or pid_alive
    known = []
    daemon = read_json(Path(state_dir) / "daemon.json") or {}
    helper = read_json(apply_record_path(state_dir)) or {}
    if daemon.get("pid"):
        known.append(("harness-manager-daemon", int(daemon["pid"]), str(daemon.get("version", ""))))
    if helper.get("pid"):
        known.append(("the apply helper", int(helper["pid"]), str(helper.get("python_version", ""))))
    for what, pid, ver in known:
        if pid == os.getpid() or not alive(pid):
            continue
        argv = cmdline(pid)
        if argv is not None:
            if any(os.path.abspath(a).startswith(venv_s.rstrip(os.sep) + os.sep) for a in argv
                   if os.path.isabs(a)):
                return f"{what} (pid {pid}) runs from it"
            continue
        if ver and ver == version:
            return f"{what} (pid {pid}) runs {version}"
    return ""


# --- one view for GET /update/app and `harness-manager update status` --------------------------


def status_view(svc: Any, state_dir: Path) -> dict[str, Any]:
    """``{running, pointer, versions, bad, staged, available, last_check, last_apply, policy,
    settings, effective, dev_install}`` from the files alone (no network, no daemon needed)."""
    from harness_manager import __version__

    from .schema import CATALOG_APP
    from .state import BadVersions, UpdateState
    from .version import is_version, parse_version

    app = svc.app()
    st = app.state()
    store = getattr(svc, "state", None) or UpdateState.under(state_dir)
    bad = dict(BadVersions(store).all(CATALOG_APP))
    for v, info in st["versions"].items():
        if info.get("state") == "bad" and v not in bad:
            bad[v] = {"reason": info.get("reason", ""), "phase": info.get("phase", ""),
                      "at": info.get("at")}
    # ready to apply: staged, not bad, not what the pointer runs or this process is
    staged = sorted((v for v, info in st["versions"].items()
                     if info.get("state") == "staged" and v not in bad and is_version(v)
                     and v != st["current"] and parse_version(v) != parse_version(__version__)),
                    key=parse_version, reverse=True)
    last_check = read_json(last_check_path(state_dir))
    settings = load_settings(state_dir)
    policy = svc.policy
    return {
        "running": __version__,
        "pointer": {"current": st["current"], "previous": st["previous"],
                    "installer": st.get("installer") or {}, "root": str(app.layout.root)},
        "versions": st["versions"], "bad": bad, "staged": staged,
        "available": (last_check or {}).get("available", ""),
        "last_check": last_check, "last_apply": read_json(last_apply_path(state_dir)),
        "policy": policy.as_dict() if hasattr(policy, "as_dict") else {},
        "settings": settings.as_dict(),
        "effective": effective(policy, settings, blocked=app.dev_install),
        "dev_install": app.dev_install,
    }
