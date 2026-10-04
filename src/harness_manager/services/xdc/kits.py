"""The two XDC kits and their checks, over any pack's pin model.

``rm_kit(model, design)``: for a reconfigurable module in the shell's partition:
``<name>_ooc.xdc`` (out-of-context timing by boundary group), ``<name>_connectivity.md``
and ``.csv`` (each boundary signal, what it reaches in the static, the board pin),
``<name>_pblock.md`` (the partition's floorplan facts) and
``<name>_wrapper_skeleton.sv`` (the full port list, unused groups tied to their
safe-idle value).

``board_kit(model, design)``: for a whole-FPGA design on the board, three files:
``<name>_pins.xdc`` (package pins), ``<name>_io.xdc`` (IO standards and extra pad
properties, by bank, plus the configuration voltage) and ``<name>_timing.xdc``
(clocks).

Every check is a ``Finding``. ``severity == "error"`` blocks the export and always
carries the reason; ``"note"`` travels with the files. Nothing is dropped silently:
a check that cannot run (an unknown bank voltage, say) is itself an error.
"""

from __future__ import annotations

import csv
import difflib
import io
import json
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any

from harness_manager.core.errors import AbsentError, RefusedError, UsageError

from .design import Design
from .hdl import expand
from .model import PinModel, _same_id
from .syntax import check_xdc

GENERATOR = "harness_manager.services.xdc"
CHECK_CODES = {
    "pin_conflict": "two things want one pin, or a pin the design may not have",
    "bank_voltage": "an IO standard the bank's fixed VCCO cannot drive, or no way to tell",
    "direction": "a port direction the board net or the boundary does not allow",
    "clock_capable": "a clock on a pin or signal that cannot carry one",
    "clock_period": "a clock period that disagrees with the board or the shell",
    "missing_pin": "a name the model does not have, or a boundary port the design lacks",
    "width": "a bus whose width does not match",
    "static_id": "a shell the model does not describe",
    "syntax": "a generated file failed the XDC syntax check (a generator bug)",
    "timed_group": "a group the design times itself (note)",
    "caution": "a board caution on a net the design uses (note)",
    "tied_off": "boundary groups the design leaves unused (note)",
}


@dataclass
class Finding:
    code: str
    subject: str
    reason: str
    hint: str = ""
    severity: str = "error"
    src: str = ""

    def line(self) -> str:
        text = f"[{self.severity}] {self.code}: {self.subject}: {self.reason}"
        return f"{text} ({self.hint})" if self.hint else text


