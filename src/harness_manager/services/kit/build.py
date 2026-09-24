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
