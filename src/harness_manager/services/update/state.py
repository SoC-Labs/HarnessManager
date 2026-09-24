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
    history/<board>.jsonl     the last ``HISTORY_KEEP`` installs of a board, oldest first
                              (HARNESS-CAT; "roll back to previous" reads it)
    pins.json                 board -> the harness release it is pinned to (HARNESS-CAT; a
                              per-board pin, never a channel)
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

    def history(self, board_id: str) -> Path:
        return self.root / "history" / f"{safe_name(board_id)}.jsonl"

    @property
    def pins(self) -> Path:
        return self.root / "pins.json"

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


#: How many installs a board's history keeps (HARNESS-CAT). The oldest go first.
HISTORY_KEEP = 20


class InstallRecords:
    """What was installed on each board.

    ``installed.json`` keeps each board's LAST record (T7's shape, which the rollback reads);
    ``history/<board>.jsonl`` keeps the last ``keep`` records, one JSON object per line,
    oldest first (HARNESS-CAT: the catalogue's history and its rollback candidates). Every
    ``put`` goes to both. A record names at least ``version`` and ``result``; the executor
    adds ``kind`` (install | overlays | restore | recovered), ``from_version`` (the release
    the board ran before), ``static_id``, ``fw_sha``, ``doors``, ``backup`` and ``detail``.
    """

    def __init__(self, state: UpdateState, *, keep: int = HISTORY_KEEP) -> None:
        self.state = state
        self.path = state.installed
        self.keep = max(1, int(keep))

    def get(self, board_id: str) -> dict[str, Any] | None:
        data = read_json(self.path, {}) or {}
        rec = data.get(board_id)
        return rec if isinstance(rec, dict) else None

    def put(self, board_id: str, record: dict[str, Any]) -> None:
        data = read_json(self.path, {}) or {}
        entry = {**record, "recorded_at": time.time()}
        data[board_id] = entry
        atomic_write_json(self.path, data)
        self._append(board_id, entry)

    def history(self, board_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """The board's installs, NEWEST first (at most ``keep``; ``limit`` cuts it shorter)."""
        rows = self._read(board_id)
        if not rows:
            last = self.get(board_id)          # a T7 record from before the history existed
            rows = [last] if last else []
        rows.reverse()
        return rows[:limit] if limit is not None else rows

    def _read(self, board_id: str) -> list[dict[str, Any]]:
        try:
            text = self.state.history(board_id).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return []
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue                        # a torn line (a crash mid-write): skip it
            if isinstance(row, dict):
                out.append(row)
        return out

    def _append(self, board_id: str, entry: dict[str, Any]) -> None:
        rows = [*self._read(board_id), entry][-self.keep:]
        body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
        atomic_write_bytes(self.state.history(board_id), body.encode("utf-8"))


class Pins:
    """Per-board pins (HARNESS-CAT; HARNESS-DIST §4.2): the harness release a board stays on.

    A pin is HM state, never a channel: ``pins.json`` maps a board id to ``{version,
    catalog, by, at}``. The planner never OFFERS a release past a board's pin (an explicit
    ``--version`` is the user's own choice and still plans).
    """

    def __init__(self, state: UpdateState) -> None:
        self.path = state.pins

    def all(self) -> dict[str, dict[str, Any]]:
        data = read_json(self.path, {}) or {}
        return {b: p for b, p in data.items() if isinstance(p, dict) and p.get("version")} \
            if isinstance(data, dict) else {}

    def get(self, board_id: str, catalog: str | None = None) -> dict[str, Any] | None:
        pin = self.all().get(board_id)
        if pin is None or (catalog and pin.get("catalog") and pin["catalog"] != catalog):
            return None
        return pin

    def set(self, board_id: str, version: str, *, catalog: str = "",
            by: str = "user") -> dict[str, Any] | None:
        """Pin ``board_id`` to ``version``. Returns the pin it replaced, if any."""
        data = read_json(self.path, {}) or {}
        if not isinstance(data, dict):
            data = {}
        before = data.get(board_id) if isinstance(data.get(board_id), dict) else None
        data[board_id] = {"version": version, "catalog": catalog, "by": by, "at": time.time()}
        atomic_write_json(self.path, data)
        return before

    def clear(self, board_id: str) -> dict[str, Any] | None:
        """Unpin. Returns the pin it removed, or None when there was none."""
        data = read_json(self.path, {}) or {}
        if not isinstance(data, dict) or board_id not in data:
            return None
        before = data.pop(board_id)
        atomic_write_json(self.path, data)
        return before if isinstance(before, dict) else None


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

