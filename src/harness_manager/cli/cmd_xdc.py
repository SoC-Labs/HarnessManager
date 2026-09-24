"""``harness-manager xdc``: constraint kits from the board pack's pin model (T10).

Verbs::

    harness-manager xdc info                          the pin model: board, shell, sources, designs
    harness-manager xdc rm-kit [--design NAME|FILE]   the RM kit for the shell's partition
    harness-manager xdc board  [--design NAME|FILE]   the full-board three-file export
        [--out DIR]    write the files and manifest.json there (refused while a check fails)
        [--static-id ID]   rm-kit only: the shell the RM is for (default: the fielded one)

No board is needed: the kits come from the pack's pin model, not from a live board.
Without ``--out`` the verb shows the checks and the files it would write.

Exit codes: 0 every check passed; 2 a bad design file or kit; 3 no such design, pack
or shell; 12 the pack has no pin model; 15 a check failed (every failure is listed,
and ``--json`` carries them all in ``error.data.checks``).

``cli/main.py`` registers it with ``cmd_xdc.register(sub)``, and ``"xdc"`` is in
``NO_ENGINE``: the verb never opens a board (CCR T10-1).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from harness_manager.core.errors import ExitCode

from .context import Ctx
from .output import TSV_COLUMNS, Result

#: TSV layouts (append-only), registered into the shared table at ``register()`` time.
XDC_TSV: dict[str, tuple[str, ...]] = {
    "xdc": ("KIT", "DESIGN", "KIND", "NAME", "DETAIL"),
    "xdc info": ("PACK", "KIND", "NAME", "DETAIL"),
}


def _fmt() -> argparse.ArgumentParser:
    fmt = argparse.ArgumentParser(add_help=False)
    g = fmt.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return fmt


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``xdc`` and its actions to the top-level subparsers. Returns the parser."""
    for layout, cols in XDC_TSV.items():
        TSV_COLUMNS.setdefault(layout, cols)
    fmt = _fmt()
    vp = subparsers.add_parser(
        "xdc", help="constraint kits from the board's pin model (RM kit, full-board export)",
        description="XDC export: an RM kit for the shell's partition, or a full-board export, "
                    "generated from the board pack's pin model and checked before it is written.",
        parents=[fmt])
    sub = vp.add_subparsers(dest="xdc_cmd", required=True, metavar="ACTION")

    def epilog(layout: str) -> str:
        return f"--tsv columns: {' '.join(XDC_TSV[layout])}"

    sub.add_parser("info", help="the pin model: board, fielded shell, sources, built-in designs",
                   parents=[fmt], epilog=epilog("xdc info"))
    for name, help_ in (("rm-kit", "the RM kit: OOC XDC, connectivity sheet, pblock facts, "
                                   "wrapper skeleton"),
                        ("board", "the full-board export: pins, IO standards by bank, clocks")):
        ap = sub.add_parser(name, help=help_, description=help_, parents=[fmt],
                            epilog=epilog("xdc"))
        ap.add_argument("--design", default=None, metavar="NAME|FILE",
                        help="a built-in design (see `xdc info`) or a design .json file")
        ap.add_argument("--out", default=None, metavar="DIR",
                        help="write the files and manifest.json into DIR")
        if name == "rm-kit":
            ap.add_argument("--static-id", default=None, metavar="ID",
                            help="the shell the RM is for (default: the model's fielded shell)")
    vp.set_defaults(fn=cmd_xdc)
    return vp


def cmd_xdc(ctx: Ctx) -> int:
    if ctx.args.xdc_cmd == "info":
        return _info(ctx)
    return _kit(ctx, ctx.args.xdc_cmd)


def _info(ctx: Ctx) -> int:
    from harness_manager.services import xdc

    cat = xdc.catalogue(ctx.pack)
    m = cat["model"]
    st = m["status"]
    rows: list[list[Any]] = [[ctx.pack, "model", m["board"].get("id"), st.get("label", "")]]
    human = [f"board        {m['board'].get('title')} ({m['board'].get('part')})",
             f"model        {st.get('label', '')}",
             f"             from {st.get('platform_ref')} @ {str(st.get('platform_commit', ''))[:10]} "
             f"by {st.get('generator')}"]
    for sid, sh in m["shells"].items():
        t = sh["totals"]
        rows.append([ctx.pack, "shell", sid, f"{t['ports']} ports / {t['bits']} bits; "
                                             f"groups {' '.join(sh['groups'])}"])
        human.append(f"shell        {sid}{' (fielded)' if sh['fielded'] else ''}: {t['ports']} ports, "
                     f"{t['bits']} bits, {t.get('decoupler_intfs')} decoupler interfaces; "
                     f"owns {sh['owns']} board nets, {sh['free']} free")
    human.append("designs")
    for d in cat["designs"]:
        rows.append([ctx.pack, "design", d["name"], f"{d.get('kit')}: {d.get('title') or d.get('error', '')}"])
        human.append(f"  {d['name']:<14} {d.get('kit') or '?':<7} {d.get('title') or d.get('error', '')}")
    human.append("sources")
    for key, s in sorted(m["sources"].items()):
        fielded = s.get("fielded_static_input")
        tag = " (= the fielded static's build input)" if fielded else ""
        changed = (s.get("last_changed") or {}).get("date", "")
        rows.append([ctx.pack, "source", key, f"{s['path']} {changed}{tag}"])
        human.append(f"  {key:<32} {s['path']}  {changed}{tag}")
    ctx.emit(Result("xdc info", cat, rows=rows, human=human))
    return ExitCode.OK


def _kit(ctx: Ctx, kit_name: str) -> int:
    from harness_manager.services import xdc

    a = ctx.args
    kit = xdc.export(ctx.pack, kit_name, a.design,
                     static_id=getattr(a, "static_id", None))
    name = kit.design.get("name")
    rows: list[list[Any]] = []
    for f in kit.findings:
        rows.append([kit_name, name, f.severity, f.code, f"{f.subject}: {f.reason}"])
    errors, notes = len(kit.errors), len(kit.findings) - len(kit.errors)
    head = f"{kit_name} {name}" + (f" for static {kit.design['static_id']}" if kit.design.get("static_id") else "")
    human = [head, f"checks: {errors} error{'s' if errors != 1 else ''}, {notes} note{'s' if notes != 1 else ''}"]
    human += ["  " + f.line() for f in kit.findings]
    if errors:
        kit.require_ok()                     # raises RefusedError with every check in data
    written: list[Path] = []
    if a.out:
        written = xdc.write_kit(kit, Path(a.out))
        human.append(f"wrote {len(written)} files to {Path(a.out).resolve()}:")
        human += [f"  {p.name}" for p in written]
    else:
        human.append("files (not written; add --out DIR):")
        human += [f"  {n}  ({t.count(chr(10))} lines)" for n, t in sorted(kit.files.items())]
    for n in sorted(kit.files):
        rows.append([kit_name, name, "file", n, str(Path(a.out) / n) if a.out else "-"])
    data = kit.manifest() | {"written": [str(p) for p in written], "out": a.out}
    if not a.out:
        data["files"] = dict(kit.files)
    ctx.emit(Result("xdc", data, rows=rows, human=human))
    return ExitCode.OK
