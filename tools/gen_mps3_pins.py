#!/usr/bin/env python3
"""Generate the MPS3 board-pin model: a DERIVED stand-in for harness Lane C's board_pins.yaml.

    tools/gen_mps3_pins.py                 write src/harness_manager_mps3/pins/mps3_board_pins.json
    tools/gen_mps3_pins.py --check         regenerate in memory; exit 1 if the committed file differs
    tools/gen_mps3_pins.py --platform DIR --ref BRANCH --pkg FILE
    tools/gen_mps3_pins.py --shell 0x72BB0A36@feat/rm-ila-mint --shell 0x44EE76D5@BRANCH
    tools/gen_mps3_pins.py --all --ref A --ref B   every fielded/<sid>/ found at A or B

**More than one shell (KIT-RC2).** The model describes every shell in ``SHELLS`` (the
default, what ``--check`` regenerates), or the ``--shell SID[@REF]`` list (REF defaults
to the first ``--ref``), or with ``--all`` every ``fielded/<sid>/mint.json`` found at the
``--ref``s, each modelled at the first ref whose shell sources hash to its record (a
record no ref matches is reported and left out, never modelled from the wrong files).
The FIRST shell is ``default_shell``: the board-level facts (package, pinmap nets, banks,
connectors) come from its ref. Every shell gets its own ``owns``/``free``/``clocks``/
``rp_boundary``/``boundary_clocks``/``connectivity``/``pblock``, checked against its own
record, and names its ``platform`` ref and commit. A source file whose bytes at a shell's
ref differ from the default shell's gets its own key (``<key>@<sid>``). A second shell is
``fielded`` only when docs/FIELDED_SHELL.md at its own ref says so (a release candidate is
not, and does not make board nets ``hw-proven``); ``mps3_pin_facts.SHELL_PBLOCK`` holds the
pblock facts that differ for one shell (RC2's ``dut_clk`` BUFGCE).

Sources (every fact in the output names the one it came from, with the commit):

- the platform repo (``--platform``, default ``../mps3-nanosoc-platform``, or
  ``$HM_PLATFORM_DIR``), read ONLY through ``git show <ref>:<path>`` so the working
  tree's branch never matters. ``--ref`` defaults to ``feat/rm-ila-mint``, the branch
  that holds the fielded static 0x72BB0A36's boundary;
- the Xilinx IBIS package model for xcku115-flvb1760 (ships with Vivado; no licence,
  no Vivado run): package pin -> IO function name -> bank and clock capability.

Cross-checks that fail the generation (exit 2) instead of writing a wrong model:

- the partition boundary is read three ways, and all three must agree:
  ``docs/contracts/partition-pins.md`` (the tables), ``fpga/shell/boundary.yaml`` (the
  declaration) and ``fpga/shell/rp_dut_stub.sv`` (the port list the static was built with);
- every shell source file must hash to the value in the fielded mint record
  (``fielded/<static_id>/mint.json`` ``static_canon``), so the model describes the static
  that is on the board and not a later edit;
- every shell pin must be a pin of the Arm pinmap with the same IO standard;
- every bank must have one VCCO;
- every curated fact's citation (tools/mps3_pin_facts.py) must still be in its file.

Never hand-edit the JSON: change a source or a fact and run this again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

import mps3_pin_facts as F  # noqa: E402

from harness_manager.services.xdc.hdl import (  # noqa: E402
    bit_sort_key,
    expand,
    parse_ansi_ports,
    parse_xdc_pins,
)

OUT = REPO / "src" / "harness_manager_mps3" / "pins" / "mps3_board_pins.json"
DEFAULT_REF = "feat/rm-ila-mint"
DEFAULT_STATIC = "0x72BB0A36"
#: The shells the committed model describes, ``SID@REF``, the default shell first. RC2
#: (0x44EE76D5) is pinned to the commit that published its record (feat/linux-harness
#: 6beea09): a minted static never changes, and the branch head moves every day.
SHELLS = ["0x72BB0A36@feat/rm-ila-mint",
          "0x44EE76D5@6beea093ff5e11517282b44034bdf59d0cbef746"]
PART = "xcku115-flvb1760-1-c"
PKG_CANDIDATES = [
    "{xilinx}/data/parts/xilinx/kintexu/public/ibis/pkg/xcku115_flvb1760.pkg",
    "/apps/Xilinx/Vivado/2024.1/data/parts/xilinx/kintexu/public/ibis/pkg/xcku115_flvb1760.pkg",
    "/tools/Xilinx/Vivado/2024.1/data/parts/xilinx/kintexu/public/ibis/pkg/xcku115_flvb1760.pkg",
]
SCHEMA = "harness-manager/board-pins"
SCHEMA_VERSION = 1

# The shell's pin constraint files, and the static_canon flag that puts each in a build
# (None: every build; build_shell.tcl globs constraints/*.xdc). 0x72BB0A36's flags say
# SHELL_TOUCH=1 and SHELL_REALPHY=0, so the touch XDC is in and the realphy XDCs are out.
SHELL_PIN_FILES = ["fpga/shell/constraints/mps3_harness.xdc",
                   "fpga/shell/constraints/optional/mps3_harness_touch.xdc"]
SHELL_PIN_FLAGS: dict[str, tuple[str, str] | None] = {
    "fpga/shell/constraints/mps3_harness.xdc": None,
    "fpga/shell/constraints/optional/mps3_harness_touch.xdc": ("SHELL_TOUCH", "1"),
}
# A shell's pins the model does not describe yet (the Arm pinmap does not place them): the
# shell's block notes them instead of failing. The MicroBlaze V (Linux) shell's DDR4 SODIMM.
UNMODELLED_PIN_FILES: dict[str, tuple[tuple[str, str], str]] = {
    "fpga/shell/constraints/mbv/ddr4_pins.xdc": (("SHELL_CPU", "mbv"), "DDR4 SODIMM (banks 49-51)"),
}
PINMAP = "fpga/monolithic/nanosoc_mps3.xdc"
PINMAP_TOP = "fpga/monolithic/nanosoc_mps3_top.sv"
SHELL_TOP = "fpga/shell/shell_top.sv"
PARTITION_MD = "docs/contracts/partition-pins.md"
BOUNDARY_YAML = "fpga/shell/boundary.yaml"
STUB = "fpga/shell/rp_dut_stub.sv"
REALPHY_PINS = "fpga/shell/constraints_realphy/mps3_realphy_pins.xdc"


class GenError(Exception):
    pass


# --- the platform repo, read through git only --------------------------------------------------


class Platform:
    def __init__(self, root: Path, ref: str) -> None:
        self.root, self.ref = root, ref
        self._cache: dict[str, str] = {}
        try:
            self.commit = self._git("rev-parse", f"{ref}^{{commit}}").strip()
        except subprocess.CalledProcessError as exc:
            raise GenError(f"{root}: cannot resolve {ref!r}: {exc.stderr.strip()}") from exc

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def text(self, path: str) -> str:
        if path not in self._cache:
            try:
                self._cache[path] = self._git("show", f"{self.commit}:{path}")
            except subprocess.CalledProcessError as exc:
                raise GenError(f"{path} is not in {self.ref}: {exc.stderr.strip()}") from exc
        return self._cache[path]

    def raw(self, path: str) -> bytes:
        return subprocess.run(["git", "-C", str(self.root), "show", f"{self.commit}:{path}"],
                              check=True, capture_output=True).stdout

    def last_commit(self, path: str) -> str:
        return self._git("log", "-1", "--format=%H %cs", self.commit, "--", path).strip()


class Sources:
    """The provenance table: one key per source file (per commit).

    ``plat`` and ``fielded_hashes`` are the SHELL BEING BUILT's (``use``): the default
    shell's first, then each other shell's. A file already in the table at the same commit,
    or with the same bytes, keeps its key (a line cited in it is the same line); the same
    path with other bytes gets ``<key>@<sid>``. ``touched`` is every key the current shell
    read, for its own fielded-hash check."""

    def __init__(self, plat: Platform) -> None:
        self.plat = plat
        self.table: dict[str, dict[str, Any]] = {}
        self.fielded_hashes: dict[str, str] = {}
        self.tag = ""
        self.touched: set[str] = set()

    def use(self, plat: Platform, fielded_hashes: dict[str, str], tag: str) -> None:
        self.plat, self.fielded_hashes, self.tag, self.touched = plat, fielded_hashes, tag, set()

    def add(self, key: str, path: str, role: str) -> str:
        for k, e in self.table.items():            # one key per file, whoever asks first
            if e["path"] == path and e.get("commit") == self.plat.commit:
                self.touched.add(k)
                return k
        data = self.plat.raw(path)
        sha = hashlib.sha256(data).hexdigest()
        for k, e in self.table.items():            # the same bytes at another commit
            if e["path"] == path and e.get("sha256") == sha:
                self.touched.add(k)
                return k
        if key in self.table:                      # the same key for another file's bytes
            key = f"{key}@{self.tag}" if self.tag else key
        if key not in self.table:
            commit, _, date = self.plat.last_commit(path).partition(" ")
            entry = {"repo": "mps3-nanosoc-platform", "ref": self.plat.ref,
                     "commit": self.plat.commit, "path": path, "role": role,
                     "sha256": hashlib.sha256(data).hexdigest(),
                     "last_changed": {"commit": commit, "date": date}}
            if path in self.fielded_hashes:
                entry["fielded_static_input"] = entry["sha256"] == self.fielded_hashes[path]
            self.table[key] = entry
        self.touched.add(key)
        return key

    def stale_for_shell(self) -> list[str]:
        """The current shell's static inputs this model read that differ from its record."""
        return sorted(e["path"] for k, e in self.table.items() if k in self.touched
                      and e["path"] in self.fielded_hashes
                      and e["sha256"] != self.fielded_hashes[e["path"]])

    def add_external(self, key: str, path: Path, role: str) -> str:
        data = path.read_bytes()
        self.table[key] = {"path": str(path), "role": role,
                           "sha256": hashlib.sha256(data).hexdigest()}
        return key


