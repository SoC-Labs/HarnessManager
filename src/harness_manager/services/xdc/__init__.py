"""XDC export (T10): constraint kits generated from a board pack's pin model.

Board-agnostic: everything board-specific is in the pack's pin model (``model.py`` says
how a pack ships one; the MPS3 pack's is ``harness_manager_mps3.pins``). Two kits:

- ``rm-kit``: for a reconfigurable module that loads into the shell's partition: an
  out-of-context XDC by boundary group, a connectivity sheet, the pblock facts and a
  wrapper skeleton;
- ``board``: for a whole-FPGA design: pins, IO standards by bank, and clocks.

``export(pack, kit, design)`` returns a ``Kit`` whose ``findings`` hold every check.
Errors block ``write_kit`` (``Kit.require_ok`` raises ``RefusedError`` listing them);
a preview may still show the files next to the failures. docs/XDC_EXPORT.md is the
user reference.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness_manager.core.errors import UsageError

from .design import Design, from_doc, load_design
from .kits import CHECK_CODES, Finding, Kit, board_kit, rm_kit
from .model import PackPins, PinModel, load_pack_pins, packs_with_pins
from .syntax import check_xdc

__all__ = [
    "CHECK_CODES", "DEFAULT_DESIGN", "KITS", "Design", "Finding", "Kit", "PackPins", "PinModel",
    "board_kit", "catalogue", "check_xdc", "export", "from_doc", "load_design", "load_pack_pins",
    "packs_with_pins", "rm_kit", "write_kit",
]

KITS = {"rm-kit": rm_kit, "board": board_kit}
KIT_OF_KIND = {"rm": "rm-kit", "board": "board"}
#: The design a kit uses when none is named.
DEFAULT_DESIGN = {"rm-kit": "minimal", "board": "blinky"}


def _kit_name(kit: str) -> str:
    if kit not in KITS:
        raise UsageError(f"no kit named {kit!r}", hint=f"kits: {', '.join(KITS)}")
    return kit


def resolve_design(pins: PackPins, kit: str, design: str | dict[str, Any] | None) -> Design:
    if design is None or design == "":
        design = DEFAULT_DESIGN[kit]
    if isinstance(design, dict):
        return from_doc(design, origin="inline")
    return load_design(str(design), pins.designs)


def export(pack: str, kit: str, design: str | dict[str, Any] | None = None, *,
           static_id: str | None = None, pins: PackPins | None = None) -> Kit:
    """Build ``kit`` for ``design`` (a built-in name, a .json path, or an inline document)."""
    kit = _kit_name(kit)
    pins = pins or load_pack_pins(pack)
    d = resolve_design(pins, kit, design)
    if KIT_OF_KIND[d.kind] != kit:
        raise UsageError(f"{d.name} is a {d.kind} design; kit {kit} needs a "
                         f"{'rm' if kit == 'rm-kit' else 'board'} design",
                         hint=f"use kit {KIT_OF_KIND[d.kind]}")
    if static_id and kit == "rm-kit":
        d.doc = dict(d.doc) | {"static_id": static_id}
    return KITS[kit](pins.model, d)


def write_kit(kit: Kit, out_dir: Path) -> list[Path]:
    """Write the files and ``manifest.json`` into ``out_dir``. Refuses a kit with errors."""
    kit.require_ok()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in sorted(kit.files.items()):
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    man = out_dir / "manifest.json"
    man.write_text(json.dumps(kit.manifest(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    written.append(man)
    return written


def catalogue(pack: str = "mps3", pins: PackPins | None = None) -> dict[str, Any]:
    """The model summary, the kits and the built-in designs, for a front end."""
    pins = pins or load_pack_pins(pack)
    designs = []
    for name in sorted(pins.designs):
        try:
            d = load_design(name, pins.designs)
        except Exception as exc:  # noqa: BLE001 - a broken built-in is listed with its reason
            designs.append({"name": name, "kind": None, "error": str(exc)})
            continue
        designs.append(d.summary() | {"kit": KIT_OF_KIND[d.kind]})
    return {"model": pins.model.summary(), "kits": list(KITS), "default_design": DEFAULT_DESIGN,
            "designs": designs, "checks": CHECK_CODES}
