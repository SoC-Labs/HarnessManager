"""A board pack's pin model, read through one small board-agnostic interface.

The model is the JSON document a pack ships (schema ``harness-manager/board-pins``):
``banks``, ``package_pins``, ``nets``, ``clocks``, ``iostandards``, ``connectors`` and
``shells`` (per ``static_id``: ``owns``, ``free``, ``rp_boundary``, ``boundary_clocks``,
``connectivity``, ``pblock``). Nothing here knows about the MPS3.

A pack exposes its model through the module ``<pack package>.pins`` with
``load_model()`` and ``design_files()``. ``load_pack_pins(name)`` finds it from the
``harness_manager.boards`` entry point without instantiating the pack.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import cached_property
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType
from typing import Any

from harness_manager.core.errors import AbsentError, UnavailableError, UsageError
from harness_manager.core.registry import GROUP

SCHEMA = "harness-manager/board-pins"
CAPABILITY = "xdc_export"


@dataclass(frozen=True)
class BoundarySignal:
    name: str
    group: str
    rm_dir: str             # "in" | "out": the RM's port direction
    shell_dir: str          # "O" | "I": the shell's view
    width: int
    clamp: Any
    note: str
    src: tuple[str, ...]

    def bits(self) -> list[str]:
        if self.width == 1:
            return [self.name]
        return [f"{self.name}[{i}]" for i in range(self.width - 1, -1, -1)]


class PinModel:
    """Typed accessors over one pin-model document."""

    def __init__(self, doc: dict[str, Any], *, pack: str = "") -> None:
        if doc.get("schema") != SCHEMA:
            raise UsageError(f"not a board-pin model (schema {doc.get('schema')!r})")
        self.doc = doc
        self.pack = pack or doc.get("board", {}).get("pack", "")

    # -- identity and provenance ---------------------------------------------------------

    @property
    def board(self) -> dict[str, Any]:
        return self.doc["board"]

    @property
    def status(self) -> dict[str, Any]:
        return self.doc.get("status", {})

    @property
    def sources(self) -> dict[str, dict[str, Any]]:
        return self.doc.get("sources", {})

    def source_ref(self, ref: str) -> str:
        """``"pinmap:238"`` -> ``"fpga/monolithic/nanosoc_mps3.xdc:238 @ e5436302"``."""
        key, _, line = ref.partition(":")
        src = self.sources.get(key)
        if not src:
            return ref
        commit = str(src.get("commit", ""))[:8]
        where = f"{src['path']}:{line}" if line else src["path"]
        return f"{where} @ {commit}" if commit else where

    # -- board nets --------------------------------------------------------------------------

    @property
    def nets(self) -> dict[str, dict[str, Any]]:
        return self.doc["nets"]

    @cached_property
    def by_pin(self) -> dict[str, str]:
        return {n["pin"]: name for name, n in self.nets.items()}

    @property
    def package_pins(self) -> dict[str, dict[str, Any]]:
        return self.doc["package_pins"]

    @property
    def banks(self) -> dict[str, dict[str, Any]]:
        return self.doc["banks"]

    def vcco(self, iostandard: str) -> float | None:
        entry = self.doc.get("iostandards", {}).get(iostandard)
        return None if entry is None else float(entry["vcco"])

    def board_clock(self, net: str) -> dict[str, Any] | None:
        return next((c for c in self.doc.get("clocks", []) if c["net"] == net), None)

    @property
    def config(self) -> dict[str, Any]:
        return self.doc.get("config", {})

    # -- shells and the partition boundary -----------------------------------------------

    @property
    def default_shell(self) -> str:
        return self.doc.get("default_shell", "")

    def shell_ids(self) -> list[str]:
        return list(self.doc.get("shells", {}))

    def shell(self, static_id: str | None = None) -> dict[str, Any]:
        sid = static_id or self.default_shell
        for key, shell in self.doc.get("shells", {}).items():
            if _same_id(key, sid):
                return shell
        known = ", ".join(self.shell_ids()) or "none"
        raise AbsentError(f"the {self.pack or 'board'} pin model has no shell {sid!r}",
                          hint=f"shells in the model: {known}")

    def boundary(self, static_id: str | None = None) -> list[BoundarySignal]:
        out = []
        for g in self.shell(static_id)["rp_boundary"]["groups"]:
            for s in g["signals"]:
                out.append(BoundarySignal(s["name"], g["id"], s["rm_dir"], s["shell_dir"],
                                          int(s["width"]), s.get("clamp"), s.get("note", ""),
                                          tuple(s.get("src", ()))))
        return out

    def boundary_groups(self, static_id: str | None = None) -> list[str]:
        return [g["id"] for g in self.shell(static_id)["rp_boundary"]["groups"]]

    def boundary_clocks(self, static_id: str | None = None) -> dict[str, dict[str, Any]]:
        return {c["signal"]: c for c in self.shell(static_id).get("boundary_clocks", [])}

    def summary(self) -> dict[str, Any]:
        """What a front end shows before any export: the board, the status, the shells."""
        shells = {}
        for sid, sh in self.doc.get("shells", {}).items():
            b = sh["rp_boundary"]
            shells[sid] = {"fielded": sh.get("fielded", False), "totals": b["totals"],
                           "groups": [g["id"] for g in b["groups"]],
                           "owns": len(sh.get("owns", {})), "free": len(sh.get("free", []))}
        return {"pack": self.pack, "board": self.board, "status": self.status,
                "default_shell": self.default_shell, "shells": shells,
                "nets": len(self.nets), "banks": {k: v.get("vcco") for k, v in self.banks.items()},
                "sources": {k: {"path": v["path"], "commit": v.get("commit"),
                                "last_changed": v.get("last_changed"),
                                "fielded_static_input": v.get("fielded_static_input")}
                            for k, v in self.sources.items()}}


def _same_id(a: str, b: str) -> bool:
    try:
        return int(str(a), 0) == int(str(b), 0)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


# --- finding a pack's pin data --------------------------------------------------------------


@dataclass
class PackPins:
    pack: str
    model: PinModel
    designs: dict[str, Path]


def _pins_module(pack: str) -> ModuleType:
    eps = [ep for ep in entry_points(group=GROUP) if ep.name == pack]
    if not eps:
        known = ", ".join(sorted(ep.name for ep in entry_points(group=GROUP))) or "none installed"
        raise AbsentError(f"no board pack named {pack!r}", hint=f"installed packs: {known}")
    package = eps[0].value.split(":", 1)[0].rsplit(".", 1)[0]
    try:
        return importlib.import_module(f"{package}.pins")
    except ModuleNotFoundError as exc:
        raise UnavailableError(CAPABILITY, f"the {pack} pack ships no pin model "
                                           f"({package}.pins)") from exc


def load_pack_pins(pack: str = "mps3") -> PackPins:
    mod = _pins_module(pack)
    return PackPins(pack, PinModel(mod.load_model(), pack=pack), dict(mod.design_files()))


def packs_with_pins() -> list[str]:
    out = []
    for ep in entry_points(group=GROUP):
        try:
            _pins_module(ep.name)
        except Exception:  # noqa: BLE001 - a pack without pins is simply not listed
            continue
        out.append(ep.name)
    return sorted(out)
