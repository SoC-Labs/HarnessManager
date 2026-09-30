"""The build guide: six steps from "I have RTL" to "it is in Program", each with a state
Harness Manager works out from what it can detect (KIT-GUIDE KG-A; docs/design/DUT_BUILD_GUIDE.md §5).

| # | step | done when |
|---|---|---|
| 1 | target  | a static is known (the board's live ``shell_id``, or ``--static-id``) and the pack can build for it |
| 2 | tools   | the Vivado found has the kit's major.minor release, and so does the ``vivado`` on PATH (if any); that Vivado starts (``launch.py``) |
| 3 | kit     | the kit is cached, every blob re-hashes, its DCP's CRC-32 is the static_id, and it matches the board |
| 4 | wrapper | the design passes every XDC-kit check (T10), with its rm_id checked for clashes |
| 5 | build   | a receipt with ``state: passed`` is in the build directory (while its ``build_rm.log`` has no verdict and was written in the last 30 min, the build is running: no command is offered) |
| 6 | check   | the receipt, its files and the pair pass every check, and the overlay is in the store |

States: ``done`` · ``next`` (can be done now; ``Guide.next`` is the first such step) ·
``blocked`` (waits for an earlier step) · ``failed`` (a check said no: the detail
says which) · ``unchecked`` (HM cannot tell from here; never a pass).

The licence (KIT-LIC, ``licence.py``): the Tools step says which device licence the kit's
release needs (2026.1: Core or higher; 2024.1: Enterprise; the static's IP needs none) and
shows it as unchecked, always: only synthesis asks for it. It also launches the kit's Vivado
once (``launch.py``): 2026.1 does not start without a licence file (exit 42), which fails
the step; a start does not prove the device licence.

Board-agnostic: the pack answers through its ``KitAdapter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import AbsentError, HarnessError, UnavailableError
from harness_manager.core.model import BoardIdentity
from harness_manager.core.pack import KitCheck

from . import build, launch, licence, render
from .schema import hex32, parse_u32, release_major_minor, same_id
from .service import KitService
from .vivado import VivadoFound, check_path, check_release, command_vivado, discover, matching

STEPS = (("target", "Target"), ("tools", "Tools"), ("kit", "Kit"),
         ("wrapper", "Wrapper and XDC"), ("build", "Build"), ("check", "Check and add"))
STATES = ("done", "next", "blocked", "failed", "unchecked")
#: What each step needs done first (a step with a prerequisite not done is ``blocked``).
NEEDS = {"target": (), "tools": (), "kit": ("target",), "wrapper": ("target",),
         "build": ("tools", "kit", "wrapper"), "check": ("build",)}

#: One card per gate of build_rm.tcl: what it means and the fix (KIT-GUIDE §5.4).
GATE_HELP: dict[str, str] = {
    "vivado_version": "another Vivado ran the script. A kit needs the release that wrote its "
                      "static (2024.1 refuses a 2026.1 DCP with [Runs 36-378]): run that one.",
    "part_installed": "the device family is not installed with this Vivado: add UltraScale "
                      "(Kintex) with the Vivado installer.",
    "static_dcp_present": "the kit's DCP is not where the script looks: run "
                          "`harness-manager kit script` again, or fetch the kit.",
    "static_id": "the DCP is not the kit's static (a wrong, stale or corrupt copy): "
                 "`harness-manager kit fetch` again. Never rebuild the static.",
    "rm_id_format": "RM_ID must be a 32-bit hex literal such as 0x01008000.",
    "rm_id_nonzero": "rm_id 0 is the greybox's: give the RM its own (HM proposes one).",
    "source_present": "a file in RM_SOURCES does not exist: fix the path in the design.",
    "sources_given": "no RTL was named: set build.sources in the design, or RM_SOURCES.",
    "ooc_xdc_present": "the OOC XDC is missing: `harness-manager xdc rm-kit` writes it.",
    "generic_file_present": "a file named by build.generics ({\"path\": ...}) does not "
                            "exist: fix the path in the design. Vivado would only warn "
                            "([Synth 8-4445]) and build a blank memory.",
    "pr_verify_ref_present": "PR_VERIFY_REF names a file that does not exist: leave it empty "
                             "(the locked static is the reference) or fix the path.",
    "synth_dcp_present": "RM_SYNTH_DCP names a file that does not exist: fix the path, or "
                         "leave it empty to synthesise from RM_SOURCES.",
    "rm_xdc_present": "RM_XDC names a file that does not exist: fix the path in the design.",
    "no_black_boxes": "a module synthesised as a black box: a source file is missing from "
                      "RM_SOURCES; the listed cells name the missing modules.",
    "boundary_bits": "the wrapper's ports are not the partition's: start again from the kit's "
                     "wrapper skeleton (xdc/<name>_wrapper_skeleton.sv).",
    "rm_id_constant": "rm_id is driven by logic: drive rm_id from one constant.",
    "rm_id_match": "the netlist's rm_id differs from RM_ID: make the localparam and the "
                   "design's rm_id one value (HM writes the manifest from the netlist's).",
    "ooc_clocks": "nothing was timed: read the kit's OOC XDC (xdc/<name>_ooc.xdc).",
    "rp_cell": "the partition instance is not in the static: the wrong kit for this script.",
    "rp_pins_link": "the partition's pin count changed at link: the wrong kit for this static, "
                    "or a static-side debug core; tell the lab.",
    "clocks_after_link": "no clock reached the partition: the wrong kit, or a broken static.",
    "drc_hdpr_link": "HDPR-16: an ILA with no RM-side debug hub (add the dbgbscan group and "
                     "an RM-side debug_bridge); HDPR-18/50: a BUFG, MMCM, BSCAN or pad inside "
                     "the RM (generate no clocks in the RM).",
    "rp_pins_opt": "opt_design changed the partition's pins: a static-side debug core punched "
                   "hub ports into it; tell the lab.",
    "drc_routed": "a routed DRC error inside the partition: read <name>_drc.rpt.",
    "rm_timing": "negative slack in the RM's own paths, or an RM-internal async crossing left "
                 "unconstrained: add an RM_XDC (read_xdc -cell). ALLOW_TIMING_FAIL=1 is for "
                 "experiments only.",
    "pr_verify": "the static in the build is not the reference: fetch the kit again, and do "
                 "not mix kits.",
    "ltx_written": "the .ltx was not written: rerun the bitstream stage.",
    "artefact": "a bitstream file is missing: rerun the bitstream stage.",
    "clearing_fits": "the clearing is larger than the harness's arena: shrink the RM, or ask "
                     "for a harness with a larger clr_max.",
    "tcl_error": "a Tcl or Vivado error outside any gate: read build_rm.log at the line "
                 "before HM_RM_BUILD_FAILED.",
}

#: One card per check that can refuse after (or before) the build: ``kit check``, ``kit
#: pack``, ``kit script`` and the kit's own checks. The check's detail says what differed;
#: this says what to do. A pair check is named ``partial: <name>`` or ``clearing: <name>``
#: (the card is ``<name>``); ``xdc`` covers every ``xdc:<code>`` without its own card.
CHECK_HELP: dict[str, str] = {
    "build": "the build did not pass: fix the gate its receipt names, then run the build again.",
    "rm_id": "the netlist drives another rm_id than RM_ID (or 0, the greybox's): make the "
             "wrapper's localparam and the design's rm_id one value, then rebuild.",
    "static_id": "the receipt names no static: it was not written by build_rm.tcl, or was "
                 "edited; rebuild.",
    "partial": "the partial is not the file the build wrote (another build's, or half-copied): "
               "copy out/ from the build again, or rebuild.",
    "clearing": "the clearing is not the file the build wrote (another build's, or half-copied): "
                "copy out/ from the build again, or rebuild.",
    "ltx": "the .ltx is not the one the build wrote: copy out/ again; a stale .ltx names probes "
           "the partial does not have.",
    "board_static": "the build is for another static than the board runs: fetch the board's kit, "
                    "generate the script again and rebuild. A partial for another static can "
                    "destroy the FPGA's configuration, and the static_id alone cannot see it.",
    "expected_static": "the build is for another static than the one --static-id names (the "
                       "receipt's static_id is the CRC-32 of the DCP the build opened): build "
                       "again with `kit script --static-id ID`, or check it without the flag "
                       "to see which static it is for.",
    "kit": "the kit of the build's static is not cached, so the partition's frames were not "
           "compared: fetch the kit, then check again.",
    "bit_header_length": "not a Xilinx .bit: use the .bit or .bin the build wrote in out/.",
    "part": "a bitstream for another device: use this build's pair.",
    "partial_flag": "a full image, not a partial: use the <name>_partial.bit the build wrote.",
    "stream": "the file is truncated or not a configuration stream: copy it again.",
    "idcode": "another device's bitstream, or a multi-SLR image: use this build's pair.",
    "device_global_writes": "the stream writes IPROG, AXSS or WBSTAR, or passes to another SLR: "
                            "a partial never does. It is a full image: use this build's partial.",
    "role": "the partial and the clearing are swapped (a partial issues START, a clearing "
            "AGHIGH): swap the two files.",
    "frame_box": "frames outside the partition: a partial for another partition or another "
                 "floorplan.",
    "vocabulary": "commands or registers the kit's own partials never use: not a partial of this "
                  "partition.",
    "bit_bin_pair": "the .bin is not the .bit's payload: a .bin from another build; copy out/ "
                    "again.",
    "clearing_fits": GATE_HELP["clearing_fits"],
    "clearing_pairs_partial": "the clearing's frames are not inside the partial's: a clearing "
                              "from another RM.",
    "xdc:static_id": "HM's pin model describes another static, so it cannot write this kit's XDCs "
                     "or check the wrapper: build for the static the model has (the XDC section "
                     "names it).",
    "xdc": "the design fails an XDC check: fix the port or group it names (the XDC section "
           "previews the same checks).",
    "rm_id_clash": "the design id is another design's: Program and the CLCD would show that "
                   "name. Take the rm_id HM proposes (add it to the design to keep it).",
    "rm_id_range": "the design id is in the platform's range: user designs take 0x8000-0xFFFF. "
                   "Take the rm_id HM proposes.",
    "files": "a kit file does not match its sha256 (corrupt or edited): fetch the kit again.",
    "shell_id": "the kit is for another static than the board runs: fetch the board's own kit.",
    "usercode": "the kit is for another implementation run of this static: fetch the kit again "
                "from the release that fielded the board.",
    "vivado": GATE_HELP["vivado_version"],
    "vivado_path": "the `vivado` on PATH is another release than the kit's (a login profile may "
                   "put one first): run the full path `kit build` prints, or put the kit's "
                   "release first on PATH (`export PATH=<its bin>:$PATH`).",
}


def troubleshooting() -> dict[str, Any]:
    """The cards the Build section lists: every gate of build_rm.tcl (in order) and every
    check that can refuse (``GATE_HELP``, ``CHECK_HELP``)."""
    return {"gates": [{"gate": g, "fix": text} for g, text in GATE_HELP.items()],
            "checks": dict(CHECK_HELP)}


@dataclass
class Step:
    id: str
    n: int
    title: str
    state: str = "blocked"
    detail: str = ""
    reason: str = ""
    actions: list[dict[str, str]] = field(default_factory=list)
    checks: list[KitCheck] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "n": self.n, "title": self.title, "state": self.state,
                "detail": self.detail, "reason": self.reason, "actions": self.actions,
                "checks": [c.__dict__ for c in self.checks]}


@dataclass
class Guide:
    pack: str
    static_id: str
    board_id: str
    kit_id: str
    profile: dict[str, Any] | None
    vivado: dict[str, Any]
    design: str
    build_dir: str
    steps: list[Step]
    rm_id: dict[str, Any] = field(default_factory=dict)
    #: The newest receipt in ``build_dir`` as ``BuildReceipt.to_json`` (its gates and where
    #: it stopped), or None.
    receipt: dict[str, Any] | None = None

    @property
    def next(self) -> Step | None:
        return next((s for s in self.steps if s.state == "next"), None)

    def to_json(self) -> dict[str, Any]:
        nxt = self.next
        return {"pack": self.pack, "static_id": self.static_id, "board_id": self.board_id,
                "kit_id": self.kit_id, "profile": self.profile, "vivado": self.vivado,
                "design": self.design, "build_dir": self.build_dir, "rm_id": self.rm_id,
                "steps": [s.to_json() for s in self.steps],
                "next": ({"step": nxt.id, "actions": nxt.actions} if nxt else None),
                "receipt": self.receipt, "troubleshooting": troubleshooting()}


def _cmd(text: str) -> dict[str, str]:
    return {"kind": "copy", "text": text}


def guide(kits: KitService, *, pack: str = "mps3", static_id: str | None = None,
          identity: BoardIdentity | None = None, board_id: str = "",
          design: str | dict[str, Any] | None = None, build_dir: Path | None = None,
          vivado: VivadoFound | None = None, store: Any = None) -> Guide:
    """Work out every step's state. Never raises for a missing piece: that is a state."""
    steps = {sid: Step(sid, i + 1, title) for i, (sid, title) in enumerate(STEPS)}
    raw: dict[str, str] = {}
    target = f"--static-id {static_id}" if static_id else (board_id or "--static-id ID")

    # 1 target ------------------------------------------------------------------------------
    s = steps["target"]
    live = identity.shell_id if identity is not None and identity.shell_id else ""
    sid = live or (static_id or "")
    kit = None
    profile = None
    adapter = None
    try:
        adapter = kits.adapter_for(pack)
    except (UnavailableError, AbsentError) as exc:
        raw["target"] = "failed"
        s.detail = exc.message if not isinstance(exc, UnavailableError) else exc.reason
    if adapter is not None:
        if live and static_id and not same_id(live, static_id):
            raw["target"] = "failed"
            s.detail = (f"the board runs {live}, not {static_id}: build for the static that is "
                        "FIELDED, not the one that was minted")
        elif not sid:
            raw["target"] = "todo"
            s.detail = "no static yet: pick a board, or name one with --static-id"
            s.actions = [_cmd("harness-manager kit guide TARGET"),
                         _cmd("harness-manager kit guide --static-id 0x72BB0A36")]
        else:
            sid = hex32(parse_u32(sid))
            kit = kits.get(sid)
            profile = adapter.build_profile(sid, kit.manifest if kit else None)
            if profile is None:
                raw["target"] = "failed"
                s.detail = (f"the {pack} pack knows nothing of static {sid}, and no kit for it "
                            "is cached: ask the lab for its kit, or pick a static that has one")
            else:
                raw["target"] = "done"
                where = f"the board {board_id} runs" if live else "static"
                s.detail = (f"{where} {sid}; partition {profile.rp_inst} "
                            f"({profile.boundary_ports} ports / {profile.boundary_bits} bits), "
                            f"part {profile.part}")

    # 2 tools -------------------------------------------------------------------------------
    s = steps["tools"]
    need = profile.vivado if profile else ""
    found = vivado if vivado is not None else discover(want=need)
    c = check_release(found, need, profile.vivado_build if profile else 0)
    # KIT-RC2: the `vivado` on PATH is what a bare `vivado` (a README, a habit) runs; a wrong
    # release there is never "done", even when the chosen Vivado is right
    pc = check_path(found, need)
    s.checks = [c] + ([pc] if pc is not None else [])
    same_release = bool(found.install and need and release_major_minor(
        found.install.version) == release_major_minor(need))
    path_wrong = pc is not None and pc.state == "warning"
    if (c.state == "ok" or same_release) and not path_wrong:   # a build number only warns
        raw["tools"] = "done"
    elif c.state == "unchecked":
        raw["tools"] = "unchecked" if found.found else "todo"
    else:
        raw["tools"] = "todo"
    have = (f"Vivado {found.install.version or '?'} at {found.install.path}"
            if found.install else "no Vivado found")
    part = profile.part if profile else ""
    # KIT-LIC: launch the Vivado `kit build` prints (the kit's release); 2026.1 exits 42
    # with no licence file. A start does not prove the device licence: synthesis checks it.
    right = matching(found, need)
    started = launch.probe(right.path, release=right.version) if right is not None else None
    started = started if started is not None and started.ran else None   # off: nothing ran
    said = ""
    if started is not None:
        s.checks.append(started.check())
        said = started.detail
        if started.ok:
            note = licence.tier_note(part, need, started.tier)
            said += (f"; {note}" if note else
                     f"; that does not prove the device licence{' for ' if part else ''}"
                     f"{licence.device_of(part)}: synthesis still checks it")
    s.detail = (f"{pc.detail}; " if path_wrong and same_release else "") + \
        f"{c.detail if c.state != 'ok' else have}; " + (f"{said}; " if said else "") + \
        licence.line(part, need, sid if profile else None)
    if raw["tools"] == "todo":
        s.actions = ([_cmd(f"export PATH={Path(right.path).parent}:$PATH")] if right else
                     [_cmd(f"install Vivado {need or '(the kit names the release)'} and put it "
                           f"on PATH, or set HARNESS_MANAGER_VIVADO")])
    if started is not None and started.state == "failed":
        raw["tools"] = "failed"
        s.reason = f"fix: {started.fix}"
        if started.action:
            s.actions = [_cmd(started.action)] + s.actions

    # 3 kit ---------------------------------------------------------------------------------
    s = steps["kit"]
    if kit is None:
        raw["kit"] = "todo"
        s.detail = f"no kit for {sid or 'the static'} in the cache"
        s.actions = [_cmd(f"harness-manager kit fetch {target}")]
    else:
        checks = kits.verify_cached(kit)
        try:
            checks += kits.check_against_board(kit.manifest, identity, pack)
        except UnavailableError as exc:
            checks.append(KitCheck("board", "unchecked", exc.reason))
        s.checks = checks
        bad = [x for x in checks if x.state == "mismatch"]
        raw["kit"] = "failed" if bad else "done"
        crc = next((x for x in checks if x.name == "static_id"), None)
        s.detail = ("; ".join(f"{x.name}: {x.detail}" for x in bad) if bad else
                    f"{kit.manifest.kit_id}, {kit.manifest.size} B; "
                    + (crc.detail if crc else "cached"))
        if bad:
            s.actions = [_cmd(f"harness-manager kit fetch {target} --source hub")]

    # 4 wrapper -----------------------------------------------------------------------------
    s = steps["wrapper"]
    rm_info: dict[str, Any] = {}
    if design is None:
        raw["wrapper"] = "todo"
        s.detail = ("no design yet: write one (docs/XDC_EXPORT.md), starting from the "
                    "partition's skeleton")
        s.actions = [_cmd(f"harness-manager xdc rm-kit --design minimal --static-id {sid or 'ID'} "
                          "--out my_rm")]
    elif profile is None or adapter is None:
        raw["wrapper"] = "blocked"
        s.detail = "waits for a target"
    else:
        raw["wrapper"], s.detail, s.checks, rm_info = _wrapper(pack, sid, design, adapter, store)
        if raw["wrapper"] == "failed":
            s.actions = [_cmd(f"harness-manager xdc rm-kit --design {design if isinstance(design, str) else 'FILE'} "
                              f"--static-id {sid}")]

    # 5 build -------------------------------------------------------------------------------
    s = steps["build"]
    receipt = None
    name = rm_info.get("name") or "my_rm"
    if build_dir is None:
        raw["build"] = "todo"
        s.detail = "no build directory yet"
        s.actions = [_cmd(f"harness-manager kit script {target} --design "
                          f"{design if isinstance(design, str) else 'my_rm.json'} "
                          f"--out build/{name}")]
    else:
        found_r = build.find_receipts(Path(build_dir))
        script = Path(build_dir) / render.SCRIPT_NAME
        run = render.vivado_command(Path(build_dir), vivado=command_vivado(found, need))
        going = build.running_build(Path(build_dir))
        if going is not None and going.fresh:
            # KIT-NANOSOC G7: a build with no verdict yet. Its receipt (or the last run's)
            # is not this build's, and a second Vivado here would overwrite out/.
            raw["build"] = "todo"
            s.detail = (f"a build is running here: stage {going.stage}, {going.log.name} "
                        f"written {_hhmm(going.mtime)}; wait for its receipt")
            s.reason = ("do not start another Vivado in this directory: it would overwrite "
                        "out/. The verdict is the last HM_RM_BUILD_* line of "
                        f"{going.log.name}")
        elif not found_r:
            raw["build"] = "todo"
            s.detail = (f"no receipt in {build_dir}/out yet" if script.is_file() else
                        f"{build_dir} holds no build_rm.tcl")
            if going is not None:           # a log with no verdict, not written for long
                s.detail += (f"; {going.log.name} stops at stage {going.stage} "
                             f"({_hhmm(going.mtime)}) with no verdict: that run died")
            s.actions = ([_cmd(" ".join(run))] if script.is_file() else
                         [_cmd(f"harness-manager kit script {target} --design "
                               f"{design if isinstance(design, str) else 'my_rm.json'} "
                               f"--out {build_dir}")])
        else:
            try:
                receipt = build.load(found_r[0])
            except HarnessError as exc:
                raw["build"] = "failed"
                s.detail = exc.message
            else:
                if going is not None and going.mtime > found_r[0].stat().st_mtime:
                    # a later run died after this receipt: out/ may hold parts of it
                    s.reason = (f"{going.log.name} is newer and stops at stage {going.stage} "
                                f"({_hhmm(going.mtime)}) with no verdict: that later run died, "
                                "and out/ may hold parts of it (kit check compares every "
                                "file with this receipt)")
                if receipt.state == "passed":
                    raw["build"] = "done"
                    s.detail = (f"{receipt.rm_name} passed {len(receipt.gates)} gates "
                                f"({found_r[0].name}; static {receipt.get('static_id')})")
                elif receipt.state == "stopped":
                    raw["build"] = "todo"
                    s.detail = f"stopped after {receipt.stage} (STOP_AFTER): finish the build"
                    s.actions = [_cmd(" ".join(run))]
                else:
                    raw["build"] = "failed"
                    g = receipt.failed_gate
                    s.detail = (f"gate {g.gate}: {g.detail}" if g else
                                f"failed at {receipt.stage}")
                    if g is not None and g.gate in GATE_HELP:
                        s.reason = f"fix: {GATE_HELP[g.gate]}"
                    s.actions = [_cmd(" ".join(run))]

    # 6 check -------------------------------------------------------------------------------
    s = steps["check"]
    if receipt is None or receipt.state != "passed":
        raw["check"] = "todo"
        s.detail = "no passed build yet"
    else:
        rel = receipt.path
        checks = build.receipt_checks(receipt)
        files = build.receipt_files(receipt)
        if adapter is not None and "partial" in files and files["partial"].is_file():
            try:
                pair, _facts = adapter.check_pair(files["partial"], files.get("clearing"),
                                                  kit=kit.manifest if kit else None)
                checks += pair
            except (OSError, ValueError) as exc:
                checks.append(KitCheck("pair", "mismatch", f"unreadable: {exc}"))
        if live and receipt.get("static_id"):
            ok = same_id(live, receipt.get("static_id"))
            checks.append(KitCheck("board_static", "ok" if ok else "mismatch",
                                   f"built for {receipt.get('static_id')}; the board runs {live}",
                                   identity=True))
        s.checks = checks
        bad = [x for x in checks if x.state == "mismatch"]
        if bad:
            raw["check"] = "failed"
            s.detail = "; ".join(f"{x.name}: {x.detail}" for x in bad[:3])
        elif _imported(store, receipt):
            raw["check"] = "done"
            s.detail = f"{receipt.rm_name} {receipt.get('rm_id')} is in the store: Program it"
            s.actions = [_cmd(f"harness-manager program TARGET {receipt.rm_name}")]
        else:
            raw["check"] = "todo"
            s.detail = (f"every check passed ({sum(x.state == 'ok' for x in checks)} ok, "
                        f"{sum(x.state == 'unchecked' for x in checks)} unchecked)")
            s.actions = [_cmd(f"harness-manager kit pack {rel} --import")]

    _resolve(steps, raw)
    vj = {**found.to_json(), "launch": started.to_json() if started is not None else None}
    return Guide(pack, sid, board_id, kit.manifest.kit_id if kit else "",
                 profile.__dict__ if profile else None, vj,
                 design if isinstance(design, str) else (str(design.get("name")) if design else ""),
                 str(build_dir) if build_dir else "", list(steps.values()), rm_info,
                 receipt.to_json() if receipt is not None else None)


