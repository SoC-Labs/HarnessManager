"""``kit.json`` v1 (the DUT build kit) and the build receipt v1, parsed strictly.

A **kit** is everything the machine that runs Vivado needs from one locked static, and
nothing else (docs/design/DUT_BUILD_KIT_STORAGE.md §2, §8)::

    {
      "schema": "hm-rm-kit", "schema_version": 1,
      "board_type": "mps3", "part": "xcku115-flvb1760-1-c",
      "static_id": "0x72BB0A36", "static_usercode": "0xC8551081",
      "harness_impl": "bare-metal",                        # "bare-metal" | "linux" | ""
      "vivado": {"release": "2024.1", "build": 5076996, "checkpoint_version": 22},
      "rp": {"inst": "u_rp_dut", "pblock": "pblock_rp_dut", "ports": 47, "bits": 148,
             "clr_max": 262144,                           # the harness's clearing arena (bytes)
             "frames": {"idcode": "0x0390D093",            # optional: the partition's stream facts
                        "partial": {"by_block": {...}, "cmds": [...], "regs": [...]},
                        "clearing": {...}}},
      "pr_verify_ref": "static/static_routed_locked.dcp",
      "access": "public",                                  # david K2: no token gate for kits
      "ip_class": "open",
      "licence_note": "the DCP holds AMD IP netlists, encrypted where AMD secures them; no Arm IP",
      "files": [{"path": "static/static_routed_locked.dcp", "role": "locked_static",
                 "size": 10152801, "sha256": "…", "crc32": "0x72BB0A36"}, ...],
      "source": {"repo_sha": "c855108…", "dirty": true},
      "generated_by": "fpga/dfx/tools/pack_kit.py"
    }

Rules:

- **``static_id`` is the CRC-32 of the locked static DCP** (``build_dfx.tcl:725``). The
  ``locked_static`` file entry must carry that same ``crc32``; ``verify`` recomputes it
  from the bytes, never trusts the field.
- Exactly one file has role ``locked_static``. Paths are relative, ``/``-separated, with no
  ``..``: a kit is a directory Vivado opens as it is.
- ``vivado.release`` is ``YYYY.N`` (optionally ``.U``). A DCP opens only in the release that
  wrote it, and the generated ``build_rm.tcl`` refuses a different major.minor (david K4).
- ``rp.frames`` feeds the board-free partial validator (``harness_manager_mps3.bitcheck``):
  the frame box and command vocabulary of the partition, so HM ships no reference partial.
- Unknown fields are kept (``extra``) and never refused, so a newer packer works with an
  older HM. A newer ``schema_version`` is refused: it may mean something this HM cannot check.

The **receipt** (``<rm>_build.json``, schema ``harness-manager-rm-build`` v1) is what
``build_rm.tcl`` writes after a pass, a failed gate or ``STOP_AFTER``. It binds the built
pair to the static: its ``static_id`` is the CRC the build computed from the DCP it opened.
Every value the Tcl writes is a JSON string; this parser types them.
"""

from __future__ import annotations

import hashlib
import json
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from harness_manager.core.errors import RefusedError

KIT_SCHEMA = "hm-rm-kit"
KIT_SCHEMA_VERSION = 1
KIT_JSON = "kit.json"

RECEIPT_SCHEMA = "harness-manager-rm-build"
RECEIPT_SCHEMA_VERSION = 1

ROLE_LOCKED_STATIC = "locked_static"
#: Roles a kit file may have. Unknown roles are kept (a newer packer), never refused.
ROLES = (ROLE_LOCKED_STATIC, "stamp", "mint_record", "static_id", "boundary", "build",
         "manifest", "xdc", "ltx", "doc", "other")

ACCESS_PUBLIC = "public"
ACCESS_TOKEN = "github-token"
ACCESSES = (ACCESS_PUBLIC, ACCESS_TOKEN)
IMPLS = ("bare-metal", "linux", "")
IP_CLASSES = ("open", "arm-aaa", "unknown")

