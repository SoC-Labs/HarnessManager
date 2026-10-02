"""The harness front-end (HARNESS-DIST H13): a mint's bundle dir in, catalogue entries out.

There is no platform bundle producer yet (R1, ``make -C fpga/dfx release-bundle``). Until
there is, this is the input contract the tool reads, built from what the platform already
writes (``fielded/<sid>/mint.json``; FLOW_CONTRACT v1.6 §0.1 ``linux_bundle.json``)::

    BUNDLE/
      mint.json            bare metal: schema mps3-mint-record (fielded/<sid>/mint.json)
      linux_bundle.json    OR Linux: schema mps3-linux-bundle v1 (FLOW_CONTRACT v1.6 §0.1)
      firmware.json        the fielded bake until mint.json records it (platform R2):
                           {version, sha, dirty, proto, features, elf_sha256}
      firmware/*.elf       optional; checked against firmware.json elf_sha256
      sd/                  the config-SD tree: config.txt + MB/HBI0309C/... with the static .bit
      overlays/open/<rm>/  overlay triples (manifest.json + partial + clearing), ip_class open
      overlays/aaa/<rm>/   the Arm Academic Access RMs: published to a separate private repo
      linux_slot.img       Linux: targets.ethernet.slot_image
      linux_legal_info.tar Linux: targets.ethernet.legal_info (GPL; published beside, never
                           installed)
      kit/                 optional: mps3-kit-<sid>.zip + kit.json (KIT-STORE §8)
      notes.md             optional: the signed release notes

Doors are named as FLOW names them: ``mcc_sd`` (the config SD + MCC REBOOT) and
``ethernet`` (the slot image and the overlays). A component still carries the app's
schema ``target`` (``mcc-sd``, ``user-usd``, ``host-store``), plus ``door`` for FLOW.

**It refuses** (and writes nothing):

- a dirty image: any mint source ``dirty``, a dirty firmware, a Linux ``image_kind`` that is
  not ``release``, a dirty kit. ``--allow-dirty REASON`` records a waiver in the signed entry
  for ``beta``/``dev`` only; ``promote`` never takes such a release to ``stable``;
- an unstamped ``.bit``, or a ``.bit`` whose header USERID is not the mint's static_usercode;
- Arm-IP (AAA) content in an open component: an overlay under ``overlays/open`` whose
  manifest says ``arm-aaa`` or that is a known AAA RM (nanosoc*, eth_ss), whatever it
  declares; any open file carrying a path into the AAA IP library. (An overlay that
  declares no ``ip_class`` at all, as every manifest does until the platform's G5, is a
  warning when its name is not a known AAA RM);
- any ``.ebf`` anywhere in the bundle (it would reflash the MCC);
- a Linux bundle that is not fieldable (``fieldable: false`` or ``mint_kind`` prototype);
- parts keyed to another static (overlays, the provisioned slot image, the flashable bit).

The overlay manifests are read with the board pack's own reader (pyverify through
``bundle.PackOverlayHandler``): the publisher runs the consumer's code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.services.update.bitheader import BitHeaderError, read_bit_header
from harness_manager.services.update.bundle import PackOverlayHandler
from harness_manager.services.update.version import is_version

from .channel_doc import schema_has_host_kit
from .common import (
    EXIT_MISMATCH,
    Layout,
    ReleaseError,
    deterministic_zip,
    sha256_bytes,
    sha256_file,
    tree_files,
    write_once,
)

DOOR_MCC_SD = "mcc_sd"          # FLOW_CONTRACT §0: the config SD + MCC REBOOT
DOOR_ETHERNET = "ethernet"      # FLOW_CONTRACT §0: slot image + overlays over the network
#: HM's schema targets -> FLOW's door names (the Linux lead's naming, 2026-09-24).
DOOR_OF_TARGET = {"mcc-sd": DOOR_MCC_SD, "user-usd": DOOR_ETHERNET, "host-store": DOOR_ETHERNET,
                  "host-kit": "host"}
AAA_RMS = frozenset({"nanosoc", "nanosoc_multicore", "multicore", "nanosoc_upy", "upy",
                     "eth_ss", "nanosoc_ila"})
AAA_MARKERS = (b"/research/AAA/", b"ip_library/arm", b"phys_ip_library")
IP_AAA_SPELLINGS = {"arm-aaa", "arm_aaa", "aaa"}


@dataclass
class Finding:
    code: str               # EBF, DIRTY, UNSTAMPED, USERID, AAA_OPEN, NOT_FIELDABLE, STATIC, …
    ok: bool
    detail: str


def sd_component_rev(revs: list[str]) -> str:
    """The SD part's name suffix: ``HBI0309C`` for one revision, ``HBI0309BC`` for the
    platform's B and C (FIX-PACK-9), ``multi`` for any other mix."""
    if len(revs) == 1:
        return revs[0]
    if revs and all(r.upper().startswith("HBI0309") and len(r) == 8 for r in revs):
        return "HBI0309" + "".join(r[-1].upper() for r in revs)
    return "multi"


