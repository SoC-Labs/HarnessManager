"""The administrator's self-update policy for a shared lab machine (U6). Read-only.

Installs are per user. On a managed machine, an administrator can set limits for every
user in one file. Harness Manager only reads it, and a user's own settings cannot loosen it.

| OS | File |
|---|---|
| Linux (and other POSIX) | ``/etc/harness-manager/policy.toml`` |
| macOS | ``/Library/Application Support/harness-manager/policy.toml`` |
| Windows | ``%ProgramData%\\harness-manager\\policy.toml`` |

::

    self_update = "off"        # off | notify | stage (default stage; true = stage, false = off)
    channel = "stable"         # the only channel users may use (default: the user's choice)
    check_interval = "12h"     # how often the service checks: s/m/h/d, or seconds; 0 = never

- **``off``:** no app update is offered, staged or switched. Rollback to a version
  already on disk stays allowed.
- **``notify``:** the service says an update exists, but stages it only when a user asks.
- **``stage``:** the default (U3): notify, stage in the background, apply on a click.

The same file holds the settings tables (``[lock]``, ``[default]``, ``[hubs.<name>]``; david S3,
2026-09-25), which ``harness_manager.settings.policy`` reads. For these three settings,
``[lock]`` may name them by their settings keys too (``updates.auto``, ``updates.channel``,
``updates.check_interval``) with the same meaning; a top-level key wins over a ``[lock]``
entry that disagrees with it (a warning says so). ``updates.auto`` is a ceiling either way:
the administrator only tightens self-update.

There is no environment variable that moves or disables the file: a user could otherwise
step around it. A file that cannot be read or parsed, or a ``self_update`` or ``channel``
value that is not understood, turns self-update **off** (it fails closed) and says why. An
unknown key or a bad ``check_interval`` is only a warning.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import RefusedError

MODES = ("off", "notify", "stage")
DEFAULT_MODE = "stage"
DEFAULT_CHECK_INTERVAL_S = 6 * 3600
MIN_CHECK_INTERVAL_S = 300
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
#: The settings tables (SET-CORE): not U6 keys, and not unknown either.
SETTINGS_TABLES = ("lock", "default", "hubs")
#: A U6 key -> the settings key a ``[lock]`` entry may use for it.
LOCK_KEYS = {"self_update": "updates.auto", "channel": "updates.channel",
             "check_interval": "updates.check_interval"}


def policy_path(platform: str = sys.platform, env: dict[str, str] | None = None) -> Path:
    """Where this OS keeps the policy file."""
    env = os.environ if env is None else env
    if platform.startswith("win"):
        base = env.get("ProgramData") or env.get("PROGRAMDATA") or r"C:\ProgramData"
        return Path(base) / "harness-manager" / "policy.toml"
    if platform == "darwin":
        return Path("/Library/Application Support/harness-manager/policy.toml")
    return Path("/etc/harness-manager/policy.toml")


@dataclass(frozen=True)
class Policy:
    path: str = ""                          # "": no policy file
    self_update: str = DEFAULT_MODE         # off | notify | stage
    channel: str = ""                       # "": the user chooses
    check_interval_s: int | None = None     # None: the client's default
    problems: tuple[str, ...] = field(default_factory=tuple)   # every warning, for the UI
    why_off: str = ""                       # what failed closed ("": nothing did)

    @property
    def interval_s(self) -> int:
        """Seconds between background checks (0: never check by itself)."""
        return DEFAULT_CHECK_INTERVAL_S if self.check_interval_s is None else self.check_interval_s

    def off_reason(self) -> str:
        """Why self-update is off ("": it is not)."""
        if self.self_update != "off":
            return ""
        why = self.why_off or 'self_update = "off"'
        return f"the administrator's policy {self.path} turns self-update off ({why})"

    def channel_for(self, asked: str | None) -> str | None:
        """The channel to use: the pinned one, and a different one asked for is refused."""
        if not self.channel:
            return asked
        if asked and asked != self.channel:
            raise RefusedError(f"the administrator's policy {self.path} pins the "
                               f"{self.channel!r} channel; {asked!r} is not allowed",
                               hint=f"use --channel {self.channel}, or leave it out")
        return self.channel

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "self_update": self.self_update, "channel": self.channel,
                "check_interval_s": self.interval_s, "problems": list(self.problems)}


def parse_interval(value: Any) -> int:
    """``3600``, ``"90m"``, ``"12h"``, ``"1d"`` -> seconds. ``ValueError`` otherwise."""
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a time")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", str(value), re.IGNORECASE)
        if not m:
            raise ValueError(f"{value!r} is not a time (say 3600, 90m, 12h or 1d)")
        seconds = float(m.group(1)) * _UNITS[(m.group(2) or "s").lower()]
    if seconds < 0:
        raise ValueError(f"{value!r} is negative")
    return int(seconds)


def _load_toml(text: str) -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - Python 3.10
        import tomli as tomllib
    return tomllib.loads(text)


def parse_policy(text: str, path: str = "") -> Policy:
    try:
        data = _load_toml(text)
    except Exception as exc:  # noqa: BLE001 - tomllib.TOMLDecodeError, or tomli's
        why = f"it is not valid TOML: {exc}"
        return Policy(path=path, self_update="off", problems=(why,), why_off=why)
    mode, channel, interval = DEFAULT_MODE, "", None
    closed: list[str] = []
    warned: list[str] = []
    data = _with_locks(data, warned)
    raw = data.get("self_update", DEFAULT_MODE)
    if raw is True:
        mode = DEFAULT_MODE
    elif raw is False:
        mode = "off"
    elif isinstance(raw, str) and raw.strip().lower() in MODES:
        mode = raw.strip().lower()
    else:
        closed.append(f"self_update = {raw!r} is not one of {', '.join(MODES)}")
    raw = data.get("channel", "")
    if isinstance(raw, str) and (raw == "" or _CHANNEL_RE.match(raw)):
        channel = raw
    else:
        closed.append(f"channel = {raw!r} is not a channel name")
    if "check_interval" in data:
        try:
            interval = parse_interval(data["check_interval"])
            if 0 < interval < MIN_CHECK_INTERVAL_S:
                warned.append(f"check_interval {interval} s is below the minimum; "
                              f"{MIN_CHECK_INTERVAL_S} s is used")
                interval = MIN_CHECK_INTERVAL_S
        except ValueError as exc:
            warned.append(f"check_interval: {exc}; the default is used")
            interval = None
    for key in sorted(set(data) - {"self_update", "channel", "check_interval", *SETTINGS_TABLES}):
        warned.append(f"unknown key {key!r} is ignored")
    if closed:
        mode = "off"
    return Policy(path=path, self_update=mode, channel=channel if not closed else "",
                  check_interval_s=interval, problems=tuple(closed + warned),
                  why_off="; ".join(closed))


def _lock_entries(data: dict[str, Any]) -> dict[str, Any]:
    """``[lock]``'s entries for ``updates.*``, however they are spelled: ``[lock.updates]``,
    ``updates.channel = …`` (a dotted key) or ``"updates.channel" = …`` (a quoted one)."""
    lock = data.get("lock")
    if not isinstance(lock, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in lock.items():
        if key == "updates" and isinstance(value, dict):
            out.update({f"updates.{k}": v for k, v in value.items()})
        elif key.startswith("updates."):
            out[key] = value
    return out


def _with_locks(data: dict[str, Any], warned: list[str]) -> dict[str, Any]:
    """The U6 keys, with a ``[lock]`` entry standing in for one the file does not set."""
    locks = _lock_entries(data)
    if not locks:
        return data
    out = dict(data)
    for name, key in LOCK_KEYS.items():
        if key not in locks:
            continue
        if name not in data:
            out[name] = locks[key]
        elif data[name] != locks[key]:
            warned.append(f"[lock] {key} = {locks[key]!r} disagrees with {name} = "
                          f"{data[name]!r}; {name} is used")
    return out


def load_policy(path: Path | None = None) -> Policy:
    """The policy in force on this machine. No file: no policy (the defaults)."""
    path = Path(path) if path is not None else policy_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Policy()
    except OSError as exc:
        why = f"it cannot be read: {exc.strerror or exc}"
        return Policy(path=str(path), self_update="off", problems=(why,), why_off=why)
    return parse_policy(text, str(path))
