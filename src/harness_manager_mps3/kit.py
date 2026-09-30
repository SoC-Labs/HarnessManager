"""The MPS3 pack's DUT build kit adapter (KIT-STORE K3, KIT-GUIDE's pack hook, david K8).

Found by module convention (``harness_manager.services.kit.kit_adapter("mps3")`` imports
``harness_manager_mps3.kit`` and calls ``make_kit_adapter()``), so the lead-owned
``pack.py`` needs no edit. It supplies what only the MPS3 pack knows:

- **``build_profile(static_id, kit)``**: part, partition (``u_rp_dut`` / ``pblock_rp_dut``,
  47 ports / 148 bits for 0x72BB0A36), the clearing arena, the Vivado release. From the
  kit when there is one, else from the pin model's shell (Vivado then unknown).
- **``check_kit(kit, identity)``**: the kit against the board's LIVE static: ``shell_id``
  (identity: CRC-32 of the DCP = static_id = what the board reports), ``usercode``
  (identity; unchecked when the board does not report it: older firmware), the part, the
  partition size against the pin model.
- **``kit_from_dir(dir)``**: ``kit import fielded/0x72BB0A36`` before any FLOW change: a
  ``fielded/<sid>/`` or mint ``prod/`` directory holds ``static_routed_locked.dcp``,
  ``static_stamp.json``, ``mint.json`` and ``static_id.txt``; the kit keeps only those.
  The Vivado release, build and checkpoint version, the part and the RP instance come from
  the DCP's own ``dcp.xml``; the partition's stream facts (``rp.frames``) from an overlay
  pair in the same directory when there is one.
- **user rm_ids (K8)**: HM proposes a design id from ``0x8000-0xFFFF`` (stable per design
  name) and warns on a clash with the overlay catalogue or the platform's own designs.
- **the pair and the manifest**: ``check_pair`` (``bitcheck`` with the kit's facts) and
  ``pack_receipt`` (the overlay triple, ``docs/contracts/overlay-manifest.md``), imported
  through ``overlays.import_overlay``, which re-checks every length and CRC.
"""

from __future__ import annotations

import json
import re
import shutil
import zipfile
import zlib
from pathlib import Path
from typing import Any

from pyverify import rm_id as rmid

from harness_manager.core.errors import RefusedError, UnavailableError, UsageError
from harness_manager.core.model import BoardIdentity
from harness_manager.core.pack import BuildProfile, KitCheck
from harness_manager.services.kit.schema import (
    DEFAULT_LICENCE_NOTE,
    KIT_SCHEMA,
    KIT_SCHEMA_VERSION,
    BuildReceipt,
    KitFile,
    KitManifest,
    crc32_file,
    hex32,
    parse_u32,
    same_id,
    sha256_file,
)

from . import bitcheck
from .constants import CLEARING_ARENA_BYTES, KNOWN_DESIGNS

PACK = "mps3"
PART = "xcku115-flvb1760-1-c"
LOCKED_DCP = "static_routed_locked.dcp"
#: The loose mint files a kit keeps (KIT-STORE §7.2), and their roles.
LOOSE_FILES = ((LOCKED_DCP, "locked_static"), ("static_stamp.json", "stamp"),
               ("mint.json", "mint_record"), ("static_id.txt", "static_id"))
USER_DESIGN_IDS = (0x8000, 0xFFFF)
USER_VERSION = (1, 0)
CAPABILITY = "build_kit"


def make_kit_adapter() -> Mps3KitAdapter:
    return Mps3KitAdapter()


def _pins() -> Any:
    from harness_manager.services import xdc

    return xdc.load_pack_pins(PACK)


def _shell(static_id: str) -> dict[str, Any] | None:
    model = _pins().model
    for sid in model.shell_ids():
        if same_id(sid, static_id):
            return model.shell(sid)
    return None