def _board_txt_core(text: bytes, rev: str) -> list[str]:
    """board.txt as the MCC reads it, for comparing revisions: ``;`` comments and trailing
    blanks dropped, this revision's own token (``HBI0309B``) made neutral, empty lines gone.
    assemble_sd.sh stamps ``@BOARD@`` with sed .../g, so a COMMENT naming the token differs
    between revisions too (the Linux lead, 2 Oct): it never counts."""
    out = []
    for line in text.decode("latin-1").replace("\r\n", "\n").split("\n"):
        line = line.split(";", 1)[0].rstrip()
        if line:
            out.append(re.sub(re.escape(rev), "HBI0309?", line, flags=re.I))
    return out


def rev_trees_finding(sd: dict[str, Path], revs: list[str]) -> Finding:
    """FIX-PACK-9: the revision folders of a multi-revision config SD are the same tree: the
    same files, byte for byte, except each board.txt, which may differ only in its revision
    token and its comments (``_board_txt_core``). The first revision in ``revs`` (sorted, so
    ``HBI0309B``) is compared with each other one."""
    def tree(rev: str) -> dict[str, tuple[str, Path]]:
        """``{name inside the folder, FAT case-blind: (its spelling, file)}``."""
        pre = f"mb/{rev.lower()}/"
        return {r[len(pre):].lower(): (r[len(pre):], p) for r, p in sd.items()
                if r.lower().startswith(pre)}

    ref_rev, problems = revs[0], []
    ref = tree(ref_rev)
    for rev in revs[1:]:
        other = tree(rev)
        if set(other) != set(ref):
            diff = sorted(set(other) ^ set(ref))
            problems.append(f"MB/{rev} and MB/{ref_rev} hold different files "
                            f"({', '.join(diff[:3])}{' …' if len(diff) > 3 else ''})")
            continue
        for key in sorted(ref):
            (name, pa), (oname, pb) = ref[key], other[key]
            a, b = pa.read_bytes(), pb.read_bytes()
            if key == "board.txt":
                if _board_txt_core(a, ref_rev) != _board_txt_core(b, rev):
                    problems.append(f"MB/{rev}/{oname} differs from MB/{ref_rev}/{name} "
                                    "beyond its BOARD: revision and comments")
            elif a != b:
                problems.append(f"MB/{rev}/{oname} differs from MB/{ref_rev}/{name}")
    return Finding("REVS", not problems, "; ".join(problems) or
                   f"{', '.join(f'MB/{r}' for r in revs)}: the same tree apart from each "
                   "board.txt's BOARD: revision (and comments)")


