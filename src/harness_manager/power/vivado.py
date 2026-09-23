"""Vivado ``report_power`` text reports -> labelled ESTIMATE readings.

A routed power report is a *model*, not a measurement: it is vectorless unless
someone fed it simulation activity, and on the MPS3 builds its overall
confidence is "Low". So every reading here:

- has ``source="estimate:vivado <rm>"``;
- has a name ending in ``_estimate``;
- has a ``reason`` that starts with ``ESTIMATE`` and names the confidence level.

Finding reports: ``find_reports(dir)`` walks a build directory for ``*.rpt``
files that really are ``report_power`` output (by content, not by name) and
names each after the partition design it describes, from the file name:

- ``power_rm_nanosoc.rpt`` (the per-RM batch the research recommends) -> ``nanosoc``
- ``config_rm_led_power_routed.rpt`` -> ``led``
- ``shell_top_power_routed.rpt`` (the shell impl run today) -> ``shell_top``

When two reports name the same design, the newest file wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from harness_manager.core.model import Reading

_SECTION_RE = re.compile(r"^\d+(\.\d+)*\.?\s+\S")   # "1. Summary", "1.2 Power Supply Summary"
_HEADER_RE = re.compile(r"^\|\s*(?P<key>[A-Za-z ]+?)\s*:\s*(?P<val>.*)$")
MARKER = "Total On-Chip Power (W)"


class NotAPowerReport(ValueError):
    pass


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class PowerEstimate:
    rm: str
    path: Path | None
    total_w: float | None
    dynamic_w: float | None
    static_w: float | None
    junction_c: float | None
    confidence: str = ""           # "Low" | "Medium" | "High"
    design_state: str = ""         # "routed"
    device: str = ""
    date: str = ""
    activity_file: str = ""        # "---" means vectorless
    rails: dict[str, tuple[float, float]] = field(default_factory=dict)  # name -> (V, total A)

    @property
    def caveat(self) -> str:
        how = "vectorless" if self.activity_file in ("", "---") else f"activity from {self.activity_file}"
        where = f" ({self.path.name})" if self.path else ""
        state = f" on the {self.design_state} design" if self.design_state else ""
        return (f"ESTIMATE, not a measurement: Vivado report_power{state}, {how}, "
                f"confidence {self.confidence or 'not stated'}{where}")

    def readings(self) -> list[Reading]:
        src = f"estimate:vivado {self.rm}"
        out = []
        for name, value, unit, what in (
            ("onchip_power_estimate", self.total_w, "W", "Total On-Chip Power"),
            ("onchip_dynamic_estimate", self.dynamic_w, "W", "Dynamic"),
            ("onchip_static_estimate", self.static_w, "W", "Device Static"),
            ("junction_temp_estimate", self.junction_c, "degC", "Junction Temperature"),
        ):
            if value is None:
                out.append(Reading.unavailable(name, unit, f"the report has no {what} value",
                                               source=src))
            else:
                out.append(Reading(name, value, unit, src, reason=self.caveat))
        return out


def parse_report(text: str, *, rm: str = "", path: Path | None = None) -> PowerEstimate:
    """Parse one ``report_power`` text report. ``NotAPowerReport`` for anything else."""
    if MARKER not in text:
        raise NotAPowerReport("not a Vivado report_power report (no 'Total On-Chip Power')")
    header: dict[str, str] = {}
    summary: dict[str, str] = {}
    rails: dict[str, tuple[float, float]] = {}
    confidence = ""
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if _SECTION_RE.match(stripped):
            section = stripped.split(None, 1)[1]
            continue
        if not section:
            m = _HEADER_RE.match(stripped)
            if m:
                header[m.group("key").strip()] = m.group("val").strip()
            continue
        if not stripped.startswith("|") or stripped.startswith("+"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if section == "Summary" and len(cells) >= 2:
            summary[cells[0]] = cells[1]
        elif section == "Power Supply Summary" and len(cells) >= 3 and cells[0] != "Source":
            v, a = _float(cells[1]), _float(cells[2])
            if v is not None and a is not None:
                rails[cells[0]] = (v, a)
        elif section == "Confidence Level" and cells[0] == "Overall confidence level":
            confidence = cells[1] if len(cells) > 1 else ""
    return PowerEstimate(
        rm=rm or header.get("Design", "") or "unknown",
        path=path,
        total_w=_float(summary.get("Total On-Chip Power (W)", "")),
        dynamic_w=_float(summary.get("Dynamic (W)", "")),
        static_w=_float(summary.get("Device Static (W)", "")),
        junction_c=_float(summary.get("Junction Temperature (C)", "")),
        confidence=confidence or summary.get("Confidence Level", ""),
        design_state=header.get("Design State", ""),
        device=header.get("Device", ""),
        date=header.get("Date", ""),
        activity_file=summary.get("Simulation Activity File", ""),
        rails=rails,
    )


def rm_name_from_path(path: Path) -> str:
    """``power_rm_nanosoc.rpt`` -> ``nanosoc``; ``shell_top_power_routed.rpt`` -> ``shell_top``."""
    stem = path.stem.lower()
    for affix in ("_power_routed", "_routed", "_power"):
        if stem.endswith(affix):
            stem = stem[: -len(affix)]
    for prefix in ("power_routed_", "power_", "config_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
    if stem.startswith("rm_"):
        stem = stem[3:]
    return stem or path.stem


def _is_power_report(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    return MARKER in head


def find_reports(directory: Path) -> dict[str, Path]:
    """Design name -> its newest ``report_power`` report under ``directory``."""
    found: dict[str, Path] = {}
    root = Path(directory)
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*.rpt")):
        if not path.is_file() or not _is_power_report(path):
            continue
        rm = rm_name_from_path(path)
        prev = found.get(rm)
        if prev is None or path.stat().st_mtime > prev.stat().st_mtime:
            found[rm] = path
    return found


def load_estimates(directory: Path) -> dict[str, PowerEstimate]:
    """Every parseable power report under ``directory``, by design name."""
    out: dict[str, PowerEstimate] = {}
    for rm, path in find_reports(directory).items():
        try:
            out[rm] = parse_report(path.read_text(encoding="utf-8", errors="replace"),
                                   rm=rm, path=path)
        except (OSError, NotAPowerReport):
            continue
    return out
