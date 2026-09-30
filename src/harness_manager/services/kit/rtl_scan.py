"""My RTL: a folder or a ``.f`` file list into a build design (lane UI2-API-BUILD, gap G8 (e)).

docs/API.md ``POST /kits/design/scan`` and the Build section's Design panel (UI_V2_PLAN.md B6,
B8). It reads text files only (no simulator, no Vivado) and proposes; the user confirms by
writing the design (``out``), and ``kit script`` / the build then check it for real:

- **sources, in compile order**: SystemVerilog/Verilog packages first (a package another
  imports before it), then every module after the modules it instantiates, VHDL by the same
  rule (``work.<entity>`` / ``component``); ``tb_*``/``*_tb`` files and ``tb``/``sim``/``test``
  folders are left out (and listed); the order of a ``.f`` list is kept, with ``+incdir+`` and
  ``+define+`` read from it (``-f`` nests; ``-y``/``-v`` library options are not followed);
- **the top**: the module nothing else instantiates (``top`` picks one; an ``rm_*`` module
  wins a tie; several candidates are a warning);
- **include dirs**: the folders of the ``.svh``/``.vh`` files the sources include;
- **generics**: the top's ``#(parameter ...)`` defaults; a string default naming a file that
  exists (a ``$readmemh`` image) becomes ``{"path": FILE}`` (``build.generics``);
- **use**: the partition's boundary groups the top's ports name (the pin model), when the top
  is a wrapper of the partition; otherwise a warning says to write one (``xdc rm-kit`` writes
  its skeleton);
- **rm_id**: the pack's proposal (user range 0x8000-0xFFFF, stable per name).

The design it proposes (``design``) is the XDC design format (docs/XDC_EXPORT.md) plus
``build``, with absolute paths; ``out`` writes it as ``<name>.json``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import AbsentError, HarnessError, UsageError

SV = (".sv", ".v")
VHDL = (".vhd", ".vhdl")
HEADERS = (".svh", ".vh")
SKIP_DIRS = {".git", ".svn", ".hg", "__pycache__", ".Xil", "xsim.dir", "tb", "sim", "test",
             "tests", "testbench", "out", "kit", "xdc"}
_TB_FILE = re.compile(r"(^tb_|_tb$|^test_|_test$)", re.I)
MAX_FILES = 2000
MAX_BYTES = 8 * 1024 * 1024

_MODULE = re.compile(r"\b(module|interface|program)\s+(?:automatic\s+|static\s+)?"
                     r"([A-Za-z_][A-Za-z0-9_$]*)")
_PACKAGE = re.compile(r"\bpackage\s+([A-Za-z_][A-Za-z0-9_$]*)\s*;")
_IMPORT = re.compile(r"\bimport\s+([A-Za-z_][A-Za-z0-9_$]*)\s*::")
_PKG_REF = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*::")
_INCLUDE = re.compile(r"`include\s+\"([^\"]+)\"")
# an instantiation: TYPE [#(...)] NAME ( ... ; the parameter block is skipped separately
_INST = re.compile(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\(|[A-Za-z_][A-Za-z0-9_$]*"
                   r"\s*(?:\[[^\]]*\]\s*)?\()")
_KEYWORDS = {"module", "endmodule", "input", "output", "inout", "wire", "reg", "logic", "assign",
             "always", "always_ff", "always_comb", "always_latch", "initial", "if", "else",
             "for", "while", "case", "casez", "casex", "endcase", "begin", "end", "function",
             "task", "generate", "endgenerate", "genvar", "parameter", "localparam", "return",
             "typedef", "struct", "enum", "import", "package", "endpackage", "interface",
             "endinterface", "bit", "int", "integer", "real", "string", "assert", "property",
             "sequence", "covergroup", "constraint", "class", "virtual", "foreach", "repeat",
             "forever", "posedge", "negedge", "or", "and", "not", "buf", "signed", "unsigned",
             "default", "unique", "priority", "automatic", "static", "const", "var", "void",
             "modport", "clocking", "program", "fork", "join", "disable", "wait", "force",
             "release", "deassign", "specify", "endspecify", "table", "primitive", "defparam"}
_VHDL_ENTITY = re.compile(r"(?im)^\s*entity\s+([A-Za-z_][A-Za-z0-9_]*)\s+is\b")
_VHDL_PACKAGE = re.compile(r"(?im)^\s*package\s+([A-Za-z_][A-Za-z0-9_]*)\s+is\b")
_VHDL_USE = re.compile(r"(?im)\buse\s+work\.([A-Za-z_][A-Za-z0-9_]*)\.")
_VHDL_INST = re.compile(r"(?im)(?::\s*entity\s+work\.([A-Za-z_][A-Za-z0-9_]*)|"
                        r"\bcomponent\s+([A-Za-z_][A-Za-z0-9_]*))")
_PARAM = re.compile(r"\bparameter\s+(?:(?:int|integer|string|bit|logic|real|unsigned|signed)"
                    r"\s+)*(?:\[[^\]]*\]\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^,;]+)")


def _blank_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


@dataclass
class HdlFile:
    path: Path
    lang: str                                   # "sv" | "vhdl"
    modules: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    uses: set[str] = field(default_factory=set)            # modules/entities it instantiates
    imports: set[str] = field(default_factory=set)         # packages it imports
    includes: list[str] = field(default_factory=list)
    text: str = ""


def read_hdl(path: Path) -> HdlFile:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    if Path(path).suffix.lower() in VHDL:
        clean = re.sub(r"--[^\n]*", "", raw)
        f = HdlFile(Path(path), "vhdl", modules=_VHDL_ENTITY.findall(clean),
                    packages=_VHDL_PACKAGE.findall(clean), text=clean)
        f.imports = set(_VHDL_USE.findall(clean))
        f.uses = {a or b for a, b in _VHDL_INST.findall(clean)}
        return f
    clean = _blank_comments(raw)
    f = HdlFile(Path(path), "sv", modules=[m[1] for m in _MODULE.findall(clean)],
                packages=_PACKAGE.findall(clean), includes=_INCLUDE.findall(clean), text=clean)
    f.imports = set(_IMPORT.findall(clean)) | {p for p in _PKG_REF.findall(clean)
                                               if p not in ("std", "$unit")}
    body = re.sub(r"\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*", " ", clean)
    f.uses = {m for m in _INST.findall(body) if m not in _KEYWORDS}
    return f


# --- finding the files --------------------------------------------------------------------------


def _skip(p: Path, root: Path) -> bool:
    rel = p.relative_to(root)
    return any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]) or \
        bool(_TB_FILE.search(p.stem))


def walk(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """(HDL files, headers, left out as test benches), path order."""
    hdl: list[Path] = []
    heads: list[Path] = []
    left: list[Path] = []
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf not in SV + VHDL + HEADERS:
            continue
        if _skip(p, root):
            left.append(p)
            continue
        (heads if suf in HEADERS else hdl).append(p)
        if len(hdl) + len(heads) > MAX_FILES:
            raise UsageError(f"{root} holds more than {MAX_FILES} HDL files",
                             hint="give the RTL folder itself, or a .f list")
    return hdl, heads, left


def read_filelist(path: Path, *, depth: int = 0) -> tuple[list[Path], list[Path], list[str],
                                                         list[str]]:
    """A ``.f`` list: (sources in its order, include dirs, defines, notes). Paths are
    relative to the list's folder; ``-f`` nests (4 deep); ``-y``/``-v`` are noted, not read."""
    if depth > 4:
        raise UsageError(f"{path}: -f nests more than 4 deep")
    base = Path(path).parent
    srcs: list[Path] = []
    incs: list[Path] = []
    defs: list[str] = []
    notes: list[str] = []
    words = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = re.sub(r"(//|#).*$", "", line).strip()
        words += line.split()
    it = iter(words)
    for w in it:
        w = os.path.expandvars(w)                # $CMSDK_DIR/... as the list's user meant it
        if w.startswith("+incdir+"):
            incs += [(base / d) for d in w[len("+incdir+"):].split("+") if d]
        elif w.startswith("+define+"):
            defs += [d for d in w[len("+define+"):].split("+") if d]
        elif w in ("-f", "-F"):
            nested = base / next(it, "")
            s2, i2, d2, n2 = read_filelist(nested, depth=depth + 1)
            srcs += s2
            incs += i2
            defs += d2
            notes += n2
        elif w in ("-y", "-v", "+libext+") or w.startswith(("-y", "-v", "+libext")):
            notes.append(f"{Path(path).name}: {w} (a library option) is not followed")
            if w in ("-y", "-v"):
                next(it, None)
        elif w.startswith(("-", "+")):
            notes.append(f"{Path(path).name}: {w} is not read")
        else:
            srcs.append(base / w)
    return srcs, incs, defs, notes


