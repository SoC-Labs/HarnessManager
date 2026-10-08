"""A user design, as the XDC export reads it: a small JSON document.

Two kinds (docs/XDC_EXPORT.md has the full reference):

``{"kind": "rm", ...}``: a reconfigurable module for the shell's partition.

- ``use``: the boundary groups the RM drives or reads, each with options:
  ``{"timed": true}`` (or a list of signals) keeps that group's data out of the
  false paths, for a group the RM times synchronously (a MAC's RMII data, a QSPI
  controller); ``{"tie": ["dut_lockup", "irq_out"]}`` names outputs of the group the
  RM does not drive, which the skeleton ties to their safe-idle value. Groups not
  listed are tied off.
- ``clocks``: extra boundary clocks to declare (``"jtag_tck"``, ``"phy_rmii_ref_clk"``),
  or ``{"port", "period_ns"}`` objects; checked against the shell's clock contract.
- ``ports`` or ``wrapper``: the RM's port list, inline or from an ANSI (System)Verilog
  wrapper file (relative to the design file), checked against the boundary.
- ``pins``: package-pin requests. The partition has no IO sites, so every one is a
  check failure that says so.
- ``static_id``: the shell the RM is for (default: the model's fielded shell).

``{"kind": "board", ...}``: a whole-FPGA design on the board.

- ``ports``: ``{"port": "led[7:0]", "net": "USER_nLED[7:0]", "dir": "out"}``;
  ``pin`` instead of ``net`` places a raw package pin; ``iostandard``, ``clock_mhz``,
  ``props`` ({"PULLUP": "true"}) are optional.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import AbsentError, UsageError

from .hdl import HdlPort, parse_ansi_ports

KINDS = ("rm", "board")
_DIRS = {"in": "in", "input": "in", "out": "out", "output": "out", "inout": "inout"}


@dataclass
class Design:
    kind: str
    name: str
    title: str = ""
    doc: dict[str, Any] = field(default_factory=dict)
    origin: str = ""                         # "builtin:<name>" or a file path
    wrapper_ports: list[HdlPort] | None = None
    wrapper_path: str = ""

    # -- rm ---------------------------------------------------------------------------------

    @property
    def static_id(self) -> str:
        return str(self.doc.get("static_id") or "")

    @property
    def use(self) -> dict[str, dict[str, Any]]:
        raw = self.doc.get("use") or {}
        if isinstance(raw, list):
            return {str(g): {} for g in raw}
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in raw.items()}

    def timed(self, group: str) -> bool | list[str]:
        opt = self.use.get(group, {}).get("timed", False)
        return list(opt) if isinstance(opt, list) else bool(opt)

    def tied(self, group: str) -> list[str]:
        """Outputs of a USED group that the design does not drive (``{"tie": [...]}``): the
        skeleton ties them to their safe-idle value, as it does a group the design does
        not use (the built-in ``minimal`` uses ``status`` for ``rm_id`` only)."""
        opt = self.use.get(group, {}).get("tie", [])
        return [str(x) for x in opt] if isinstance(opt, list) else []

    @property
    def clocks(self) -> list[dict[str, Any]]:
        out = []
        for c in self.doc.get("clocks") or []:
            out.append({"port": c} if isinstance(c, str) else dict(c))
        return out

    @property
    def pins(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self.doc.get("pins") or []]

    def rm_ports(self) -> list[dict[str, Any]] | None:
        """The RM's declared ports ``[{name, dir, width, line?}]``, or None if not given."""
        if self.wrapper_ports is not None:
            return [{"name": p.name, "dir": p.direction, "width": p.width, "line": p.line}
                    for p in self.wrapper_ports]
        raw = self.doc.get("ports")
        if raw is None:
            return None
        out = []
        for p in raw:
            if not isinstance(p, dict) or "name" not in p:
                raise UsageError(f"design {self.name}: every port needs a name, not {p!r}")
            d = _DIRS.get(str(p.get("dir", "")).lower())
            if d is None:
                raise UsageError(f"design {self.name}: port {p['name']} needs dir in/out/inout")
            out.append({"name": str(p["name"]), "dir": d, "width": int(p.get("width", 1))})
        return out

    # -- board --------------------------------------------------------------------------------

    def board_ports(self) -> list[dict[str, Any]]:
        raw = self.doc.get("ports") or []
        out = []
        for p in raw:
            if not isinstance(p, dict) or not p.get("port"):
                raise UsageError(f"design {self.name}: every port needs \"port\", not {p!r}")
            q = dict(p)
            if "dir" in q:
                d = _DIRS.get(str(q["dir"]).lower())
                if d is None:
                    raise UsageError(f"design {self.name}: port {q['port']}: dir must be in, out or inout")
                q["dir"] = d
            out.append(q)
        return out

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "title": self.title, "origin": self.origin}