class Mps3KitAdapter:
    pack = PACK

    # -- the profile ----------------------------------------------------------------------

    def build_profile(self, static_id: str, kit: KitManifest | None = None) -> BuildProfile | None:
        if kit is not None:
            return BuildProfile(
                pack=PACK, static_id=hex32(kit.static_u32), part=kit.part,
                rp_inst=kit.rp.inst, rp_pblock=kit.rp.pblock, boundary_ports=kit.rp.ports,
                boundary_bits=kit.rp.bits, clr_max=kit.rp.clr_max or CLEARING_ARENA_BYTES,
                vivado=kit.vivado.release, vivado_build=kit.vivado.build,
                static_usercode=kit.static_usercode, harness_impl=kit.harness_impl,
                kit_id=kit.kit_id, user_design_ids=USER_DESIGN_IDS, source="kit")
        shell = _shell(static_id)
        if shell is None:
            return None
        pb = {k: v.get("value") for k, v in shell.get("pblock", {}).items()}
        totals = shell["rp_boundary"]["totals"]
        return BuildProfile(
            pack=PACK, static_id=hex32(parse_u32(static_id)), part=_pins().model.board["part"],
            rp_inst=str(pb.get("rp_instance") or "u_rp_dut"),
            rp_pblock=str(pb.get("name") or "pblock_rp_dut"),
            boundary_ports=int(totals["ports"]), boundary_bits=int(totals["bits"]),
            clr_max=CLEARING_ARENA_BYTES, static_usercode=str(shell.get("usercode") or ""),
            user_design_ids=USER_DESIGN_IDS, source="pack")

    # -- the kit against the board --------------------------------------------------------

    def check_kit(self, kit: KitManifest, identity: BoardIdentity | None) -> list[KitCheck]:
        out: list[KitCheck] = []
        sid = hex32(kit.static_u32)
        if identity is None:
            out.append(KitCheck("shell_id", "unchecked", "no board: the kit was not compared "
                                                         "with a running static", identity=True))
        elif not identity.shell_id:
            out.append(KitCheck("shell_id", "unchecked", "the board did not report its static",
                                identity=True))
        else:
            ok = same_id(identity.shell_id, sid)
            out.append(KitCheck("shell_id", "ok" if ok else "mismatch",
                                f"the board runs {identity.shell_id}; the kit is for {sid}"
                                + ("" if ok else ": a partial built from it would not fit this "
                                                 "board (fetch the kit of the running static)"),
                                identity=True))
        if identity is not None and kit.static_usercode:
            if not identity.usercode:
                out.append(KitCheck("usercode", "unchecked", "the board does not report its "
                                    "usercode (older firmware); not a pass", identity=True))
            else:
                ok = same_id(identity.usercode, kit.static_usercode)
                out.append(KitCheck("usercode", "ok" if ok else "mismatch",
                                    f"the board's implementation run {identity.usercode}; the "
                                    f"kit's {kit.static_usercode}" + (
                                        "" if ok else ": the same static_id from ANOTHER "
                                        "implementation run, whose partials destroy the "
                                        "configuration"), identity=True))
        out.append(KitCheck("part", "ok" if kit.part == PART else "mismatch",
                            f"{kit.part}" + ("" if kit.part == PART else f", the MPS3 is {PART}")))
        shell = _shell(sid)
        if shell is None:
            out.append(KitCheck("boundary", "unchecked", f"the pin model does not describe "
                                                         f"{sid}; the kit's partition is taken "
                                                         "as it is"))
        else:
            t = shell["rp_boundary"]["totals"]
            ok = (kit.rp.ports, kit.rp.bits) == (int(t["ports"]), int(t["bits"]))
            out.append(KitCheck("boundary", "ok" if ok else "mismatch",
                                f"kit {kit.rp.ports} ports / {kit.rp.bits} bits, pin model "
                                f"{t['ports']} / {t['bits']}"))
        return out

    # -- loose mint files -> a kit --------------------------------------------------------

    def kit_from_dir(self, directory: Path) -> tuple[dict[str, Any], dict[str, Path]] | None:
        d = Path(directory)
        dcp = d / LOCKED_DCP
        if not dcp.is_file():
            return None
        crc = crc32_file(dcp)
        sid = hex32(crc)
        sid_txt = d / "static_id.txt"
        if sid_txt.is_file():
            said = sid_txt.read_text(encoding="utf-8", errors="replace").strip()
            if not same_id(said, sid):
                raise RefusedError(f"{d}: static_id.txt says {said}, but the CRC-32 of "
                                   f"{LOCKED_DCP} is {sid}",
                                   hint="the DCP is not the one that static_id names (a stale "
                                        "or overwritten copy): fetch it again")
        info = read_dcp_xml(dcp)
        if info.get("part") and info["part"] != PART:
            raise RefusedError(f"{dcp} is for {info['part']}, not the MPS3's {PART}")
        shell = _shell(sid)
        if shell is None:
            raise UnavailableError(CAPABILITY, f"the MPS3 pin model describes "
                                               f"{', '.join(_pins().model.shell_ids())} only, so "
                                               f"the partition of {sid} is not known here; its "
                                               "kit must come packed by the mint (FLOW F1/F3)")
        pb = {k: v.get("value") for k, v in shell.get("pblock", {}).items()}
        totals = shell["rp_boundary"]["totals"]
        stamp = _json(d / "static_stamp.json")
        mint = _json(d / "mint.json")
        release = info.get("release") or _mint_vivado(mint)
        if not release:
            raise RefusedError(f"{dcp}: the Vivado release is in neither dcp.xml nor mint.json")
        rp_inst = info.get("rp_inst") or str(pb.get("rp_instance") or "u_rp_dut")
        files: dict[str, Path] = {}
        entries = []
        for name, role in LOOSE_FILES:
            p = d / name
            if p.is_file():
                rel = f"static/{name}"
                files[rel] = p
                entries.append(KitFile(rel, role, p.stat().st_size, sha256_file(p),
                                       sid if role == "locked_static" else ""))
        repo = ((mint.get("sources") or {}).get("repo") or {}).get("value") or {}
        frames = _frames_from_dir(d)
        doc = {
            "schema": KIT_SCHEMA, "schema_version": KIT_SCHEMA_VERSION,
            "board_type": PACK, "part": info.get("part") or PART, "static_id": sid,
            "static_usercode": str(stamp.get("usercode") or shell.get("usercode") or ""),
            "harness_impl": _impl(mint),
            "vivado": {"release": release, "build": int(info.get("build") or 0),
                       "checkpoint_version": int(info.get("checkpoint_version") or 0)},
            "rp": {"inst": rp_inst, "pblock": str(pb.get("name") or "pblock_rp_dut"),
                   "ports": int(totals["ports"]), "bits": int(totals["bits"]),
                   "clr_max": CLEARING_ARENA_BYTES, **({"frames": frames} if frames else {})},
            "pr_verify_ref": f"static/{LOCKED_DCP}",
            "access": "public", "ip_class": "open", "licence_note": DEFAULT_LICENCE_NOTE,
            "files": [e.to_json() for e in entries],
            "source": {"repo_sha": repo.get("sha", ""), "dirty": bool(repo.get("dirty")),
                       "from": d.name},
            "generated_by": "harness_manager_mps3.kit (kit import of loose mint files)",
        }
        return doc, files

    # -- rm_id (K8) -----------------------------------------------------------------------

    def taken_designs(self, store: Any = None) -> dict[int, str]:
        """design id -> name: the platform's own designs and every overlay HM can see."""
        from .overlays import OverlayCatalogue

        taken = dict(KNOWN_DESIGNS)
        try:
            for e in OverlayCatalogue(store=store).entries():
                taken.setdefault(rmid.design_id(e.ref.rm_id), e.ref.name)
        except Exception:  # noqa: BLE001 - a broken catalogue must not hide the proposal
            pass
        return taken

    def propose_rm_id(self, name: str, taken: dict[int, str]) -> int:
        """A user rm_id: v1.0, design id in 0x8000-0xFFFF, stable per name (the same name
        proposes the same id on every machine), stepping past ids another design holds."""
        lo, hi = USER_DESIGN_IDS
        span = hi - lo + 1
        start = zlib.crc32(name.encode("utf-8")) % span
        for i in range(span):
            design = lo + (start + i) % span
            owner = taken.get(design)
            if owner is None or owner == name:
                return rmid.make_rm_id(design, *USER_VERSION)
        raise RefusedError("every user design id 0x8000-0xFFFF is taken")

    def rm_id_checks(self, rm_id: Any, name: str, taken: dict[int, str]) -> list[KitCheck]:
        try:
            value = rmid.parse_rm_id(rm_id)
        except (TypeError, ValueError):
            return [KitCheck("rm_id", "mismatch", f"{rm_id!r} is not a 32-bit id")]
        if value == 0:
            return [KitCheck("rm_id", "mismatch", "rm_id 0x00000000 is the greybox's (the "
                                                  "decoupler's clamp value); give your RM its own")]
        design = rmid.design_id(value)
        lo, hi = USER_DESIGN_IDS
        out = []
        if not lo <= design <= hi:
            out.append(KitCheck("rm_id_range", "warning",
                                f"design id 0x{design:04X} is in the platform's range; user "
                                f"designs take 0x{lo:04X}-0x{hi:04X} (HM proposes one)"))
        owner = taken.get(design)
        if owner is not None and owner != name:
            out.append(KitCheck("rm_id_clash", "warning",
                                f"design id 0x{design:04X} is already {owner!r}'s: the Program "
                                f"page and the CLCD would name {name!r} {owner!r}; pick another "
                                "(the shell still verifies the full rm_id after a swap)"))
        else:
            out.append(KitCheck("rm_id_clash", "ok", f"design id 0x{design:04X} is "
                                + (f"{name!r}'s own" if owner else "unused")))
        return out

    # -- the pair and the manifest --------------------------------------------------------

    def check_pair(self, partial: Path, clearing: Path | None, *, kit: KitManifest | None,
                   bin_path: Path | None = None, clr_max: int = 0
                   ) -> tuple[list[KitCheck], dict[str, Any]]:
        ref = kit.rp.frames if kit is not None and kit.rp.frames else None
        v, facts = bitcheck.check(partial, clearing=clearing, bin_path=bin_path, ref=ref,
                                  part=kit.part if kit else PART,
                                  clearing_max=clr_max or (kit.rp.clr_max if kit else 0)
                                  or CLEARING_ARENA_BYTES)
        return list(v.items), facts

    def pack_receipt(self, receipt: BuildReceipt, out_root: Path) -> Path:
        """``out_root/<rm>/{manifest.json, <rm>.bin, <rm>_clear.bin[, <rm>.ltx], receipt}``
        from a receipt that passed ``build.receipt_checks`` (the caller checks)."""
        from harness_manager.services.kit.build import receipt_files

        name = receipt.rm_name
        if not name or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
            raise UsageError(f"the receipt's rm_name {name!r} is not a plain name")
        files = receipt_files(receipt)
        d = Path(out_root) / name
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(files["partial"], d / f"{name}.bin")
        shutil.copyfile(files["clearing"], d / f"{name}_clear.bin")
        r = receipt
        manifest: dict[str, Any] = {
            "schema": 1,
            "static_id": r.get("static_id"),
            "rm_id": hex32(parse_u32(r.get("rm_id"))),
            "rm_name": name,
            "clearing": {"file": f"{name}_clear.bin", "len": int(r.get("clearing_len")),
                         "crc32": r.get("clearing_crc32").lower()},
            "partial": {"file": f"{name}.bin", "len": int(r.get("partial_len")),
                        "crc32": r.get("partial_crc32").lower()},
            "built": r.get("built")[:10],
            "vivado": r.get("vivado"),
            "build_receipt": r.path.name,
        }
        if r.get("static_usercode"):
            manifest["static_usercode"] = r.get("static_usercode")
        if "ltx" in files:
            shutil.copyfile(files["ltx"], d / f"{name}.ltx")
            manifest["ltx"] = f"{name}.ltx"
            manifest["ltx_crc32"] = r.get("ltx_crc32").lower()
        shutil.copyfile(r.path, d / r.path.name)
        (d / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        from pyverify.overlay import Overlay, OverlayValidationError

        try:
            Overlay.load(d).validate(expected_static_id=parse_u32(r.get("static_id")))
        except (OverlayValidationError, ValueError) as exc:
            raise RefusedError(f"the overlay written to {d} fails its own check: {exc}") from exc
        return d

    def check_overlay(self, overlay_dir: Path, *, kit: KitManifest | None = None
                      ) -> tuple[list[KitCheck], dict[str, Any]]:
        """UI2 G5: a packed overlay folder (``manifest.json`` + the pair it names, as ``kit
        pack`` writes it) checked before an import: ``manifest`` (it parses and its pair is
        there), ``crc`` (lengths and CRC-32 as the manifest says; an ``.ltx`` it names too),
        and the pair's stream checks (``check_pair``, against the kit's frame box when
        ``kit`` is given). Returns (checks, facts: ``{name, rm_id, static_id,
        static_usercode, partial_len, clearing_len, pair}``). Reads files only."""
        from pyverify.overlay import OverlayManifestError, OverlayValidationError

        from .overlays import _optional_problems, load_overlay_dir

        d = Path(overlay_dir)
        try:
            overlay, raw = load_overlay_dir(d)
        except (OverlayManifestError, ValueError, KeyError, TypeError) as exc:
            return [KitCheck("manifest", "mismatch",
                             f"{d}: not a kit-built overlay ({exc}): it needs manifest.json and "
                             "the partial and clearing it names")], {}
        m = overlay.manifest
        facts: dict[str, Any] = {
            "name": m.rm_name, "rm_id": rmid.format_rm_id(m.rm_id),
            "static_id": rmid.format_rm_id(m.static_id),
            "static_usercode": (rmid.format_rm_id(m.static_usercode)
                                if m.static_usercode is not None else ""),
            "partial_len": m.partial.len, "clearing_len": m.clearing.len}
        pair = {"partial": overlay.partial_path(), "clearing": overlay.clearing_path()}
        missing = [f"{role} {p.name}" for role, p in pair.items() if not p.is_file()]
        checks = [KitCheck("manifest", "mismatch" if missing else "ok",
                           f"manifest.json names {', '.join(missing)}, which is not there"
                           if missing else f"manifest.json names {m.rm_name}: "
                           f"{pair['partial'].name} and {pair['clearing'].name}")]
        if missing:
            return checks, facts
        try:
            overlay.validate()
            bad = _optional_problems(overlay, raw)
        except (OverlayValidationError, ValueError) as exc:
            bad = [str(exc)]
        checks.append(KitCheck("crc", "mismatch" if bad else "ok",
                               "; ".join(bad) if bad else
                               f"partial {m.partial.len} B, clearing {m.clearing.len} B: lengths "
                               "and CRC-32 match the manifest"))
        if bad:
            return checks, facts
        pair_checks, facts["pair"] = self.check_pair(pair["partial"], pair["clearing"], kit=kit)
        checks += pair_checks
        return checks, facts

    def import_overlay(self, store: Any, overlay_dir: Path) -> dict[str, Any]:
        """Into the content store (``overlays.import_overlay``: refuses a bad CRC), so it
        shows in Program. Returns ``{sha256, name, rm_id, static_id, static_usercode,
        shadowed_by, shadow_same_bits}``: ``shadowed_by`` names the catalogue entry Program
        lists INSTEAD when one has the same (name, rm_id, static_id) (``_shadow``)."""
        from .overlays import import_overlay, load_overlay_dir

        sha = import_overlay(store, overlay_dir)
        overlay, _raw = load_overlay_dir(overlay_dir)
        m = overlay.manifest
        shadowed_by, same = _shadow(store, sha, overlay)
        return {"sha256": sha, "name": m.rm_name, "rm_id": rmid.format_rm_id(m.rm_id),
                "static_id": rmid.format_rm_id(m.static_id),
                "static_usercode": (rmid.format_rm_id(m.static_usercode)
                                    if m.static_usercode is not None else ""),
                "shadowed_by": shadowed_by, "shadow_same_bits": same}


# --- helpers -------------------------------------------------------------------------------------


def _shadow(store: Any, sha: str, overlay: Any) -> tuple[str, bool]:
    """KIT-NANOSOC: the catalogue keeps the FIRST overlay of a (name, rm_id, static_id):
    overlay dirs (``--overlay-dir``, ``mps3.overlay_dirs``) before the store, an earlier
    store record before a later one, and only logs the rest. An import it shadows is not in
    Program, and ``program TARGET NAME`` loads the other one (the fielded ``nanosoc`` hides
    a rebuilt ``nanosoc``). Returns (the source Program lists, whether its partial and
    clearing are byte-identical to the import's); ("", False) when the import is listed."""
    from .overlays import STORE_SOURCE_PREFIX, OverlayCatalogue

    m = overlay.manifest
    try:
        entries = OverlayCatalogue(store=store).entries()
    except Exception:  # noqa: BLE001 - a broken catalogue must not fail the import
        return "", False
    for e in entries:
        em = e.overlay.manifest
        if (em.rm_name, em.rm_id, em.static_id) != (m.rm_name, m.rm_id, m.static_id):
            continue
        if e.ref.source == STORE_SOURCE_PREFIX + sha:
            return "", False
        try:
            same = (sha256_file(Path(e.overlay.partial_path())) == sha256_file(
                Path(overlay.partial_path())) and sha256_file(Path(e.overlay.clearing_path()))
                == sha256_file(Path(overlay.clearing_path())))
        except OSError:
            same = False
        return e.ref.source, same
    return "", False

_XML = {
    "checkpoint_version": re.compile(r'<Checkpoint\s+Version="(\d+)"'),
    "build": re.compile(r'<BUILD_NUMBER\s+Name="(\d+)"'),
    "release": re.compile(r'<PRODUCT\s+Name="Vivado v(\d{4}\.\d+(?:\.\d+)?)'),
    "part": re.compile(r'<Part\s+Name="([^"]+)"'),
    "rp_inst": re.compile(r'<HDBlackboxInfo\s+Name="(\S+)\s+HD\.RECONFIGURABLE'),
}


def read_dcp_xml(dcp: Path) -> dict[str, str]:
    """The Vivado release, build, checkpoint version, part and RP instance a checkpoint
    names in its own ``dcp.xml`` (a DCP is a zip). ``{}`` when it has none."""
    try:
        with zipfile.ZipFile(dcp) as zf:
            text = zf.read("dcp.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return {}
    out = {}
    for key, rx in _XML.items():
        m = rx.search(text)
        if m:
            out[key] = m.group(1)
    return out


def _json(p: Path) -> dict[str, Any]:
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _mint_vivado(mint: dict[str, Any]) -> str:
    tools = (mint.get("tools") or {}).get("vivado") or {}
    return str(tools.get("value") or "") if isinstance(tools, dict) else ""


def _impl(mint: dict[str, Any]) -> str:
    impl = mint.get("harness_impl")
    if isinstance(impl, dict):
        impl = impl.get("value")
    return impl if impl in ("bare-metal", "linux") else ""


def _frames_from_dir(d: Path) -> dict[str, Any] | None:
    for partial in sorted(d.glob("*_partial.bin")):
        clearing = partial.with_name(partial.stem + "_clear.bin")
        if clearing.is_file():
            try:
                return bitcheck.frames_of_pair(partial, clearing)
            except (OSError, ValueError):
                continue
    return None