# --- ordering -------------------------------------------------------------------------------------


def compile_order(files: list[HdlFile]) -> list[HdlFile]:
    """Packages before their importers, modules after what they instantiate; ties and cycles
    keep the given order."""
    defines: dict[str, HdlFile] = {}
    for f in files:
        for name in f.modules + f.packages:
            defines.setdefault(name, f)
    order: list[HdlFile] = []
    state: dict[int, int] = {}

    def visit(f: HdlFile) -> None:
        key = id(f)
        if state.get(key) == 2:
            return
        if state.get(key) == 1:
            return                               # a cycle: keep what we have
        state[key] = 1
        needs = [defines[n] for n in sorted(f.imports) if n in defines] + \
            [defines[n] for n in sorted(f.uses) if n in defines]
        for dep in needs:
            if dep is not f:
                visit(dep)
        state[key] = 2
        order.append(f)

    for f in sorted(files, key=lambda x: 0 if x.packages and not x.modules else 1):
        visit(f)
    return order


def top_candidates(files: list[HdlFile]) -> list[str]:
    """Modules nothing else instantiates, in file order."""
    used = set().union(*(f.uses for f in files)) if files else set()
    return [m for f in files for m in f.modules if m not in used]


def parameters(text: str, module: str) -> list[dict[str, str]]:
    """``[{name, default}]`` of the module's ``#( ... )`` header, in order."""
    m = re.search(rf"\bmodule\s+{re.escape(module)}\s*#\s*\(", text)
    if m is None:
        return []
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return [{"name": n, "default": d.strip()} for n, d in _PARAM.findall(text[m.end():i - 1])]