def _check_rm_id(doc: dict[str, Any]) -> None:
    """``rm_id``, when given, is one 32-bit number written ``0x0100XXXX`` (hex digits)."""
    raw = doc.get("rm_id")
    if raw in (None, ""):
        return
    try:
        if isinstance(raw, bool):
            raise ValueError(raw)
        val = raw if isinstance(raw, int) else int(str(raw).strip(), 0)
    except ValueError:
        raise UsageError(f"design field \"rm_id\" is {raw!r}, not a number",
                         hint="write it as 8 hex digits, like 0x01000001 (0x0100XXXX), "
                              "or leave the field out") from None
    if not 0 <= val <= 0xFFFFFFFF:
        raise UsageError(f"design field \"rm_id\" is {raw!r}, outside 32 bits",
                         hint="write it as 8 hex digits, like 0x01000001 (0x0100XXXX)")


def from_doc(doc: Any, *, origin: str = "", base_dir: Path | None = None) -> Design:
    if not isinstance(doc, dict):
        raise UsageError("a design must be a JSON object")
    kind = str(doc.get("kind", ""))
    if kind not in KINDS:
        raise UsageError(f"design kind must be one of {', '.join(KINDS)}, not {kind!r}")
    name = str(doc.get("name") or "")
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise UsageError(f"design name must be a plain identifier, not {name!r}")
    d = Design(kind, name, str(doc.get("title", "")), doc, origin)
    _check_rm_id(doc)
    wrapper = doc.get("wrapper")
    if wrapper:
        if kind != "rm":
            raise UsageError("only an rm design takes a wrapper file")
        path = Path(str(wrapper))
        if not path.is_absolute():
            if base_dir is None:
                raise UsageError("a wrapper path must be absolute in an inline design",
                                 hint="or give the ports inline")
            path = base_dir / path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise AbsentError(
                f"cannot read the wrapper {path}: {exc.strerror}",
                hint="\"wrapper\" names YOUR RM's source file (it must exist). To get a "
                     "starting file, run rm-kit once without the \"wrapper\" field: it writes "
                     "a wrapper skeleton you can copy and edit") from exc
        try:
            d.wrapper_ports = parse_ansi_ports(text, doc.get("module"),
                                               params=doc.get("params") or {"NGPIO": 16})
        except ValueError as exc:
            raise UsageError(f"wrapper {path.name}: {exc}",
                             hint="the reader takes an ANSI-style module header") from exc
        d.wrapper_path = str(path)
    return d


def load_design(spec: str, builtins: dict[str, Path]) -> Design:
    """A built-in design by name, or a design file by path."""
    if spec in builtins:
        path = builtins[spec]
        return from_doc(json.loads(path.read_text(encoding="utf-8")), origin=f"builtin:{spec}",
                        base_dir=path.parent)
    path = Path(spec)
    if path.suffix == ".json" or path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AbsentError(f"cannot read the design {spec}: {exc.strerror}",
                              hint=f"built-in designs: {', '.join(sorted(builtins))}") from exc
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UsageError(f"{spec} is not JSON: {exc}") from exc
        return from_doc(doc, origin=str(path.resolve()), base_dir=path.resolve().parent)
    raise AbsentError(f"no design named {spec!r}",
                      hint=f"built-in designs: {', '.join(sorted(builtins))}; or a .json path")