#: The default note: what a static DCP holds, for the licensing question (KIT-STORE §2.3).
DEFAULT_LICENCE_NOTE = ("the locked static DCP holds AMD IP netlists (MicroBlaze, AXI "
                        "peripherals, debug_bridge, ...), encrypted where AMD secures them, "
                        "and the SoC Labs shell netlist; it holds no Arm IP")

_HEX32 = re.compile(r"^0[xX][0-9A-Fa-f]{1,8}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_RELEASE = re.compile(r"^(\d{4})\.(\d+)(?:\.(\d+))?$")

RECEIPT_STATES = ("passed", "failed", "stopped")
STAGES = ("init", "preflight", "synth", "link", "impl", "verify", "bitstream")


class KitFormatError(RefusedError):
    """A kit.json or receipt that is malformed: refused whole (exit 15)."""


def _fail(where: str, what: str, *, what_file: str = KIT_JSON) -> KitFormatError:
    return KitFormatError(f"{what_file} {where}: {what}",
                          hint="the kit is malformed; fetch it again or ask whoever packed it")


# --- small shared helpers ---------------------------------------------------------------------


def hex32(value: int) -> str:
    """``0x72BB0A36``: the spelling kit.json, the receipt and ``static_id.txt`` use."""
    return f"0x{value & 0xFFFFFFFF:08X}"


def parse_u32(value: Any) -> int:
    """An id as an int: ``"0x72bb0a36"``, ``"0x0100_8000"`` or an int. ``ValueError`` if not."""
    if isinstance(value, bool):
        raise ValueError("not an id")
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    text = str(value).strip()
    if not text:
        raise ValueError("empty id")
    return int(text, 0) & 0xFFFFFFFF


def same_id(a: Any, b: Any) -> bool:
    """Compare two ids by u32 value ("0x72bb0a36" == "0x72BB0A36")."""
    try:
        return parse_u32(a) == parse_u32(b)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def canon_id(value: Any) -> str:
    """The store's spelling of an id: lowercase, zero-padded (``0x72bb0a36``), as the shell
    reports it and ``harness_manager_mps3.overlays`` stores it."""
    return f"0x{parse_u32(value):08x}"


def crc32_file(path: Path) -> int:
    """zlib CRC-32 of a file, 1 MiB at a time: the same number as ``build_dfx.tcl``'s."""
    crc = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def release_major_minor(release: str) -> str:
    """``"2024.1.2"`` -> ``"2024.1"``; ``""`` for anything that is not a Vivado release."""
    m = _RELEASE.match(str(release or "").strip())
    return f"{m.group(1)}.{m.group(2)}" if m else ""


def safe_rel_path(value: Any, where: str, *, what_file: str = KIT_JSON) -> str:
    """A relative ``/`` path with no ``..``, no drive and no backslash."""
    if not isinstance(value, str) or not value:
        raise _fail(where, "must be a non-empty relative path", what_file=what_file)
    if "\\" in value or ":" in value:
        raise _fail(where, f"{value!r} must use '/' and no drive letter", what_file=what_file)
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ("..", "") for part in value.split("/")):
        raise _fail(where, f"{value!r} must be relative, with no '..'", what_file=what_file)
    return str(p)


# --- kit.json ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class KitFile:
    path: str                 # relative to the kit directory: "static/static_routed_locked.dcp"
    role: str
    size: int
    sha256: str
    crc32: str = ""           # "0x72BB0A36"; required for the locked static

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "role": self.role, "size": self.size,
                               "sha256": self.sha256}
        if self.crc32:
            out["crc32"] = self.crc32
        return out


@dataclass(frozen=True)
class VivadoRelease:
    release: str              # "2024.1"
    build: int = 0            # 5076996 (dcp.xml BUILD_NUMBER); 0 = not recorded
    checkpoint_version: int = 0   # 22 (dcp.xml Checkpoint Version)

    @property
    def major_minor(self) -> str:
        return release_major_minor(self.release)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"release": self.release}
        if self.build:
            out["build"] = self.build
        if self.checkpoint_version:
            out["checkpoint_version"] = self.checkpoint_version
        return out