@dataclass
class HarnessBuild:
    version: str
    tag: str
    impl: str
    entry: dict[str, Any]
    board: dict[str, Any]
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assets: list[Path] = field(default_factory=list)
    aaa_assets: list[Path] = field(default_factory=list)
    pending: list[tuple[Path, bytes]] = field(default_factory=list, repr=False)

    def write(self) -> None:
        """Put the assets in the tree (write-once). Called only after the channel accepts
        the entry, so a refused release leaves nothing behind."""
        for path, data in self.pending:
            write_once(path, data)


def _val(node: Any) -> Any:
    """mint.json wraps facts as {"reason": …, "value": …}; unwrap (a bare value passes)."""
    if isinstance(node, dict) and "value" in node and set(node) <= {"value", "reason"}:
        return node["value"]
    return node


def _hex(v: Any) -> str:
    try:
        return f"0x{int(str(v), 16):08X}"
    except (TypeError, ValueError):
        return ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from None


def _is_aaa(ip_class: str) -> bool:
    return ip_class.strip().lower() in IP_AAA_SPELLINGS


@dataclass
class _Ident:
    impl: str
    static_id: str
    usercode: str
    ver32: str
    fw: dict[str, Any]
    vivado: str
    dirty_sources: list[str]
    extra: dict[str, Any] = field(default_factory=dict)


def _identity_bare_metal(bundle: Path, mint: dict[str, Any], fw: dict[str, Any]) -> _Ident:
    if mint.get("schema") != "mps3-mint-record":
        raise ReleaseError(f"{bundle}/mint.json is not an mps3-mint-record")
    su = _val(mint.get("static_usercode")) or {}
    usercode = _hex(_val(su.get("usercode")) if isinstance(su, dict) else su)
    ver32 = _hex(_val(su.get("usr_access"))) if isinstance(su, dict) else ""
    dirty = [f"mint source {name} ({(_val(v) or {}).get('sha', '?')[:12]})"
             for name, v in (mint.get("sources") or {}).items()
             if isinstance(_val(v), dict) and _val(v).get("dirty")]
    if mint.get("record_kind", "mint") != "mint":
        raise ReleaseError(f"mint.json is a {mint.get('record_kind')!r} record, not a mint",
                           hint="only a mint is released")
    mint_fw = _val(mint.get("firmware")) or {}
    fw = {**{k: _val(v) for k, v in mint_fw.items() if k in
             ("version", "sha", "dirty", "proto", "features", "elf_sha256")}, **fw}
    return _Ident("bare-metal", _hex(mint.get("static_id")), usercode, ver32, fw,
                  str(_val((mint.get("tools") or {}).get("vivado")) or ""), dirty,
                  {"mint_repo_sha": ((_val((mint.get("sources") or {}).get("repo")) or {})
                                     .get("sha", ""))})


def _identity_linux(bundle: Path, lb: dict[str, Any], fw: dict[str, Any],
                    findings: list[Finding]) -> _Ident:
    # linux_bundle.py writes "schema_version": "1" (a string); v1 either way.
    if lb.get("schema") != "mps3-linux-bundle" or str(lb.get("schema_version")) != "1":
        raise ReleaseError(f"{bundle}/linux_bundle.json is not an mps3-linux-bundle v1")
    fieldable = lb.get("fieldable") is True and lb.get("mint_kind", "mint") == "mint"
    findings.append(Finding("NOT_FIELDABLE", fieldable,
                            "fieldable mint" if fieldable else
                            f"mint_kind {lb.get('mint_kind')!r}, fieldable "
                            f"{lb.get('fieldable')!r}: a prototype (P-mint) is never released "
                            "(FLOW_CONTRACT §5)"))
    eth = (lb.get("targets") or {}).get("ethernet") or {}
    comps = eth.get("components") or {}
    dirty = []
    kind = comps.get("image_kind", "")
    if kind != "release":
        dirty.append(f"Linux image_kind {kind or '(none)'!r} (a lab image may carry a baked host key)")
    if comps.get("dirty") is True:
        dirty.append("Linux image built from a dirty tree")
    # The harness semver: the bundle's own ``harness`` (Linux request L1), else firmware.json,
    # else the image's version record (IMAGE_CONTRACT §6 ``harness``).
    fw = {"version": str(comps.get("harness", "") or ""),
          "sha": str(comps.get("harnessd_sha256", ""))[:8], **fw}
    if lb.get("harness"):                       # L1: the bundle's own semver wins
        fw["version"] = str(lb["harness"])
    return _Ident("linux", _hex(lb.get("static_id")), _hex(lb.get("static_usercode")),
                  _hex(lb.get("static_ver32")), fw, "2026.1", dirty)


