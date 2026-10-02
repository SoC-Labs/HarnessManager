"""``harness-manager kit``: DUT build kits and the build guide (KIT-STORE K5, KIT-GUIDE KG-C).

Verbs::

    kit info   [TARGET | --static-id ID]           the static, its kit, the Vivado it needs, sources
    kit fetch  [TARGET | --static-id ID] [--out DIR] [--source cache|channel|hub|PATH]
               [--print-tcl]                        the kit into the cache (and a plain dir)
    kit verify DIR|ZIP [TARGET]                     a kit directory or zip: files, CRC-32 = static_id, the board
    kit list                                        cached kits
    kit import DIR|ZIP                              a kit dir or zip, or loose mint files (fielded/<sid>/)
    kit guide  [TARGET | --static-id ID] [--design D] [--build-dir DIR] [--why GATE]
                                                    the six steps, each with its state, and what to do next
    kit script [TARGET | --static-id ID] --design D --out DIR [--jobs N] [--stop-after STAGE]
                                                    build_rm.tcl + the kit + the XDC kit + README
    kit build  DIR [--stop-after STAGE] [--jobs N] [--gui]
                                                    prints the Vivado command, with the full path of
                                                    the kit's release (HM does not run it yet), and
                                                    the Tcl line for a Vivado that is already open
    kit check  RECEIPT|BUILD_DIR|PARTIAL [--clearing C] [--static-id ID] [TARGET]
                                                    the receipt, its files and the pair, board-free
    kit pack   RECEIPT|BUILD_DIR [--out DIR] [--import]   the overlay triple (+ into Program)

No verb needs a board: TARGET, when given, adds the board's live static (shell_id,
usercode) to the checks; the kits live in the engine's content store
(``<state_dir>/store``). Vivado is never run by these verbs, except ``vivado -version``
(``$HARNESS_MANAGER_VIVADO=off`` turns that off).

Exit codes: 0 done; 2 bad arguments; 3 no such kit, receipt or source; 12 the pack has no
build kit, or (``kit build``) no Vivado of the kit's release is found; 14 the kit or build
is for another static than the board (identity), or (``kit check``) than ``--static-id``;
15 a check failed (every check is listed with ``--json``, in ``error.data.checks``).
"""

from __future__ import annotations

import argparse
from contextlib import suppress
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    ExitCode,
    HarnessError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.model import BoardIdentity
from harness_manager.core.pack import KitCheck, kit_refusal

from .context import Ctx
from .output import TSV_COLUMNS, Result, with_data

#: TSV layouts (append-only), registered into the shared table at ``register()`` time.
KIT_TSV: dict[str, tuple[str, ...]] = {
    "kit": ("STATIC_ID", "KIND", "NAME", "STATE", "DETAIL"),
    "kit list": ("STATIC_ID", "KIT_ID", "VIVADO", "SIZE", "SOURCE", "IMPORTED"),
    "kit guide": ("N", "STEP", "STATE", "DETAIL", "NEXT"),
}
STAGES = ("preflight", "synth", "link", "impl", "verify", "bitstream")


