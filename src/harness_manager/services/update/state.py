"""Where the update service keeps its state, and the small durable records it needs.

Layout under ``<state_dir>/update/``::

    serials.json              (catalog, channel) -> {serial, sha256} last accepted
                              (anti-rollback; format 2, migrated from channel-only keys)
    bad_versions.json         catalog -> version -> why it is never offered again
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

from harness_manager.core.errors import RefusedError

from .version import same_version


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


# --- anti-rollback: the last serial seen per (catalog, channel) ------------------------

#: ``serials.json`` format 2: ``{"format": 2, "catalogs": {catalog: {channel: entry}}}``.
#: Format 1 (T7) was ``{channel: entry}`` at the top level.
SERIALS_FORMAT = 2
#: Where T7's channel-only serials go: before catalogues the one channel a client read was
#: the MPS3 platform channel (``schema.LEGACY_CATALOG``; imported lazily, no cycle).
_LEGACY_CATALOG = "mps3-harness"


def _is_serial_entry(v: Any) -> bool:
    return isinstance(v, dict) and "serial" in v


class SerialStore:
    """Channel serials never go down; a serial is never reused for different content.

    Keyed by (catalogue, channel) (HM_SELF_UPDATE §4.6), so the app catalogue and a
    board pack's harness catalogue can both have a ``stable`` channel without one's
    serial refusing the other's. A T7 file (channel-only keys) is read as the MPS3
    harness catalogue's history and rewritten in format 2 on the next accept.
    """

    def __init__(self, state: UpdateState, *, legacy_catalog: str = _LEGACY_CATALOG) -> None:
        self.path = state.serials
        self.legacy_catalog = legacy_catalog

    def load(self) -> dict[str, dict[str, dict[str, Any]]]:
        """catalog -> channel -> entry, whatever format is on disk (format 1 migrated)."""
        data = read_json(self.path, {}) or {}
        if not isinstance(data, dict):
            return {}
        if data.get("format") == SERIALS_FORMAT:
            cats = data.get("catalogs") or {}
            return {c: {n: e for n, e in chans.items() if _is_serial_entry(e)}
                    for c, chans in cats.items() if isinstance(chans, dict)} \
                if isinstance(cats, dict) else {}
        legacy = {n: e for n, e in data.items() if _is_serial_entry(e)}
        return {self.legacy_catalog: legacy} if legacy else {}

    def _save(self, cats: dict[str, dict[str, dict[str, Any]]]) -> None:
        old = read_json(self.path, {}) or {}
        doc: dict[str, Any] = {"format": SERIALS_FORMAT, "catalogs": cats}
        if isinstance(old, dict) and old.get("format") == SERIALS_FORMAT:
            if "migrated" in old:
                doc["migrated"] = old["migrated"]
        elif isinstance(old, dict) and any(_is_serial_entry(v) for v in old.values()):
            doc["migrated"] = {"from_format": 1, "to_catalog": self.legacy_catalog,
                               "at": time.time()}
        atomic_write_json(self.path, doc)

    def migrate(self) -> bool:
        """Rewrite a format-1 file as format 2 now (``accept`` does it anyway). True if it did."""
        data = read_json(self.path, {}) or {}
        if not isinstance(data, dict) or data.get("format") == SERIALS_FORMAT or not data:
            return False
        self._save(self.load())
        return True

    def last(self, channel: str, catalog: str | None = None) -> tuple[int, str]:
        entry = self.load().get(catalog or self.legacy_catalog, {}).get(channel) or {}
        try:
            return int(entry.get("serial", 0)), str(entry.get("sha256", ""))
        except (TypeError, ValueError):
            return 0, ""

    def check(self, channel: str, serial: int, sha256: str, catalog: str | None = None) -> None:
        last, last_sha = self.last(channel, catalog)
        what = f"the {channel!r} channel" + (f" of the {catalog!r} catalogue" if catalog else "")
        if serial < last:
            raise RefusedError(
                f"{what} offers serial {serial}, older than serial {last} "
                "already accepted: a rollback or a stale mirror",
                hint="refusing it; wait for the mirror to catch up, or check the channel source")
        if serial == last and last_sha and sha256 != last_sha:
            raise RefusedError(
                f"{what} reuses serial {serial} for different content",
                hint="a published channel never changes without a new serial; refusing it")

    def accept(self, channel: str, serial: int, sha256: str, catalog: str | None = None) -> None:
        cats = self.load()
        last, _ = self.last(channel, catalog)
        if serial >= last:
            cats.setdefault(catalog or self.legacy_catalog, {})[channel] = {
                "serial": serial, "sha256": sha256, "accepted_at": time.time()}
            self._save(cats)


# --- versions never offered again (a failed health check) -------------------------------


class BadVersions:
    """Versions marked bad: never offered, auto-staged or installed by default again.

    OTA §5.4: when a new app version fails its health check after an apply, the apply
    helper (lane OTA-D) marks it here and rolls back; ``offer`` then skips it until the
    channel's current version is a newer one. Keyed by catalogue (``hm-app``, or a
    harness catalogue), so a harness version and an app version never collide.
    """

    def __init__(self, state: UpdateState) -> None:
        self.path = state.root / "bad_versions.json"

    def all(self, catalog: str) -> dict[str, dict[str, Any]]:
        data = read_json(self.path, {}) or {}
        cat = data.get(catalog) if isinstance(data, dict) else None
        return {v: e for v, e in cat.items() if isinstance(e, dict)} \
            if isinstance(cat, dict) else {}

    def get(self, catalog: str, version: str) -> dict[str, Any] | None:
        return next((e for v, e in self.all(catalog).items() if same_version(v, version)), None)

    def is_bad(self, catalog: str, version: str) -> bool:
        return self.get(catalog, version) is not None

    def mark(self, catalog: str, version: str, reason: str, *, phase: str = "health") -> None:
        data = read_json(self.path, {}) or {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault(catalog, {})[version] = {"reason": reason, "phase": phase,
                                                 "at": time.time()}
        atomic_write_json(self.path, data)

    def clear(self, catalog: str, version: str) -> bool:
        """Offer ``version`` again (an operator decided the failure was not the release's)."""
        data = read_json(self.path, {}) or {}
        cat = data.get(catalog) if isinstance(data, dict) else None
        if not isinstance(cat, dict):
            return False
        hit = [v for v in cat if same_version(v, version)]
        for v in hit:
            del cat[v]
        if hit:
            atomic_write_json(self.path, data)
        return bool(hit)


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