def _slot_frames(img: Path, si: dict[str, Any], warnings: list[str]) -> list[Finding]:
    """The slot image against ``linux_bundle.json``'s ``slot_image``: stage0 would take it
    (an S0LB v2 table, every region CRC good), and its frames are the declared ones. The
    app checks the same at install (``bundle.check_os_component``); refusing here keeps a
    release that would fail there from being signed."""
    from harness_manager.services.update import s0lb

    data = img.read_bytes()
    try:
        table = s0lb.parse(data)
    except s0lb.S0lbError as exc:
        return [Finding("STATIC", False, f"linux_slot.img is not a boot image stage0 would "
                                         f"take: {exc}")]
    out = [Finding("STATIC", table.ok, "linux_slot.img: " + ("; ".join(table.problems)
                                                             if table.problems else
                                                             "a good S0LB v2 boot table"))]
    declared = si.get("s0lb")
    if isinstance(declared, dict) and declared:
        diff = s0lb.compare(table, declared)
        out.append(Finding("STATIC", not diff, "linux_slot.img frames "
                           + ("; ".join(diff) if diff else "match linux_bundle.json")))
    else:
        warnings.append("linux_bundle.json declares no slot_image.s0lb: the app's frame check "
                        "at install will be UNCHECKED for this release")
    if si.get("bytes") is not None and si.get("bytes") != len(data):
        out.append(Finding("STATIC", False, f"linux_slot.img is {len(data)} B, linux_bundle.json "
                                            f"says {si.get('bytes')}"))
    return out


def _scan_aaa(label: str, files: dict[str, Path]) -> list[Finding]:
    hits = []
    for rel, path in files.items():
        data = path.read_bytes()
        if any(m in data for m in AAA_MARKERS):
            hits.append(rel)
    return [Finding("AAA_OPEN", not hits,
                    f"{label}: {', '.join(hits[:3])} carry a path into the Arm IP library"
                    if hits else f"{label}: no Arm IP library paths")]


#: A release's ``compat.min_app`` unless ``--min-app`` says otherwise: 1.0.0, the first Harness
#: Manager with FIX-PACK-9's per-revision MBBIOS rule, which a B+C config SD needs (an older
#: one keeps only MB/HBI0309C's line and refuses or mis-keeps MB/HBI0309B's).
MIN_APP = "1.0.0"


