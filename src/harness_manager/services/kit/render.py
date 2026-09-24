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
    "RM_SYNTH_HOOK": "",
    "RM_SYNTH_DCP": "",
    "RM_XDC": "",
    "VIVADO_BUILD": "",
    "OUT_DIR": "out",
}
STAGES = ("preflight", "synth", "link", "impl", "verify", "bitstream")


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
        if k not in ("RM_SOURCES", "RM_INCLUDE_DIRS", "RM_DEFINES"):
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


def vivado_command(script_dir: Path, *, stop_after: str = "", jobs: int | None = None,
                   vivado: str = "vivado") -> list[str]:
    """The command ``kit build`` runs (or prints): a batch run of the script, logged beside it."""
    d = Path(script_dir)
    argv = [vivado, "-mode", "batch", "-source", tcl_path(d / SCRIPT_NAME),
            "-log", tcl_path(d / "build_rm.log"), "-journal", tcl_path(d / "build_rm.jou")]
    extra = []
    if stop_after:
        extra.append(f"STOP_AFTER={stop_after}")
    if jobs:
        extra.append(f"JOBS={int(jobs)}")
    if extra:
        argv += ["-tclargs", *extra]
    return argv


# --- markers in a Vivado log ---------------------------------------------------------------------

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
