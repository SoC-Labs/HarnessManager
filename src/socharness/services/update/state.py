"""Where the update service keeps its state, and the small durable records it needs.

Layout under ``<state_dir>/update/``::

    serials.json              channel -> {serial, sha256} last accepted (anti-rollback)
    keys.json(.minisig)       the accepted key rotation (re-verified on every load)
    cache/blobs/<sha256>      downloaded assets, named by their verified hash
    cache/partial/<sha256>.part  an interrupted download (resumed with a Range request)
    work/<version>/           extracted bundle components
    backups/<board>/          config-SD backups taken before a harness install
    journal/<board>.json      the phase of a harness install in progress (resume / recover)
    installed.json            board -> the last install outcome ("installed" or "written")
    app/                      the app self-updater's venvs and switch pointer (``app.py``)

Every JSON write is write-then-rename, so a crash leaves the old or the new
file, never half of one.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from socharness.core.errors import RefusedError


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_bytes(path, (json.dumps(obj, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text) or "_"


@dataclass(frozen=True)
class UpdateState:
    root: Path

    @classmethod
    def under(cls, state_dir: Path) -> UpdateState:
        return cls(Path(state_dir) / "update")

    @property
    def serials(self) -> Path:
        return self.root / "serials.json"

    @property
    def keys_json(self) -> Path:
        return self.root / "keys.json"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    def work(self, version: str) -> Path:
        return self.root / "work" / safe_name(version)

    def backups(self, board_id: str) -> Path:
        return self.root / "backups" / safe_name(board_id)

    def journal(self, board_id: str) -> Path:
        return self.root / "journal" / f"{safe_name(board_id)}.json"

    @property
    def installed(self) -> Path:
        return self.root / "installed.json"

    @property
    def app(self) -> Path:
        return self.root / "app"


# --- anti-rollback: the last serial seen per channel -----------------------------------


class SerialStore:
    """Channel serials never go down; a serial is never reused for different content."""

    def __init__(self, state: UpdateState) -> None:
        self.path = state.serials

    def last(self, channel: str) -> tuple[int, str]:
        data = read_json(self.path, {}) or {}
        entry = data.get(channel) or {}
        try:
            return int(entry.get("serial", 0)), str(entry.get("sha256", ""))
        except (TypeError, ValueError):
            return 0, ""

    def check(self, channel: str, serial: int, sha256: str) -> None:
        last, last_sha = self.last(channel)
        if serial < last:
            raise RefusedError(
                f"the {channel!r} channel offers serial {serial}, older than serial {last} "
                "already accepted: a rollback or a stale mirror",
                hint="refusing it; wait for the mirror to catch up, or check the channel source")
        if serial == last and last_sha and sha256 != last_sha:
            raise RefusedError(
                f"the {channel!r} channel reuses serial {serial} for different content",
                hint="a published channel never changes without a new serial; refusing it")

    def accept(self, channel: str, serial: int, sha256: str) -> None:
        data = read_json(self.path, {}) or {}
        last, _ = self.last(channel)
        if serial >= last:
            data[channel] = {"serial": serial, "sha256": sha256, "accepted_at": time.time()}
            atomic_write_json(self.path, data)


# --- install records and the in-progress journal ------------------------------------


class InstallRecords:
    def __init__(self, state: UpdateState) -> None:
        self.path = state.installed

    def get(self, board_id: str) -> dict[str, Any] | None:
        data = read_json(self.path, {}) or {}
        rec = data.get(board_id)
        return rec if isinstance(rec, dict) else None

    def put(self, board_id: str, record: dict[str, Any]) -> None:
        data = read_json(self.path, {}) or {}
        data[board_id] = {**record, "recorded_at": time.time()}
        atomic_write_json(self.path, data)


class StoredComponents:
    """Host-store components already imported, by their signed sha256 (so a check can say
    "up to date" instead of offering the same overlays again)."""

    def __init__(self, state: UpdateState) -> None:
        self.path = state.root / "stored_components.json"

    def all(self) -> set[str]:
        data = read_json(self.path, {}) or {}
        return set(data) if isinstance(data, dict) else set()

    def add(self, sha256: str, name: str, version: str) -> None:
        data = read_json(self.path, {}) or {}
        if not isinstance(data, dict):
            data = {}
        data[sha256] = {"name": name, "version": version, "at": time.time()}
        atomic_write_json(self.path, data)


class Journal:
    """One harness install in progress per board: its phase and its backup."""

    def __init__(self, state: UpdateState, board_id: str) -> None:
        self.path = state.journal(board_id)
        self.board_id = board_id

    def read(self) -> dict[str, Any] | None:
        data = read_json(self.path)
        return data if isinstance(data, dict) else None

    def write(self, **fields: Any) -> dict[str, Any]:
        data = self.read() or {"board_id": self.board_id, "started_at": time.time(),
                               "pid": os.getpid(), "phases": []}
        phase = fields.get("phase")
        if phase and (not data["phases"] or data["phases"][-1] != phase):
            data["phases"].append(phase)
        data.update(fields)
        data["updated_at"] = time.time()
        atomic_write_json(self.path, data)
        return data

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

