"""The user's settings files: ``settings.toml`` and ``boards.toml`` (lane SET-CORE; david S1).

Both live in the config directory (``config_dir``: the service's own state dir when it has
one, else ``$HARNESS_MANAGER_STATE_DIR``, else ``~/.config/harness-manager``).

- ``settings.toml`` (new): the app's settings (``[general]``, ``[tools]``, ``[updates]``, …)
  and the hubs (``[hubs.<name>]``). Always 0600: it holds no secret, but it names where
  they are.
- ``boards.toml`` (kept, as T9 made it): one table per board, which may name a hub
  (``hub.use = "<name>"``). A write keeps its mode (a new file is 0600) and copies it to
  ``boards.toml.bak-<date>`` first, once a day.

**Reading** uses ``tomllib`` (stdlib), so a reader never needs ``tomlkit``. **Writing**
edits the file in place with ``tomlkit``: comments, order and spacing a person wrote
survive. A write is atomic (a 0600 temp file, fsync, rename) and serialised by a lock file
(``.settings.lock``), so the service and a CLI cannot interleave.

**Migration: nothing moves until something is written.** The first write to
``settings.toml`` folds ``update/settings.json`` (``{channel, auto}``) into ``[updates]``
and records ``schema = 1``. Until then, ``update/settings.json`` is read as the fallback.
For one release, every write that touches ``updates.channel`` or ``updates.auto`` also
rewrites ``update/settings.json``, so a rollback to an older Harness Manager sees the
user's current choice.

A file that cannot be parsed is a problem, not a crash: reading it gives nothing (and says
why); writing to it is refused, naming the file, so a hand edit is never overwritten.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import shutil
import stat
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import HeldError, UsageError

from .policy import flatten, load_toml
from .schema import split_key

SETTINGS_FILE = "settings.toml"
BOARDS_FILE = "boards.toml"
LOCK_FILE = ".settings.lock"
LEGACY_UPDATES = ("update", "settings.json")
SCHEMA_VERSION = 1
STATE_DIR_ENV = "HARNESS_MANAGER_STATE_DIR"
BOARD_DEFAULTS = "defaults"            # [boards.defaults]: every board's defaults
_HEADER = ("Harness Manager settings. The Settings menu and `harness-manager config` edit "
           "this file;\nso can you: comments are kept. Secrets are never here (see "
           "`harness-manager config path`).")


def config_dir(state_dir: Path | str | None = None,
               env: Mapping[str, str] | None = None) -> Path:
    """The one rule for where settings live: the caller's own state dir (a service started
    with ``--state-dir``), else ``$HARNESS_MANAGER_STATE_DIR``, else ``~/.config/harness-manager``.

    ``engine.resolve_state_dir`` and its four copies (SETTINGS.md §12.6) should call this
    (SET-WIRE)."""
    if state_dir is not None:
        return Path(state_dir)
    env = os.environ if env is None else env
    raw = env.get(STATE_DIR_ENV, "")
    return Path(raw) if raw else Path.home() / ".config" / "harness-manager"


@dataclass
class UserLayer:
    """What the user's files say: canonical key -> value, and which file said it."""

    values: dict[str, Any] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    hubs: list[str] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    migrated: bool = False


