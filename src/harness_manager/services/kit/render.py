"""Render ``build_rm.tcl`` (package data) for one static, one Vivado release and one design.

HM owns ONE board-agnostic template (david K5). The kit supplies the static's facts
(``BuildProfile``: part, partition, boundary bits, static_id, usercode, Vivado release,
clearing arena), the user's design supplies the RM (name, rm_id, top, sources, hooks,
XDCs), and HM supplies the paths. Every value lands in the script's ``array set P {...}``
block, which ``-tclargs NAME=VALUE`` overrides at run time, so ``kit build`` never needs
to re-render.

Values are Tcl words inside braces. A brace or a backslash-newline would end or bend that
word, so a value holding either is refused (``UsageError``); paths are written with ``/``
(Tcl on Windows takes them), and a list element holding a space is braced.

The script writes only the build RECEIPT; ``kit pack`` writes the overlay manifest from
it (david K5). It needs nothing but Vivado: no python, no exec, no platform checkout.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable, Mapping
from importlib.resources import files
from pathlib import Path, PurePath

from harness_manager.core.errors import UsageError
from harness_manager.core.pack import BuildProfile

TEMPLATE_PACKAGE = "harness_manager.services.kit"
TEMPLATE_NAME = "build_rm.tcl.template"
SCRIPT_NAME = "build_rm.tcl"
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")

#: Values a render may leave out.
DEFAULTS = {
    "JOBS": "2",
    "STOP_AFTER": "bitstream",
    "ALLOW_TIMING_FAIL": "0",
    "PR_VERIFY_REF": "",
    "RM_INCLUDE_DIRS": "",
    "RM_DEFINES": "",
    "RM_GENERICS": "",
    "RM_GENERIC_FILES": "",
    "RM_SYNTH_HOOK": "",
    "RM_SYNTH_DCP": "",
    "RM_XDC": "",
    "VIVADO_BUILD": "",
    "OUT_DIR": "out",
}
STAGES = ("preflight", "synth", "link", "impl", "verify", "bitstream")
#: Values that are Tcl lists (``tcl_list`` checked each element).
LIST_PARAMS = ("RM_SOURCES", "RM_INCLUDE_DIRS", "RM_DEFINES", "RM_GENERICS", "RM_GENERIC_FILES")


def template_text() -> str:
    return (files(TEMPLATE_PACKAGE) / "templates" / TEMPLATE_NAME).read_text(encoding="utf-8")


def placeholders(text: str | None = None) -> set[str]:
    return set(_PLACEHOLDER.findall(template_text() if text is None else text))


def tcl_path(p: str | PurePath) -> str:
    """A path as the script writes it: ``/`` separators (Tcl on Windows takes them)."""
    return str(p).replace("\\", "/")


def tcl_word(value: str, key: str) -> str:
    if any(c in value for c in "{}") or "\\\n" in value or "\n" in value:
        raise UsageError(f"{key}: {value!r} holds a brace or a newline, which the generated "
                         "Tcl cannot carry", hint="rename the file or directory")
    return value


def tcl_list(items: Iterable[str], key: str) -> str:
    out = []
    for item in items:
        item = tcl_word(tcl_path(item), key)
        out.append(f"{{{item}}}" if (" " in item or "\t" in item or not item) else item)
    return " ".join(out)


def render(values: Mapping[str, str], *, hm_version: str = "") -> str:
    """The template with every ``{{NAME}}`` filled. Unknown names and missing values are
    refused, so the script can never silently carry an empty static_id or part."""
    from harness_manager import __version__

    text = template_text()
    wanted = placeholders(text)
    vals = {**DEFAULTS, "HM_VERSION": hm_version or __version__,
            "GENERATED_AT": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"), **{k: str(v) for k, v in values.items()}}
    unknown = sorted(set(values) - wanted)
    if unknown:
        raise UsageError(f"build_rm.tcl has no parameter {', '.join(unknown)}")
    missing = sorted(k for k in wanted if k not in vals)
    if missing:
        raise UsageError(f"build_rm.tcl needs {', '.join(missing)}")
    if vals["STOP_AFTER"] not in STAGES:
        raise UsageError(f"STOP_AFTER must be one of {', '.join(STAGES)}")
    for k, v in vals.items():
        if k not in LIST_PARAMS:
            tcl_word(v, k)
    return _PLACEHOLDER.sub(lambda m: vals[m.group(1)], text)


def profile_values(profile: BuildProfile, *, board: str = "") -> dict[str, str]:
    """The static's half of the parameters, from the pack's ``BuildProfile``."""
    if not profile.vivado:
        raise UsageError(f"the Vivado release of static {profile.static_id} is not known",
                         hint="fetch its kit first: the release is a fact of the kit")
    return {
        "BOARD": board or profile.pack,
        "KIT_ID": profile.kit_id or f"{profile.pack}/{profile.static_id}/vivado-{profile.vivado}",
        "STATIC_ID": profile.static_id,
        "STATIC_USERCODE": profile.static_usercode,
        "VIVADO_VERSION": profile.vivado,
        "VIVADO_BUILD": str(profile.vivado_build or ""),
        "PART": profile.part,
        "RP_INST": profile.rp_inst,
        "RP_PBLOCK": profile.rp_pblock,
        "BOUNDARY_PORTS": str(profile.boundary_ports),
        "BOUNDARY_BITS": str(profile.boundary_bits),
        "CLEARING_MAX": str(profile.clr_max),
    }


#: The ways to run the script (KIT-INTERACTIVE, proven on Vivado 2026.1 in
#: docs/evidence/2026-09-30-kit-interactive): ``batch`` runs it and exits; ``gui`` and ``tcl``
#: run it and stay open, so a ``STOP_AFTER=link`` leaves the linked design open to floorplan.
MODES = ("batch", "gui", "tcl")


def tclargs(*, stop_after: str = "", jobs: int | None = None) -> list[str]:
    """The ``NAME=VALUE`` words for ``-tclargs`` (or ``set argv``): only what was asked for,
    so the script's own values stand for the rest."""
    extra = []
    if stop_after:
        if stop_after not in STAGES:
            raise UsageError(f"STOP_AFTER must be one of {', '.join(STAGES)}")
        extra.append(f"STOP_AFTER={stop_after}")
    if jobs:
        extra.append(f"JOBS={int(jobs)}")
    return extra