def _fmt() -> argparse.ArgumentParser:
    fmt = argparse.ArgumentParser(add_help=False)
    g = fmt.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return fmt


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``kit`` and its actions to the top-level subparsers. Returns the parser."""
    for layout, cols in KIT_TSV.items():
        TSV_COLUMNS.setdefault(layout, cols)
    fmt = _fmt()
    vp = subparsers.add_parser(
        "kit", help="DUT build kits: fetch the static's kit, generate build_rm.tcl, check and "
                    "add the partial",
        description="Build a DUT for a board: the static's build kit (the locked DCP, "
                    "cached and CRC-checked), a generated Vivado script, and the checks that "
                    "turn its output into an overlay you can Program.", parents=[fmt])
    sub = vp.add_subparsers(dest="kit_cmd", required=True, metavar="ACTION")

    def ep(layout: str) -> str:
        return f"--tsv columns: {' '.join(KIT_TSV[layout])}"

    def target(ap: argparse.ArgumentParser, *, static: bool = True) -> None:
        ap.add_argument("target", nargs="?", default=None, metavar="TARGET",
                        help="a board (host[:port]): adds its live static to the checks")
        if static:
            ap.add_argument("--static-id", default=None, metavar="ID",
                            help="the static to build for, with no board (e.g. 0x72BB0A36)")
        ap.add_argument("--pack", default="mps3", help="the board pack (default mps3)")

    ap = sub.add_parser("info", help="the static, its kit, the Vivado it needs, the sources",
                        parents=[fmt], epilog=ep("kit"))
    target(ap)
    ap = sub.add_parser("fetch", help="put the static's kit in the cache (and --out a dir)",
                        parents=[fmt], epilog=ep("kit"))
    target(ap)
    ap.add_argument("--out", default=None, metavar="DIR", help="export the kit into DIR")
    ap.add_argument("--source", default=None, metavar="cache|channel|hub|PATH",
                    help="take it from this source only")
    ap.add_argument("--print-tcl", action="store_true",
                    help="print `set HM_KIT_DIR ...` lines for a Vivado Tcl session")
    ap = sub.add_parser("verify", help="check a kit directory or zip (and it against a board)",
                        parents=[fmt], epilog=ep("kit"))
    ap.add_argument("dir", metavar="DIR|ZIP",
                    help="the kit directory (`kit fetch --out DIR`), or a kit zip (checked "
                         "as it is; nothing is cached)")
    target(ap, static=False)
    sub.add_parser("list", help="the cached kits", parents=[fmt], epilog=ep("kit list"))
    ap = sub.add_parser("import", help="a kit dir or zip, or a fielded/<sid>/ dir, into the cache",
                        parents=[fmt], epilog=ep("kit"))
    ap.add_argument("path", metavar="DIR|ZIP",
                    help="the kit directory or zip (or a fielded/<sid>/ directory)")
    ap = sub.add_parser("guide", help="the build steps, each with its state, and what to do next",
                        parents=[fmt], epilog=ep("kit guide"))
    target(ap)
    ap.add_argument("--design", default=None, metavar="NAME|FILE",
                    help="the RM design (.json, docs/XDC_EXPORT.md)")
    ap.add_argument("--build-dir", default=None, metavar="DIR", help="where `kit script` wrote")
    ap.add_argument("--why", default=None, metavar="GATE", help="the fix for one build gate")
    ap = sub.add_parser("script", help="write build_rm.tcl, the kit and the XDC kit into a dir",
                        parents=[fmt], epilog=ep("kit"))
    target(ap)
    ap.add_argument("--design", required=True, metavar="NAME|FILE",
                    help="required: the RM design, a built-in one (`xdc info`) or a design "
                         ".json file")
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="the build directory (without it: show the files, write nothing)")
    ap.add_argument("--kit-dir", default=None, metavar="DIR",
                    help="use this exported kit instead of copying one into DIR/kit")
    ap.add_argument("--jobs", type=int, default=2, metavar="N", help="Vivado threads (default 2)")
    ap.add_argument("--stop-after", default="bitstream", choices=STAGES,
                    help="the last stage the script runs (default bitstream: all of them)")
    ap = sub.add_parser("build", help="the Vivado command for a build dir (HM does not run it yet)",
                        parents=[fmt], epilog=ep("kit"))
    ap.add_argument("dir", metavar="DIR", help="the build directory `kit script --out` wrote")
    # "" (the default) keeps the script's own STOP_AFTER; the metavar hides it from the help.
    ap.add_argument("--stop-after", default="", choices=("",) + STAGES,
                    metavar="{" + ",".join(STAGES) + "}",
                    help="stop after this stage (default: what `kit script` wrote)")
    ap.add_argument("--jobs", type=int, default=None, metavar="N",
                    help="Vivado threads (default: what `kit script` wrote)")
    ap.add_argument("--gui", action="store_true",
                    help="the command for the Vivado GUI (-mode gui): it runs the script and "
                         "stays open, so with --stop-after link the linked design is there "
                         "to floorplan")
    ap = sub.add_parser("check", help="a build receipt and its pair, or a bare partial, board-free",
                        parents=[fmt], epilog=ep("kit"))
    ap.add_argument("path", metavar="RECEIPT|BUILD_DIR|PARTIAL",
                    help="a build receipt, the build directory holding one, or a bare partial")
    ap.add_argument("--clearing", default=None, metavar="FILE", help="a bare partial's clearing")
    target(ap)
    ap = sub.add_parser("pack", help="the overlay triple from a passed receipt (--import: into "
                                     "Program)", parents=[fmt], epilog=ep("kit"))
    ap.add_argument("path", metavar="RECEIPT|BUILD_DIR",
                    help="a passed build receipt, or the build directory holding one")
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="the overlay root (default: <build dir>/overlay)")
    ap.add_argument("--import", dest="do_import", action="store_true",
                    help="import it into the content store, so it shows in Program")
    vp.set_defaults(fn=cmd_kit)
    return vp


# --- plumbing ---------------------------------------------------------------------------------


def _state_dir() -> Path:
    from harness_manager.engine import resolve_state_dir

    return resolve_state_dir()


def _service() -> Any:
    from harness_manager.services.kit import KitService

    return KitService.for_state_dir(_state_dir())


def _board_identity(ctx: Ctx) -> tuple[BoardIdentity | None, str]:
    """The board's identity when a TARGET was given: (identity, board id)."""
    if not getattr(ctx.args, "target", None):
        return None, ""
    from .engine import get_engine

    engine = ctx.engine or get_engine(ctx.args)
    ctx.engine = engine
    try:
        with ctx.board(note="cli kit") as (cand, session):
            return session.identity(), cand.board_id
    finally:
        with suppress(Exception):
            engine.close_all()