def _hhmm(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _resolve(steps: dict[str, Step], raw: dict[str, str]) -> None:
    """raw done/failed/unchecked/todo/blocked -> the five states. Every todo whose
    prerequisites are done is ``next`` (fetching the kit does not wait for Vivado);
    ``Guide.next`` is the first of them."""
    for sid, s in steps.items():
        r = raw.get(sid, "blocked")
        if r in ("done", "failed", "unchecked"):
            s.state = r
            continue
        waits = [steps[n] for n in NEEDS[sid] if raw.get(n) not in ("done", "unchecked")]
        if r == "todo" and not waits:
            s.state = "next"
        else:
            s.state = "blocked"
            if waits and not s.reason:
                s.reason = "waits for " + ", ".join(f"{w.n} {w.title.lower()}" for w in waits)


def _wrapper(pack: str, sid: str, design: str | dict[str, Any], adapter: Any, store: Any
             ) -> tuple[str, str, list[KitCheck], dict[str, Any]]:
    from harness_manager.services import xdc

    try:
        pins = xdc.load_pack_pins(pack)
        d = (xdc.from_doc(design, origin="inline") if isinstance(design, dict)
             else xdc.load_design(str(design), pins.designs))
    except HarnessError as exc:
        return "failed", exc.message, [], {}
    checks: list[KitCheck] = []
    taken = adapter.taken_designs(store)
    rm = d.doc.get("rm_id")
    info: dict[str, Any] = {"name": d.name}
    if not rm:
        proposal = hex32(adapter.propose_rm_id(d.name, taken))
        info.update(rm_id=proposal, proposed=True)
        checks.append(KitCheck("rm_id_proposed", "warning", f"no rm_id: HM proposes {proposal}"))
        rm = proposal
    else:
        info.update(rm_id=hex32(parse_u32(rm)), proposed=False)
    checks += adapter.rm_id_checks(rm, d.name, taken)
    try:
        kit = xdc.export(pack, "rm-kit", design if isinstance(design, dict) else str(design),
                         static_id=sid, pins=pins)
    except AbsentError as exc:
        checks.append(KitCheck("xdc", "unchecked", exc.message))
        return "unchecked", f"{exc.message} (the XDC checks need this static in the pin model)", \
            checks, info
    errors = kit.errors
    for f in kit.findings:
        checks.append(KitCheck(f"xdc:{f.code}", "mismatch" if f.severity == "error" else "ok",
                               f"{f.subject}: {f.reason}"))
    bad_rm = [c for c in checks if c.name == "rm_id_clash" and c.state == "mismatch"
              or c.name == "rm_id" and c.state == "mismatch"]
    if errors or bad_rm:
        first = errors[0].line() if errors else bad_rm[0].detail
        return "failed", f"{len(errors) + len(bad_rm)} check(s) failed; first: {first}", checks, info
    b = kit.facts.get("boundary", {})
    warn = [c.detail for c in checks if c.state == "warning"]
    detail = (f"{d.name}: {b.get('ports', '?')} ports / {b.get('bits', '?')} bits checked; "
              f"rm_id {info['rm_id']}" + (f" (warning: {'; '.join(warn)})" if warn else ""))
    return "done", detail, checks, info


def _imported(store: Any, receipt: Any) -> bool:
    if store is None:
        return False
    try:
        rm = f"0x{parse_u32(receipt.get('rm_id')):08x}"
        sid = f"0x{parse_u32(receipt.get('static_id')):08x}"
    except (TypeError, ValueError):
        return False
    return bool(store.find("overlay", rm_id=rm, static_id=sid))


def why(gate: str) -> str:
    """The troubleshooting card for one gate (``kit guide --why GATE``)."""
    if gate not in GATE_HELP:
        from harness_manager.core.errors import UsageError

        raise UsageError(f"no gate named {gate!r}", hint=f"gates: {', '.join(sorted(GATE_HELP))}")
    return GATE_HELP[gate]