@dataclass(frozen=True)
class Partition:
    inst: str                 # "u_rp_dut"
    pblock: str               # "pblock_rp_dut"
    ports: int                # 47
    bits: int                 # 148 (the OOC netlist's bit-level port count)
    clr_max: int = 0          # bytes; 0 = not recorded (the pack's default applies)
    frames: dict[str, Any] = field(default_factory=dict)   # the partition's stream facts

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"inst": self.inst, "pblock": self.pblock, "ports": self.ports,
                               "bits": self.bits}
        if self.clr_max:
            out["clr_max"] = self.clr_max
        if self.frames:
            out["frames"] = self.frames
        return out


@dataclass(frozen=True)
class KitManifest:
    board_type: str
    part: str
    static_id: str            # "0x72BB0A36" as written
    vivado: VivadoRelease
    rp: Partition
    files: tuple[KitFile, ...]
    static_usercode: str = ""
    harness_impl: str = ""
    pr_verify_ref: str = ""
    access: str = ACCESS_PUBLIC
    ip_class: str = "open"
    licence_note: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    generated_by: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # -- derived -------------------------------------------------------------------------------

    @property
    def static_u32(self) -> int:
        return parse_u32(self.static_id)

    @property
    def kit_id(self) -> str:
        """``mps3/0x72BB0A36/vivado-2024.1``: what the guide and the receipt call this kit."""
        return f"{self.board_type}/{hex32(self.static_u32)}/vivado-{self.vivado.release}"

    @property
    def locked_static(self) -> KitFile:
        return next(f for f in self.files if f.role == ROLE_LOCKED_STATIC)

    def file(self, role: str) -> KitFile | None:
        return next((f for f in self.files if f.role == role), None)

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": KIT_SCHEMA, "schema_version": KIT_SCHEMA_VERSION,
            "board_type": self.board_type, "part": self.part,
            "static_id": self.static_id, "static_usercode": self.static_usercode,
            "harness_impl": self.harness_impl, "vivado": self.vivado.to_json(),
            "rp": self.rp.to_json(), "pr_verify_ref": self.pr_verify_ref,
            "access": self.access, "ip_class": self.ip_class,
            "licence_note": self.licence_note,
            "files": [f.to_json() for f in self.files],
            "source": self.source, "generated_by": self.generated_by,
        }
        return {**out, **self.extra}

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=1, sort_keys=True) + "\n"


_KIT_KEYS = {"schema", "schema_version", "board_type", "part", "static_id", "static_usercode",
             "harness_impl", "vivado", "rp", "pr_verify_ref", "access", "ip_class",
             "licence_note", "files", "source", "generated_by"}