def _static(ctx: Ctx, ident: BoardIdentity | None) -> str:
    sid = getattr(ctx.args, "static_id", None)
    live = ident.shell_id if ident is not None else ""
    if not sid and not live:
        raise UsageError("which static? give a board (TARGET) or --static-id",
                         hint="e.g. harness-manager kit info --static-id 0x72BB0A36")
    from harness_manager.services.kit.schema import hex32, parse_u32

    try:
        return hex32(parse_u32(sid or live))
    except ValueError:
        raise UsageError(f"{sid or live!r} is not a static id",
                         hint="a 32-bit hex id such as 0x72BB0A36") from None


def _rows(sid: str, kind: str, checks: list[KitCheck]) -> list[list[Any]]:
    return [[sid, kind, c.name, c.state, c.detail] for c in checks]


def _human(checks: list[KitCheck]) -> list[str]:
    return [f"  {c.state:<9} {c.name:<28} {c.detail}" for c in checks]


def _refuse(checks: list[KitCheck], what: str) -> None:
    exc = kit_refusal(checks, what)
    if exc is not None:
        raise with_data(exc, checks=[c.__dict__ for c in checks])


def cmd_kit(ctx: Ctx) -> int:
    return {"info": _info, "fetch": _fetch, "verify": _verify, "list": _list,
            "import": _import, "guide": _guide, "script": _script, "build": _build,
            "check": _check, "pack": _pack}[ctx.args.kit_cmd](ctx)


# --- verbs ------------------------------------------------------------------------------------


def _info(ctx: Ctx) -> int:
    from harness_manager.services.kit import vivado

    kits = _service()
    ident, bid = _board_identity(ctx)
    sid = _static(ctx, ident)
    kit = kits.get(sid)
    profile = kits.profile(ctx.pack, sid)
    rel = profile.vivado if profile else ""
    found = vivado.discover(want=rel)
    checks = [vivado.check_release(found, rel, profile.vivado_build if profile else 0)]
    pc = vivado.check_path(found, rel)
    checks += [pc] if pc is not None else []
    if kit is not None:
        checks += kits.check_against_board(kit.manifest, ident, ctx.pack)
    data = {"static_id": sid, "board_id": bid or None, "cached": kit is not None,
            "kit": kit.summary() if kit else None,
            "profile": profile.__dict__ if profile else None,
            "vivado": found.to_json(), "sources": kits.sources(),
            "checks": [c.__dict__ for c in checks]}
    human = [f"static     {sid}" + (f" (the board {bid} runs it)" if bid else ""),
             f"kit        {kit.manifest.kit_id}, {kit.manifest.size} B, from {kit.source}"
             if kit else "kit        not cached: harness-manager kit fetch "
             + (f"--static-id {sid}" if not bid else ctx.args.target)]
    if profile is not None:
        human.append(f"partition  {profile.rp_inst} / {profile.rp_pblock}, "
                     f"{profile.boundary_ports} ports / {profile.boundary_bits} bits, part "
                     f"{profile.part}")
    human.append(f"vivado     needs {rel or '(the kit names it)'}; found "
                 + (f"{found.install.version or '?'} at {found.install.path}" if found.install
                    else f"none ({found.reason})"))
    human.append("sources    " + ", ".join(
        f"{s['name']}{'' if s['available'] else ' (unavailable)'}" for s in kits.sources()))
    human += _human(checks)
    rows = [[sid, "kit", kit.manifest.kit_id if kit else "-", "cached" if kit else "absent",
             kit.source if kit else "-"]]
    rows += [[sid, "source", s["name"], "available" if s["available"] else "unavailable",
              s["reason"]] for s in kits.sources()]
    rows += _rows(sid, "check", checks)
    ctx.emit(Result("kit", data, rows=rows, human=human))
    return ExitCode.OK