def ingest(bundle: Path, version: str, *, catalog: str = "mps3-harness", layout: Layout,
           access: str = "github-token", allow_dirty: str = "", channel: str = "beta",
           min_app: str = MIN_APP, mcc_fw_tested: tuple[str, ...] = ("1.3.2",),
           pack: str = "mps3") -> HarnessBuild:
    """Validate ``bundle`` and build its entry. Raises on a refusal; writes nothing (the
    assets are in ``HarnessBuild.pending`` until ``write()``)."""
    bundle = Path(bundle)
    if not bundle.is_dir():
        raise ReleaseError(f"no bundle directory at {bundle}")
    if not is_version(version) or version.startswith("v"):
        raise ReleaseError(f"{version!r} is not a harness release version like 1.2.0")
    findings: list[Finding] = []
    warnings: list[str] = []
    everything = tree_files(bundle)

    # .ebf anywhere
    ebf = [r for r in everything if r.lower().endswith(".ebf")]
    findings.append(Finding("EBF", not ebf, f"refusing {', '.join(ebf)}: an .ebf reflashes the "
                            "MCC" if ebf else "no .ebf in the bundle"))

    fw = _read_json(bundle / "firmware.json") if (bundle / "firmware.json").is_file() else {}
    if (bundle / "linux_bundle.json").is_file():
        lb = _read_json(bundle / "linux_bundle.json")
        ident = _identity_linux(bundle, lb, fw, findings)
    elif (bundle / "mint.json").is_file():
        lb = {}
        ident = _identity_bare_metal(bundle, _read_json(bundle / "mint.json"), fw)
    else:
        raise ReleaseError(f"{bundle} has neither mint.json nor linux_bundle.json")
    if not ident.static_id:
        raise ReleaseError("the mint names no static_id")
    fw = ident.fw

    # dirty
    dirty = list(ident.dirty_sources)
    if fw.get("dirty") is True:
        dirty.append(f"firmware {fw.get('sha', '?')} built dirty")
    if (bundle / "kit" / "kit.json").is_file():
        kit_src = (_read_json(bundle / "kit" / "kit.json").get("source") or {})
        if kit_src.get("dirty") is True:
            dirty.append("kit built from a dirty tree")
    if dirty and allow_dirty:
        if channel == "stable":
            raise ReleaseError("--allow-dirty is never allowed on stable")
        warnings.append(f"DIRTY WAIVER ({allow_dirty}): {'; '.join(dirty)}")
    findings.append(Finding("DIRTY", not dirty or bool(allow_dirty),
                            "clean" if not dirty else "; ".join(dirty)))

    # firmware elf, if shipped
    elfs = sorted((bundle / "firmware").glob("*.elf")) if (bundle / "firmware").is_dir() else []
    if elfs and fw.get("elf_sha256"):
        ok = sha256_file(elfs[0]) == str(fw["elf_sha256"]).lower()
        findings.append(Finding("FIRMWARE", ok, f"{elfs[0].name} sha256 "
                                + ("matches firmware.json" if ok else "differs from firmware.json")))
    if not fw.get("sha"):
        warnings.append("no firmware sha (firmware.json / mint.json R2): HM cannot tell two "
                        "releases on one static apart (HARNESS-DIST P2)")

    # the SD tree + the static .bit
    sd_root = bundle / "sd"
    sd = tree_files(sd_root) if sd_root.is_dir() else {}
    if not sd:
        raise ReleaseError(f"{bundle} has no sd/ tree (the config-SD files with the static .bit)")
    bits = [r for r in sd if r.lower().endswith(".bit")]
    part = ""
    if not bits:
        findings.append(Finding("UNSTAMPED", False, "the SD tree carries no .bit"))
    for rel in bits:
        try:
            hdr = read_bit_header(sd[rel])
        except (BitHeaderError, OSError) as exc:
            findings.append(Finding("UNSTAMPED", False, f"{rel}: {exc}"))
            continue
        part = part or hdr.part
        findings.append(Finding("UNSTAMPED", hdr.stamped,
                                f"{rel}: USERID {hdr.userid or 'none'}" + ("" if hdr.stamped else
                                " (an unstamped bitstream names no implementation)")))
        if hdr.stamped:
            same = bool(ident.usercode) and int(hdr.userid, 16) == int(ident.usercode, 16)
            findings.append(Finding("USERID", same, f"{rel}: header USERID {hdr.userid}, mint "
                                    f"static_usercode {ident.usercode or '(none)'}"))
    if lb:
        want = ((lb.get("targets") or {}).get("mcc_sd") or {}).get("flashable_bit") or {}
        if want.get("sha256"):
            ok = any(sha256_file(sd[r]) == want["sha256"] for r in bits)
            findings.append(Finding("STATIC", ok, f"mcc_sd flashable_bit {want.get('name')} "
                                    + ("is the SD .bit" if ok else "is NOT the .bit in sd/")))
    revs = sorted({r.split("/")[1] for r in sd if r.lower().startswith("mb/") and r.count("/") >= 2})
    if len(revs) > 1:
        findings.append(rev_trees_finding(sd, revs))

    # overlays: open vs AAA, keyed to this static
    handler = PackOverlayHandler(pack)
    groups: dict[str, dict[str, Path]] = {}
    for cls in ("open", "aaa"):
        root = bundle / "overlays" / cls
        if not root.is_dir():
            continue
        groups[cls] = tree_files(root)
        for man in sorted(root.glob("*/manifest.json")):
            try:
                info = handler.inspect(man.parent)
            except Exception as exc:  # noqa: BLE001 - any unreadable manifest refuses
                findings.append(Finding("STATIC", False, f"overlays/{cls}/{man.parent.name}: "
                                        f"unreadable manifest: {exc}"))
                continue
            name = info["rm_name"]
            same_sid = int(info["static_id"], 16) == int(ident.static_id, 16)
            findings.append(Finding("STATIC", same_sid, f"overlay {name}: keyed to "
                                    f"{info['static_id']}, the mint is {ident.static_id}"))
            uc = info.get("static_usercode") or ""
            if ident.usercode:
                ok = bool(uc) and int(uc, 16) == int(ident.usercode, 16)
                findings.append(Finding("USERID", ok, f"overlay {name}: static_usercode "
                                        f"{uc or '(none)'}, the mint is {ident.usercode}"))
            declared = str(info.get("ip_class") or "")
            if cls == "open":
                aaa = _is_aaa(declared) or name.lower() in AAA_RMS or \
                    name.lower().startswith("nanosoc")
                findings.append(Finding(
                    "AAA_OPEN", not aaa,
                    f"overlay {name} (ip_class {declared or 'undeclared'}) "
                    + ("is Arm IP: it belongs in overlays/aaa/ (a private repo)" if aaa
                       else "is open")))
                if not declared and not aaa:
                    warnings.append(f"overlay {name} declares no ip_class (platform G5/R5 "
                                    "pending): published as open because its name is not a "
                                    "known Arm-IP RM")
            elif not _is_aaa(declared):
                warnings.append(f"overlay {name} is under overlays/aaa but declares ip_class "
                                f"{declared or '(none)'}: published as AAA anyway")
            try:
                handler.validate(man.parent)
            except Exception as exc:  # noqa: BLE001
                findings.append(Finding("STATIC", False, f"overlay {name}: length/CRC: {exc}"))
    findings += _scan_aaa("sd/", sd)
    if "open" in groups:
        findings += _scan_aaa("overlays/open/", groups["open"])

    # Linux: the slot image and its provisioning
    img = bundle / "linux_slot.img"
    legal = bundle / "linux_legal_info.tar"
    if lb:
        eth = (lb.get("targets") or {}).get("ethernet") or {}
        si = eth.get("slot_image") or {}
        if not img.is_file():
            findings.append(Finding("STATIC", False, "linux_slot.img is missing"))
        else:
            ok = not si.get("sha256") or sha256_file(img) == si["sha256"]
            findings.append(Finding("STATIC", ok, "linux_slot.img sha256 "
                                    + ("matches" if ok else "differs from") + " linux_bundle.json"))
        prov = _hex((eth.get("provisioned") or {}).get("static_id"))
        findings.append(Finding("STATIC", prov == ident.static_id,
                                f"slot image provisioned for {prov or '(none)'}, the static is "
                                f"{ident.static_id}"))
        if img.is_file():
            findings += _slot_frames(img, si, warnings)
        li = eth.get("legal_info") or {}
        if not legal.is_file() or (li.get("sha256") and sha256_file(legal) != li["sha256"]):
            findings.append(Finding("NOT_FIELDABLE", False, "linux_legal_info.tar is missing or "
                                    "not the one linux_bundle.json names (GPL)"))
    elif img.is_file():
        findings.append(Finding("STATIC", False, "a bare-metal mint carries a linux_slot.img"))

    # kit
    kit_zip = sorted((bundle / "kit").glob("mps3-kit-*.zip")) if (bundle / "kit").is_dir() else []
    kit_meta = _read_json(bundle / "kit" / "kit.json") if kit_zip and \
        (bundle / "kit" / "kit.json").is_file() else {}
    if kit_zip:
        ksid = _hex(kit_meta.get("static_id"))
        findings.append(Finding("STATIC", ksid == ident.static_id,
                                f"kit keyed to {ksid or '(none)'}, the static is {ident.static_id}"))
        if _is_aaa(str(kit_meta.get("ip_class", "open"))):
            findings.append(Finding("AAA_OPEN", False, "the kit declares Arm IP"))
        findings += _scan_aaa("kit/", {p.name: p for p in kit_zip})

    bad = [f for f in findings if not f.ok]
    if bad:
        identity_codes = {"USERID", "STATIC"}
        raise ReleaseError(
            f"harness {version} from {bundle.name} is refused ({len(bad)}):\n    "
            + "\n    ".join(f"[{f.code}] {f.detail}" for f in bad),
            hint="nothing was written; fix the mint or its bundle",
            code=EXIT_MISMATCH if all(f.code in identity_codes for f in bad) else 15)

    # --- the entry + assets ---
    tag = f"{catalog}-v{version}"
    base = f"{catalog}-{version}"
    comps: list[dict[str, Any]] = []
    assets: list[Path] = []
    aaa_assets: list[Path] = []
    pending: list[tuple[Path, bytes]] = []

    def put(name: str, data: bytes, *, aaa: bool = False, acc: str = access) -> dict[str, Any]:
        repo = layout.aaa_repo if aaa else layout.repo
        path = layout.asset_path(tag, name, repo)
        pending.append((path, data))
        (aaa_assets if aaa else assets).append(path)
        a: dict[str, Any] = {"name": name, "url": layout.rel_url(tag, name, repo),
                             "sha256": sha256_bytes(data), "size": len(data)}
        if aaa or acc != "public":
            a["access"], a["repo"] = "github-token", repo
        return a

    sd_bytes = {r: p.read_bytes() for r, p in sd.items()}
    rev = sd_component_rev(revs)
    comps.append({**put(f"{base}-sd-{rev}.zip", deterministic_zip(sd_bytes)),
                  "name": f"sd-{rev}", "target": "mcc-sd", "kind": "sd", "door": DOOR_MCC_SD,
                  "files": {r: sha256_bytes(b) for r, b in sd_bytes.items()}})
    if lb and img.is_file():
        # The target stays HM's older spelling ``user-usd`` so apps before OTA-C still parse
        # the channel (the schema reads it as ``ethernet``); ``door`` is FLOW's name. The
        # declarations the install checks travel with it (schema ``_os_slot_extras``).
        si = ((lb.get("targets") or {}).get("ethernet") or {}).get("slot_image") or {}
        prov = ((lb.get("targets") or {}).get("ethernet") or {}).get("provisioned") or {}
        os_comp: dict[str, Any] = {**put(f"{base}-linux_slot.img", img.read_bytes()),
                                   "name": "os-slot", "target": "user-usd", "kind": "os-slot",
                                   "format": "raw", "door": DOOR_ETHERNET,
                                   "provisioned": {"static_id": _hex(prov.get("static_id"))
                                                   or ident.static_id}}
        if isinstance(si.get("s0lb"), dict) and si["s0lb"]:
            os_comp["s0lb"] = si["s0lb"]
        if si.get("crc32"):
            os_comp["crc32"] = si["crc32"]
        os_comp["bytes"] = img.stat().st_size
        comps.append(os_comp)
    if groups.get("open"):
        data = deterministic_zip({r: p.read_bytes() for r, p in groups["open"].items()})
        comps.append({**put(f"{base}-overlays-open.zip", data), "name": "overlays-open",
                      "target": "host-store", "kind": "overlays", "ip_class": "open",
                      "door": DOOR_ETHERNET})
    if groups.get("aaa"):
        data = deterministic_zip({r: p.read_bytes() for r, p in groups["aaa"].items()})
        comps.append({**put(f"{base}-overlays-aaa.zip", data, aaa=True), "name": "overlays-aaa",
                      "target": "host-store", "kind": "overlays", "ip_class": "arm-aaa",
                      "door": DOOR_ETHERNET})
    if kit_zip:
        if schema_has_host_kit():
            kit_access = str(kit_meta.get("access") or access)       # K2: may be public
            comps.append({**put(kit_zip[0].name, kit_zip[0].read_bytes(), acc=kit_access),
                          "name": "kit", "target": "host-kit", "kind": "rm-kit",
                          "static_id": ident.static_id,
                          "vivado": str((kit_meta.get("vivado") or {}).get("release", "")
                                        if isinstance(kit_meta.get("vivado"), dict)
                                        else kit_meta.get("vivado", ident.vivado))})
        else:
            warnings.append("kit NOT published: this app's schema has no host-kit target yet "
                            "(KIT-STORE K4 / KIT-CORE); the release goes out without it")
    wire = str(fw.get("version") or "")
    ident_doc: dict[str, Any] = {"static_id": ident.static_id, "usercode": ident.usercode,
                                 "impl": ident.impl, "proto": str(fw.get("proto") or ""),
                                 "features": list(fw.get("features") or []),
                                 "fw_sha": str(fw.get("sha") or ""), "ver32": ident.ver32}
    if wire:
        ident_doc["harness"] = wire        # what the firmware REPORTS, never the tag (rule 1)
    stamped = bool(wire) and wire.lstrip("v") == version.lstrip("v")
    if not stamped:
        warnings.append(f"firmware VERSION is {wire or '(unknown)'}, not {version} (U7 stamping "
                        "pending): HM matches this release by fw_sha")
    entry: dict[str, Any] = {
        "version": version, "status": "current", "identity": ident_doc,
        "compat": {"min_app": min_app, "board_revs": revs, "mcc_fw_tested": list(mcc_fw_tested),
                   "net_protocol": str(fw.get("proto") or "")},
        "rekey": False, "components": comps, "vivado": ident.vivado, "tag": tag,
        "firmware": {"version": wire, "sha": str(fw.get("sha") or ""), "stamped": stamped,
                     "elf_sha256": str(fw.get("elf_sha256") or "")},
        "source": {"bundle": bundle.name, "impl": ident.impl, **ident.extra,
                   "dirty": bool(dirty)},
    }
    if dirty and allow_dirty:
        entry["source"]["dirty_waiver"] = allow_dirty
    if (bundle / "notes.md").is_file():
        entry["notes"] = (bundle / "notes.md").read_text(encoding="utf-8").strip()
    elif lb and str(lb.get("release_notes") or "").strip():
        entry["notes"] = str(lb["release_notes"]).strip()      # Linux request L1
    if lb and legal.is_file():
        a = put(f"{base}-linux_legal_info.tar", legal.read_bytes())
        entry["legal_info"] = a            # published beside the release, never installed
    if lb:
        # RELEASE-PIPE: the Linux bundle's own manifest (FLOW_CONTRACT §0.1) travels beside
        # the release too, for the platform's tools; informational, never installed.
        entry["linux_bundle"] = put(f"{base}-linux_bundle.json",
                                    (bundle / "linux_bundle.json").read_bytes())
    board = {"pack": pack, "part": part.split("-")[0] if part else "", "revisions": revs}
    return HarnessBuild(version=version, tag=tag, impl=ident.impl, entry=entry, board=board,
                        findings=findings, warnings=warnings, assets=assets,
                        aaa_assets=aaa_assets, pending=pending)
