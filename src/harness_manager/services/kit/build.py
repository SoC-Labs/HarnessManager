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

Board-agnostic: the overlay manifest itself is the pack's format (``KitAdapter`` of the
MPS3 pack writes ``pyverify``'s overlay triple).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_manager.core.pack import KitCheck

from .schema import BuildReceipt, crc32_file, hex32, load_receipt, parse_u32, same_id

RECEIPT_GLOB = "*_build.json"


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