def vivado_command(script_dir: Path, *, stop_after: str = "", jobs: int | None = None,
                   vivado: str = "vivado", mode: str = "batch") -> list[str]:
    """The command ``kit build`` prints: a run of the script, logged beside it. ``mode``
    ``batch`` (the default) exits at the end; ``gui`` and ``tcl`` stay open after it."""
    if mode not in MODES:
        raise UsageError(f"mode must be one of {', '.join(MODES)}")
    d = Path(script_dir)
    argv = [vivado, "-mode", mode, "-source", tcl_path(d / SCRIPT_NAME),
            "-log", tcl_path(d / "build_rm.log"), "-journal", tcl_path(d / "build_rm.jou")]
    extra = tclargs(stop_after=stop_after, jobs=jobs)
    if extra:
        argv += ["-tclargs", *extra]
    return argv


#: Where ``source_tcl``'s line runs. A design already open there stays open: Vivado 2026.1
#: opens the build's own projects beside it (in -mode tcl and in the GUI; KIT-INTERACTIVE A2/A3),
#: each linked static holding about 3 GB more.
SOURCE_WHEN = ("its Tcl console; a design already open stays open beside the build's, so "
               "close_project it first to save memory")
SOURCE_LOG = ("its HM_ lines go to that session's log, not build_rm.log; the receipt, "
              "out/<name>_build.json, is the verdict either way")


