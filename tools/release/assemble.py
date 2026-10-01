"""Assemble a harness BUNDLE dir (``harness.py``'s input contract) from platform artifacts.

There is still no platform bundle producer (R1). This is the stand-in: it READS what a
finished mint and image build leave on disk and lays it out as the bundle dir that
``harness.ingest`` validates, signs into a channel entry and turns into assets. Every
input is read-only; the bundle is written under ``dest`` only.

Inputs (``assemble``; ``python -m tools.release harness-release`` and ``make harness-release``
drive it)::

    from_dir     the mint's prod dir: linux_bundle.json (Linux, FLOW_CONTRACT v1.6 §0.1) or
                 mint.json (bare metal), the flashable .bit, linux_slot.img and
                 linux_legal_info.tar beside it
    bit          the config-SD .bit; default: linux_bundle.json's
                 targets.mcc_sd.flashable_bit, found next to it
    stage0_bake  a stage0 RE-BAKE record (schema mps3-stage0-bake v1) whose baked_bit IS
                 ``bit``: the SD carries that re-bake instead of the one the Linux bundle
                 names (the public v2.0.0 bundle's generic bake, label MPS3). It must name
                 the same static_id and UserID, a mint, and a stage0 compiled for this
                 static; the bundle's copy of linux_bundle.json records the swap
    sd_templates the platform's fpga/mps3_sd/templates: the SD tree is stamped from them the
                 way assemble_sd.sh stamps it (config.txt at the root; per revision
                 MB/HBI0309<rev>/board.txt with @BOARD@ filled in; Nanosoc/nanosoc.txt,
                 images.txt and the .bit under the name F0FILE gives)
    sd_tree      OR a ready config-SD tree, taken as it is
    overlays     overlay triples ``<rm>/manifest.json`` (+ partial, clearing, .ltx). The
                 Arm-IP RMs (``harness.AAA_RMS``) are LEFT OUT unless ``include_aaa``;
                 with it they go to overlays/aaa (the private AAA repo), never overlays/open
    kit          the RM kit zip (``kit.json`` is read from inside it)
    notes        release notes (default: written from what was assembled)

Checks made here, before ``ingest`` repeats the hard ones (nothing is written on a refusal):
the inputs exist; a Linux bundle's flashable .bit is the SD .bit (or a matching re-bake
record says why not); each overlay staged is the one the Linux bundle was packed with
(rm_name + both CRC-32s); a kit is keyed to the same static.
"""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import ReleaseError, sha256_file
from .harness import AAA_RMS, _hex, _val

REVS = ("A", "B", "C")
TEMPLATES = ("config.txt", "board.txt", "nanosoc.txt")
DEFAULT_APPFILE = "Nanosoc\\nanosoc.txt"
DEFAULT_F0FILE = "nanosoc.bit"
_KEY_RE = r"^\s*{key}\s*:\s*([^;\s]+)"


