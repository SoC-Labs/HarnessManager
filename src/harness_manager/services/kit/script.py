"""``kit script``: one build directory for one design against one static's kit.

The directory (``--out build/my_rm``)::

    build_rm.tcl                   the generated script (render.py); all paths relative to it
    README.txt                     the next three commands
    kit/                           the kit, exported from the cache (unless --kit-dir names one)
        kit.json  static/static_routed_locked.dcp ...
    xdc/                           T10's RM kit for the design: <name>_ooc.xdc, the wrapper
                                   skeleton, the connectivity sheet, the pblock facts
    out/                           written by Vivado: the pair, the .ltx, <name>_build.json

The design is T10's design document (docs/XDC_EXPORT.md), plus two optional keys this
verb reads:

- ``rm_id``: the RM's 32-bit id. Without one HM proposes a user id (design id
  0x8000-0xFFFF, stable per name, david K8) and the skeleton and the script agree on it;
- ``build``: ``{top, sources, include_dirs, defines, synth_hook, synth_dcp, rm_xdc}``,
  paths relative to the design file. ``top`` defaults to ``rm_<name>`` (the skeleton's
  module). With no ``sources``, the design's ``wrapper`` is the one source. With neither
  (and no synth hook or DCP), the XDC kit's own skeleton ``xdc/<name>_wrapper_skeleton.sv``
  is the one source (KIT-RC2): a design that names no RTL, such as the built-in
  ``minimal``, builds as its skeleton, and the ``sources`` note says so.

The printed command (``command``, README.txt) names the FULL path of the Vivado discovery
chose when it is the kit's release (``vivado.command_vivado``): a bare ``vivado`` runs
whatever is first on PATH, which on a lab box can be another release. Nothing here runs
Vivado, except ``vivado -version`` through discovery.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import AbsentError, RefusedError, UsageError
from harness_manager.core.pack import KitCheck, kit_refusal

from . import render
from . import vivado as viv
from .schema import hex32, parse_u32
from .service import CachedKit, KitService

KIT_SUBDIR = "kit"
XDC_SUBDIR = "xdc"
OUT_SUBDIR = "out"


@dataclass
class BuildScript:
    design: str
    rm_id: str
    rm_id_proposed: bool
    static_id: str
    kit_id: str
    files: dict[str, str]                      # text files, relative to the build dir
    params: dict[str, str]
    checks: list[KitCheck] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    out_dir: Path | None = None
    written: list[Path] = field(default_factory=list)
    vivado: str = "vivado"                     # what the command starts with (a full path when found)
    vivado_release: str = ""                   # the kit's release

    @property
    def receipt(self) -> str:
        return f"{OUT_SUBDIR}/{self.design}_build.json"

    def to_json(self) -> dict[str, Any]:
        return {"design": self.design, "rm_id": self.rm_id, "rm_id_proposed": self.rm_id_proposed,
                "static_id": self.static_id, "kit_id": self.kit_id,
                "files": dict(self.files), "params": dict(self.params),
                "checks": [c.__dict__ for c in self.checks], "command": list(self.command),
                "vivado": self.vivado, "vivado_release": self.vivado_release,
                "receipt": self.receipt,
                "out_dir": str(self.out_dir) if self.out_dir else None,
                "written": [str(p) for p in self.written]}


def _design(pack: str, spec: str | dict[str, Any]) -> Any:
    from harness_manager.services import xdc

    pins = xdc.load_pack_pins(pack)
    if isinstance(spec, dict):
        return pins, xdc.from_doc(spec, origin="inline")
    return pins, xdc.load_design(str(spec), pins.designs)


def _rel_to(design: Any, value: str) -> str:
    p = Path(value)
    if p.is_absolute():
        return render.tcl_path(p)
    if design.origin.startswith(("builtin:", "inline")):
        raise UsageError(f"design {design.name}: {value!r} is relative, but the design has no "
                         "file to be relative to", hint="use an absolute path")
    return render.tcl_path(Path(design.origin).parent / p)


def make_script(kits: KitService, *, pack: str, static_id: str, design: str | dict[str, Any],
                out_dir: Path | None = None, kit_dir: Path | None = None, jobs: int = 2,
                stop_after: str = "bitstream", board: str = "", store: Any = None,
                vivado: viv.VivadoFound | None = None) -> BuildScript:
    """Render the build directory for ``design`` against the cached kit of ``static_id``;
    write it when ``out_dir`` is given. ``RefusedError`` (15) when the design fails an
    XDC check or its rm_id is 0; warnings (an rm_id clash, a proposed rm_id, no Vivado of
    the kit's release here) are listed. ``vivado``: a discovery result (default: run it)."""
    kit: CachedKit = kits.require(static_id)
    adapter = kits.adapter_for(pack)
    profile = adapter.build_profile(static_id, kit.manifest)
    if profile is None:
        raise AbsentError(f"the {pack} pack cannot build for static {static_id}")
    pins, d = _design(pack, design)
    if d.kind != "rm":
        raise UsageError(f"{d.name} is a {d.kind} design; a DUT build needs an rm design")
    d = copy.copy(d)
    d.doc = dict(d.doc)
    checks: list[KitCheck] = []

    taken = adapter.taken_designs(store)
    proposed = not d.doc.get("rm_id")
    if proposed:
        d.doc["rm_id"] = hex32(adapter.propose_rm_id(d.name, taken))
        checks.append(KitCheck("rm_id_proposed", "warning",
                               f"the design names no rm_id; HM proposes {d.doc['rm_id']} "
                               "(add it to the design to keep it)"))
    rm_id = hex32(parse_u32(d.doc["rm_id"]))
    checks += adapter.rm_id_checks(rm_id, d.name, taken)
    d.doc["static_id"] = profile.static_id

    from harness_manager.services import xdc

    files: dict[str, str] = {}
    ooc = ""
    try:
        xkit = xdc.rm_kit(pins.model, d)
    except AbsentError as exc:
        xkit = None
        checks.append(KitCheck("xdc", "unchecked", f"{exc.message}: no OOC XDC was generated; "
                               "pass -tclargs RM_OOC_XDC=<file>"))
    if xkit is not None:
        for f in xkit.findings:
            checks.append(KitCheck(f"xdc:{f.code}", "mismatch" if f.severity == "error"
                                   else "ok", f"{f.subject}: {f.reason}"))
        for name, text in xkit.files.items():
            files[f"{XDC_SUBDIR}/{name}"] = text
        ooc = f"{XDC_SUBDIR}/{d.name}_ooc.xdc"
    refusal = kit_refusal([c for c in checks if c.name.startswith(("xdc", "rm_id"))
                           and c.state == "mismatch"], f"design {d.name}")
    if refusal is not None:
        refusal.data = {"checks": [c.__dict__ for c in checks]}  # type: ignore[attr-defined]
        raise refusal

    b = d.doc.get("build") or {}
    if not isinstance(b, dict):
        raise UsageError(f"design {d.name}: build must be an object")
    sources = [_rel_to(d, s) for s in b.get("sources") or []]
    if not sources and d.wrapper_path:
        sources = [render.tcl_path(d.wrapper_path)]
    skeleton = f"{XDC_SUBDIR}/{d.name}_wrapper_skeleton.sv"
    if not sources and not b.get("synth_hook") and not b.get("synth_dcp"):
        if skeleton in files:
            # KIT-RC2: a design that names no RTL builds as its skeleton (minimal)
            sources = [skeleton]
            checks.append(KitCheck("sources", "warning",
                                   f"the design names no RTL (build.sources, or a wrapper): "
                                   f"RM_SOURCES is the XDC kit's skeleton {skeleton}, which "
                                   f"drives rm_id and ties every other output off. For your "
                                   f"own RTL set build.sources, or pass -tclargs "
                                   f"RM_SOURCES=\"a.sv b.sv\""))
        else:
            checks.append(KitCheck("sources", "warning",
                                   "the design names no RTL (build.sources, or a wrapper): set "
                                   "them, or pass -tclargs RM_SOURCES=\"a.sv b.sv\""))
    kit_rel = render.tcl_path(kit_dir) if kit_dir else KIT_SUBDIR
    locked = kit.manifest.locked_static.path
    ref = kit.manifest.pr_verify_ref
    values = {
        **render.profile_values(profile, board=board),
        "STATIC_DCP": f"{kit_rel}/{locked}",
        "PR_VERIFY_REF": "" if (not ref or ref == locked) else f"{kit_rel}/{ref}",
        "RM_NAME": d.name,
        "RM_ID": rm_id,
        "RM_TOP": str(b.get("top") or f"rm_{d.name}"),
        "RM_SOURCES": render.tcl_list(sources, "RM_SOURCES"),
        "RM_INCLUDE_DIRS": render.tcl_list([_rel_to(d, s) for s in b.get("include_dirs") or []],
                                           "RM_INCLUDE_DIRS"),
        "RM_DEFINES": render.tcl_list([str(x) for x in b.get("defines") or []], "RM_DEFINES"),
        "RM_SYNTH_HOOK": _rel_to(d, b["synth_hook"]) if b.get("synth_hook") else "",
        "RM_SYNTH_DCP": _rel_to(d, b["synth_dcp"]) if b.get("synth_dcp") else "",
        "RM_OOC_XDC": ooc,
        "RM_XDC": _rel_to(d, b["rm_xdc"]) if b.get("rm_xdc") else "",
        "OUT_DIR": OUT_SUBDIR,
        "JOBS": str(int(jobs)),
        "STOP_AFTER": stop_after or "bitstream",
    }
    files[render.SCRIPT_NAME] = render.render(values)
    found = vivado if vivado is not None else viv.discover(want=profile.vivado)
    vexe = viv.command_vivado(found, profile.vivado)
    if vexe == "vivado":                          # none of the kit's release here: say so
        checks.append(viv.check_release(found, profile.vivado, profile.vivado_build))
    else:
        pc = viv.check_path(found, profile.vivado)
        if pc is not None and pc.state == "warning":
            checks.append(pc)
    command = render.vivado_command(out_dir or Path("."), jobs=None, vivado=vexe)
    result = BuildScript(d.name, rm_id, proposed, profile.static_id, kit.manifest.kit_id,
                         files, values, checks, command, vivado=vexe,
                         vivado_release=profile.vivado)
    files["README.txt"] = _readme(result, kit)
    if out_dir is not None:
        result.out_dir = Path(out_dir)
        result.written = write(result, kits, kit, out_dir, export_kit=kit_dir is None)
        result.command = render.vivado_command(Path(out_dir).resolve(), vivado=vexe)
    return result


def write(result: BuildScript, kits: KitService, kit: CachedKit, out_dir: Path, *,
          export_kit: bool = True) -> list[Path]:
    out_dir = Path(out_dir)
    if out_dir.exists() and not out_dir.is_dir():
        raise RefusedError(f"{out_dir} is a file")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for rel, text in sorted(result.files.items()):
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        written.append(p)
    if export_kit:
        written += kits.export(kit, out_dir / KIT_SUBDIR)
    (out_dir / OUT_SUBDIR).mkdir(exist_ok=True)
    return written


def _readme(r: BuildScript, kit: CachedKit) -> str:
    m = kit.manifest
    return "\n".join([
        f"Build {r.design} (rm_id {r.rm_id}) for static {r.static_id}",
        f"kit {r.kit_id}: Vivado {m.vivado.release} exactly (another major.minor is refused),",
        f"part {m.part}, partition {m.rp.inst} ({m.rp.ports} ports / {m.rp.bits} bits).",
        "",
        "1. Build (about 20 min for a small RM with 2 threads; 4-8 GB of RAM):",
        f"     {r.vivado} -mode batch -source build_rm.tcl -log build_rm.log -journal build_rm.jou",
        (f"   ({r.vivado} is Vivado {m.vivado.release} on the machine that wrote this; elsewhere use "
         f"that release's vivado: a bare `vivado` runs whatever is first on PATH)"
         if r.vivado != "vivado" else
         f"   (`vivado` must be Vivado {m.vivado.release}: check with `vivado -version`, or run "
         f"`harness-manager kit build .` for the full path)"),
        "   Vivado exits 0 even when a gate fails: the verdict is the last HM_RM_BUILD_* line",
        f"   and {r.receipt}. Synth only: -tclargs STOP_AFTER=synth",
        "2. Check the pair, with no board and no Vivado:",
        f"     harness-manager kit check {r.receipt}",
        "3. Add it to Program (writes the overlay manifest from the receipt):",
        f"     harness-manager kit pack {r.receipt} --import",
        "",
        "Every step, with its state: harness-manager kit guide --static-id "
        f"{r.static_id} --design <your design .json> --build-dir .",
        "",
    ])