def cite(plat: Platform, sources: Sources, path: str, pattern: str, role: str = "") -> str:
    """``key:line`` for the first line of ``path`` that holds ``pattern``; GenError if none."""
    text = plat.text(path)
    idx = text.find(pattern)
    if idx < 0:
        raise GenError(f"citation lost: {pattern!r} is no longer in {path} at {plat.ref}")
    line = text.count("\n", 0, idx) + 1
    key = sources.add(_key(path), path, role or "cited prose")
    return f"{key}:{line}"


def _key(path: str) -> str:
    name = Path(path).name
    if path.startswith("fielded/"):          # fielded/<sid>/README.md: one per record
        name = f"{Path(path).parent.name}_{name}"
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# --- parsers -----------------------------------------------------------------------------------


def parse_pkg(path: Path) -> dict[str, str]:
    text = path.read_text(errors="replace")
    if "[Pin Numbers]" not in text:
        raise GenError(f"{path}: no [Pin Numbers] section")
    sec = re.split(r"\n\[", text.split("[Pin Numbers]", 1)[1], maxsplit=1)[0]
    pins: dict[str, str] = {}
    for line in sec.splitlines():
        m = re.match(r"^([A-Z]{1,2}\d{1,2})\s+\|\s+\d+\s+(\S+)\s", line)
        if m and m.group(2).startswith("IO_"):
            pins[m.group(1)] = m.group(2)
    if len(pins) < 100:
        raise GenError(f"{path}: only {len(pins)} IO pins read")
    return pins


def pin_function(fn: str) -> tuple[str, str]:
    """``IO_L12P_T1U_N10_GC_66`` -> ``("66", "GC")``; the clock capability is GC, QBC, DBC or ""."""
    bank = fn.rsplit("_", 1)[1]
    parts = set(fn.split("_"))
    clock = "GC" if "GC" in parts else "QBC" if "QBC" in parts else "DBC" if "DBC" in parts else ""
    return bank, clock