# --- the scan -------------------------------------------------------------------------------------


@dataclass
class Scan:
    path: Path
    kind: str                                 # "folder" | "filelist"
    name: str
    top: str
    tops: list[str]
    sources: list[Path]
    include_dirs: list[Path]
    defines: list[str]
    packages: list[str]
    generics: dict[str, Any]
    use: dict[str, Any]
    ports: list[dict[str, Any]]
    rm_id: str
    rm_id_proposed: bool
    left_out: list[Path]
    warnings: list[str]
    written: Path | None = None

    def design(self) -> dict[str, Any]:
        build: dict[str, Any] = {"top": self.top, "sources": [str(p) for p in self.sources]}
        if self.include_dirs:
            build["include_dirs"] = [str(p) for p in self.include_dirs]
        if self.defines:
            build["defines"] = list(self.defines)
        if self.generics:
            build["generics"] = dict(self.generics)
        return {"kind": "rm", "name": self.name, "rm_id": self.rm_id, "use": dict(self.use),
                "build": build}

    def to_json(self) -> dict[str, Any]:
        return {"path": str(self.path), "kind": self.kind, "name": self.name, "top": self.top,
                "tops": list(self.tops), "sources": [str(p) for p in self.sources],
                "include_dirs": [str(p) for p in self.include_dirs],
                "defines": list(self.defines), "packages": list(self.packages),
                "generics": dict(self.generics), "use": dict(self.use), "ports": self.ports,
                "rm_id": self.rm_id, "rm_id_proposed": self.rm_id_proposed,
                "left_out": [str(p) for p in self.left_out], "warnings": list(self.warnings),
                "design": self.design(),
                "written": str(self.written) if self.written else None}


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _boundary(pack: str, static_id: str) -> dict[str, str]:
    """signal name -> boundary group, from the pack's pin model (empty when unknown)."""
    from harness_manager.services import xdc

    try:
        model = xdc.load_pack_pins(pack).model
        return {s.name: s.group for s in model.boundary(static_id or None)}
    except (HarnessError, LookupError, OSError, ValueError):
        return {}