def stays_open(rp_inst: str, rp_pblock: str) -> str:
    """What to do with the linked design that ``STOP_AFTER=link`` leaves open in the GUI, in
    ``-mode tcl`` or in a session that sourced the script: the floorplan loop, as proven in
    docs/evidence/2026-09-30-kit-interactive. ``hm_save_floorplan`` is a proc the script
    defines; ``write_xdc -cell`` is NOT the way (it writes the partition's own pblock too,
    which read back ``-cell`` takes the RM's cells out of it)."""
    return (f"after link the design stays open (GUI, -mode tcl, or the Tcl line): floorplan "
            f"your RM in a child pblock of {rp_pblock} (create_pblock, resize_pblock inside it, "
            f"set_property PARENT {rp_pblock}, add_cells_to_pblock), save it with "
            f"`hm_save_floorplan FILE` (not write_xdc -cell), and name FILE as the design's "
            f"build.rm_xdc (read_xdc -cell {rp_inst} after the next link)")


def source_tcl(script_dir: Path, *, stop_after: str = "", jobs: int | None = None) -> str:
    """One Tcl line for a Vivado that is ALREADY running (its Tcl console, or ``vivado -mode
    tcl``): ``cd {DIR}; set argv {...}; source build_rm.tcl``. ``set argv`` is always there,
    ``{}`` too: a session keeps its ``argv`` (from ``-tclargs``, or the last ``set argv``), and
    the script reads it, so a line without it would run with whatever the last one set."""
    d = tcl_word(tcl_path(Path(script_dir)), "the build directory")
    words = " ".join(tclargs(stop_after=stop_after, jobs=jobs))
    return f"cd {{{d}}}; set argv {{{words}}}; source {SCRIPT_NAME}"


_PARAM_BLOCK = re.compile(r"^array set P \{\n(.*?)^\}", re.M | re.S)
_PARAM_LINE = re.compile(r"^\s+([A-Z_]+)\s+\{(.*)\}\s*$")


def script_params(text: str) -> dict[str, str]:
    """The ``array set P {...}`` block of a generated ``build_rm.tcl``: ``{NAME: value}``
    (the value as rendered, braces stripped). ``{}`` for a file that is not one."""
    m = _PARAM_BLOCK.search(text)
    if m is None:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        lm = _PARAM_LINE.match(line)
        if lm:
            out[lm.group(1)] = lm.group(2)
    return out


# --- markers in a Vivado log ---------------------------------------------------------------------

#: FIX-PACK-3 (P8): ``build_rm.log`` ECHOES the sourced script, so the literal text
#: ``HM_RM_BUILD_FAILED gate=`` stands in it after a real ``HM_RM_BUILD_COMPLETE`` (the
#: script's own ``puts`` lines, indented after a ``#``). "The last HM_RM_BUILD_* line" is
#: then the echo. The verdict is the receipt's ``state`` (what HM reads); in the log, the
#: last line that STARTS with ``HM_RM_BUILD_`` (``parse_markers`` is anchored the same way).
VERDICT_GREP = "grep -E '^HM_RM_BUILD_' build_rm.log | tail -1"
VERDICT_HOW = ("the verdict is the receipt's state (out/<name>_build.json), or the last "
               "line of build_rm.log that STARTS with HM_RM_BUILD_ (`" + VERDICT_GREP + "`; "
               "the log also echoes the script, whose text holds HM_RM_BUILD_FAILED)")

_MARK = re.compile(r"^(HM_GATE|HM_STAGE|HM_RECEIPT|HM_RM_BUILD_COMPLETE|HM_RM_BUILD_FAILED|"
                   r"HM_RM_BUILD_STOPPED)\b(.*)$")


def parse_markers(log_text: str) -> list[tuple[str, str]]:
    """``(marker, rest)`` for every ``HM_*`` line of a Vivado log, ANCHORED at line start:
    Vivado echoes the sourced script, so an unanchored match finds the script's own
    ``puts "HM_RM_BUILD_FAILED ..."`` line (the spike's first wait loop did)."""
    out = []
    for line in log_text.splitlines():
        m = _MARK.match(line)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out