def parse_partition_md(text: str) -> list[dict[str, Any]]:
    groups = []
    for m in re.finditer(r"<!-- BEGIN GENERATED\[boundary-(?P<id>[a-z0-9_]+)\][^\n]*\n(?P<body>.*?)"
                         r"<!-- END GENERATED", text, re.S):
        rows = [r for r in m.group("body").splitlines() if r.startswith("|")]
        header = [c.strip().lower() for c in rows[0].strip("|").split("|")]
        signals = []
        for row in rows[2:]:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            rec = dict(zip(header, cells, strict=False))
            width = rec["width"]
            signals.append({"name": rec["signal"].strip("`"), "dir": rec["dir"],
                            "width": int(width) if width.isdigit() else width,
                            "domain": rec.get("domain", ""), "note": rec.get("notes", "")})
        groups.append({"id": m.group("id"), "signals": signals,
                       "line": text.count("\n", 0, m.start()) + 1})
    if not groups:
        raise GenError(f"{PARTITION_MD}: no GENERATED boundary tables")
    return groups


def parse_boundary_yaml(text: str) -> dict[str, Any]:
    """The regular subset of boundary.yaml that the boundary needs (no YAML library)."""
    out: dict[str, Any] = {"groups": [], "totals": {}}
    m = re.search(r"^ngpio:\s*(\d+)", text, re.M)
    out["ngpio"] = int(m.group(1)) if m else None
    for key in ("ports", "bits"):
        m = re.search(rf"^totals:\s*\n(?:\s+\w+:.*\n)*?\s+{key}:\s*(\d+)", text, re.M)
        out["totals"][key] = int(m.group(1)) if m else None
    group = sig = None
    for line in text.splitlines():
        s = line.split("#", 1)[0].rstrip() if not line.strip().startswith("note") else line.rstrip()
        m = re.match(r"^\s+- id:\s*(\w+)", s)
        if m:
            group = {"id": m.group(1), "signals": []}
            out["groups"].append(group)
            continue
        m = re.match(r'^\s+- name:\s*"?([\w]+)"?', s)
        if m and group is not None:
            sig = {"name": m.group(1)}
            group["signals"].append(sig)
            continue
        m = re.match(r'^\s+(dir|width|clamp|domain):\s*"?([^"]*)"?\s*$', s)
        if m and sig is not None:
            val: Any = m.group(2).strip()
            if m.group(1) in ("width", "clamp") and re.fullmatch(r"\d+", val):
                val = int(val)
            sig[m.group(1)] = val
    return out


# --- the model ---------------------------------------------------------------------------------


def find_pkg(explicit: str | None) -> Path:
    cands = [explicit] if explicit else [c.format(xilinx=os.environ.get("XILINX_VIVADO", "/nonexistent"))
                                          for c in PKG_CANDIDATES]
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    raise GenError("the Xilinx IBIS package file xcku115_flvb1760.pkg was not found "
                   "(pass --pkg, or set XILINX_VIVADO)")