def _fetch(ctx: Ctx) -> int:
    kits = _service()
    ident, bid = _board_identity(ctx)
    sid = _static(ctx, ident)
    kit, src = kits.fetch(sid, source=ctx.args.source,
                          progress=lambda phase, done, total: ctx.note(
                              f"{phase}: {done}/{total}") if total and done >= total else None)
    checks = kits.verify_cached(kit) + kits.check_against_board(kit.manifest, ident, ctx.pack)
    _refuse(checks, f"the kit for {kit.static_id}")
    written: list[Path] = []
    out = Path(ctx.args.out) if ctx.args.out else None
    if out is not None:
        written = kits.export(kit, out)
    data = {"static_id": kit.static_id, "source": src, "kit": kit.summary(),
            "checks": [c.__dict__ for c in checks], "out": str(out) if out else None,
            "written": [str(p) for p in written]}
    if ctx.args.print_tcl and ctx.fmt == "human":
        d = (out or Path(".")).resolve().as_posix()
        ctx.emit(Result("kit", data, human=[
            f"set HM_KIT_DIR {{{d}}}", f"set HM_STATIC_ID {kit.static_id}",
            f"set HM_VIVADO {kit.manifest.vivado.release}"]))
        return ExitCode.OK
    human = [f"kit {kit.manifest.kit_id} from {src}" + (f" -> {out}" if out else " (cached)")]
    human += _human(checks)
    if out is not None:
        from harness_manager.services.kit.guide import builds_dir

        human.append(f"next: harness-manager kit script --static-id {kit.static_id} "
                     f"--design my_rm.json --out {builds_dir('my_rm')} --kit-dir {out}")
    ctx.emit(Result("kit", data, rows=_rows(kit.static_id, "check", checks), human=human))
    return ExitCode.OK


def _verify(ctx: Ctx) -> int:
    kits = _service()
    ident, _ = _board_identity(ctx)
    manifest, checks = kits.verify_dir(Path(ctx.args.dir), ident, pack=ctx.pack)
    _refuse(checks, f"the kit in {ctx.args.dir}")
    ctx.emit(Result("kit", {"static_id": manifest.static_id, "kit_id": manifest.kit_id,
                            "checks": [c.__dict__ for c in checks]},
                    rows=_rows(manifest.static_id, "check", checks),
                    human=[f"{ctx.args.dir}: kit {manifest.kit_id}", *_human(checks)]))
    return ExitCode.OK


def _list(ctx: Ctx) -> int:
    kits = _service().list()
    rows = [[k.static_id, k.manifest.kit_id, k.manifest.vivado.release, k.manifest.size,
             k.source, k.meta.get("imported_at", "")] for k in kits]
    human = [f"{k.static_id}  {k.manifest.kit_id:<32} {k.manifest.size:>10} B  {k.source}"
             for k in kits] or ["no kits cached: harness-manager kit fetch --static-id ID"]
    ctx.emit(Result("kit list", {"kits": [k.summary() for k in kits]}, rows=rows, human=human))
    return ExitCode.OK


def _import(ctx: Ctx) -> int:
    res = _service().import_(Path(ctx.args.path), source=f"path:{Path(ctx.args.path).resolve()}")
    kit = res.kit
    ctx.emit(Result("kit", {"static_id": kit.static_id, "kit": kit.summary(),
                            "already": res.already, "checks": [c.__dict__ for c in res.checks]},
                    rows=_rows(kit.static_id, "check", res.checks),
                    human=[f"{'already cached' if res.already else 'cached'}: "
                           f"{kit.manifest.kit_id} ({kit.manifest.size} B)",
                           *_human(res.checks)]))
    return ExitCode.OK