def scan(path: Path, *, name: str = "", top: str = "", pack: str = "mps3", static_id: str = "",
         rm_id: str = "", adapter: Any = None, store: Any = None) -> Scan:
    """The proposal for ``path`` (module docstring). ``AbsentError`` for no path or no HDL,
    ``UsageError`` for a bad name, a ``top`` no file defines, or too many files."""
    p = Path(path)
    if not p.exists():
        raise AbsentError(f"no such file or folder: {p}", hint="give an RTL folder or a .f list")
    warnings: list[str] = []
    defines: list[str] = []
    include_dirs: list[Path] = []
    left: list[Path] = []
    if p.is_file():
        if p.suffix.lower() not in (".f", ".flist", ".lst"):
            raise UsageError(f"{p.name} is not a file list (.f)", hint="give an RTL folder or a "
                                                                       ".f list")
        kind = "filelist"
        paths, include_dirs, defines, notes = read_filelist(p)
        warnings += notes
        missing = [x for x in paths if not x.is_file()]
        if missing:
            warnings.append(f"{len(missing)} file(s) in the list do not exist: "
                            + ", ".join(str(x) for x in missing[:4]))
        paths = [x for x in paths if x.is_file()]
        heads = [x for x in paths if x.suffix.lower() in HEADERS]
        paths = [x for x in paths if x.suffix.lower() in SV + VHDL]
    else:
        kind = "folder"
        paths, heads, left = walk(p)
    if not paths:
        raise AbsentError(f"no HDL (.sv, .v, .vhd) in {p}",
                          hint="give the folder that holds your RTL, or a .f list")
    if sum(x.stat().st_size for x in paths) > MAX_BYTES:
        raise UsageError(f"the HDL under {p} is over {MAX_BYTES // (1024 * 1024)} MB",
                         hint="give the RTL folder itself, or a .f list")
    files = [read_hdl(x) for x in paths]
    ordered = files if kind == "filelist" else compile_order(files)
    tops = top_candidates(files)
    if top:
        if not any(top in f.modules for f in files):
            raise UsageError(f"no file under {p} defines module {top!r}",
                             hint=f"candidates: {', '.join(tops) or 'none'}")
        chosen = top
    else:
        rm = [t for t in tops if t.startswith("rm_")]
        chosen = (rm or tops or [files[-1].modules[0] if files[-1].modules else ""])[0]
        if len(tops) > 1:
            warnings.append(f"{len(tops)} modules are instantiated by nothing ({', '.join(tops[:6])}"
                            f"{'...' if len(tops) > 6 else ''}): {chosen} is the top; pick "
                            "another with top")
    if not chosen:
        raise AbsentError(f"no module or entity in {p}", hint="give the folder with your RTL")
    nm = name or (chosen[3:] if chosen.startswith("rm_") and len(chosen) > 3 else chosen)
    if not _NAME.fullmatch(nm):
        raise UsageError(f"name {nm!r} is not a design name (letters, digits, _)")
    # include dirs: the folders of the headers the sources include
    wanted = {inc for f in files for inc in f.includes}
    for h in heads:
        if h.name in wanted or any(str(h).endswith(w) for w in wanted):
            parent = h.parent
            if parent not in include_dirs:
                include_dirs.append(parent)
    unresolved = sorted(w for w in wanted if not any(h.name == Path(w).name for h in heads))
    if unresolved:
        warnings.append(f"`include of {', '.join(unresolved[:4])} found no file here: add its "
                        "folder to build.include_dirs")
    top_file = next(f for f in files if chosen in f.modules)
    generics: dict[str, Any] = {}
    for prm in parameters(top_file.text, chosen) if top_file.lang == "sv" else []:
        d = prm["default"]
        m = re.fullmatch(r'"([^"]+)"', d)
        if m and (top_file.path.parent / m.group(1)).is_file():
            generics[prm["name"]] = {"path": str((top_file.path.parent / m.group(1)).resolve())}
        elif m and re.search(r"\.(hex|mem|coe|mif)$", m.group(1)):
            warnings.append(f"parameter {prm['name']} names {m.group(1)}, which is not beside "
                            f"{top_file.path.name}: set it in build.generics")
    use: dict[str, Any] = {}
    ports: list[dict[str, Any]] = []
    if top_file.lang == "sv":
        from harness_manager.services.xdc.hdl import parse_ansi_ports

        try:
            hdl_ports = parse_ansi_ports(top_file.text, chosen)
        except ValueError as exc:
            hdl_ports = []
            warnings.append(f"the ports of {chosen} could not be read ({exc})")
        boundary = _boundary(pack, static_id)
        ports = [{"name": x.name, "dir": x.direction, "width": x.width,
                  "group": boundary.get(x.name)} for x in hdl_ports]
        outside = [x["name"] for x in ports if x["group"] is None]
        if ports and boundary and not outside:
            use = {g: {} for g in dict.fromkeys(x["group"] for x in ports)}
        elif ports and boundary:
            warnings.append(f"{chosen} is not a wrapper of the partition: {len(outside)} of its "
                            f"ports are not boundary signals ({', '.join(outside[:5])}). Write "
                            "a wrapper (`harness-manager xdc rm-kit` writes its skeleton) and "
                            "make it the top")
    proposed = False
    if rm_id:
        from .schema import hex32, parse_u32

        try:
            rm = hex32(parse_u32(rm_id))
        except (TypeError, ValueError):
            raise UsageError(f"rm_id {rm_id!r} is not a 32-bit hex id") from None
    elif adapter is not None:
        from .schema import hex32

        rm = hex32(adapter.propose_rm_id(nm, adapter.taken_designs(store)))
        proposed = True
    else:
        rm = ""
    packages = [pk for f in ordered for pk in f.packages]
    return Scan(p, kind, nm, chosen, tops, [f.path for f in ordered], include_dirs, defines,
                packages, generics, use, ports, rm, proposed, left, warnings)


def write_design(s: Scan, out: Path) -> Path:
    """Write the proposal: ``out`` a folder (``<out>/<name>.json``) or a ``.json`` path.
    Refuses to overwrite a file that is not a design of the same name."""
    out = Path(out)
    target = out if out.suffix.lower() == ".json" else out / f"{s.name}.json"
    if target.exists():
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        if not isinstance(old, dict) or old.get("name") != s.name:
            raise UsageError(f"{target} exists and is not the design {s.name!r}",
                             hint="give another out, or remove it")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(s.design(), indent=2) + "\n", encoding="utf-8")
    s.written = target
    return target