@dataclass
class Kit:
    kind: str                         # "rm-kit" | "board"
    design: dict[str, Any]
    files: dict[str, str]
    findings: list[Finding]
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def manifest(self) -> dict[str, Any]:
        return {"kind": self.kind, "design": self.design, "passed": self.ok,
                "files": sorted(self.files), "checks": [asdict(f) for f in self.findings],
                "facts": self.facts, "generator": GENERATOR}

    def to_json(self) -> dict[str, Any]:
        return self.manifest() | {"files": dict(self.files)}

    def require_ok(self) -> None:
        if self.ok:
            return
        n = len(self.errors)
        exc = RefusedError(f"the {self.kind} for {self.design.get('name')} failed {n} "
                           f"check{'s' if n != 1 else ''}: " + "; ".join(
                               f"{f.code} {f.subject}: {f.reason}" for f in self.errors[:3])
                           + (" ..." if n > 3 else ""),
                           hint="fix the design (every failure is listed with --json), "
                                "then export again")
        exc.data = {"checks": [asdict(f) for f in self.findings]}  # type: ignore[attr-defined]
        raise exc

    def zip_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, text in sorted(self.files.items()):
                info = zipfile.ZipInfo(f"{self.design.get('name')}/{name}", (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, text)
            info = zipfile.ZipInfo(f"{self.design.get('name')}/manifest.json", (2026, 1, 1, 0, 0, 0))
            zf.writestr(info, json.dumps(self.manifest(), indent=1, sort_keys=True) + "\n")
        return buf.getvalue()


def _banner(model: PinModel, title: str, design: Design, extra: list[str]) -> list[str]:
    st = model.status
    board = model.board
    lines = [
        "#" * 78,
        f"# {title}",
        f"# design: {design.name}" + (f" ({design.title})" if design.title else ""),
        f"# board:  {board.get('title', board.get('id'))}, part {board.get('part')}",
        f"# model:  {st.get('label', 'board-pin model')}",
        f"#         {st.get('generator', '')} from {st.get('platform_ref', '?')} "
        f"@ {str(st.get('platform_commit', ''))[:10]}",
        "# Generated by Harness Manager (xdc export). Regenerate rather than edit: the",
        "# checks that passed are listed in manifest.json next to this file.",
    ]
    lines += [f"# {x}" if x else "#" for x in extra]
    lines.append("#" * 78)
    return lines


def _ports_q(names: list[str]) -> str:
    return "{" + " ".join(names) + "}" if len(names) > 1 or any("[" in n for n in names) else names[0]


def _fmt_ns(v: float) -> str:
    return f"{float(v):.3f}"


# =============================================================================================
# the RM kit
# =============================================================================================


def rm_kit(model: PinModel, design: Design) -> Kit:
    if design.kind != "rm":
        raise UsageError(f"{design.name} is a {design.kind} design; the RM kit needs kind \"rm\"",
                         hint="use `xdc board` for a whole-FPGA design")
    findings: list[Finding] = []
    try:
        shell = model.shell(design.static_id or None)
    except AbsentError as exc:
        findings.append(Finding("static_id", design.static_id, exc.message,
                                hint=f"this model describes {', '.join(model.shell_ids())}; the "
                                     "export below is for the fielded shell and will not link "
                                     "into another static"))
        shell = model.shell(None)
    sid = shell["static_id"]
    signals = model.boundary(sid)
    by_name = {s.name: s for s in signals}
    groups = model.boundary_groups(sid)
    bclocks = model.boundary_clocks(sid)
    pblock = {k: v["value"] for k, v in shell.get("pblock", {}).items()}
    use = design.use

    # -- groups the design uses ---------------------------------------------------------------
    for g in use:
        if g not in groups:
            findings.append(Finding("missing_pin", f"use.{g}",
                                    f"{g!r} is not a boundary group of {sid}",
                                    hint=f"groups: {', '.join(groups)}"))
            continue
        timed = design.timed(g)
        if isinstance(timed, list):
            members = {s.name for s in signals if s.group == g}
            for t in timed:
                if t not in members:
                    findings.append(Finding("missing_pin", f"use.{g}.timed", f"{t} is not in group {g}",
                                            hint=f"{g}: {', '.join(sorted(members))}"))
        outs = {s.name for s in signals if s.group == g and s.rm_dir == "out" and s.name != "rm_id"}
        for t in design.tied(g):
            if t not in outs:
                findings.append(Finding("missing_pin", f"use.{g}.tie",
                                        f"{t} is not an output of group {g} the skeleton can tie",
                                        hint=f"{g} outputs: {', '.join(sorted(outs)) or 'none'}"))
        if timed:
            findings.append(Finding("timed_group", g, "kept out of the false paths: the RM times "
                                    "this group's data against its clock; the shell side owns the "
                                    "pad timing", severity="note"))
    unused = [g for g in groups if g not in use]
    if unused:
        findings.append(Finding("tied_off", ", ".join(unused), "not used by this design: tie the "
                                "outputs to their safe-idle value (the skeleton does)", severity="note"))

    # -- the RM's port list against the boundary ---------------------------------------------
    ports = design.rm_ports()
    if ports is not None:
        seen: dict[str, dict[str, Any]] = {}
        for p in ports:
            if p["name"] in seen:
                findings.append(Finding("pin_conflict", p["name"], "declared twice in the RM's port list"))
                continue
            seen[p["name"]] = p
            sig = by_name.get(p["name"])
            if sig is None:
                close = difflib.get_close_matches(p["name"], by_name, n=1)
                findings.append(Finding(
                    "missing_pin", p["name"],
                    f"not a port of the {sid} boundary ({len(signals)} ports): the static has no "
                    "wire for it, and adding one is a new static (a re-mint)",
                    hint=f"did you mean {close[0]}?" if close else "remove it, or carry it over dut_gpio"))
                continue
            if p["dir"] != sig.rm_dir:
                findings.append(Finding("direction", p["name"],
                                        f"the RM declares it {p['dir']}; the boundary makes it an RM "
                                        f"{sig.rm_dir} (the shell {'drives' if sig.rm_dir == 'in' else 'reads'} it)",
                                        src=model.source_ref(sig.src[0]) if sig.src else ""))
            if int(p["width"]) != sig.width:
                findings.append(Finding("width", p["name"], f"{p['width']} bits in the RM, "
                                        f"{sig.width} on the boundary"))
        for sig in signals:
            if sig.name not in seen:
                findings.append(Finding(
                    "missing_pin", sig.name,
                    f"the RM has no port {sig.name} ({sig.rm_dir}, {sig.width} bit"
                    f"{'s' if sig.width > 1 else ''}): every RM carries the whole boundary, used "
                    "or not (the DFX link fails on a missing partition pin)",
                    hint="add it and tie it off; the skeleton in this kit has every port"))

    # -- clocks ---------------------------------------------------------------------------------
    declared: dict[str, dict[str, Any]] = {}
    for name, bc in bclocks.items():
        rule = bc.get("declare", "always")
        grp = by_name[name].group if name in by_name else ""
        if rule == "always" or (rule == "when_timed" and grp in use and design.timed(grp)):
            declared[name] = bc
    for req in design.clocks:
        port = str(req.get("port", ""))
        if port not in by_name:
            findings.append(Finding("missing_pin", port or "(clock)",
                                    f"a clock on {port!r}, which is not a boundary port of {sid}"))
            continue
        bc = bclocks.get(port)
        if bc is None:
            findings.append(Finding(
                "clock_capable", port,
                f"{port} is not a clock the shell drives across the boundary "
                f"({by_name[port].rm_dir} data, group {by_name[port].group}); the shell's clocks "
                f"are {', '.join(bclocks)}",
                hint="derive RM clocks from dut_clk (an MMCM is not allowed in the partition; "
                     "use create_generated_clock on the RM's divider in <rm>_rm.xdc)"))
            continue
        if "period_ns" in req and abs(float(req["period_ns"]) - float(bc["period_ns"])) > 1e-6:
            findings.append(Finding(
                "clock_period", port,
                f"{float(req['period_ns']):g} ns requested; the shell drives {bc['period_ns']:g} ns "
                "(the OOC period must match the static byte for byte)",
                hint="dut_clk is DRP-retunable at run time, but the OOC XDC tracks the static's "
                     "default" if port == "dut_clk" else "",
                src=model.source_ref(bc.get("src", ""))))
            continue
        declared[port] = bc

    # -- pin requests: the partition has no IO sites -------------------------------------------
    owns = shell.get("owns", {})
    for p in design.pins:
        pin = str(p.get("pin", ""))
        net = model.by_pin.get(pin)
        owner = owns.get(net, {}).get("shell_port") if net else None
        reason = (f"PACKAGE_PIN {pin} requested for {p.get('port', '?')}: the partition has "
                  f"{pblock.get('io_sites', 0)} IO sites, so an RM cannot own a pad")
        if owner:
            reason += f"; {pin} is also the shell's {owner}"
        findings.append(Finding(
            "pin_conflict", str(p.get("port", pin)), reason,
            hint="reach board IO through the shell (dut_gpio: LEDs [7:0], switches [15:8]); a "
                 "new pad is a shell variant, which is a re-mint"))

    files: dict[str, str] = {}
    n = design.name
    files[f"{n}_ooc.xdc"] = _render_ooc(model, design, sid, signals, declared, use)
    files[f"{n}_connectivity.md"], files[f"{n}_connectivity.csv"] = \
        _render_connectivity(model, design, shell, signals, declared, use)
    files[f"{n}_pblock.md"] = _render_pblock(model, design, shell)
    files[f"{n}_wrapper_skeleton.sv"] = _render_skeleton(model, design, sid, signals, use)
    _syntax_gate(files, findings)
    facts = {"static_id": sid, "boundary": shell["rp_boundary"]["totals"],
             "clocks": {k: v["period_ns"] for k, v in declared.items()},
             "groups_used": [g for g in groups if g in use], "pblock": pblock,
             "skeleton_undriven": [s.name for s in signals if _left_to_design(s, design, use)],
             "model": _provenance(model)}
    return Kit("rm-kit", design.summary() | {"static_id": sid}, files, findings, facts)


def _left_to_design(s: Any, design: Design, use: dict[str, Any]) -> bool:
    """An output the skeleton leaves to the design (``// assign X = ...;``): an output of a
    USED group, other than ``rm_id`` and the group's ``tie`` list. A design with none of
    these is a complete RM as its skeleton (``minimal``); one with any is not (KIT-NANOSOC:
    the built-in ``nanosoc`` names seven used groups and no RTL)."""
    return (s.rm_dir == "out" and s.name != "rm_id" and s.group in use
            and s.name not in design.tied(s.group))


def _timed_signals(design: Design, signals: list[Any]) -> set[str]:
    out: set[str] = set()
    for g in design.use:
        t = design.timed(g)
        if t is True:
            out |= {s.name for s in signals if s.group == g}
        elif isinstance(t, list):
            out |= set(t)
    return out


def _port_ref(sig: Any) -> str:
    return f"{sig.name}[*]" if sig.width > 1 else sig.name


def _render_ooc(model: PinModel, design: Design, sid: str, signals: list[Any],
                declared: dict[str, dict[str, Any]], use: dict[str, Any]) -> str:
    pblock = {k: v["value"] for k, v in model.shell(sid).get("pblock", {}).items()}
    out = _banner(model, f"{design.name}_ooc.xdc -- out-of-context timing for an RM of static {sid}",
                  design, [
                      "OOC ONLY: read with read_xdc after synth_design in your OOC run, and only",
                      "after the RM checkpoint is written: a checkpoint keeps the create_clock",
                      "lines read into it, and at the DFX link one whose name the static also",
                      "uses (dut_clk: the static's OSCCLK1 clock) overwrites the static's clock",
                      "and leaves the static<->RM boundary untimed (Linux v2.0.0 known issue 11;",
                      "kit check warns). build_rm.tcl writes its checkpoint first.",
                      "No Tcl control flow (read_xdc rejects it); -quiet on every port query so a",
                      "tied-off port that synthesis removed is not an error.",
                  ])
    out.append("")
    out.append("# --- clocks the shell drives across the boundary (partition-timing contract) ---")
    for name, bc in declared.items():
        wf = bc.get("waveform")
        wave = f" -waveform {{{_fmt_ns(wf[0])} {_fmt_ns(wf[1])}}}" if wf else ""
        why = "OOC-only; no static counterpart" if bc.get("ooc_only") else f"static: {bc.get('static_clock', '')}"
        out.append(f"# {name}: {why}")
        out.append(f"create_clock -name {name} -period {_fmt_ns(bc['period_ns'])}{wave} [get_ports {name}]")
    if "dut_clk" in declared and pblock.get("dut_clk_hd_clk_src"):
        out.append("# the static's clock buffer that drives dut_clk into the partition")
        out.append(f"set_property HD.CLK_SRC {pblock['dut_clk_hd_clk_src']} [get_ports -quiet dut_clk]")
    groups: dict[str, list[str]] = {}
    for name, bc in declared.items():
        groups.setdefault(bc.get("async_group", name), []).append(name)
    if len(groups) > 1:
        out.append("")
        out.append("# --- every boundary clock is asynchronous to the others (the static owns the CDC) ---")
        parts = [f"set_clock_groups -name async_{design.name} -asynchronous \\"]
        items = list(groups.values())
        for i, names in enumerate(items):
            tail = " \\" if i < len(items) - 1 else ""
            parts.append(f"    -group [get_clocks -quiet -include_generated_clocks {_ports_q(names)}]{tail}")
        out += parts
    out.append("")
    out.append("# --- boundary false paths: every crossing is CDC'd on the static side ---")
    timed = _timed_signals(design, signals)
    by_group: dict[str, list[Any]] = {}
    for s in signals:
        by_group.setdefault(s.group, []).append(s)
    for g, sigs in by_group.items():
        frm = [_port_ref(s) for s in sigs if s.rm_dir == "in" and s.name not in declared and s.name not in timed]
        to = [_port_ref(s) for s in sigs if s.rm_dir == "out" and s.name not in timed]
        kept = [s.name for s in sigs if s.name in timed]
        state = "used" if g in use else "tied off"
        out.append(f"# {g} ({state})" + (f"; timed by the RM, not false-pathed: {' '.join(kept)}" if kept else ""))
        if frm:
            out.append(f"set_false_path -from [get_ports -quiet {_ports_q(frm)}]")
        if to:
            out.append(f"set_false_path -to   [get_ports -quiet {_ports_q(to)}]")
    return "\n".join(out) + "\n"


def _render_connectivity(model: PinModel, design: Design, shell: dict[str, Any], signals: list[Any],
                         declared: dict[str, Any], use: dict[str, Any]) -> tuple[str, str]:
    sid = shell["static_id"]
    timed = _timed_signals(design, signals)
    conn: dict[str, list[dict[str, Any]]] = {}
    for c in shell.get("connectivity", []):
        conn.setdefault(c["signal"], []).append(c)
    rows = []
    for s in signals:
        timing = "clock" if s.name in declared else "timed" if s.name in timed else \
            ("-" if s.name in model.boundary_clocks(sid) else "false path")
        facts = conn.get(s.name) or [{"rows": [{"bit": None, "static": "?", "board_net": None, "pin": None}],
                                      "via": "-", "src": ""}]
        for c in facts:
            for r in c["rows"]:
                bit = s.name if r["bit"] is None and s.width == 1 else \
                    f"{s.name}[{c['bits'][0]}:{c['bits'][1]}]" if r["bit"] is None else f"{s.name}[{r['bit']}]"
                net = r.get("board_net") or ""
                rows.append({
                    "signal": bit, "group": s.group, "rm_dir": s.rm_dir, "width": s.width,
                    "used": "yes" if s.group in use else "tied off", "timing": timing,
                    "swap_clamp": "-" if s.clamp is None else str(s.clamp),
                    "reaches": r["static"], "via": c.get("via", "-"), "board_net": net,
                    "pin": r.get("pin") or "",
                    "bank": model.nets[net]["bank"] if net in model.nets else "",
                    "source": model.source_ref(c.get("src", "")),
                })
    cols = ["signal", "group", "rm_dir", "width", "used", "timing", "swap_clamp", "reaches", "via",
            "board_net", "pin", "bank", "source"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    b = shell["rp_boundary"]["totals"]
    md = [f"# {design.name}: boundary connectivity (static {sid})", "",
          f"The partition boundary of static `{sid}`: {b['ports']} ports, {b['bits']} bits"
          + (f", {b['decoupler_intfs']} decoupler interfaces" if b.get("decoupler_intfs") else "") + ".",
          "Directions are the RM's. `swap clamp` is what the shell's DFX decoupler holds the",
          "RM's output at during a partial reconfiguration (`-` = not decoupled: the shell drives it).",
          "`reaches` is the block in the static; `board net`/`pin` is where it lands on the board.",
          "",
          f"Model: {model.status.get('label', '')}.", "",
          "| signal | group | RM dir | used | timing | swap clamp | reaches in the static | via | board net | pin |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| `{r['signal']}` | {r['group']} | {r['rm_dir']} | {r['used']} | {r['timing']} | "
                  f"{r['swap_clamp']} | {r['reaches']} | {r['via']} | {r['board_net'] or '-'} | {r['pin'] or '-'} |")
    md += ["", "## Sources", ""]
    for key in sorted({r["source"] for r in rows if r["source"]}):
        md.append(f"- {key}")
    return "\n".join(md) + "\n", buf.getvalue()


def _render_pblock(model: PinModel, design: Design, shell: dict[str, Any]) -> str:
    sid = shell["static_id"]
    lines = [f"# {design.name}: the partition's floorplan (static {sid})", "",
             "The RM is placed and routed inside this pblock by the DFX link. It is fixed by the",
             "static: an RM that does not fit, or needs a site type the pblock lacks, needs a new",
             "static (a re-mint).", "", "| fact | value | source |", "|---|---|---|"]
    for key, v in shell.get("pblock", {}).items():
        val = v["value"]
        if isinstance(val, list):
            val = " ".join(str(x) for x in val)
        elif isinstance(val, dict):
            val = ", ".join(f"{k} {x:,}" if isinstance(x, int) else f"{k} {x}" for k, x in val.items())
        lines.append(f"| {key.replace('_', ' ')} | {val} | {model.source_ref(v.get('src', ''))} |")
    lines += ["", "What this means for an RM:", "",
              "- no IO sites: every board pin reaches the RM through the shell (see the connectivity sheet);",
              "- no BUFG, MMCM or BSCAN site: generate no clock in the RM; an RM debug hub uses the",
              "  shell's BSCAN legs (group dbgbscan) and runs on phy_rmii_ref_clk;",
              "- the capacity above is the whole pblock; leave routing headroom.", ""]
    return "\n".join(lines)


def _render_skeleton(model: PinModel, design: Design, sid: str, signals: list[Any],
                     use: dict[str, Any]) -> str:
    ngpio = model.shell(sid)["rp_boundary"].get("ngpio")
    rm_id = str(design.doc.get("rm_id") or "")
    lines = [f"// {design.name}_wrapper_skeleton.sv -- every partition port of static {sid}.",
             "// Generated by Harness Manager (xdc export). Groups this design does not use are",
             "// tied to the value the shell's decoupler holds them at during a swap.",
             "`timescale 1ns / 1ps", "",
             f"module rm_{design.name}" + (f" #(parameter int NGPIO = {ngpio})" if ngpio else "") + " ("]
    decls = []
    for s in signals:
        d = "input " if s.rm_dir == "in" else "output"
        rng = f"[{'NGPIO' if (ngpio and s.width == ngpio and s.group == 'gpio') else s.width}-1:0]" \
            if s.width > 1 else ""
        rng = rng.replace(f"[{s.width}-1:0]", f"[{s.width - 1}:0]")
        decls.append(f"  {d} logic {rng:<12} {s.name}")
    lines.append(",\n".join(decls))
    lines.append(");")
    lines.append("")
    group = None
    for s in signals:
        if s.rm_dir != "out":
            continue
        if s.group != group:
            group = s.group
            lines.append(f"  // {group}: " + ("used by this design: drive these" if group in use
                                              else "not used: tied to the decoupler's safe-idle value"))
        if s.name == "rm_id":
            val = f"32'h{int(rm_id, 0):08X}" if rm_id else "32'h0000_0000"
            lines.append(f"  assign rm_id = {val};  // this RM's identity (the overlay manifest's rm_id)")
            continue
        clamp = int(s.clamp or 0)
        if _left_to_design(s, design, use):
            lines.append(f"  // assign {s.name} = ...;")
            continue
        tie = "  // not driven by this design: its safe-idle value" if s.group in use else ""
        val = f"'{clamp}" if s.width > 1 and clamp in (0, 1) and clamp == 0 else \
            (f"{s.width}'d{clamp}" if s.width > 1 else f"1'b{clamp}")
        lines.append(f"  assign {s.name} = {val};{tie}")
    lines += ["", "endmodule", ""]
    return "\n".join(lines)


# =============================================================================================
# the full-board kit
# =============================================================================================


def board_kit(model: PinModel, design: Design) -> Kit:
    if design.kind != "board":
        raise UsageError(f"{design.name} is an {design.kind} design; the board export needs kind \"board\"",
                         hint="use `xdc rm-kit` for a reconfigurable module")
    findings: list[Finding] = []
    rows: list[dict[str, Any]] = []
    port_seen: set[str] = set()
    for p in design.board_ports():
        try:
            pbits = expand(str(p["port"]))
        except ValueError as exc:
            findings.append(Finding("missing_pin", str(p["port"]), str(exc)))
            continue
        if "net" in p:
            try:
                targets = [("net", n) for n in expand(str(p["net"]))]
            except ValueError as exc:
                findings.append(Finding("missing_pin", str(p["port"]), str(exc)))
                continue
        elif "pin" in p:
            pins = p["pin"] if isinstance(p["pin"], list) else [p["pin"]]
            targets = [("pin", str(x)) for x in pins]
        else:
            findings.append(Finding("missing_pin", str(p["port"]), "no board net or package pin given",
                                    hint='add "net": "<board net>" (or "pin")'))
            continue
        if len(targets) != len(pbits):
            findings.append(Finding("width", str(p["port"]), f"{len(pbits)} port bits onto "
                                    f"{len(targets)} {targets[0][0] if targets else 'net'}s"))
            continue
        for bit, (kind, target) in zip(pbits, targets, strict=True):
            if bit in port_seen:
                findings.append(Finding("pin_conflict", bit, "the design places this port twice"))
                continue
            port_seen.add(bit)
            if kind == "net":
                net = target
                if net not in model.nets:
                    close = difflib.get_close_matches(net, model.nets, n=2)
                    findings.append(Finding(
                        "missing_pin", bit, f"{net} is not a board net of {model.board.get('id')}",
                        hint=f"did you mean {' or '.join(close)}?" if close else "see `harness-manager xdc nets`"))
                    continue
                pin = model.nets[net]["pin"]
            else:
                pin = target
                if pin not in model.package_pins:
                    findings.append(Finding("missing_pin", bit,
                                            f"{pin} is not a user IO pin of {model.board.get('part')}"))
                    continue
                net = model.by_pin.get(pin)
            rows.append({"port": bit, "net": net, "pin": pin, "spec": p})

    # -- per-row checks -------------------------------------------------------------------------
    by_pin: dict[str, str] = {}
    bank_use: dict[str, list[tuple[str, str, float]]] = {}
    for r in rows:
        bit, net, pin, p = r["port"], r["net"], r["pin"], r["spec"]
        nd = model.nets.get(net) if net else None
        pp = model.package_pins[pin]
        r["bank"] = pp["bank"]
        if pin in by_pin:
            findings.append(Finding("pin_conflict", bit, f"{pin} ({net or 'unnamed'}) is already {by_pin[pin]}"))
            continue
        by_pin[pin] = bit
        if nd and nd.get("reserved"):
            findings.append(Finding("pin_conflict", bit, f"{net} is reserved: {nd['reserved']['reason']}",
                                    src=model.source_ref(nd["reserved"].get("src", ""))))
        for c in (nd or {}).get("cautions", []):
            findings.append(Finding("caution", bit, f"{net}: {c['text']}", severity="note",
                                    src=model.source_ref(c.get("src", ""))))
        # direction
        net_dir = nd["dir"] if nd else "inout"
        want = p.get("dir") or net_dir
        r["dir"] = want
        if net_dir == "in" and want != "in":
            findings.append(Finding("direction", bit, f"{net} is driven by the board (an input to the "
                                    f"FPGA); the design makes it {want}, which would fight the board's driver",
                                    src=model.source_ref(nd["src"][1] if nd and len(nd["src"]) > 1 else "")))
        elif net_dir == "out" and want != "out":
            findings.append(Finding("direction", bit, f"{net} only goes from the FPGA to the board "
                                    f"(nothing on the board drives it); the design makes it {want}",
                                    src=model.source_ref(nd["src"][1] if nd and len(nd["src"]) > 1 else "")))
        # IO standard and bank voltage
        std = p.get("iostandard") or (nd or {}).get("iostandard")
        r["iostandard"] = std
        bank = model.banks.get(pp["bank"], {})
        if not std:
            findings.append(Finding("bank_voltage", bit, f"no IO standard for {net or pin}",
                                    hint=f"bank {pp['bank']} runs at {bank.get('vcco')} V; give one, "
                                         f"e.g. LVCMOS{str(bank.get('vcco', '')).replace('.', '')}"
                                    if bank.get("vcco") else f"bank {pp['bank']}'s VCCO is unknown"))
        else:
            v = model.vcco(std)
            if v is None:
                findings.append(Finding("bank_voltage", bit, f"unknown IO standard {std}",
                                        hint=f"known: {', '.join(sorted(model.doc.get('iostandards', {})))}"))
            elif bank.get("vcco") is None:
                findings.append(Finding("bank_voltage", bit,
                                        f"bank {pp['bank']}'s VCCO is not in the model "
                                        f"({bank.get('vcco_reason', 'unknown')}), so {std} cannot be checked",
                                        hint="confirm the bank voltage on the schematic; a later pin database "
                                             "adds it to the pin database"))
                bank_use.setdefault(pp["bank"], []).append((bit, std, v))
            elif abs(v - float(bank["vcco"])) > 1e-6:
                findings.append(Finding("bank_voltage", bit,
                                        f"{std} needs VCCO {v:g} V; bank {pp['bank']} is fixed at "
                                        f"{bank['vcco']:g} V on this board",
                                        hint=f"use a {bank['vcco']:g} V standard (the board uses "
                                             f"{', '.join(bank.get('evidence', {}))} there)"))
        r["props"] = dict((nd or {}).get("props", {})) | dict(p.get("props") or {})
        # clocks
        mhz = p.get("clock_mhz")
        if mhz is not None:
            try:
                mhz = float(mhz)
                if mhz <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                findings.append(Finding("clock_period", bit, f"clock_mhz must be a positive number, not {mhz!r}"))
                continue
            r["clock_mhz"] = mhz
            cc = pp.get("clock_capable", "")
            if cc != "GC":
                findings.append(Finding(
                    "clock_capable", bit,
                    f"{pin} ({pp['function']}) is not a global-clock (GC) pin"
                    + (f"; {cc} is a byte-lane clock only" if cc else ""),
                    hint="put the clock on a GC pin (the board oscillators are: OSCCLK[*])",
                    src=model.source_ref("xilinx_pkg")))
            osc = (nd or {}).get("oscillator")
            if osc and abs(float(osc["mhz"]) - mhz) > 1e-3:
                findings.append(Finding(
                    "clock_period", bit, f"{net} is {osc['osc']}, which the board controller programs to "
                    f"{osc['mhz']:g} MHz; the design says {mhz:g} MHz",
                    hint=f"change {osc['osc']} in the SD's board configuration, or the design",
                    src=model.source_ref(osc.get("src", ""))))
    for bank, uses in bank_use.items():
        volts = {v for _, _, v in uses}
        if len(volts) > 1:
            findings.append(Finding("bank_voltage", f"bank {bank}", "the design mixes "
                                    + ", ".join(f"{b} {s}" for b, s, _ in uses) + " in one bank; one bank has one VCCO"))
    files = _render_board(model, design, rows)
    _syntax_gate(files, findings)
    facts = {"ports": len(rows), "banks": sorted({r["bank"] for r in rows}, key=int),
             "clocks": {r["port"]: r["clock_mhz"] for r in rows if r.get("clock_mhz")},
             "model": _provenance(model)}
    return Kit("board", design.summary(), files, findings, facts)


def _render_board(model: PinModel, design: Design, rows: list[dict[str, Any]]) -> dict[str, str]:
    n = design.name
    part = model.board.get("part")
    note = ["WHOLE-FPGA design: this replaces the harness shell on the board. For a design",
            "that loads into the shell's partition, use the RM kit instead."]
    pins = _banner(model, f"{n}_pins.xdc -- package pins ({part})", design, note)
    pins.append("")
    for r in rows:
        nd = model.nets.get(r["net"]) if r["net"] else None
        tag = f"{r['net']}, bank {r['bank']}, {nd['verified']}" if nd else f"bank {r['bank']}, no board net"
        pins.append(f"set_property PACKAGE_PIN {r['pin']:<5} [get_ports {_ports_q([r['port']])}] ;# {tag}")
    iof = _banner(model, f"{n}_io.xdc -- IO standards and pad properties, by bank", design, [])
    for bank in sorted({r["bank"] for r in rows}, key=int):
        b = model.banks.get(bank, {})
        iof.append("")
        iof.append(f"# bank {bank}: VCCO {b.get('vcco') if b.get('vcco') is not None else 'unknown'} V"
                   + (f", {b['kind']}" if b.get("kind") else ""))
        for r in rows:
            if r["bank"] != bank:
                continue
            if r.get("iostandard"):
                iof.append(f"set_property IOSTANDARD {r['iostandard']} [get_ports {_ports_q([r['port']])}]")
            for k, v in sorted(r.get("props", {}).items()):
                iof.append(f"set_property {k} {v} [get_ports {_ports_q([r['port']])}]")
    if model.config:
        iof.append("")
        iof.append("# configuration bank voltage (a board fact)")
        for k, v in model.config.items():
            iof.append(f"set_property {k} {v['value']} [current_design]")
    tim = _banner(model, f"{n}_timing.xdc -- clocks", design, [
        "Board clocks only. The model has no board trace delays: add set_input_delay /",
        "set_output_delay for synchronous IO, or set_false_path for asynchronous IO",
        "(buttons, switches, LEDs)."])
    tim.append("")
    clocks = [r for r in rows if r.get("clock_mhz")]
    for r in clocks:
        period = 1000.0 / r["clock_mhz"]
        tim.append(f"create_clock -name {r['port'].replace('[', '_').replace(']', '')} -period "
                   f"{period:.3f} -waveform {{0.000 {period / 2:.3f}}} [get_ports {_ports_q([r['port']])}]")
    if not clocks:
        tim.append("# this design declares no board clock")
    return {f"{n}_pins.xdc": "\n".join(pins) + "\n", f"{n}_io.xdc": "\n".join(iof) + "\n",
            f"{n}_timing.xdc": "\n".join(tim) + "\n"}


# --- shared -------------------------------------------------------------------------------------


def _syntax_gate(files: dict[str, str], findings: list[Finding]) -> None:
    for name, text in files.items():
        if name.endswith(".xdc"):
            for problem in check_xdc(text):
                findings.append(Finding("syntax", name, problem,
                                        hint="a generator bug: report it with the design"))


def _provenance(model: PinModel) -> dict[str, Any]:
    st = model.status
    return {"label": st.get("label"), "derived": st.get("derived"), "lane_c": st.get("lane_c"),
            "platform_ref": st.get("platform_ref"), "platform_commit": st.get("platform_commit"),
            "generator": st.get("generator")}


def same_static(a: str, b: str) -> bool:
    return _same_id(a, b)
