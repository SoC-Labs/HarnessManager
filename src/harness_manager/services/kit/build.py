"""The build receipt, read back: is this build fit to become an overlay?

``build_rm.tcl`` writes ``<rm>_build.json`` in its ``OUT_DIR`` after a pass, a failed gate
or a ``STOP_AFTER`` (schema ``harness-manager-rm-build`` v1, ``schema.parse_receipt``).
The receipt is the binding between the pair and the static: its ``static_id`` is the
CRC-32 the build computed from the DCP it opened, and its ``rm_id_netlist`` is the id the
netlist drives. So nobody writes an overlay manifest by hand (david K5): ``kit pack``
derives it from a receipt that passes ``receipt_checks``.

``receipt_checks`` refuses:

- a build that did not pass (``state`` failed or stopped), or any ``FAIL`` gate;
- ``rm_id`` != ``rm_id_netlist`` (the shell compares them after every swap);
- a partial, clearing or ``.ltx`` whose length or CRC-32 is not the one the build recorded
  (a file copied from another build, or half-copied).

It also says what the build's timing was (``timing``, never a refusal: the ``rm_timing`` gate
is the verdict): the RM's own worst slack, or, when no timed path lies inside the partition
(``minimal``: every output a constant), that, with the whole design's WNS/WHS from the
receipt or from ``<name>_timing.rpt`` beside it (FIX-PACK-3: a blank ``rm_wns`` read as "not
measured").

Board-agnostic: the overlay manifest itself is the pack's format (``KitAdapter`` of the
MPS3 pack writes ``pyverify``'s overlay triple).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_manager.core.pack import KitCheck

from .schema import BuildReceipt, crc32_file, hex32, load_receipt, parse_u32, same_id

RECEIPT_GLOB = "*_build.json"
LOG_NAME = "build_rm.log"
#: A build log written within this many seconds is a build still running. Vivado writes a
#: line every few minutes at most, even in route_design (KIT-NANOSOC: 14 min of route, a
#: phase line every 1-5 min, at load 30); a log older than this with no verdict is a run
#: that died (killed, out of memory, a lost session).
RUNNING_FRESH_S = 30 * 60


@dataclass(frozen=True)
class RunningBuild:
    stage: str          # the last HM_STAGE
    log: Path
    mtime: float
    fresh: bool         # written within RUNNING_FRESH_S: running; else it died


def running_build(build_dir: Path, *, now: float | None = None,
                  fresh_s: float = RUNNING_FRESH_S) -> RunningBuild | None:
    """A build started in ``build_dir`` that has no verdict yet (KIT-NANOSOC G7): its
    ``build_rm.log`` has an ``HM_STAGE`` with no ``HM_RM_BUILD_*`` after it. The receipt is
    written only at the end, so until then the directory looks unbuilt (or shows the LAST
    run's receipt), and the guide offered the Vivado command again: a second Vivado in the
    same directory overwrites ``out/``. None when there is no log or its run has ended."""
    log = Path(build_dir) / LOG_NAME
    try:
        st = log.stat()
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    from .render import parse_markers

    stage = ""
    for mark, rest in parse_markers(text):
        if mark == "HM_STAGE":
            stage = rest.split()[0] if rest else "?"
        elif mark.startswith("HM_RM_BUILD_"):
            stage = ""
    if not stage:
        return None
    now = time.time() if now is None else now
    return RunningBuild(stage, log, st.st_mtime, now - st.st_mtime <= fresh_s)


def find_receipts(build_dir: Path) -> list[Path]:
    """Receipts in a build directory (``kit script`` layout: ``<dir>/out/<rm>_build.json``)
    or in the directory itself. Newest first."""
    d = Path(build_dir)
    found = [*d.glob(RECEIPT_GLOB), *(d / "out").glob(RECEIPT_GLOB)] if d.is_dir() else []
    return sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)


def receipt_files(r: BuildReceipt) -> dict[str, Path]:
    """The files the receipt names, resolved beside it: ``partial``, ``clearing``, ``ltx``."""
    base = r.path.parent
    out = {}
    for role, key in (("partial", "partial_bin"), ("clearing", "clearing_bin"), ("ltx", "ltx")):
        if r.get(key):
            out[role] = base / r.get(key)
    return out


def build_dir_of(r: BuildReceipt) -> Path:
    """The build directory a receipt belongs to (``kit script`` layout: ``<dir>/out/``)."""
    return r.path.parent.parent if r.path.parent.name == "out" else r.path.parent


def stopped_words(r: BuildReceipt) -> str:
    """A ``stopped`` receipt in words (KIT-INTERACTIVE): a ``STOP_AFTER`` run that ended
    where it was asked to, with every gate up to there passed. It is not a failure (a
    failed gate writes ``failed``), and not a build to pack: it has no pair yet."""
    n = sum(g.verdict == "PASS" for g in r.gates)
    return (f"stopped after {r.stage} (STOP_AFTER={r.stage}), not a failure: the {n} gates "
            f"up to there passed; there is no pair to check or pack until the build runs "
            f"to the end")


def finish_hint(r: BuildReceipt) -> str:
    """The command that prints how to run a stopped build to the end. ``--stop-after
    bitstream`` is explicit: a script written with ``kit script --stop-after link`` keeps
    stopping there otherwise."""
    return f"harness-manager kit build {build_dir_of(r)} --stop-after bitstream"


def receipt_checks(r: BuildReceipt) -> list[KitCheck]:
    """Every check a receipt must pass before it becomes an overlay (module docstring)."""
    checks: list[KitCheck] = []
    ok = r.state == "passed"
    detail = {"passed": f"the build passed {len(r.gates)} gates",
              "stopped": f"the build stopped after {r.stage} (STOP_AFTER): finish it",
              "failed": f"the build failed at {r.stage}"}[r.state]
    g = r.failed_gate
    if g is not None:
        detail += f": gate {g.gate}: {g.detail}"
    checks.append(KitCheck("build", "ok" if ok and g is None else "mismatch", detail))
    if not ok:
        return checks

    want, got = r.get("rm_id"), r.get("rm_id_netlist")
    try:
        same = same_id(want, got) and parse_u32(want) != 0
    except (TypeError, ValueError):
        same = False
    checks.append(KitCheck("rm_id", "ok" if same else "mismatch",
                           f"the netlist drives {got}" + ("" if same else f", RM_ID is {want}")))
    sid = r.get("static_id")
    checks.append(KitCheck("static_id", "ok" if sid else "mismatch",
                           f"built against static {sid} (the CRC-32 of the DCP the build opened)"
                           if sid else "the receipt names no static_id"))
    checks.append(KitCheck("timing", "ok", timing_words(r)))
    files = receipt_files(r)
    for role in ("partial", "clearing", "ltx"):
        p = files.get(role)
        if p is None:
            if role != "ltx":
                checks.append(KitCheck(role, "mismatch", f"the receipt names no {role}"))
            continue
        if not p.is_file():
            checks.append(KitCheck(role, "mismatch", f"{p} is missing"))
            continue
        want_crc = r.get(f"{role}_crc32")
        crc = hex32(crc32_file(p))
        bad = []
        if want_crc and not same_id(crc, want_crc):
            bad.append(f"CRC-32 {crc}, the receipt says {want_crc}")
        want_len = r.get(f"{role}_len")
        if want_len and str(p.stat().st_size) != want_len:
            bad.append(f"{p.stat().st_size} B, the receipt says {want_len}")
        checks.append(KitCheck(role, "mismatch" if bad else "ok",
                               f"{p.name}: " + ("; ".join(bad) + " (a file from another build, "
                                                "or half-copied)" if bad else
                                                f"{p.stat().st_size} B, CRC-32 {crc} as built")))
    return checks


_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def design_slack_from_report(rpt: Path) -> tuple[str, str] | None:
    """(WNS, WHS) of the whole design, in ns, from ``report_timing_summary``'s "Design Timing
    Summary" table (columns WNS, TNS, TNS failing, TNS total, WHS, ...). ``""`` for a figure
    Vivado gives as NA; None when the file or the table is not there."""
    try:
        lines = Path(rpt).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for i, line in enumerate(lines):
        if line.strip() != "| Design Timing Summary":
            continue
        for j in range(i + 1, min(i + 12, len(lines))):
            if lines[j].split()[:2] == ["WNS(ns)", "TNS(ns)"]:
                row = next((x.split() for x in lines[j + 2:j + 4] if x.strip()), [])
                if len(row) < 5:
                    return None
                return tuple(v if _NUM.match(v) else "" for v in (row[0], row[4]))  # type: ignore[return-value]
        return None
    return None


def _ns(v: str) -> str:
    return f"{v} ns" if v else "none"


def timing_words(r: BuildReceipt) -> str:
    """The build's timing in one line (``receipt_checks``' ``timing``; module docstring)."""
    wns, whs = r.get("rm_wns"), r.get("rm_whs")
    if wns or whs:
        return f"your RM's paths: setup WNS {_ns(wns)}, hold WHS {_ns(whs)}"
    if r.get("rm_timing_note"):
        return r.get("rm_timing_note")
    rpt = r.get("timing_rpt") or f"{r.rm_name}_timing.rpt"
    dwns, dwhs = r.get("design_wns"), r.get("design_whs")
    if not (dwns or dwhs):                  # a receipt from before FIX-PACK-3: the report
        got = design_slack_from_report(r.path.parent / rpt)
        if got is None:
            return (f"no timed path inside the partition; the whole-design WNS is in {rpt}, "
                    "which is not beside the receipt")
        dwns, dwhs = got
    return (f"no timed path inside the partition; whole-design WNS {_ns(dwns)}, "
            f"WHS {_ns(dwhs)} from {rpt}")


def load(path: Path) -> BuildReceipt:
    """A receipt from its path, or the newest receipt in a build directory."""
    p = Path(path)
    if p.is_dir():
        found = find_receipts(p)
        if not found:
            from harness_manager.core.errors import AbsentError

            raise AbsentError(f"no build receipt in {p}",
                              hint="build_rm.tcl writes <rm>_build.json in its OUT_DIR; "
                                   "run the build first")
        p = found[0]
    return load_receipt(p)


def _pack_of(r: BuildReceipt, default: str) -> str:
    return r.kit_id.split("/", 1)[0] if "/" in r.kit_id else default


def check_any(kits: Any, path: Path, *, clearing: Path | None = None, static_id: str = "",
              identity: Any = None, pack: str = "mps3"
              ) -> tuple[list[KitCheck], dict[str, Any], str, BuildReceipt | None]:
    """``kit check`` for the CLI and the API: a receipt (or a build dir: its newest receipt)
    with its files and pair, else a bare partial (``clearing`` beside it). With the kit of
    the static cached, the pair is held to its ``rp.frames``; with a board identity, the
    build's static to the board's (identity). Returns (checks, facts, static_id, receipt).

    ``static_id`` with a receipt (KIT-RC2): the static the caller expects. It is compared
    with the receipt's (the CRC-32 the build computed) and a difference REFUSES, as an
    identity check (``expected_static``, exit 14): a partial for another static must never
    look like it passed because the flag was ignored. With a bare partial it picks the kit
    whose frame box the pair is held to."""
    p = Path(path)
    if p.is_dir() or p.suffix.lower() == ".json":
        r = load(p)
        checks = receipt_checks(r)
        facts: dict[str, Any] = {"receipt": r.to_json()}
        sid = r.get("static_id")
        if static_id:
            try:
                want = hex32(parse_u32(static_id))
            except (TypeError, ValueError):
                want = str(static_id)
            same = bool(sid) and same_id(want, sid)
            checks.append(KitCheck("expected_static", "ok" if same else "mismatch",
                                   f"built for {sid or 'no static'}; --static-id is {want}"
                                   + ("" if same else ": this build is not for the static you "
                                                      "named"), identity=True))
        files = receipt_files(r)
        if r.state == "passed" and files.get("partial") and files["partial"].is_file():
            kit = kits.get(sid) if sid else None
            adapter = kits.adapter_for(_pack_of(r, pack))
            bit = files["partial"].with_suffix(".bit")
            pair, facts["pair"] = adapter.check_pair(
                bit if bit.is_file() else files["partial"], files.get("clearing"),
                kit=kit.manifest if kit else None,
                bin_path=files["partial"] if bit.is_file() else None)
            checks += pair
            if kit is None:
                checks.append(KitCheck("kit", "unchecked", f"no kit for {sid} in the cache: "
                                                           "the frame box was not compared"))
        if identity is not None and getattr(identity, "shell_id", "") and sid:
            same = same_id(identity.shell_id, sid)
            checks.append(KitCheck("board_static", "ok" if same else "mismatch",
                                   f"built for {sid}; the board runs {identity.shell_id}",
                                   identity=True))
        return checks, facts, sid, r
    if not p.is_file():
        from harness_manager.core.errors import AbsentError

        raise AbsentError(f"no such file: {p}", hint="a receipt (.json), a build directory, "
                                                   "or a partial (.bin/.bit)")
    sid = hex32(parse_u32(static_id)) if static_id else ""
    kit = kits.get(sid) if sid else None
    checks, facts = kits.adapter_for(pack).check_pair(p, clearing,
                                                      kit=kit.manifest if kit else None)
    checks = list(checks)
    if sid and kit is None:
        checks.append(KitCheck("kit", "unchecked", f"no kit for {sid} in the cache: the "
                                                   "frame box was not compared"))
    return checks, facts, sid, None