def _guide(ctx: Ctx) -> int:
    from harness_manager.services.kit import guide
    from harness_manager.services.store import ContentStore

    a = ctx.args
    if a.why:
        text = guide.why(a.why)
        ctx.emit(Result("kit guide", {"gate": a.why, "fix": text},
                        rows=[["-", a.why, "why", text, "-"]], human=[f"{a.why}: {text}"]))
        return ExitCode.OK
    kits = _service()
    ident, bid = _board_identity(ctx)
    g = guide.guide(kits, pack=ctx.pack, static_id=a.static_id, identity=ident, board_id=bid,
                    design=a.design, build_dir=Path(a.build_dir) if a.build_dir else None,
                    store=ContentStore(_state_dir() / "store"))
    facts = [f"static {g.static_id}"] if bid and g.static_id else []
    facts += [f"kit {g.kit_id}"] if g.kit_id else []
    head = (f"Build a DUT for {bid or 'static ' + (g.static_id or '?')}"
            + (f" ({', '.join(facts)})" if facts else ""))
    human = [head]
    rows = []
    for s in g.steps:
        mark = {"done": "done", "next": "NEXT", "blocked": "-", "failed": "FAILED",
                "unchecked": "?"}[s.state]
        human.append(f"  {mark:<7}{s.n} {s.title:<16} {s.detail}")
        if s.reason:
            human.append(f"  {'':<7}  {'':<16} {s.reason}")
        nxt = "; ".join(x["text"] for x in s.actions) if s.state in ("next", "failed") else ""
        rows.append([s.n, s.id, s.state, s.detail, nxt or "-"])
    if g.next is not None and g.next.actions:
        human.append(f"next: {g.next.actions[0]['text']}")
    ctx.emit(Result("kit guide", g.to_json(), rows=rows, human=human))
    return ExitCode.OK


def _script(ctx: Ctx) -> int:
    from harness_manager.services.kit import script
    from harness_manager.services.store import ContentStore

    a = ctx.args
    kits = _service()
    ident, bid = _board_identity(ctx)
    sid = _static(ctx, ident)
    s = script.make_script(kits, pack=ctx.pack, static_id=sid, design=a.design,
                           out_dir=Path(a.out) if a.out else None,
                           kit_dir=Path(a.kit_dir) if a.kit_dir else None, jobs=a.jobs,
                           stop_after=a.stop_after, board=bid,
                           store=ContentStore(_state_dir() / "store"))
    data = s.to_json()
    if not a.out:
        data["files"] = {k: v for k, v in s.files.items()}
    human = [f"{s.design} (rm_id {s.rm_id}{', proposed' if s.rm_id_proposed else ''}) for "
             f"static {s.static_id}, kit {s.kit_id}"]
    human += _human([c for c in s.checks if c.state != "ok"])
    if a.out:
        human.append(f"wrote {len(s.written)} files to {Path(a.out).resolve()}")
        human.append(f"next: {' '.join(s.command)}")
        human.append(f"then: harness-manager kit check {Path(a.out) / s.receipt}")
    else:
        human.append("files (not written; add --out DIR):")
        human += [f"  {n}" for n in sorted(s.files)]
    rows = _rows(s.static_id, "check", s.checks) + [
        [s.static_id, "file", n, "written" if a.out else "preview", "-"] for n in sorted(s.files)]
    ctx.emit(Result("kit", data, rows=rows, human=human))
    return ExitCode.OK