class SettingsFiles:
    """Read and write the user's two files under one config directory."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = config_dir(root)

    # --- paths ---

    @property
    def settings_path(self) -> Path:
        return self.root / SETTINGS_FILE

    @property
    def boards_path(self) -> Path:
        return self.root / BOARDS_FILE

    @property
    def legacy_updates_path(self) -> Path:
        return self.root.joinpath(*LEGACY_UPDATES)

    def file_for(self, key: str) -> Path:
        return self.boards_path if split_key(key)[0] == "boards" else self.settings_path

    # --- reading (stdlib only) ---

    def read(self, *, boards: bool = True) -> UserLayer:
        """Both files (``boards=False``: settings.toml only). Never raises: a file that
        cannot be read or parsed gives nothing, and ``problems`` says why."""
        out = UserLayer()
        data = self._read_toml(self.settings_path, out.problems)
        out.migrated = data.get("schema") == SCHEMA_VERSION
        body = {k: v for k, v in data.items() if k != "schema"}
        if "boards" in body:
            out.problems.append(f"{self.settings_path}: [boards] belongs in {BOARDS_FILE}; "
                                "ignored here")
            body.pop("boards")
        for key, value in flatten(body).items():
            out.values[key] = value
            out.origin[key] = SETTINGS_FILE
        hubs = data.get("hubs")
        if isinstance(hubs, dict):
            out.hubs = sorted(k for k, v in hubs.items() if isinstance(v, dict))
        if not out.migrated:
            legacy = _read_json(self.legacy_updates_path)
            for name in ("channel", "auto"):
                value = legacy.get(name)
                key = f"updates.{name}"
                if isinstance(value, str) and value and key not in out.values:
                    out.values[key] = value
                    out.origin[key] = "/".join(LEGACY_UPDATES)
        if not boards:
            return out
        boards = self._read_toml(self.boards_path, out.problems).get("boards", {})
        if not isinstance(boards, dict):
            out.problems.append(f"{self.boards_path}: [boards] must be a table of boards")
            boards = {}
        for key, value in flatten({"boards": boards}, split_root=False).items():
            out.values[key] = value
            out.origin[key] = BOARDS_FILE
        out.boards = sorted(k for k, v in boards.items()
                            if isinstance(v, dict) and k != BOARD_DEFAULTS)
        return out

    @staticmethod
    def _read_toml(path: Path, problems: list[str]) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            problems.append(f"{path} cannot be read: {exc.strerror or exc}; its settings are "
                            "not used")
            return {}
        try:
            return load_toml(text)
        except Exception as exc:  # noqa: BLE001 - tomllib's or tomli's decode error
            problems.append(f"{path} is not valid TOML ({exc}); its settings are not used "
                            "until it is fixed")
            return {}

    # --- writing (tomlkit) ---

    def write(self, changes: Mapping[str, Any]) -> list[Path]:
        """Apply ``{key: value}`` (``None`` removes the key) to the right files, atomically
        per file, under the lock. Values are already validated (the resolver's ``set``).
        Returns the files written."""
        by_file: dict[Path, dict[tuple[str, ...], Any]] = {}
        for key, value in changes.items():
            by_file.setdefault(self.file_for(key), {})[split_key(key)] = value
        written: list[Path] = []
        with self.lock():
            for path, edits in by_file.items():
                if path == self.settings_path:
                    self._write_settings(edits)
                else:
                    self._write_boards(edits)
                written.append(path)
            touched = {k for edits in by_file.values() for k in edits}
            if touched & {("updates", "channel"), ("updates", "auto")}:
                self._mirror_legacy_updates()
                written.append(self.legacy_updates_path)
        return written

    def _write_settings(self, edits: dict[tuple[str, ...], Any]) -> None:
        import tomlkit

        path = self.settings_path
        doc = self._parse_for_write(path)
        if not len(doc) and not doc.as_string().strip():
            for line in _HEADER.splitlines():
                doc.add(tomlkit.comment(line))
            doc.add(tomlkit.nl())
        if doc.get("schema") != SCHEMA_VERSION:
            # The first write: fold the pre-settings update/settings.json into [updates].
            legacy = _read_json(self.legacy_updates_path)
            for name in ("channel", "auto"):
                value = legacy.get(name)
                parts = ("updates", name)
                if isinstance(value, str) and value and parts not in edits \
                        and not _has(doc, parts):
                    _set(doc, parts, value)
            doc["schema"] = SCHEMA_VERSION      # tomlkit keeps a top-level key above the tables
        for parts, value in edits.items():
            if parts[0] == "schema":
                raise UsageError("schema is the file's own version, not a setting")
            if value is None:
                _unset(doc, parts)
            else:
                _set(doc, parts, value)
        _atomic_write(path, doc.as_string(), 0o600)

    def _write_boards(self, edits: dict[tuple[str, ...], Any]) -> None:
        path = self.boards_path
        doc = self._parse_for_write(path)
        mode = 0o600
        if path.exists():
            with contextlib.suppress(OSError):
                mode = stat.S_IMODE(path.stat().st_mode)
            backup = path.with_name(f"{BOARDS_FILE}.bak-{_dt.date.today():%Y%m%d}")
            if not backup.exists():
                shutil.copy2(path, backup)
        for parts, value in edits.items():
            if value is None:
                _unset(doc, parts)
            else:
                _set(doc, parts, value)
        _atomic_write(path, doc.as_string(), mode)

    @staticmethod
    def _parse_for_write(path: Path) -> Any:
        import tomlkit

        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return tomlkit.document()
        try:
            return tomlkit.parse(text)
        except Exception as exc:  # noqa: BLE001 - tomlkit's ParseError and friends
            raise UsageError(f"{path} is not valid TOML ({exc}); it is not overwritten",
                             hint=f"fix or remove {path}, then try again") from None

    def _mirror_legacy_updates(self) -> None:
        """update/settings.json, as an older Harness Manager reads it (one release)."""
        data = self._read_toml(self.settings_path, [])
        updates = data.get("updates", {}) if isinstance(data.get("updates"), dict) else {}
        rec = {name: updates.get(name) if isinstance(updates.get(name), str) else ""
               for name in ("channel", "auto")}
        _atomic_write(self.legacy_updates_path,
                      json.dumps(rec, indent=1, sort_keys=True) + "\n", 0o644)

    @contextlib.contextmanager
    def lock(self, timeout_s: float = 10.0) -> Iterator[None]:
        """One writer at a time, across processes (flock / msvcrt.locking)."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / LOCK_FILE
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                try:
                    _lock_fd(fd)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise HeldError(f"another Harness Manager is writing the settings in "
                                        f"{self.root}", hint="try again in a moment") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                _unlock_fd(fd)
        finally:
            os.close(fd)


# --- helpers -----------------------------------------------------------------------------------


def _lock_fd(fd: int) -> None:
    if sys.platform.startswith("win"):  # pragma: no cover - Windows
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if sys.platform.startswith("win"):  # pragma: no cover - Windows
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Write-then-rename; the temp file has ``mode`` before it has any content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name == "posix":
            os.chmod(tmp, mode)                     # whatever the umask said
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _is_table(item: Any) -> bool:
    return isinstance(item, Mapping)


def _has(doc: Any, parts: tuple[str, ...]) -> bool:
    node = doc
    for p in parts:
        if not _is_table(node) or p not in node:
            return False
        node = node[p]
    return True


def _set(doc: Any, parts: tuple[str, ...], value: Any) -> None:
    import tomlkit
    from tomlkit.items import InlineTable

    node = doc
    for depth, p in enumerate(parts[:-1]):
        if p not in node:
            leaf_parent = depth == len(parts) - 2
            # Inside an inline table (``hub = { ... }``) only an inline table may nest
            # (SET-PACK: ``boards.<b>.hub.shares.<name>`` when the hub has no shares yet).
            node[p] = tomlkit.inline_table() if isinstance(node, InlineTable) \
                else tomlkit.table(is_super_table=not leaf_parent)
        nxt = node[p]
        if not _is_table(nxt):
            raise UsageError(f"{'.'.join(parts[:depth + 1])} is a value in the file, not a "
                             "table; it cannot hold " + ".".join(parts))
        node = nxt
    leaf = parts[-1]
    if isinstance(value, list):
        arr = tomlkit.array()
        arr.extend(value)
        value = arr
    node[leaf] = value


def _unset(doc: Any, parts: tuple[str, ...]) -> None:
    chain = [doc]
    node = doc
    for p in parts[:-1]:
        if not _is_table(node) or p not in node:
            return
        node = node[p]
        chain.append(node)
    if not _is_table(node) or parts[-1] not in node:
        return
    del node[parts[-1]]
    # Remove tables the change left empty, unless a comment in them says something.
    for depth in range(len(parts) - 1, 0, -1):
        table, parent = chain[depth], chain[depth - 1]
        if len(table) or "#" in table.as_string():
            break
        del parent[parts[depth - 1]]
