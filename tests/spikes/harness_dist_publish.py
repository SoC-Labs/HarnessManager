"""Spike (lane HARNESS-DIST): a prototype of the lead-run release command.

This is the producer half that T7 does not have. T7 built the consumer (fetch,
verify, plan, install). Nothing yet turns a mint's outputs into a signed
``channel.json`` plus assets. This module does it for the spike only, with
throwaway keys under /tmp, and it dogfoods the consumer:

- every release entry is validated with ``schema.parse_channel`` BEFORE signing,
  so the publisher can never sign a channel the app would refuse;
- the zips are deterministic (sorted members, fixed timestamps, fixed modes), so
  re-packing the same mint gives the same sha256 and a re-publish is a no-op;
- assets are content-named (``<version>/<name>``) and never rewritten: a
  second publish with the same name and different bytes is refused.

Input is a normalised ``MintRecord``. Two adapters show what the platform
already writes: ``from_fielded_mint`` (bare metal, ``fielded/<sid>/mint.json``)
and ``from_linux_bundle`` (``prod/linux_bundle.json``, schema
``mps3-linux-bundle`` v1, FLOW_CONTRACT §0.1). The real command is a ``harness``
front-end to OTA-R's release tool, which signs with the ``minisign`` CLI
(docs/design/HARNESS_DISTRIBUTION.md §4.4); this file is not it.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.services.update import minisign
from harness_manager.services.update.schema import parse_channel

ZIP_DATE = (2026, 1, 1, 0, 0, 0)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def deterministic_zip(files: dict[str, bytes]) -> bytes:
    """Same members in, same bytes out (sorted, fixed date, fixed mode, deflate)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[name])
    return buf.getvalue()


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


@dataclass
class MintRecord:
    """What one mint hands the release step, normalised across bare metal and Linux."""

    static_id: str
    usercode: str
    impl: str                                # bare-metal | linux
    fw_sha: str                              # the firmware/image git sha the board reports
    wire_harness: str                        # what `version.harness` will say ("1.0.0" today)
    ver32: str = ""                          # USR_ACCESS / HARNESS_VER32
    proto: str = ""
    features: list[str] = field(default_factory=list)
    vivado: str = ""                         # the release a DUT kit needs
    sd_files: dict[str, bytes] = field(default_factory=dict)       # config-SD tree
    overlays_open: dict[str, bytes] = field(default_factory=dict)  # <rm>/<file> -> bytes
    overlays_aaa: dict[str, bytes] = field(default_factory=dict)
    os_image: bytes = b""                    # linux_slot.img
    kit_zip: bytes = b""                     # KIT-STORE's mps3-kit-<sid>.zip
    source: dict[str, Any] = field(default_factory=dict)          # repo sha, mint dir, ...


def from_fielded_mint(mint_json: dict[str, Any], **files: Any) -> MintRecord:
    """Bare metal: ``fielded/<sid>/mint.json`` names the static; the bytes come from the mint."""
    canon = mint_json.get("static_canon", {})
    return MintRecord(static_id=mint_json["static_id"],
                      usercode=mint_json.get("static_usercode", canon.get("static_usercode", "")),
                      impl="bare-metal", fw_sha=files.pop("fw_sha"),
                      wire_harness=files.pop("wire_harness", "1.0.0"),
                      ver32=mint_json.get("usr_access", ""), **files)


def from_linux_bundle(bundle: dict[str, Any], **files: Any) -> MintRecord:
    """Linux: ``prod/linux_bundle.json`` (mps3-linux-bundle v1) already names both doors."""
    if bundle.get("schema") != "mps3-linux-bundle" or bundle.get("schema_version") != 1:
        raise ValueError("not an mps3-linux-bundle v1 manifest")
    if not bundle.get("fieldable"):
        raise ValueError("a prototype (P-mint) bundle is never published (FLOW §5)")
    comps = bundle["targets"]["ethernet"].get("components", {})
    return MintRecord(static_id=bundle["static_id"], usercode=bundle["static_usercode"],
                      impl="linux", fw_sha=comps.get("harnessd_sha256", "")[:8],
                      wire_harness=files.pop("wire_harness", "1.0.0"),
                      ver32=bundle.get("static_ver32", ""), **files)


class ReleaseStore:
    """The "web": ``assets/<version>/<name>`` + ``channel/<name>/channel.json(.minisig)``."""

    def __init__(self, root: Path, *, url_base: str = "") -> None:
        self.root = Path(root)
        self.url_base = url_base          # "" = relative URLs (a mirror is a plain copy)

    def put(self, version: str, name: str, data: bytes) -> dict[str, Any]:
        path = self.root / "assets" / version / name
        if path.exists() and path.read_bytes() != data:
            raise RuntimeError(f"asset {version}/{name} exists with different bytes: a "
                               "published asset is never rewritten; bump the version")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        url = (f"{self.url_base}assets/{version}/{name}" if self.url_base
               else f"../../assets/{version}/{name}")
        return {"url": url, "sha256": sha256_bytes(data), "size": len(data), "name": name}