def _build(ctx: Ctx) -> int:
    """Print the command, never run it. It names the FULL path of the Vivado discovery chose
    for the script's release (``tools.vivado``, PATH, the install roots): a bare ``vivado``
    runs whatever PATH has first (``/etc/profile.d`` may put 2024.1 there). No Vivado of
    that release here: UNAVAILABLE (12), with what was found."""
    from harness_manager.services.kit import render, vivado

    d = Path(ctx.args.dir)
    script = d / render.SCRIPT_NAME
    if not script.is_file():
        raise AbsentError(f"{d} holds no {render.SCRIPT_NAME}",
                          hint="harness-manager kit script ... --out DIR writes it")
    params = render.script_params(script.read_text(encoding="utf-8", errors="replace"))
    rel = params.get("VIVADO_VERSION", "")
    if not rel:
        raise UsageError(f"{script} names no VIVADO_VERSION: not a build_rm.tcl that "
                         "Harness Manager wrote", hint="harness-manager kit script ... --out DIR")
    found = vivado.discover(want=rel)
    inst = vivado.matching(found, rel)
    if inst is None:
        c = vivado.check_release(found, rel)
        err = UnavailableError("vivado", f"no Vivado {rel} to run {script}: {c.detail}",
                               hint=f"install Vivado {rel}, or point $HARNESS_MANAGER_VIVADO "
                                    "(or tools.vivado) at its vivado or its install directory; "
                                    "`harness-manager config test tools vivado` shows what is "
                                    "found")
        raise with_data(err, vivado=found.to_json(), release=rel)
    a = ctx.args
    mode = "gui" if getattr(a, "gui", False) else "batch"
    how = {"stop_after": a.stop_after, "jobs": a.jobs, "vivado": inst.path}
    commands = {m: render.vivado_command(d.resolve(), mode=m, **how) for m in render.MODES}
    cmd = commands[mode]
    source = render.source_tcl(d.resolve(), stop_after=a.stop_after, jobs=a.jobs)
    line = render.shell_line(cmd)                   # the Build tab shows the same line (run_commands)
    pc = vivado.check_path(found, rel)
    notes = [f"Vivado {inst.version} ({inst.how}); the kit needs {rel}"]
    if pc is not None and pc.state == "warning":
        notes.append(pc.detail)
    stays = (render.stays_open(params.get("RP_INST", "") or "u_rp_dut",
                               params.get("RP_PBLOCK", "") or "pblock_rp_dut")
             if (a.stop_after or params.get("STOP_AFTER")) == "link" else "")
    data = {"dir": str(d), "command": cmd, "mode": mode, "commands": commands,
            "source_tcl": source, "stays_open": stays or None, "ran": False,
            "vivado": found.to_json(), "release": rel,
            "checks": [c.__dict__ for c in ([pc] if pc else [])],
            "note": "Harness Manager does not run Vivado yet: run this command yourself"}
    # N1 guard (FIX-PACK-8): a script from before N2 builds an untimed boundary, and so did
    # the last build here when its report says so
    from harness_manager.services.kit import build

    script_first = build.script_reads_ooc_xdc_first(
        script.read_text(encoding="utf-8", errors="replace"))
    last = None
    found_r = build.find_receipts(d)
    if found_r:
        with suppress(HarnessError, OSError, ValueError):   # an unreadable receipt: say nothing
            last = build.load_receipt(found_r[0])
    bt = build.boundary_of(last) if last is not None else None
    data["boundary"] = {"script_reads_ooc_xdc_first": script_first,
                        "last_build": bt.to_json() if bt is not None else None,
                        "last_receipt": str(last.path) if last is not None else None}
    human = [line, *[f"  {n}" for n in notes]]
    if script_first:
        human.append(f"WARNING: {build.SCRIPT_OOC_FIRST}")
    if bt is not None:
        human += _boundary_lines(bt.to_json(), last=f"the last build here, {last.path.name}")
    human += [f"In a Vivado that is already open ({render.SOURCE_WHEN}), type:", f"  {source}",
              f"  ({render.SOURCE_LOG})"]
    if stays:
        human.append(f"  {stays}")
    human.append("(Harness Manager does not run Vivado yet: run the command above; "
                 + render.VERDICT_HOW + ")")
    rows = [["-", "command", inst.path, "not-run", line],
            ["-", "source_tcl", "vivado", "not-run", source]]
    ctx.emit(Result("kit", data, rows=rows, human=human))
    return ExitCode.OK


