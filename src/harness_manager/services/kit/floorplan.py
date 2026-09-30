"""The partition's floorplan and a build's use of it (lane UI2-API-BUILD, gap G8 (b) (c)).

- ``pblock_facts(pack, static_id)``: the partition pblock as the pack's pin model states it
  (``shells.<id>.pblock``: name, instance, SLR, clock regions, slice range, site types, IO
  sites, capacity, BRAM tiles, the reference use, each with its source), for the Build
  section's "Constraints and floorplan" panel. It is fixed by the static: a bigger or moved
  pblock is a new static (a mint).
- ``utilisation(path)``: ``build_rm.tcl``'s post-route ``<name>_util.rpt`` read back
  (``report_utilization -pblocks <RP_PBLOCK>``; when the pblock was not found the script
  reports ``-cells <rp>`` against the device). LUT, FF, BRAM tiles, DSP (and CLB, CARRY8)
  used against what the pblock offers, so Check can show the meters. Vivado's own
  ``Available`` column is the capacity when the report is against the pblock; against the
  device, the pin model's pblock capacity is used where it names one.

Board-agnostic: the pin model is read through ``services.xdc.load_pack_pins``; the report is
Vivado's. Nothing here runs Vivado. A report that cannot be read is ``None`` with nothing
guessed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: The rows Check shows, by key, and the site-type names Vivado prints for them (UltraScale
#: first; the 7-series names too, so the parser is not the MPS3's alone).
UTIL_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("LUT", ("CLB LUTs", "Slice LUTs")),
    ("FF", ("CLB Registers", "Slice Registers")),
    ("BRAM", ("Block RAM Tile",)),
    ("DSP", ("DSPs",)),
    ("CLB", ("CLB", "Slice")),
    ("CARRY8", ("CARRY8", "CARRY4")),
)
#: The pin model's capacity keys for the same rows (``pblock.capacity`` / ``bram_tiles``).
MODEL_CAPACITY = {"LUT": "LUT", "FF": "FF", "CLB": "CLB"}
#: Above this share of the pblock a row is ``high`` (the prototype's "hot" meter is > 70 %).
WARN_PCT = 70.0
HIGH_PCT = 90.0

_HEADER_KEY = re.compile(r"^\|\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.*?)\s*$")


def pblock_facts(pack: str, static_id: str) -> dict[str, Any] | None:
    """The partition pblock of ``static_id`` from the pack's pin model, or None when the
    model has no such shell (or the pack has no pin model)."""
    from harness_manager.core.errors import HarnessError
    from harness_manager.services import xdc

    if not static_id:
        return None
    try:
        pins = xdc.load_pack_pins(pack)
        shell = pins.model.shell(static_id)
    except (HarnessError, LookupError, OSError, ValueError):
        return None
    raw = shell.get("pblock") or {}
    if not raw:
        return None
    val = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in raw.items()}
    src = {k: pins.model.source_ref(v.get("src", "")) for k, v in raw.items()
           if isinstance(v, dict) and v.get("src")}
    cap = dict(val.get("capacity") or {})
    if val.get("bram_tiles") is not None:
        cap.setdefault("BRAM", val.get("bram_tiles"))
    return {
        "static_id": str(shell.get("static_id") or static_id),
        "name": val.get("name") or "",
        "rp_instance": val.get("rp_instance") or "",
        "slr": val.get("slr") or "",
        "clock_regions": list(val.get("clock_regions") or []),
        "slice_range": val.get("slice_range") or "",
        "site_types": list(val.get("site_types") or []),
        "io_sites": val.get("io_sites"),
        "snapping_mode": val.get("snapping_mode") or "",
        "exclude_placement_contain_routing": bool(val.get("exclude_placement_contain_routing")),
        "capacity": {k: cap.get(k) for k in ("LUT", "FF", "CLB", "BRAM", "DSP")},
        "reference_use": val.get("reference_use") or "",
        "rules": val.get("no_clock_or_bscan_sites") or "",
        "fixed": True,
        "fixed_why": "the partition's floorplan is part of the static: a bigger or moved pblock "
                     "is a new static (a mint), which re-keys every overlay",
        "sources": src,
    }


def _num(cell: str) -> float | int | None:
    cell = cell.strip().rstrip("*").strip()
    if not cell or cell in ("-", "_"):
        return None
    try:
        f = float(cell)
    except ValueError:
        return None
    return int(f) if f.is_integer() and "." not in cell else f


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _col(cells: list[str], cols: list[str], key: str) -> float | int | None:
    i = cols.index(key) if key in cols else -1
    return _num(cells[i]) if 0 <= i < len(cells) else None


def parse_util_report(text: str) -> dict[str, Any] | None:
    """``report_utilization``'s text: ``{command, against, pblock, design_state, device,
    rows: {key: {site_type, used, available, util_pct}}}``, or None when it holds no
    site-type table."""
    head: dict[str, str] = {}
    lines = text.splitlines()
    for line in lines[:20]:
        m = _HEADER_KEY.match(line)
        if m:
            head[m.group(1).strip().lower()] = m.group(2)
    rows: dict[str, dict[str, Any]] = {}
    cols: list[str] | None = None
    for line in lines:
        if not line.startswith("|"):
            if not line.startswith("+"):
                cols = None                   # a table ended
            continue
        cells = _cells(line)
        if cells and cells[0] == "Site Type":
            cols = [c.lower() for c in cells]
            continue
        if cols is None or "used" not in cols:
            continue
        name = cells[0].rstrip("*").strip() if cells else ""
        if not name or name in rows or line.startswith("|  "):
            continue                          # sub-rows are indented; the first table wins
        rows[name] = {"site_type": name, "used": _col(cells, cols, "used"),
                      "available": _col(cells, cols, "available"),
                      "util_pct": _col(cells, cols, "util%")}
    if not rows:
        return None
    command = head.get("command", "")
    pm = re.search(r"-pblocks\s+(?:\[get_pblocks\s+(?:-quiet\s+)?)?([A-Za-z0-9_/]+)", command)
    against = "pblock" if "-pblocks" in command else ("cells" if "-cells" in command else
                                                       "design")
    return {"command": command, "against": against, "pblock": pm.group(1) if pm else "",
            "design_state": head.get("design state", ""), "device": head.get("device", ""),
            "tool": head.get("tool version", ""), "date": head.get("date", ""), "rows": rows}


def _level(pct: float | None) -> str:
    if pct is None:
        return "unchecked"
    return "high" if pct > HIGH_PCT else ("warn" if pct > WARN_PCT else "ok")


def utilisation(path: Path, facts: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The Check section's utilisation (docs/API.md "Import a design, and the build's ..."):
    ``{path, against, pblock, design_state, rows: [{key, site_type, used, available,
    util_pct, level, capacity_from}], worst}``. ``facts`` (``pblock_facts``) supplies the
    capacity when the report is not against the pblock. None when the file is missing or is
    not a utilisation report."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rep = parse_util_report(text)
    if rep is None:
        return None
    cap = (facts or {}).get("capacity") or {}
    out_rows: list[dict[str, Any]] = []
    for key, names in UTIL_ROWS:
        row = next((rep["rows"][n] for n in names if n in rep["rows"]), None)
        if row is None:
            continue
        used, avail, pct = row["used"], row["available"], row["util_pct"]
        source = "report"
        if rep["against"] != "pblock":
            model = cap.get(key)
            if model:
                avail, pct, source = model, None, "pin model"
            else:
                source = "report (device)"
        if pct is None and used is not None and avail:
            pct = round(100.0 * float(used) / float(avail), 2)
        out_rows.append({"key": key, "site_type": row["site_type"], "used": used,
                         "available": avail, "util_pct": pct, "level": _level(pct),
                         "capacity_from": source})
    order = {"unchecked": 0, "ok": 1, "warn": 2, "high": 3}
    worst = max(out_rows, key=lambda r: (order[r["level"]], r["util_pct"] or 0.0),
                default=None)
    return {"path": str(path), "against": rep["against"],
            "pblock": rep["pblock"] or (facts or {}).get("name", ""),
            "design_state": rep["design_state"], "device": rep["device"],
            "tool": rep["tool"], "rows": out_rows,
            "worst": {"key": worst["key"], "util_pct": worst["util_pct"],
                      "level": worst["level"]} if worst else None}


def util_report_for(receipt: Any) -> Path | None:
    """Where a receipt's ``<name>_util.rpt`` is (beside it, as ``build_rm.tcl`` writes it), or
    None. The name is the receipt's ``rm_name``, never a path it carries."""
    name = str(getattr(receipt, "rm_name", "") or "")
    base = getattr(getattr(receipt, "path", None), "parent", None)
    if not name or base is None or "/" in name or "\\" in name or name in (".", ".."):
        return None
    p = Path(base) / f"{name}_util.rpt"
    return p if p.is_file() else None


def build_utilisation(receipt: Any, facts: dict[str, Any] | None = None
                      ) -> dict[str, Any] | None:
    """``utilisation`` of the report beside ``receipt``, or None."""
    p = util_report_for(receipt)
    return utilisation(p, facts) if p is not None else None