def read_record(plat: Platform, static_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(mint, canon)``: the fielded mint record of ``static_id`` at ``plat`` and its
    ``static_canon`` value (``mint.json``, else the record's ``static_canon.json``)."""
    mint_path = f"fielded/{static_id}/mint.json"
    mint = json.loads(plat.text(mint_path))
    if mint.get("static_id", "").lower() != static_id.lower():
        raise GenError(f"{mint_path} is for {mint.get('static_id')}, not {static_id}")
    canon = (mint.get("static_canon") or {}).get("value")
    if canon is None:
        try:
            raw = json.loads(plat.text(f"fielded/{static_id}/static_canon.json"))
        except GenError:
            raw = None
        canon = raw.get("value", raw) if isinstance(raw, dict) else None
    if not isinstance(canon, dict) or not canon.get("inputs"):
        raise GenError(f"{mint_path}: no static_canon with inputs (the hashes the shell's "
                       "sources must match)")
    if canon.get("part") != PART:
        raise GenError(f"{mint_path}: part {canon.get('part')!r}, expected {PART}")
    return mint, canon


FIELDED_MD = "docs/FIELDED_SHELL.md"


def fielded_row(plat: Platform) -> tuple[str, str]:
    """``(static_id, pattern)`` of the ``fielded`` row of docs/FIELDED_SHELL.md at ``plat``
    (the one authority for what the board runs); ``("", "")`` when there is none."""
    try:
        text = plat.text(FIELDED_MD)
    except GenError:
        return "", ""
    m = re.search(r"^\| `fielded` \| `(0x[0-9A-Fa-f]{8})` \|", text, re.M)
    return (m.group(1), m.group(0)) if m else ("", "")


def _flag_on(flags: dict[str, Any], gate: tuple[str, str] | None) -> bool:
    return gate is None or str(flags.get(gate[0], "")) == gate[1]


def shell_pins(plat: Platform, src: Sources, flags: dict[str, Any], nets: dict[str, Any],
               by_pin: dict[str, str], *, board_owner: bool, fielded: bool = True
               ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Which board nets the shell's constraints place: ``(shell_nets, clocks, notes)``.
    ``board_owner``: the default shell annotates the board nets (``hw-proven`` and the
    source line); another FIELDED shell annotates only a net no earlier shell placed, and a
    shell that is not fielded (a release candidate) annotates none."""
    shell_nets: dict[str, dict[str, Any]] = {}
    shell_clocks = []
    notes: list[str] = []
    for path in SHELL_PIN_FILES:
        if not _flag_on(flags, SHELL_PIN_FLAGS.get(path)):
            continue
        key = src.add(_key(path), path, "a fielded-shell pin constraint file")
        first = plat.text(path).splitlines()[0].lstrip("#").strip()
        file_title = first.split(" -- ", 1)[1].rstrip(".") if " -- " in first else path
        sp, sclk = parse_xdc_pins(plat.text(path))
        for pname, p in sp.items():
            if not p.pin:
                continue
            board = by_pin.get(p.pin)
            if board is None:
                raise GenError(f"{path}:{p.pin_line}: {pname} on {p.pin}, a pin the Arm pinmap does not place")
            bnet = nets[board]
            if bnet["iostandard"] and p.iostandard and bnet["iostandard"] != p.iostandard:
                raise GenError(f"{path}:{p.pin_line}: {pname} on {p.pin} is {p.iostandard}; the "
                               f"pinmap says {board} is {bnet['iostandard']}")
            shell_nets[board] = {"shell_port": pname, "src": f"{key}:{p.pin_line}",
                                 "iostandard": p.iostandard, "props": dict(sorted((p.props or {}).items())),
                                 "function": p.group or file_title}
            if board_owner or (fielded and bnet["verified"] != "hw-proven"):
                bnet["verified"] = "hw-proven"
                bnet["src"].append(f"{key}:{p.pin_line}")
        for c in sclk:
            board = by_pin.get(sp[c.port].pin) if c.port in sp else None
            shell_clocks.append({"port": c.port, "net": board, "name": c.name,
                                 "period_ns": c.period_ns, "src": f"{key}:{c.line}"})
    for path, (gate, what) in UNMODELLED_PIN_FILES.items():
        if _flag_on(flags, gate):
            try:
                n = sum(1 for ln in plat.text(path).splitlines()
                        if re.match(r"^\s*set_property\s+PACKAGE_PIN\b", ln))
            except GenError:
                n = 0
            notes.append(f"{what}: {n} pins placed by {path} ({gate[0]}={gate[1]}) are this "
                         "shell's but not modelled: the Arm pinmap does not place them")
    return shell_nets, shell_clocks, notes


def shell_dirs(plat: Platform, src: Sources, shell_nets: dict[str, Any], nets: dict[str, Any],
               top_ports: dict[str, Any], *, board_owner: bool) -> None:
    shell_top_ports = {b: p for p in parse_ansi_ports(plat.text(SHELL_TOP)) for b in p.bits()}
    k_stop = src.add("shell_top", SHELL_TOP, "the fielded static's top (shell port directions)")
    for board, sn in shell_nets.items():
        tp = shell_top_ports.get(sn["shell_port"])
        if tp is None:
            raise GenError(f"{SHELL_TOP} has no port {sn['shell_port']}")
        sn["shell_dir"] = tp.direction
        sn["src"] = [sn["src"], f"{k_stop}:{tp.line}"]
        if tp.direction != nets[board]["dir"] and (board_owner or "dir_note" not in nets[board]):
            nets[board]["dir"] = "inout"      # e.g. an I2C pad the Arm wrapper drives only
            nets[board].setdefault("dir_note", f"the Arm wrapper declares {top_ports[board].direction}, "
                                               f"the fielded shell {tp.direction}")


def shell_boundary(plat: Platform, src: Sources) -> dict[str, Any]:
    """The partition boundary: three renders (partition-pins.md, boundary.yaml, the stub)
    must agree."""
    k_md = src.add("partition_pins", PARTITION_MD, "the RP boundary contract (rendered tables)")
    k_y = src.add("boundary_yaml", BOUNDARY_YAML, "the RP boundary declaration")
    k_stub = src.add("rp_dut_stub", STUB, "the inert RP stub the static was built with")
    md = parse_partition_md(plat.text(PARTITION_MD))
    yml = parse_boundary_yaml(plat.text(BOUNDARY_YAML))
    ngpio = yml["ngpio"]
    stub = {p.name: p for p in parse_ansi_ports(plat.text(STUB), params={"NGPIO": ngpio or 16})}
    ygroups = {g["id"]: {s["name"]: s for s in g["signals"]} for g in yml["groups"]}
    rm_dir = {"O": "in", "I": "out"}
    groups_out = []
    ports = bits = 0
    for g in md:
        ys = ygroups.get(g["id"])
        if ys is None:
            raise GenError(f"{BOUNDARY_YAML} has no group {g['id']!r}")
        if set(ys) != {s["name"] for s in g["signals"]}:
            raise GenError(f"group {g['id']}: partition-pins.md and boundary.yaml list different signals")
        sigs = []
        for s in g["signals"]:
            y = ys[s["name"]]
            width = ngpio if s["width"] == "NGPIO" else s["width"]
            if y.get("dir") != s["dir"] or str(y.get("width")) != str(s["width"]):
                raise GenError(f"{s['name']}: partition-pins.md says {s['dir']}/{s['width']}, "
                               f"boundary.yaml {y.get('dir')}/{y.get('width')}")
            sp = stub.get(s["name"])
            if sp is None:
                raise GenError(f"{STUB} has no port {s['name']}")
            if sp.direction != rm_dir[s["dir"]] or sp.width != width:
                raise GenError(f"{s['name']}: the stub is {sp.direction}[{sp.width}], the contract "
                               f"{rm_dir[s['dir']]}[{width}]")
            clamp = y.get("clamp")
            sigs.append({"name": s["name"], "shell_dir": s["dir"], "rm_dir": rm_dir[s["dir"]],
                         "width": width, "width_symbol": s["width"] if s["width"] == "NGPIO" else None,
                         "clamp": None if clamp in (None, "none") else clamp,
                         "decoupled": clamp not in (None, "none"),
                         "domain": s["domain"] or None, "note": s["note"],
                         "src": [f"{k_md}:{g['line']}", f"{k_stub}:{sp.line}"]})
            ports += 1
            bits += width
        groups_out.append({"id": g["id"], "signals": sigs})
    extra = sorted(set(stub) - {s["name"] for g in groups_out for s in g["signals"]})
    if extra:
        raise GenError(f"{STUB} has ports the contract lacks: {extra}")
    if (ports, bits) != (yml["totals"]["ports"], yml["totals"]["bits"]):
        raise GenError(f"the boundary counts {ports} ports / {bits} bits, boundary.yaml says "
                       f"{yml['totals']}")
    intf_m = re.search(r"(\d+) INTFs, IDs 0-(\d+)", plat.text(BOUNDARY_YAML))
    return {"totals": {"ports": ports, "bits": bits,
                       "decoupler_intfs": int(intf_m.group(1)) if intf_m else None},
            "ngpio": ngpio, "direction_convention": "shell_dir is the shell's view (O = shell drives); "
                                                     "rm_dir is the RM's port direction",
            "groups": groups_out, "src": [f"{k_md}:1", f"{k_y}:1", f"{k_stub}:1"]}


def shell_facts(plat: Platform, src: Sources, boundary: dict[str, Any], nets: dict[str, Any],
                sid: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """``(boundary_clocks, connectivity, pblock)``: the curated facts, cited at ``plat``;
    ``F.SHELL_PBLOCK[sid]`` replaces a pblock fact for that shell."""
    groups_out = boundary["groups"]
    bclocks = []
    for bc in F.BOUNDARY_CLOCKS:
        bclocks.append({k: v for k, v in bc.items() if k != "cite"} | {"src": cite(plat, src, *bc["cite"])})

    all_sigs = {s["name"]: (g["id"], s) for g in groups_out for s in g["signals"]}
    conn = []
    covered: set[str] = set()
    for c in F.CONNECTIVITY:
        ref = cite(plat, src, *c["cite"])
        names = [c["signal"]] if "signal" in c else \
            [s["name"] for s in next(g for g in groups_out if g["id"] == c["group"])["signals"]]
        for name in names:
            if name not in all_sigs:
                raise GenError(f"connectivity fact for {name}, not a boundary signal")
            gid, s = all_sigs[name]
            hi, lo = c.get("bits", [s["width"] - 1, 0])
            rows = []
            for i in range(lo, hi + 1) if (s["width"] > 1 and ("board_net" in c or "{i}" in c["static"])) else [None]:
                bn = None
                if "board_net" in c:
                    expr = c["board_net"]
                    mm = re.search(r"\{i([+-]\d+)?\}", expr)
                    if mm:
                        bn = expr.replace(mm.group(0), str(i + int(mm.group(1) or 0)))
                    else:
                        bn = expr
                    if bn not in nets:
                        raise GenError(f"connectivity: {bn} is not a board net")
                rows.append({"bit": i, "static": c["static"].replace("{i}", str(i) if i is not None else "*"),
                             "board_net": bn, "pin": nets[bn]["pin"] if bn else None})
            conn.append({"signal": name, "group": gid, "bits": [hi, lo] if s["width"] > 1 else None,
                         "via": c.get("via", "-"), "rows": rows, "src": ref})
            covered.add(name)
    uncovered = sorted(set(all_sigs) - covered)
    if uncovered:
        raise GenError(f"no connectivity fact for {uncovered}")

    own = {p["key"]: p for p in getattr(F, "SHELL_PBLOCK", {}).get(sid, [])}
    pblock = {}
    for p in F.PBLOCK:
        p = own.pop(p["key"], p)
        pblock[p["key"]] = {"value": p["value"], "src": cite(plat, src, *p["cite"])}
    for p in own.values():
        pblock[p["key"]] = {"value": p["value"], "src": cite(plat, src, *p["cite"])}
    return bclocks, conn, pblock


def shell_block(sid: str, mint: dict[str, Any], flags: dict[str, Any], plat: Platform,
                shell_nets: dict[str, Any], shell_clocks: list[dict[str, Any]],
                nets: dict[str, Any], boundary: dict[str, Any], bclocks: list[dict[str, Any]],
                conn: list[dict[str, Any]], pblock: dict[str, Any], mint_key: str,
                notes: list[str], fielded: bool = True,
                fielded_src: str = "") -> dict[str, Any]:
    owned = sorted(shell_nets, key=bit_sort_key)
    free = sorted((n for n in nets if n not in shell_nets), key=bit_sort_key)
    block = {
        "static_id": sid, "fielded": fielded,
        "usercode": mint.get("static_usercode", {}).get("value", {}).get("usercode"),
        "flags": flags, "mint_record": f"{mint_key}:1",
        "platform": {"ref": plat.ref, "commit": plat.commit},
        "owns": {n: shell_nets[n] for n in owned},
        "free": free,
        "clocks": shell_clocks,
        "rp_boundary": boundary,
        "boundary_clocks": bclocks,
        "connectivity": conn,
        "pblock": pblock,
    }
    if not fielded:
        block["fielded_src"] = fielded_src
    if notes:
        block["notes"] = notes
    return block


def build(shells: list[tuple[Platform, str]] | Platform, pkg_path: Path,
          static_id: str = "") -> dict[str, Any]:
    """The model for ``shells`` (``[(platform at its ref, static_id)]``, the default first).
    ``build(plat, pkg, sid)`` is the one-shell form."""
    if isinstance(shells, Platform):
        shells = [(shells, static_id or DEFAULT_STATIC)]
    if not shells:
        raise GenError("no shell to model")
    seen_ids = [s.lower() for _, s in shells]
    if len(set(seen_ids)) != len(seen_ids):
        raise GenError(f"a shell is named twice: {[s for _, s in shells]}")
    plat, static_id = shells[0]
    src = Sources(plat)
    notes: list[str] = []

    # -- the fielded mint record: the hashes every static input must match ------------------
    mint, canon = read_record(plat, static_id)
    src.use(plat, {i["path"]: i["sha256"] for i in canon["inputs"]}, "")
    mint_key = src.add("mint_record", f"fielded/{static_id}/mint.json",
                       f"the fielded mint record of {static_id}")
    flags = canon.get("flags", {})

    # -- the package ----------------------------------------------------------------------
    pkg = parse_pkg(pkg_path)
    src.add_external("xilinx_pkg", pkg_path, F.PKG_NOTE)
    package_pins = {}
    for pin, fn in sorted(pkg.items()):
        bank, clock = pin_function(fn)
        package_pins[pin] = {"bank": bank, "function": fn, "clock_capable": clock}

    # -- the Arm pinmap: every board net ----------------------------------------------------
    k_map = src.add("pinmap", PINMAP, "the Arm MPS3 pinmap, ported verbatim (every board net)")
    k_top = src.add("pinmap_top", PINMAP_TOP, "the Arm MPS3 wrapper's port directions")
    xports, xclocks = parse_xdc_pins(plat.text(PINMAP))
    top_ports = {b: p for p in parse_ansi_ports(plat.text(PINMAP_TOP)) for b in p.bits()}
    group_ids = []
    nets: dict[str, dict[str, Any]] = {}
    for name, xp in xports.items():
        if not xp.pin:
            raise GenError(f"{PINMAP}: {name} has properties but no PACKAGE_PIN")
        if xp.pin not in package_pins:
            raise GenError(f"{PINMAP}:{xp.pin_line}: {name} on {xp.pin}, not an IO pin of {PART}")
        gid = next((g for t, g, _ in F.GROUPS if xp.group.startswith(t)), None)
        if gid is None:
            raise GenError(f"{PINMAP}: no group id for the banner {xp.group!r} (add it to mps3_pin_facts.GROUPS)")
        if gid not in group_ids:
            group_ids.append(gid)
        tp = top_ports.get(name)
        if tp is None:
            raise GenError(f"{PINMAP}: {name} is placed but {PINMAP_TOP} has no such port")
        net = {"pin": xp.pin, "bank": package_pins[xp.pin]["bank"],
               "iostandard": xp.iostandard or None, "dir": tp.direction, "group": gid,
               "verified": "pinmap", "src": [f"{k_map}:{xp.pin_line}", f"{k_top}:{tp.line}"]}
        if not xp.iostandard:
            net["iostandard_reason"] = f"{PINMAP} gives {name} no IOSTANDARD"
            notes.append(f"{name}: no IOSTANDARD in the Arm pinmap")
        if xp.props:
            net["props"] = dict(sorted(xp.props.items()))
        nets[name] = net
    missing_top = sorted(set(top_ports) - set(nets))
    if missing_top:
        raise GenError(f"{PINMAP_TOP} ports with no pin in {PINMAP}: {missing_top[:8]}")
    by_pin = {n["pin"]: name for name, n in nets.items()}

    # -- clocks -------------------------------------------------------------------------------
    clocks = []
    for c in xclocks:
        if c.port not in nets:
            raise GenError(f"{PINMAP}:{c.line}: create_clock on {c.port}, not a board net")
        clocks.append({"net": c.port, "period_ns": c.period_ns,
                       "mhz": round(1000.0 / c.period_ns, 3), "src": f"{k_map}:{c.line}",
                       "what": "Arm fpga_timing.xdc board clock"})
        nets[c.port]["clock_ns"] = c.period_ns
    for osc in F.OSCILLATORS:
        path, pat = osc["cite"]
        ref = cite(plat, src, path, pat, "the MCC's oscillator set-points (SD config)")
        mhz = float(pat.split(":")[1])
        net = nets[osc["net"]]
        net["oscillator"] = {"osc": osc["osc"], "mhz": mhz, "src": ref}
        if not any(c["net"] == osc["net"] for c in clocks):
            clocks.append({"net": osc["net"], "period_ns": round(1000.0 / mhz, 3), "mhz": mhz,
                           "src": ref, "what": f"{osc['osc']} as programmed by the MCC (SD config)"})
    clocks.sort(key=lambda c: bit_sort_key(c["net"]))

    # -- reserved nets and cautions -----------------------------------------------------------
    for r in F.RESERVED:
        ref = cite(plat, src, *r["cite"])
        for n in r["nets"]:
            nets[n]["reserved"] = {"reason": r["reason"], "src": ref}
    for c in F.CAUTIONS:
        ref = cite(plat, src, *c["cite"])
        for n in c["nets"]:
            nets[n].setdefault("cautions", []).append({"text": c["text"], "src": ref})

    # -- the shell: which board nets the fielded static owns -------------------------------------
    shell_nets, shell_clocks, shell_notes = shell_pins(plat, src, flags, nets, by_pin,
                                                       board_owner=True)
    shell_dirs(plat, src, shell_nets, nets, top_ports, board_owner=True)

    # -- the realphy variant: measured pins, not fielded -----------------------------------------
    rk = src.add("realphy_pins", REALPHY_PINS, "LAN8720 shield PHY pins (SHELL_REALPHY variant, measured)")
    for n, line in enumerate(plat.text(REALPHY_PINS).splitlines(), 1):
        m = re.search(r"PACKAGE_PIN (\w+) .*\[get_ports \{?([\w\[\]]+)\}?\]", line)
        if m and m.group(1) in by_pin:
            nets[by_pin[m.group(1)]].setdefault("variants", []).append(
                {"shell_variant": "SHELL_REALPHY (not in the fielded static)",
                 "use": f"LAN8720 {m.group(2)}", "verified": "measured", "src": f"{rk}:{n}"})

    # -- banks ----------------------------------------------------------------------------------
    banks: dict[str, dict[str, Any]] = {}
    for pp in package_pins.values():
        b = banks.setdefault(pp["bank"], {"io_pins": 0, "vcco": None, "evidence": {}})
        b["io_pins"] += 1
    for name, n in sorted(nets.items(), key=lambda kv: bit_sort_key(kv[0])):
        std = n["iostandard"]
        if not std:
            continue
        if std not in F.IOSTANDARD_VCCO:
            raise GenError(f"{name}: IOSTANDARD {std} is not in the VCCO table")
        b = banks[n["bank"]]
        b["evidence"].setdefault(std, []).append(name)
    for bank, b in banks.items():
        volts = {F.IOSTANDARD_VCCO[s] for s in b["evidence"]}
        if len(volts) > 1:
            raise GenError(f"bank {bank}: the board's own standards need VCCO {sorted(volts)}")
        if volts:
            b["vcco"] = volts.pop()
            b["vcco_source"] = "inferred from the IO standards the board's pinmap uses in this bank"
            b["evidence"] = {s: {"count": len(v), "example": v[0]} for s, v in sorted(b["evidence"].items())}
        else:
            b["vcco_reason"] = "no board net of the pinmap sits in this bank: VCCO unknown"
            b["evidence"] = {}
        if b["vcco"] == 3.3:
            b["kind"] = "HR (3.3 V; UltraScale HP banks stop at 1.8 V)"
    banks = dict(sorted(banks.items(), key=lambda kv: int(kv[0])))

    # -- the partition boundary, its clocks, connectivity and pblock ----------------------------
    boundary = shell_boundary(plat, src)
    bclocks, conn, pblock = shell_facts(plat, src, boundary, nets, static_id)

    # connectors
    connectors = []
    for c in F.CONNECTORS:
        entry = {k: v for k, v in c.items() if k != "cite"} | {"src": cite(plat, src, *c["cite"])}
        for net in ([*expand(c["nets"])] if "nets" in c else []) + list((c.get("pins") or {}).values()):
            if net not in nets:
                raise GenError(f"connector {c['id']}: {net} is not a board net")
        connectors.append(entry)
    for c in connectors:
        for pin_no, net in sorted((c.get("pins") or {}).items(), key=lambda kv: int(kv[0])):
            nets[net].setdefault("connectors", []).append({"connector": c["id"], "pin": pin_no,
                                                           "verified": "inferred", "src": c["src"]})

    # sanity: every source this model read that the fielded static was BUILT from matches
    stale = src.stale_for_shell()
    if stale:
        raise GenError(f"these files differ from what {static_id} was built from: {stale} "
                       f"(pick the ref that holds the fielded static)")

    config = {}
    for line_no, line in enumerate(plat.text(SHELL_PIN_FILES[0]).splitlines(), 1):
        m = re.match(r"^set_property\s+(CONFIG_VOLTAGE|CFGBVS)\s+(\S+)\s+\[current_design\]", line)
        if m:
            config[m.group(1)] = {"value": m.group(2), "src": f"{_key(SHELL_PIN_FILES[0])}:{line_no}"}

    shells_out = {static_id: shell_block(static_id, mint, flags, plat, shell_nets, shell_clocks,
                                         nets, boundary, bclocks, conn, pblock, mint_key,
                                         shell_notes)}

    # -- every other shell: its own record, its own ref, the same board --------------------------
    for splat, sid in shells[1:]:
        smint, scanon = read_record(splat, sid)
        src.use(splat, {i["path"]: i["sha256"] for i in scanon["inputs"]}, sid)
        skey = src.add(f"mint_record_{_key(sid)}", f"fielded/{sid}/mint.json",
                       f"the fielded mint record of {sid}")
        sflags = scanon.get("flags", {})
        # fielded only when docs/FIELDED_SHELL.md at its own ref says so (a release candidate
        # has a record before the cutover); a shell that is not does not make nets hw-proven
        row, pattern = fielded_row(splat)
        s_fielded = row.lower() == sid.lower()
        f_src = cite(splat, src, FIELDED_MD, pattern, "what the board runs (the fielded row)") \
            if pattern else ""
        s_nets, s_clocks, s_notes = shell_pins(splat, src, sflags, nets, by_pin,
                                               board_owner=False, fielded=s_fielded)
        shell_dirs(splat, src, s_nets, nets, top_ports, board_owner=False)
        sbound = shell_boundary(splat, src)
        sbclk, sconn, spb = shell_facts(splat, src, sbound, nets, sid)
        stale = src.stale_for_shell()
        if stale:
            raise GenError(f"these files differ from what {sid} was built from: {stale} "
                           f"(pick the ref that holds the fielded static: --shell {sid}@REF)")
        shells_out[sid] = shell_block(sid, smint, sflags, splat, s_nets, s_clocks, nets, sbound,
                                      sbclk, sconn, spb, skey, s_notes, fielded=s_fielded,
                                      fielded_src=f_src)

    model = {
        "schema": SCHEMA, "schema_version": SCHEMA_VERSION,
        "board": {"id": "mps3-hbi0309c", "pack": "mps3",
                  "title": "Arm V2M-MPS3 (HBI0309C), Kintex UltraScale KU115",
                  "part": PART, "package": "flvb1760"},
        "status": {
            "derived": True, "lane_c": False,
            "label": "DERIVED (harness Lane C's board_pins.yaml does not exist yet)",
            "generator": "tools/gen_mps3_pins.py", "platform_ref": plat.ref,
            "platform_commit": plat.commit,
            "verified_levels": {
                "hw-proven": "placed by the fielded static's own constraints, running on the board",
                "measured": "measured on the bench (a non-fielded shell variant)",
                "pinmap": "Arm's MPS3 pinmap, ported verbatim; not exercised by this platform",
                "inferred": "a bench report or a historical survey; not a pinmap",
            },
            "notes": notes + [F.CONNECTOR_NOTE,
                              "DDR4 (banks 49-51, src/linux_soc/hw/pins.csv) and FMC are not modelled yet."],
        },
        "sources": dict(sorted(src.table.items())),
        "config": config,
        "iostandards": {k: {"vcco": v} for k, v in sorted(F.IOSTANDARD_VCCO.items())},
        "groups": {gid: {"title": t, "reaches": r} for t, gid, r in F.GROUPS if gid in group_ids},
        "banks": banks,
        "package_pins": package_pins,
        "nets": dict(sorted(nets.items(), key=lambda kv: bit_sort_key(kv[0]))),
        "clocks": clocks,
        "connectors": connectors,
        "default_shell": static_id,
        "shells": shells_out,
    }
    return model


def render(model: dict[str, Any]) -> str:
    return json.dumps(model, indent=1, sort_keys=False, ensure_ascii=False) + "\n"


def parse_shell(spec: str, default_ref: str) -> tuple[str, str]:
    """``"0x44EE76D5@feat/x"`` -> ``("0x44EE76D5", "feat/x")``; no ``@``: the default ref."""
    sid, _, ref = spec.partition("@")
    if not re.fullmatch(r"0x[0-9A-Fa-f]{8}", sid):
        raise GenError(f"--shell {spec!r}: the static id must be 0x followed by 8 hex digits")
    return sid, ref or default_ref


def fielded_ids(plat: Platform) -> list[str]:
    """Every ``fielded/<sid>/mint.json`` at ``plat``'s commit."""
    try:
        out = plat._git("ls-tree", "--name-only", f"{plat.commit}:fielded")
    except subprocess.CalledProcessError:
        return []
    return [n for n in out.split() if re.fullmatch(r"0x[0-9A-Fa-f]{8}", n)
            and _has(plat, f"fielded/{n}/mint.json")]


def _has(plat: Platform, path: str) -> bool:
    return subprocess.run(["git", "-C", str(plat.root), "cat-file", "-e", f"{plat.commit}:{path}"],
                          capture_output=True).returncode == 0


def matches_record(plat: Platform, sid: str) -> str:
    """"" when every static input of ``sid``'s record at ``plat`` hashes to the record,
    else why not."""
    try:
        _, canon = read_record(plat, sid)
    except (GenError, json.JSONDecodeError, KeyError) as exc:
        return str(exc)
    bad = []
    for i in canon["inputs"]:
        try:
            data = plat.raw(i["path"])
        except subprocess.CalledProcessError:
            bad.append(f"{i['path']} (absent)")
            continue
        if hashlib.sha256(data).hexdigest() != i["sha256"]:
            bad.append(i["path"])
    return f"{len(bad)} static inputs differ at {plat.ref}: {bad[:3]}" if bad else ""


def resolve_shells(root: Path, specs: list[str], refs: list[str], find_all: bool,
                   report: Any = None) -> list[tuple[Platform, str]]:
    """``--shell``/``--all``/the default ``SHELLS`` -> ``[(Platform, sid)]``, default first."""
    plats: dict[str, Platform] = {}

    def plat_at(ref: str) -> Platform:
        if ref not in plats:
            plats[ref] = Platform(root, ref)
        return plats[ref]

    if not find_all:
        return [(plat_at(ref), sid) for sid, ref in (parse_shell(s, refs[0]) for s in specs)]
    out: list[tuple[Platform, str]] = []
    wanted = [parse_shell(s, refs[0])[0] for s in specs]     # the default order, if named
    found: list[str] = []
    for ref in refs:
        for sid in fielded_ids(plat_at(ref)):
            if sid.lower() not in (f.lower() for f in found):
                found.append(sid)
    found.sort(key=lambda s: (wanted.index(s) if s in wanted else len(wanted), s))
    for sid in found:
        why = []
        for ref in refs:
            pl = plat_at(ref)
            if not _has(pl, f"fielded/{sid}/mint.json"):
                continue
            miss = matches_record(pl, sid)
            if not miss:
                out.append((pl, sid))
                break
            why.append(miss)
        else:
            if report:
                report(f"gen_mps3_pins: {sid} left out: no ref holds the sources its record "
                       f"hashes ({'; '.join(why) or 'no record'})")
    if not out:
        raise GenError(f"--all: no fielded shell at {refs} matches its own record")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", default=os.environ.get("HM_PLATFORM_DIR", str(REPO.parent / "mps3-nanosoc-platform")))
    ap.add_argument("--ref", action="append", default=None,
                    help=f"the ref a shell with no @REF is read at (default {DEFAULT_REF}, or "
                         "$HM_PLATFORM_REF); with --all, every ref to search (repeatable)")
    ap.add_argument("--shell", action="append", default=None, metavar="SID[@REF]",
                    help="a shell to model (repeatable; the first is the default shell). "
                         f"Default: {' '.join(SHELLS)}")
    ap.add_argument("--static-id", default=None,
                    help="one shell, at the first --ref (the pre-KIT-RC2 form of --shell)")
    ap.add_argument("--all", action="store_true",
                    help="every fielded/<sid>/ record found at the --refs")
    ap.add_argument("--pkg", default=None, help="the Xilinx IBIS package file (xcku115_flvb1760.pkg)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--check", action="store_true", help="exit 1 if --out differs from a fresh generation")
    a = ap.parse_args(argv)
    refs = a.ref or [os.environ.get("HM_PLATFORM_REF") or DEFAULT_REF]
    if a.shell:
        specs = list(a.shell)
    elif a.static_id:
        specs = [a.static_id]
    elif a.ref or os.environ.get("HM_PLATFORM_REF"):
        # an explicit --ref with no --shell: the default shells, read at that ref
        specs = [s.partition("@")[0] for s in SHELLS]
    else:
        specs = list(SHELLS)
    try:
        shells = resolve_shells(Path(a.platform), specs, refs, a.all,
                                report=lambda m: print(m, file=sys.stderr))
        text = render(build(shells, find_pkg(a.pkg)))
    except GenError as exc:
        print(f"gen_mps3_pins: {exc}", file=sys.stderr)
        return 2
    what = ", ".join(f"{sid}@{pl.ref} ({pl.commit[:10]})" for pl, sid in shells)
    out = Path(a.out)
    if a.check:
        current = out.read_text() if out.is_file() else ""
        if current != text:
            print(f"gen_mps3_pins: {out} is stale against {what}: "
                  "run tools/gen_mps3_pins.py", file=sys.stderr)
            return 1
        print(f"gen_mps3_pins: {out.name} is current against {what}")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"gen_mps3_pins: wrote {out} ({len(text)} bytes) from {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