def _obj(v: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(v, Mapping):
        raise _fail(where, "must be an object")
    return v


def _str(d: Mapping[str, Any], key: str, where: str, *, required: bool = True,
         default: str = "") -> str:
    if key not in d or d[key] is None:
        if required:
            raise _fail(where, f"missing required field {key!r}")
        return default
    v = d[key]
    if not isinstance(v, str):
        raise _fail(f"{where}.{key}", "must be a string")
    return v


def _int(d: Mapping[str, Any], key: str, where: str, *, required: bool = True,
         minimum: int = 0) -> int:
    if key not in d or d[key] is None:
        if required:
            raise _fail(where, f"missing required field {key!r}")
        return 0
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise _fail(f"{where}.{key}", f"must be an integer >= {minimum}, not {v!r}")
    return v


def _hex(v: str, where: str, *, allow_empty: bool = False) -> str:
    if allow_empty and not v:
        return ""
    if not _HEX32.match(v):
        raise _fail(where, f"{v!r} is not a 32-bit hex id such as 0x72BB0A36")
    return v


def parse_kit(doc: Any) -> KitManifest:
    """Validate a kit.json document. ``KitFormatError`` (exit 15) for anything malformed."""
    d = _obj(doc, "")
    if d.get("schema") != KIT_SCHEMA:
        raise _fail("schema", f"is {d.get('schema')!r}, not {KIT_SCHEMA!r}: not a build kit")
    ver = d.get("schema_version")
    if ver != KIT_SCHEMA_VERSION:
        raise _fail("schema_version", f"{ver!r} is not {KIT_SCHEMA_VERSION}: a newer kit "
                                      "format than this Harness Manager understands")
    impl = _str(d, "harness_impl", "", required=False)
    if impl not in IMPLS:
        raise _fail("harness_impl", f"{impl!r} is not one of {IMPLS[:2]}")
    access = _str(d, "access", "", required=False, default=ACCESS_PUBLIC)
    if access not in ACCESSES:
        raise _fail("access", f"{access!r} is not one of {ACCESSES}")
    ip_class = _str(d, "ip_class", "", required=False, default="open")
    if ip_class not in IP_CLASSES:
        raise _fail("ip_class", f"{ip_class!r} is not one of {IP_CLASSES}")
    if ip_class == "arm-aaa" and access != ACCESS_TOKEN:
        raise _fail("access", "an arm-aaa kit must be access 'github-token', never public")

    v = _obj(d.get("vivado"), "vivado")
    release = _str(v, "release", "vivado")
    if not release_major_minor(release):
        raise _fail("vivado.release", f"{release!r} is not a Vivado release such as 2024.1")
    vivado = VivadoRelease(release, _int(v, "build", "vivado", required=False),
                           _int(v, "checkpoint_version", "vivado", required=False))

    r = _obj(d.get("rp"), "rp")
    frames = r.get("frames") or {}
    if not isinstance(frames, Mapping):
        raise _fail("rp.frames", "must be an object")
    rp = Partition(_str(r, "inst", "rp"), _str(r, "pblock", "rp"),
                   _int(r, "ports", "rp", minimum=1), _int(r, "bits", "rp", minimum=1),
                   _int(r, "clr_max", "rp", required=False), dict(frames))

    raw_files = d.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise _fail("files", "must be a non-empty list")
    files: list[KitFile] = []
    seen: set[str] = set()
    for i, f in enumerate(raw_files):
        where = f"files[{i}]"
        f = _obj(f, where)
        path = safe_rel_path(f.get("path"), f"{where}.path")
        if path.lower() in seen or path == KIT_JSON:
            raise _fail(f"{where}.path", f"{path!r} is listed twice (or is kit.json itself)")
        seen.add(path.lower())
        sha = _str(f, "sha256", where).lower()
        if not _SHA.match(sha):
            raise _fail(f"{where}.sha256", f"{sha!r} is not a sha256 digest")
        crc = _hex(_str(f, "crc32", where, required=False), f"{where}.crc32", allow_empty=True)
        files.append(KitFile(path, _str(f, "role", where, required=False, default="other"),
                             _int(f, "size", where), sha, crc))
    locked = [f for f in files if f.role == ROLE_LOCKED_STATIC]
    if len(locked) != 1:
        raise _fail("files", f"needs exactly one file with role {ROLE_LOCKED_STATIC!r}, "
                             f"has {len(locked)}")
    static_id = _hex(_str(d, "static_id", ""), "static_id")
    if not locked[0].crc32:
        raise _fail("files", "the locked static needs its crc32 (it IS the static_id)")
    if not same_id(locked[0].crc32, static_id):
        raise _fail("files", f"the locked static's crc32 {locked[0].crc32} is not the kit's "
                             f"static_id {static_id}")
    pr_ref = _str(d, "pr_verify_ref", "", required=False)
    if pr_ref:
        pr_ref = safe_rel_path(pr_ref, "pr_verify_ref")
        if pr_ref.lower() not in seen:
            raise _fail("pr_verify_ref", f"{pr_ref!r} is not one of the kit's files")
    source = d.get("source") or {}
    if not isinstance(source, Mapping):
        raise _fail("source", "must be an object")
    return KitManifest(
        board_type=_str(d, "board_type", ""), part=_str(d, "part", ""),
        static_id=static_id, vivado=vivado, rp=rp, files=tuple(files),
        static_usercode=_hex(_str(d, "static_usercode", "", required=False), "static_usercode",
                             allow_empty=True),
        harness_impl=impl, pr_verify_ref=pr_ref, access=access, ip_class=ip_class,
        licence_note=_str(d, "licence_note", "", required=False),
        source=dict(source), generated_by=_str(d, "generated_by", "", required=False),
        extra={k: v for k, v in d.items() if k not in _KIT_KEYS})


def load_kit_json(path: Path) -> KitManifest:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise KitFormatError(f"cannot read {path}: {exc.strerror}",
                             hint="is this a kit directory?") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise KitFormatError(f"{path} is not JSON: {exc}", hint="fetch the kit again") from exc
    return parse_kit(doc)


def file_entry(root: Path, rel: str, role: str, *, with_crc: bool = False) -> KitFile:
    """A ``KitFile`` measured from ``root/rel`` (a packer's helper)."""
    p = root / rel
    return KitFile(rel, role, p.stat().st_size, sha256_file(p),
                   hex32(crc32_file(p)) if with_crc else "")


# --- the build receipt ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    gate: str
    verdict: str              # PASS | FAIL | NOTE
    detail: str = ""


@dataclass(frozen=True)
class BuildReceipt:
    path: Path
    state: str                # passed | failed | stopped
    stage: str
    kit_id: str
    fields: dict[str, str]    # every other key the Tcl wrote, as written
    gates: tuple[Gate, ...]

    def get(self, key: str, default: str = "") -> str:
        return self.fields.get(key, default)

    @property
    def rm_name(self) -> str:
        return self.get("rm_name")

    @property
    def failed_gates(self) -> list[Gate]:
        return [g for g in self.gates if g.verdict == "FAIL"]

    @property
    def failed_gate(self) -> Gate | None:
        failed = self.failed_gates
        return failed[-1] if failed else None

    def to_json(self) -> dict[str, Any]:
        return {"path": str(self.path), "state": self.state, "stage": self.stage,
                "kit_id": self.kit_id, **self.fields,
                "gates": [{"gate": g.gate, "verdict": g.verdict, "detail": g.detail}
                          for g in self.gates]}


def parse_receipt(doc: Any, path: Path) -> BuildReceipt:
    rf = f"receipt {Path(path).name}"
    if not isinstance(doc, Mapping):
        raise _fail("", "must be a JSON object", what_file=rf)
    if doc.get("schema") != RECEIPT_SCHEMA:
        raise _fail("schema", f"is {doc.get('schema')!r}: not a build receipt", what_file=rf)
    if doc.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise _fail("schema_version", f"{doc.get('schema_version')!r} is not "
                                      f"{RECEIPT_SCHEMA_VERSION}", what_file=rf)
    state = doc.get("state")
    if state not in RECEIPT_STATES:
        raise _fail("state", f"{state!r} is not one of {RECEIPT_STATES}", what_file=rf)
    gates_raw = doc.get("gates", [])
    if not isinstance(gates_raw, list):
        raise _fail("gates", "must be a list", what_file=rf)
    gates = []
    for i, g in enumerate(gates_raw):
        if not isinstance(g, Mapping) or not isinstance(g.get("gate"), str):
            raise _fail(f"gates[{i}]", "must be {gate, verdict, detail}", what_file=rf)
        gates.append(Gate(str(g["gate"]), str(g.get("verdict", "")), str(g.get("detail", ""))))
    fields = {str(k): ("" if v is None else str(v)) for k, v in doc.items()
              if k not in ("schema", "schema_version", "state", "stage", "kit_id", "gates")}
    return BuildReceipt(Path(path), str(state), str(doc.get("stage", "")),
                        str(doc.get("kit_id", "")), fields, tuple(gates))


def load_receipt(path: Path) -> BuildReceipt:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise KitFormatError(f"cannot read the receipt {path}: {exc.strerror}",
                             hint="build_rm.tcl writes <rm>_build.json in its OUT_DIR") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise KitFormatError(f"the receipt {path} is not JSON: {exc}",
                             hint="a receipt cut short by a crash: run the build again") from exc
    return parse_receipt(doc, Path(path))