@dataclass
class Assembled:
    """What ``assemble`` wrote, and what it left out (for the report and the notes)."""

    bundle: Path
    impl: str
    static_id: str
    usercode: str
    sd_files: dict[str, int] = field(default_factory=dict)          # rel -> bytes
    overlays_open: list[str] = field(default_factory=list)
    overlays_aaa: list[str] = field(default_factory=list)
    left_out: dict[str, str] = field(default_factory=dict)           # name -> why
    sizes: dict[str, int] = field(default_factory=dict)              # input -> bytes
    sources: dict[str, str] = field(default_factory=dict)            # input -> path
    rebake: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _json(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from None
    if not isinstance(doc, dict):
        raise ReleaseError(f"{path} is not a JSON object")
    return doc


def _u32(v: Any) -> int | None:
    try:
        return int(str(v), 16)
    except (TypeError, ValueError):
        return None


def _same(a: Any, b: Any) -> bool:
    return _u32(a) is not None and _u32(a) == _u32(b)


def _copy(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)                 # content only: no modes, no times from the input
    return dst.stat().st_size


def _need_file(path: Path | None, what: str, hint: str = "") -> Path:
    if path is None or not Path(path).is_file():
        raise ReleaseError(f"{what} is missing: {path or '(not given)'}", hint=hint)
    return Path(path)


def _cfg_value(text: str, key: str, default: str) -> str:
    m = re.search(_KEY_RE.format(key=re.escape(key)), text, re.M)
    return m.group(1) if m else default


# --- the config-SD tree ------------------------------------------------------------------


def revisions(spec: str) -> tuple[str, ...]:
    spec = (spec or "C").strip().upper()
    if spec == "ALL":
        return REVS
    revs = tuple(r.strip().removeprefix("HBI0309") for r in spec.split(",") if r.strip())
    bad = [r for r in revs if r not in REVS]
    if bad or not revs:
        raise ReleaseError(f"board revision {spec!r} is not A, B, C or ALL")
    return revs


def stamp_sd_tree(templates: Path, bit: Path, dest: Path, *, revs: tuple[str, ...] = ("C",),
                  images_txt: bool = True) -> dict[str, int]:
    """The config-SD tree from the platform's templates, as ``fpga/mps3_sd/assemble_sd.sh``
    stamps it with no experiment variant (byte-identical templates, ``@BOARD@`` filled in).
    Returns ``{relative path: bytes}``."""
    templates = Path(templates)
    for name in TEMPLATES:
        _need_file(templates / name, f"the SD template {name}",
                   hint="point --sd-templates at the platform's fpga/mps3_sd/templates")
    board_tpl = (templates / "board.txt").read_bytes()     # bytes: keep CRLF/LF as they are
    note_tpl = (templates / "nanosoc.txt").read_bytes()
    appfile = _cfg_value(board_tpl.decode("utf-8", "replace"), "APPFILE",
                         DEFAULT_APPFILE).replace("\\", "/")
    app_dir, _, note_name = appfile.rpartition("/")
    f0 = _cfg_value(note_tpl.decode("utf-8", "replace"), "F0FILE", DEFAULT_F0FILE)
    if "/" in f0 or "\\" in f0 or not f0.lower().endswith(".bit"):
        raise ReleaseError(f"the SD template names F0FILE {f0!r}: expected a plain .bit name")
    out: dict[str, int] = {}
    out["config.txt"] = _copy(templates / "config.txt", dest / "config.txt")
    for rev in revs:
        mb = dest / "MB" / f"HBI0309{rev}"
        mb.mkdir(parents=True, exist_ok=True)
        (mb / "board.txt").write_bytes(board_tpl.replace(b"@BOARD@", f"HBI0309{rev}".encode()))
        out[f"MB/HBI0309{rev}/board.txt"] = (mb / "board.txt").stat().st_size
        app = mb / app_dir if app_dir else mb
        rel_app = f"MB/HBI0309{rev}/{app_dir + '/' if app_dir else ''}"
        out[rel_app + note_name] = _copy(templates / "nanosoc.txt", app / note_name)
        if images_txt and (templates / "images.txt").is_file():
            out[rel_app + "images.txt"] = _copy(templates / "images.txt", app / "images.txt")
        out[rel_app + f0] = _copy(bit, app / f0)
    return out


def copy_sd_tree(src: Path, dest: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in sorted(Path(src).rglob("*")):
        if p.is_symlink():
            raise ReleaseError(f"{p} is a symlink: a config-SD tree carries only regular files")
        if p.is_file():
            rel = p.relative_to(src).as_posix()
            out[rel] = _copy(p, dest / rel)
    if not out:
        raise ReleaseError(f"the config-SD tree {src} is empty")
    return out


# --- the stage0 re-bake ------------------------------------------------------------------


def apply_rebake(lb: dict[str, Any], bit: Path, record_path: Path) -> dict[str, Any]:
    """Check a stage0 re-bake record against the Linux bundle and the .bit; return what the
    bundle's copy of linux_bundle.json records. Refuses anything that is not the same
    static and implementation."""
    rec = _json(record_path)
    if rec.get("schema") != "mps3-stage0-bake" or str(rec.get("schema_version")) != "1":
        raise ReleaseError(f"{record_path} is not an mps3-stage0-bake v1 record")
    baked = rec.get("baked_bit") or {}
    sha = sha256_file(bit)
    problems = []
    if baked.get("sha256") != sha:
        problems.append(f"its baked_bit {baked.get('name')} ({str(baked.get('sha256'))[:12]}) "
                        f"is not {bit.name} ({sha[:12]})")
    if not _same(rec.get("static_id"), lb.get("static_id")):
        problems.append(f"static_id {rec.get('static_id')} != the bundle's {lb.get('static_id')}")
    if not _same(rec.get("static_usercode"), lb.get("static_usercode")):
        problems.append(f"static_usercode {rec.get('static_usercode')} != the bundle's "
                        f"{lb.get('static_usercode')}")
    if rec.get("mint_kind", "mint") != "mint":
        problems.append(f"mint_kind {rec.get('mint_kind')!r}: a prototype bake is never released")
    if rec.get("stage0_identity_checked") is not True:
        problems.append("stage0_identity_checked is not true (the stage0 inside may be for "
                        "another static)")
    baked_sid = (((rec.get("stage0_elf_check") or {}).get("baked_constants") or {})
                 .get("mps3_stage0_static_id"))
    if baked_sid is not None and not _same(baked_sid, lb.get("static_id")):
        problems.append(f"the stage0 inside is baked for {baked_sid}")
    if problems:
        raise ReleaseError(f"the stage0 re-bake {record_path.name} does not fit this bundle:\n    "
                           + "\n    ".join(problems),
                           hint="a re-bake may change stage0 only: same static, same UserID")
    mcc = (lb.setdefault("targets", {})).setdefault("mcc_sd", {})
    was = dict(mcc.get("flashable_bit") or {})
    elf = rec.get("stage0_elf") or {}
    mcc["flashable_bit"] = {"name": bit.name, "sha256": sha, "bytes": bit.stat().st_size}
    mcc["stage0"] = {"name": elf.get("name", ""), "sha256": elf.get("sha256", ""),
                     "identity_checked": True,
                     "baked_constants": (rec.get("stage0_elf_check") or {})
                     .get("baked_constants", {})}
    note = {"replaces": was, "record": record_path.name,
            "record_sha256": sha256_file(record_path),
            "note": str(rec.get("note") or "")}
    mcc["rebake"] = note
    return note


# --- overlays ----------------------------------------------------------------------------


def _is_aaa_rm(name: str) -> bool:
    n = name.lower()
    return n in AAA_RMS or n.startswith("nanosoc")


def stage_overlays(src: Path, dest: Path, *, include_aaa: bool, lb: dict[str, Any],
                   out: Assembled) -> None:
    """``<rm>/`` dirs with a manifest.json -> overlays/open or overlays/aaa (or left out)."""
    src = Path(src)
    if not src.is_dir():
        raise ReleaseError(f"no overlay directory at {src}")
    packed = {str(o.get("rm_name")): o for o in
              ((lb.get("targets") or {}).get("ethernet") or {}).get("overlays") or []
              if isinstance(o, dict)}
    seen = set()
    for d in sorted(p for p in src.iterdir()):
        if not d.is_dir():
            out.left_out[d.name] = "not an overlay directory"
            continue
        man = d / "manifest.json"
        if not man.is_file():
            out.left_out[d.name] = "no manifest.json"
            continue
        m = _json(man)
        name = str(m.get("rm_name") or d.name)
        seen.add(name)
        if packed:
            want = packed.get(name)
            if want is None:
                raise ReleaseError(f"overlay {name} is not in the set linux_bundle.json was "
                                   "packed with", hint="assemble from the same mint's overlays")
            got = {"partial_crc32": (m.get("partial") or {}).get("crc32"),
                   "clearing_crc32": (m.get("clearing") or {}).get("crc32")}
            diff = [k for k, v in got.items() if not _same(v, want.get(k))]
            if diff:
                raise ReleaseError(f"overlay {name} is not the one linux_bundle.json was packed "
                                   f"with ({', '.join(diff)} differ)",
                                   hint="assemble from the same mint's overlays")
        aaa = _is_aaa_rm(name) or str(m.get("ip_class", "")).lower() in ("arm-aaa", "arm_aaa",
                                                                        "aaa")
        if aaa and not include_aaa:
            out.left_out[name] = ("Arm IP (AAA_RMS): left out by default; --include-aaa puts it "
                                  "in the private AAA repo")
            continue
        cls = "aaa" if aaa else "open"
        for f in sorted(d.iterdir()):
            if f.is_symlink() or not f.is_file():
                raise ReleaseError(f"{f}: an overlay carries only regular files")
            _copy(f, dest / "overlays" / cls / d.name / f.name)
        (out.overlays_aaa if aaa else out.overlays_open).append(name)
    missing = sorted(set(packed) - seen)
    if missing:
        out.warnings.append(f"linux_bundle.json lists overlays not in {src}: "
                            f"{', '.join(missing)}")


# --- the kit -----------------------------------------------------------------------------


def stage_kit(kit_zip: Path, dest: Path, static_id: str, out: Assembled) -> None:
    kit_zip = _need_file(kit_zip, "the RM kit zip")
    try:
        with zipfile.ZipFile(kit_zip) as zf:
            names = [n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "kit.json"]
            if not names:
                raise ReleaseError(f"{kit_zip.name} holds no kit.json")
            meta = json.loads(zf.read(min(names, key=len)))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise ReleaseError(f"{kit_zip.name} is not a kit zip: {exc}") from None
    if not _same(meta.get("static_id"), static_id):
        raise ReleaseError(f"the kit is keyed to {meta.get('static_id')}, the static is "
                           f"{static_id}")
    if str(meta.get("access") or "github-token") == "public" and \
            "INTERNAL" in str(meta.get("distribution", "")).upper():
        raise ReleaseError("the kit says access public but distribution INTERNAL-ONLY",
                           hint="fix kit.json: an internal kit is never public")
    kd = dest / "kit"
    sid = f"0x{_u32(static_id):08X}"
    out.sizes["kit"] = _copy(kit_zip, kd / f"mps3-kit-{sid}.zip")
    (kd / "kit.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n",
                                 encoding="utf-8")
    out.sources["kit"] = str(kit_zip)
    if meta.get("contains_amd_ip"):
        out.warnings.append("the kit holds AMD IP (the locked static DCP): it is published "
                            f"private (access {meta.get('access') or 'github-token'}); its "
                            "licence call is david's (D2)")


# --- the whole bundle --------------------------------------------------------------------


def assemble(dest: Path, *, from_dir: Path, bit: Path | None = None,
             stage0_bake: Path | None = None, sd_templates: Path | None = None,
             sd_tree: Path | None = None, board_revs: str = "C", images_txt: bool = True,
             overlays: Path | None = None, include_aaa: bool = False,
             kit: Path | None = None, firmware_json: Path | None = None,
             notes: Path | None = None, version: str = "", test: bool = False) -> Assembled:
    """Lay ``from_dir`` (+ the other inputs) out as a bundle dir at ``dest`` (must not exist)."""
    from_dir = Path(from_dir)
    dest = Path(dest)
    if not from_dir.is_dir():
        raise ReleaseError(f"no mint directory at {from_dir}")
    if dest.exists():
        raise ReleaseError(f"{dest} exists: the bundle is assembled into a new directory")
    if (sd_templates is None) == (sd_tree is None):
        raise ReleaseError("give the config-SD as ONE of --sd-templates DIR (with the .bit) "
                           "or --sd DIR (a ready tree)")
    lb_path, mint_path = from_dir / "linux_bundle.json", from_dir / "mint.json"
    lb: dict[str, Any] = {}
    if lb_path.is_file():
        lb = _json(lb_path)
        if lb.get("schema") != "mps3-linux-bundle":
            raise ReleaseError(f"{lb_path} is not an mps3-linux-bundle")
        impl, sid, uc = "linux", str(lb.get("static_id", "")), str(lb.get("static_usercode", ""))
    elif mint_path.is_file():
        mint = _json(mint_path)
        impl, sid = "bare-metal", _hex(mint.get("static_id"))
        su = _val(mint.get("static_usercode")) or {}
        uc = _hex(_val(su.get("usercode")) if isinstance(su, dict) else su)
    else:
        raise ReleaseError(f"{from_dir} has neither linux_bundle.json nor mint.json",
                           hint="point --from at the mint's prod/ directory")
    out = Assembled(bundle=dest, impl=impl, static_id=sid, usercode=uc)
    if sd_tree is not None and (bit is not None or stage0_bake is not None):
        raise ReleaseError("--bit and --stage0-bake go with --sd-templates: a ready --sd tree "
                           "is taken as it is")

    # the config-SD .bit
    if sd_templates is not None:
        if bit is None and lb:
            want = ((lb.get("targets") or {}).get("mcc_sd") or {}).get("flashable_bit") or {}
            bit = from_dir / str(want.get("name") or "config_rm_greybox_stage0.bit")
        bit = _need_file(bit, "the config-SD .bit",
                         hint="pass --bit FILE (the stage0 base .bit the MCC loads)")
        out.sizes["sd .bit"] = bit.stat().st_size
        out.sources["sd .bit"] = str(bit)
    if lb and bit is not None:
        want = ((lb.get("targets") or {}).get("mcc_sd") or {}).get("flashable_bit") or {}
        if stage0_bake is not None:
            out.rebake = apply_rebake(lb, bit, Path(stage0_bake))
            out.sources["stage0 re-bake"] = str(stage0_bake)
        elif want.get("sha256") and want["sha256"] != sha256_file(bit):
            raise ReleaseError(
                f"{bit.name} is not the flashable .bit linux_bundle.json names "
                f"({want.get('name')}, {str(want.get('sha256'))[:12]})",
                hint="pass --stage0-bake RECORD (mps3-stage0-bake v1) for a stage0 re-bake "
                     "of the same static, or the .bit the bundle names")
        comps = ((lb.get("targets") or {}).get("ethernet") or {}).get("components") or {}
        mcc = (lb.get("targets") or {}).get("mcc_sd") or {}
        elf_sha = (mcc.get("stage0") or {}).get("sha256", "")
        if comps.get("stage0_sha256") and elf_sha and comps["stage0_sha256"] != elf_sha:
            out.warnings.append(
                f"the OS image records stage0 {str(comps['stage0_sha256'])[:12]}, the config SD "
                f"carries stage0 {elf_sha[:12]} (a re-bake: "
                f"{(mcc.get('rebake') or {}).get('note') or 'no note'})")

    dest.mkdir(parents=True)
    try:
        sd_dest = dest / "sd"
        if sd_tree is not None:
            out.sd_files = copy_sd_tree(Path(sd_tree), sd_dest)
            out.sources["sd tree"] = str(sd_tree)
        else:
            out.sd_files = stamp_sd_tree(Path(sd_templates), bit, sd_dest,  # type: ignore[arg-type]
                                         revs=revisions(board_revs), images_txt=images_txt)
            out.sources["sd templates"] = str(sd_templates)
        if lb:
            for name in ("linux_slot.img", "linux_legal_info.tar"):
                src = from_dir / name
                if src.is_file():
                    out.sizes[name] = _copy(src, dest / name)
                    out.sources[name] = str(src)
            (dest / "linux_bundle.json").write_text(json.dumps(lb, indent=2, sort_keys=True)
                                                    + "\n", encoding="utf-8")
        else:
            shutil.copyfile(mint_path, dest / "mint.json")
        if firmware_json is not None:
            shutil.copyfile(_need_file(firmware_json, "firmware.json"), dest / "firmware.json")
        if overlays is not None:
            stage_overlays(Path(overlays), dest, include_aaa=include_aaa, lb=lb, out=out)
            out.sources["overlays"] = str(overlays)
        else:
            out.warnings.append("no --overlays: the release carries no overlays")
        if kit is not None:
            stage_kit(Path(kit), dest, sid, out)
        text = (Path(notes).read_text(encoding="utf-8") if notes is not None
                else default_notes(out, lb, version, test=test))
        if "«" in text or "»" in text:
            raise ReleaseError(f"the release notes {notes} still hold «…» placeholders",
                               hint="fill them in first: the notes are signed into the release")
        if test:
            text = TEST_BANNER + "\n\n" + text
        (dest / "notes.md").write_text(text.strip() + "\n", encoding="utf-8")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return out


TEST_BANNER = ("TEST BUILD: signed with a throwaway TEST key (id 7E57C0DE...). No Harness "
               "Manager build trusts it; never install it on a real board.")


def default_notes(a: Assembled, lb: dict[str, Any], version: str, *, test: bool) -> str:
    comps = ((lb.get("targets") or {}).get("ethernet") or {}).get("components") or {}
    lines = [f"SoC Labs MPS3 harness {version or '(version)'} ({a.impl}), static {a.static_id}, "
             f"UserID {a.usercode}."]
    if a.impl == "linux":
        lines.append(f"Linux image: harness {comps.get('harness', '?')}, kernel "
                     f"{comps.get('kernel', '?')}, build {comps.get('sha', '?')}, "
                     f"{comps.get('image_kind', '?')} image.")
    if a.rebake:
        lines.append(f"Config SD: the stage0 re-bake {a.rebake.get('record')} "
                     f"({a.rebake.get('note') or 'stage0 only'}).")
    lines.append(f"Overlays: {', '.join(a.overlays_open) or 'none'}"
                 + (f"; Arm IP (private AAA repo): {', '.join(a.overlays_aaa)}"
                    if a.overlays_aaa else "") + ".")
    aaa_out = sorted(k for k, v in a.left_out.items() if v.startswith("Arm IP"))
    if aaa_out:
        lines.append(f"Not in this release (Arm IP, by licence): {', '.join(aaa_out)}.")
    lines.append("Per-board identity (label, IP, MAC) is set after install with "
                 "`harness-manager board identity`.")
    return "\n".join(lines)
