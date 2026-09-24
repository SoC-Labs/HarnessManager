"""Per-static probes files for fabric debug over XVC (lane XVC-CORE, X2/X5).

The RM's own ``.ltx`` travels with its overlay (``overlays.py``; the content store keeps
it once lane FIXES lands CCR X-2). Two probes files belong to the STATIC instead, so
they are kept per ``static_id`` in the state dir::

    <state_dir>/statics/<static_id>/static.ltx          the MIG calibration view (Linux)
    <state_dir>/statics/<static_id>/static.ltx.json     its sidecar (FLOW_CONTRACT.md)
    <state_dir>/statics/<static_id>/full/<rm>.ltx       a full-design file per debug RM (X5)
    <state_dir>/statics/<static_id>/full/<rm>.ltx.json  its sidecar

``static_id`` is the canonical 0x-prefixed lowercase u32 (``0x72bb0a36``). A sidecar
whose ``static_id`` is not the board's is refused (the file is for another static).
X5: the mint flow stages a full-design ``.ltx`` of each debug configuration; Harness
Manager prefers it when present (it holds the static cores AND the RM's ILAs, so one
``PROBES.FILE`` shows both), else offers the RM's ``.ltx``.

Nothing here talks to a board or a hub.
"""

from __future__ import annotations

import json
import re
import shutil
import zlib
from pathlib import Path
from typing import Any

from harness_manager.core.errors import AbsentError, RefusedError, UsageError

STATICS_DIR = "statics"
STATIC_LTX = "static.ltx"
#: What the mint's hub directory calls the static probes file (FLOW_CONTRACT.md).
MINT_STATIC_LTX = "config_rm_greybox_static.ltx"
_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def canonical_id(value: object) -> str:
    """``0x72BB0A36`` / ``1925909046`` -> ``0x72bb0a36``; UsageError otherwise."""
    try:
        n = int(str(value), 0) if not isinstance(value, int) else value
    except ValueError as exc:
        raise UsageError(f"{value!r} is not a static id (0x72BB0A36)") from exc
    if not 0 <= n <= 0xFFFFFFFF:
        raise UsageError(f"{value!r} is not a 32-bit static id")
    return f"0x{n:08x}"


def statics_root() -> Path:
    from harness_manager.engine import resolve_state_dir  # the one state-dir rule

    return resolve_state_dir() / STATICS_DIR


def crc32_of(path: Path) -> int:
    crc = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def _sidecar(path: Path) -> dict[str, Any]:
    side = path.with_name(path.name + ".json")
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _describe(path: Path, static_id: str, source: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    side = _sidecar(path)
    crc_ok: bool | None = None
    if "ltx_crc32" in side:
        try:
            crc_ok = crc32_of(path) == int(str(side["ltx_crc32"]), 0)
        except (OSError, ValueError):
            crc_ok = False
    return {"path": path, "name": path.name, "crc_ok": crc_ok, "source": source,
            "static_id": static_id, "vivado": str(side.get("vivado") or "")}


class StaticStore:
    """The per-static probes files (module docstring). ``root`` defaults to the state dir's."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else statics_root()

    def dir_for(self, static_id: object) -> Path:
        return self.root / canonical_id(static_id)

    # -- lookups (never raise for a missing file) ------------------------------------------

    def static_ltx(self, static_id: object) -> dict[str, Any] | None:
        """The static's MIG-view probes file, or None."""
        try:
            sid = canonical_id(static_id)
        except UsageError:
            return None
        return _describe(self.root / sid / STATIC_LTX, sid, "statics store")

    def full_ltx(self, static_id: object, rm_name: str) -> dict[str, Any] | None:
        """The full-design probes file for ``rm_name`` on this static (X5), or None."""
        try:
            sid = canonical_id(static_id)
        except UsageError:
            return None
        if not rm_name or not _NAME.match(rm_name):
            return None
        return _describe(self.root / sid / "full" / f"{rm_name}.ltx", sid, "statics store")

    # -- imports ----------------------------------------------------------------------------

    def put_static_ltx(self, ltx: Path, static_id: object, sidecar: Path | None = None) -> Path:
        return self._put(ltx, self.dir_for(static_id) / STATIC_LTX, static_id, sidecar)

    def put_full_ltx(self, ltx: Path, static_id: object, rm_name: str,
                     sidecar: Path | None = None) -> Path:
        if not _NAME.match(rm_name or ""):
            raise UsageError(f"{rm_name!r} is not an RM name")
        return self._put(ltx, self.dir_for(static_id) / "full" / f"{rm_name}.ltx", static_id,
                         sidecar)

    def import_mint_dir(self, directory: Path, static_id: object) -> Path:
        """Take ``config_rm_greybox_static.ltx`` (+ sidecar) from a mint's hub directory."""
        src = Path(directory) / MINT_STATIC_LTX
        if not src.is_file():
            raise AbsentError(f"no {MINT_STATIC_LTX} in {directory}",
                              hint="the static probes file is staged for mbv (Linux) mints only")
        side = src.with_name(src.name + ".json")
        return self.put_static_ltx(src, static_id, side if side.is_file() else None)

    def _put(self, ltx: Path, dest: Path, static_id: object, sidecar: Path | None) -> Path:
        sid = canonical_id(static_id)
        ltx = Path(ltx)
        if not ltx.is_file():
            raise AbsentError(f"no probes file at {ltx}")
        if sidecar is not None:
            try:
                meta = json.loads(Path(sidecar).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RefusedError(f"the sidecar {sidecar} is unreadable: {exc}") from exc
            said = meta.get("static_id") if isinstance(meta, dict) else None
            if said is not None and canonical_id(said) != sid:
                raise RefusedError(f"{ltx.name} is for static {canonical_id(said)}, not {sid}",
                                   hint="import the probes file of the mint the board runs")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ltx, dest)
        if sidecar is not None:
            shutil.copyfile(sidecar, dest.with_name(dest.name + ".json"))
        return dest