def _check(ctx: Ctx) -> int:
    from harness_manager.services.kit import build

    kits = _service()
    ident, _ = _board_identity(ctx)
    p = Path(ctx.args.path)
    clearing = Path(ctx.args.clearing) if ctx.args.clearing else None
    checks, facts, sid, r = build.check_any(kits, p, clearing=clearing,
                                            static_id=ctx.args.static_id or "", identity=ident,
                                            pack=ctx.pack)
    what = f"the build {r.rm_name}" if r is not None else f"the partial {p.name}"
    sid = sid or "-"
    if r is not None and r.state == "stopped":
        # KIT-INTERACTIVE: a STOP_AFTER run is not a failure (before: exit 15, "failed 1
        # check"). Every other check still refuses (a --static-id that is not the receipt's).
        rest = [c for c in checks if c.name != "build"]
        _refuse(rest, what)
        words = build.stopped_words(r)
        ctx.emit(Result("kit", {"static_id": sid, "passed": False, "state": "stopped",
                                "stopped_after": r.stage, "next": build.finish_hint(r),
                                "checks": [c.__dict__ for c in rest], "facts": facts},
                        rows=[[sid, "check", "build", "stopped", words]]
                        + _rows(sid, "check", rest),
                        human=[f"{what}: {words}", *_human(rest),
                               f"next: {build.finish_hint(r)}"]))
        return ExitCode.OK
    _refuse(checks, what)
    ctx.emit(Result("kit", {"static_id": sid, "passed": True,
                            "checks": [c.__dict__ for c in checks], "facts": facts},
                    rows=_rows(sid, "check", checks),
                    human=[f"{what}: passed (unchecked is not a pass: see the list)",
                           *_boundary_lines(facts.get("boundary")),
                           *_human(checks)]))
    return ExitCode.OK


def _boundary_lines(boundary: dict[str, Any] | None, *, last: str = "") -> list[str]:
    """N1 guard (FIX-PACK-8): the WARNING and its fix when the build's static<->RM boundary
    was not timed (``build.BoundaryTiming``); nothing when it was, or with no report."""
    if not boundary or boundary.get("timed", True):
        return []
    return [f"WARNING{f' ({last})' if last else ''}: {boundary['words']}",
            f"  fix: {boundary['fix']}"]


def _pack(ctx: Ctx) -> int:
    from harness_manager.services.kit import build
    from harness_manager.services.store import ContentStore

    kits = _service()
    p = Path(ctx.args.path)
    if not (p.is_dir() or p.suffix.lower() == ".json"):
        raise UsageError(f"{p} is not a build receipt or a build directory",
                         hint="kit pack takes out/<rm>_build.json (or the build dir)")
    checks, _facts, _sid, r = build.check_any(kits, p, pack=ctx.pack)
    assert r is not None
    _refuse(checks, f"the build {r.rm_name or p}")
    base = r.path.parent.parent if r.path.parent.name == "out" else r.path.parent
    out_root = Path(ctx.args.out) if ctx.args.out else base / "overlay"
    adapter = kits.adapter_for(r.kit_id.split("/", 1)[0] if "/" in r.kit_id else ctx.pack)
    d = adapter.pack_receipt(r, out_root)
    imported = None
    if ctx.args.do_import:
        imported = adapter.import_overlay(ContentStore(_state_dir() / "store"), d)
    data = {"static_id": r.get("static_id"), "rm_name": r.rm_name, "rm_id": r.get("rm_id"),
            "overlay_dir": str(d), "manifest": str(d / "manifest.json"), "imported": imported,
            "checks": [c.__dict__ for c in checks]}
    human = [f"overlay {r.rm_name} ({r.get('rm_id')}) for static {r.get('static_id')} -> {d}"]
    if imported and imported.get("shadowed_by"):
        # KIT-NANOSOC: another overlay of the same name, rm_id and static is listed first
        same = imported.get("shadow_same_bits")
        human.append(f"imported into the store ({imported['sha256'][:12]}), but Program lists "
                     f"{imported['shadowed_by']} instead: it has the same name, rm_id and "
                     "static, and the first one found wins"
                     + (" (its partial and clearing are byte-identical to this build's, so "
                        "it loads the same bits)" if same else
                        f". To load this build: harness-manager program TARGET {r.rm_name} "
                        f"--overlay-dir {out_root}; to keep both, give the design another "
                        "name or rm_id version"))
    else:
        human.append(f"imported into the store ({imported['sha256'][:12]}): it shows in Program"
                     if imported else f"next: harness-manager kit pack {p} --import  (or: "
                                      f"harness-manager program TARGET {r.rm_name} --overlay-dir {out_root})")
    rows = [[r.get("static_id"), "overlay", r.rm_name, "imported" if imported else "written",
             str(d)]] + _rows(r.get("static_id"), "check", checks)
    ctx.emit(Result("kit", data, rows=rows, human=human))
    return ExitCode.OK