def build_release(store: ReleaseStore, version: str, mint: MintRecord, *,
                  identity_harness: str | None = None, notes_url: str = "",
                  min_app: str = "0.1.0", with_kit: bool = False,
                  released_at: str = "2026-09-24T12:00:00Z") -> dict[str, Any]:
    """One channel release entry from a mint. ``identity_harness``: what the release says
    the board will report as ``version.harness`` (None = the wire value, the honest one)."""
    tag = f"mps3-harness-{version}"
    comps: list[dict[str, Any]] = []
    if mint.sd_files:
        data = deterministic_zip(mint.sd_files)
        comps.append({**store.put(version, f"{tag}-sd-HBI0309C.zip", data),
                      "name": "sd-HBI0309C", "target": "mcc-sd", "kind": "sd",
                      "files": {k: sha256_bytes(v) for k, v in mint.sd_files.items()}})
    if mint.os_image:
        comps.append({**store.put(version, f"{tag}-linux_slot.img", mint.os_image),
                      "name": "os-slot", "target": "user-usd", "kind": "os-slot",
                      "format": "raw"})
    if mint.overlays_open:
        data = deterministic_zip(mint.overlays_open)
        comps.append({**store.put(version, f"{tag}-overlays-open.zip", data),
                      "name": "overlays-open", "target": "host-store", "kind": "overlays",
                      "ip_class": "open"})
    if mint.overlays_aaa:
        data = deterministic_zip(mint.overlays_aaa)
        comps.append({**store.put(version, f"{tag}-overlays-aaa.zip", data),
                      "name": "overlays-aaa", "target": "host-store", "kind": "overlays",
                      "ip_class": "arm-aaa", "access": "github-token",
                      "repo": "SoC-Labs/mps3-harness-dist-aaa"})
    if with_kit and mint.kit_zip:
        # KIT-STORE's proposal: kind rm-kit on a target the planner never deploys.
        comps.append({**store.put(version, f"mps3-kit-{mint.static_id}.zip", mint.kit_zip),
                      "name": "kit", "target": "host-kit", "kind": "rm-kit",
                      "vivado": mint.vivado})
    ident = {"static_id": mint.static_id, "usercode": mint.usercode, "impl": mint.impl,
             "proto": mint.proto, "features": list(mint.features), "fw_sha": mint.fw_sha,
             "ver32": mint.ver32}
    harness = mint.wire_harness if identity_harness is None else identity_harness
    if harness:
        ident["harness"] = harness
    return {"version": version, "status": "current", "identity": ident,
            "compat": {"min_app": min_app, "board_revs": ["HBI0309C"],
                       "mcc_fw_tested": ["1.3.2"], "net_protocol": mint.proto},
            "rekey": False, "components": comps, "released_at": released_at,
            "notes_url": notes_url, "vivado": mint.vivado, "source": mint.source}


def channel_document(channel: str, serial: int, key: minisign.SecretKey,
                     releases: list[dict[str, Any]], current: str, *,
                     board: dict[str, Any] | None = None,
                     issued_at: str = "2026-09-24T12:00:00Z",
                     expires_at: str = "2027-03-24T00:00:00Z") -> dict[str, Any]:
    """Newest first; ``status`` derived (current / superseded) unless a release is withdrawn;
    ``rekey`` derived from the static_id of the release that precedes it."""
    rels = [dict(r) for r in releases]
    ordered = sorted(rels, key=lambda r: tuple(int(x) for x in r["version"].split(".")),
                     reverse=True)
    for i, r in enumerate(ordered):
        if r.get("status") != "withdrawn":
            r["status"] = "current" if r["version"] == current else "superseded"
        older = ordered[i + 1] if i + 1 < len(ordered) else None
        r["rekey"] = bool(older) and older["identity"]["static_id"].lower() != \
            r["identity"]["static_id"].lower()
        if r["rekey"]:
            r["compat"] = {**r["compat"], "replaces_static_ids": [older["identity"]["static_id"]]}
    return {"schema": "harness-manager-channel", "schema_version": 1, "channel": channel,
            "serial": serial, "issued_at": issued_at, "expires_at": expires_at,
            "signing_key_id": key.public.id_hex,
            "board": board or {"pack": "mps3", "part": "xcku115", "revisions": ["HBI0309C"]},
            "harness": {"current": current, "releases": ordered}}


def publish_channel(store: ReleaseStore, doc: dict[str, Any], key: minisign.SecretKey) -> Path:
    """Validate with the APP's parser, then sign. Refuses a serial that does not go up."""
    parse_channel(doc)                                   # the publisher dogfoods the consumer
    path = store.root / "channel" / doc["channel"] / "channel.json"
    if path.is_file():
        old = json.loads(path.read_bytes())
        if doc["serial"] <= old["serial"]:
            raise RuntimeError(f"serial {doc['serial']} does not go up from {old['serial']}")
    data = json.dumps(doc, indent=1, sort_keys=True).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    comment = f"timestamp:1758715200\tfile:channel.json\tchannel:{doc['channel']}\tserial:{doc['serial']}"
    path.with_name("channel.json.minisig").write_text(minisign.sign(data, key,
                                                                    trusted_comment=comment))
    return path
