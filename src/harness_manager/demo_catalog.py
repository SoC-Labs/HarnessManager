"""The demo's offline catalogue: signed harness releases, a DUT build kit, an app update.

``harness-manager app --demo`` shows the Harness versions card, the Build section and the
app-update banner with nothing on the network. Everything here is built in the demo's own
state dir (``<state>/demo/`` for ``--demo``; a temporary directory for a bare
``DemoEngine(showcase=True)``) each time the demo engine starts:

- **a signed harness catalogue** (``demo-fixtures/www``): ``stable`` lists 1.0.0 (the old
  static 0x3F1A560F), 1.1.0 (the ILA static 0x72BB0A36, what the bare-metal demo board
  runs) and 1.1.1 (a firmware re-bake on the same static); ``beta`` (and ``dev``, the same)
  adds 2.0.0, the Linux harness on 0x4C1A0003 (what the Linux demo board runs). The same four mints as the
  HARNESS-CAT fixtures (``tests/fakes/hcat_catalog.py``), published in the same shapes and
  validated with the app's own parser before signing. The key is a THROWAWAY minisign key
  made in memory at every start; the demo's update service trusts only it. On the bare-metal
  board the list shows every verdict: 1.1.0 and 1.1.1 fit, 1.0.0 is a re-key and 2.0.0
  needs a Linux harness (the door). A history (1.0.0 -> 1.1.0 on 09-24) and a pin (1.1.1)
  are seeded once, so the card has them; what you do in the demo afterwards is kept.
- **a DUT build kit** for 0x72BB0A36: the fixture kit of ``tests/fakes/kit_fixture`` (a
  FAKE DCP whose CRC-32 is the static id, the stamp, the measured partition frames), made
  here byte for byte and imported into the demo's kit cache, so the Build section's steps
  have states; the Linux static 0x4C1A0003 gets one of the same shape (Vivado 2026.1). The
  releases carry them as ``rm-kit`` components too, so ``kit fetch`` works offline.
- **an app update, off by default.** ``HARNESS_MANAGER_DEMO_UPDATE=staged`` marks a newer
  Harness Manager as staged, so the "Restart to update" banner shows. Applying it is always
  refused (the demo installs and switches nothing). Without the knob the demo reports
  itself as an install that never self-updates: no banner, and the background checker does
  nothing.

**Offline by construction.** ``DemoDownloader`` refuses every non-``file:`` URL, the
update service's default source is the local catalogue (never the GitHub one), the token
is "" (``gh`` is never asked), and the app catalogue does not exist.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import time
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_manager import __version__
from harness_manager.core.errors import AbsentError, RefusedError, UnreachableError

#: The knob that stages a pretend app update (the banner): ``staged``; anything else is off.
UPDATE_ENV = "HARNESS_MANAGER_DEMO_UPDATE"
UPDATE_STAGED = "staged"

#: The demo mints' statics (the HARNESS-CAT fixtures' values).
S_OLD, U_OLD = "0x3F1A560F", "0xD46FCDCB"          # fielded until 09-24
S_ILA, U_ILA = "0x72BB0A36", "0xC8551081"          # fielded 09-24: the bare-metal demo board
S_LNX, U_LNX = "0x4C1A0003", "0x3C0FFEE3"          # mint 3 (a placeholder): the Linux board

V08 = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed")
V011 = V08 + ("dut_egress", "jtag_server", "xvc_dbgbr", "stats", "log", "reboot", "touch_cal")
LINUX_FEATURES = tuple(f for f in V011 if f != "windowed")

FW_OLD, FW_ILA, FW_REBAKE, FW_LNX = "cb31b0f2", "d68dd0ed", "0e12a0b0", "5eed0000"
NOTES = {
    "1.0.0": "The fielded static until 09-24.",
    "1.1.0": "RM ILAs over XVC; the ILA static 0x72BB0A36.",
    "1.1.1": "Firmware re-bake: the console flush fix.",
    "2.0.0": "The MicroBlaze V Linux harness (mint 3).",
}
STABLE = ("1.0.0", "1.1.0", "1.1.1")
BETA = STABLE + ("2.0.0",)
PIN = "1.1.1"
CATALOG = "mps3-harness"

ZIP_DATE = (2026, 1, 1, 0, 0, 0)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def deterministic_zip(files: dict[str, bytes]) -> bytes:
    """Same members in, same bytes out: a re-publish at the next start is a no-op."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[name])
    return buf.getvalue()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


# --- the DUT build kit (tests/fakes/kit_fakes.py's fixture, made here) ----------------------------

KIT_USERCODE = U_ILA
#: rp.frames of 0x72BB0A36, measured on its fielded dbg_demo pair (kit_fakes.FRAMES_72BB0A36).
FRAMES_72BB0A36: dict[str, Any] = {
    "idcode": "0x0390D093",
    "partial": {"by_block": {"0": {"rows": [0, 1], "columns": [94, 200]},
                             "1": {"rows": [0, 1], "columns": [6, 11]}},
                "cmds": ["DESYNC", "GRESTORE", "LFRM", "MFW", "NULL", "RCRC", "SHUTDOWN",
                         "START", "WCFG"],
                "regs": ["CMD", "COR0", "CRC", "CTL0", "CTL1", "FAR", "FDRI", "IDCODE", "MASK",
                         "MFWR"]},
    "clearing": {"by_block": {"0": {"rows": [0, 1], "columns": [101, 193]}},
                 "cmds": ["AGHIGH", "DESYNC", "MFW", "NULL", "RCRC", "SHUTDOWN", "WCFG"],
                 "regs": ["CMD", "COR0", "CRC", "CTL0", "CTL1", "FAR", "FDRI", "IDCODE", "MASK",
                          "MFWR"]},
    "from": ["config_rm_dbg_demo_pblock_rp_dut_partial.bin",
             "config_rm_dbg_demo_pblock_rp_dut_partial_clear.bin"],
}


def forge_crc(prefix: bytes, target: int) -> bytes:
    """4 bytes that make ``zlib.crc32(prefix + them) == target`` (CRC-32 is affine over GF(2))."""
    base = zlib.crc32(prefix + b"\0\0\0\0")
    pivots: dict[int, tuple[int, int]] = {}
    for bit in range(32):
        v, m = zlib.crc32(prefix + (1 << bit).to_bytes(4, "little")) ^ base, 1 << bit
        while v:
            h = v.bit_length() - 1
            if h not in pivots:
                pivots[h] = (v, m)
                break
            pv, pm = pivots[h]
            v, m = v ^ pv, m ^ pm
    v, m = (target & 0xFFFFFFFF) ^ base, 0
    while v:
        pv, pm = pivots[v.bit_length() - 1]
        v, m = v ^ pv, m ^ pm
    return m.to_bytes(4, "little")


def dcp_xml(*, release: str = "2024.1", build: int = 5076996, cpver: int = 22,
            part: str = "xcku115-flvb1760-1-c", rp_inst: str = "u_rp_dut") -> str:
    return (f'<?xml version="1.0"?>\n<Checkpoint Version="{cpver}" Minor="0">\n'
            f'\t<BUILD_NUMBER Name="{build}"/>\n'
            f'\t<PRODUCT Name="Vivado v{release} (64-bit)"/>\n'
            f'\t<Part Name="{part}"/>\n\t<Top Name="shell_top"/>\n'
            f'\t<HDBlackboxInfo Name="{rp_inst} HD.RECONFIGURABLE"/>\n</Checkpoint>\n')


def fake_dcp(static_id: str, *, release: str = "2024.1") -> bytes:
    """A zip whose CRC-32 is ``static_id``. NOT a checkpoint: Vivado would refuse it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(zipfile.ZipInfo("dcp.xml", (2026, 9, 24, 0, 0, 0)), dcp_xml(release=release))
        zf.writestr(zipfile.ZipInfo("README.txt", (2026, 9, 24, 0, 0, 0)),
                    "FAKE: a Harness Manager test fixture, not a Vivado checkpoint.\n")
        zf.comment = b"\0\0\0\0"
    data = buf.getvalue()
    return data[:-4] + forge_crc(data[:-4], int(static_id, 16))


def kit_stamp(static_id: str, usercode: str) -> dict[str, Any]:
    return {"schema": "mps3-static-stamp", "schema_version": "1",
            "generated_by": "tests/fakes/kit_fakes.py", "static_id": static_id,
            "stamped": True, "harness_version": "1.0.0", "usercode": usercode}


def build_kit(dest: Path, static_id: str = S_ILA, *, usercode: str = KIT_USERCODE,
              release: str = "2024.1", impl: str = "bare-metal",
              frames: dict[str, Any] | None = FRAMES_72BB0A36) -> Path:
    """A kit directory (``kit.json`` + ``static/``), the shape of ``tests/fakes/kit_fixture``."""
    from harness_manager.services.kit.schema import DEFAULT_LICENCE_NOTE, sha256_file

    dest = Path(dest)
    (dest / "static").mkdir(parents=True, exist_ok=True)
    (dest / "static" / "static_routed_locked.dcp").write_bytes(fake_dcp(static_id,
                                                                        release=release))
    (dest / "static" / "static_stamp.json").write_text(
        json.dumps(kit_stamp(static_id, usercode), indent=2) + "\n", encoding="utf-8")

    def entry(rel: str, role: str, crc: str = "") -> dict[str, Any]:
        p = dest / rel
        out: dict[str, Any] = {"path": rel, "role": role, "size": p.stat().st_size,
                               "sha256": sha256_file(p)}
        if crc:
            out["crc32"] = crc
        return out

    rp: dict[str, Any] = {"inst": "u_rp_dut", "pblock": "pblock_rp_dut", "ports": 47,
                          "bits": 148, "clr_max": 262144}
    if frames:
        rp["frames"] = frames
    doc = {
        "schema": "hm-rm-kit", "schema_version": 1,
        "board_type": "mps3", "part": "xcku115-flvb1760-1-c",
        "static_id": static_id, "static_usercode": usercode, "harness_impl": impl,
        "vivado": {"release": release, "build": 5076996, "checkpoint_version": 22},
        "rp": rp, "pr_verify_ref": "static/static_routed_locked.dcp",
        "access": "public", "ip_class": "open", "licence_note": DEFAULT_LICENCE_NOTE,
        "files": [entry("static/static_routed_locked.dcp", "locked_static", static_id),
                  entry("static/static_stamp.json", "stamp")],
        "source": {"fixture": True, "demo": True},
        "generated_by": "harness_manager.demo_catalog (a FAKE DCP: not a Vivado checkpoint)",
    }
    (dest / "kit.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                                   encoding="utf-8")
    return dest


def kit_zip(root: Path) -> bytes:
    return deterministic_zip({p.relative_to(root).as_posix(): p.read_bytes()
                              for p in sorted(root.rglob("*")) if p.is_file()})


# --- the signed harness catalogue (tests/spikes/harness_dist_publish.py's shapes) -----------------


def _sd(static_id: str, fw_sha: str) -> dict[str, bytes]:
    """The config-SD tree (placeholder bitstream: the demo never writes an SD card)."""
    return {
        "config.txt": b"TITLE: V2M-MPS3 config\nUSB_REMOTE: TRUE\nUARTMODE: 0\n",
        "MB/HBI0309C/board.txt": b"APPFILE: Nanosoc\\nanosoc.txt\n",
        "MB/HBI0309C/Nanosoc/nanosoc.txt": b"F0FILE: nanosoc.bit\n[OSCCLKS]\nOSC0: 25.0\n",
        "MB/HBI0309C/Nanosoc/nanosoc.bit":
            f"DEMO placeholder bitstream: static {static_id}, firmware {fw_sha}\n".encode(),
    }


def _overlays(static_id: str) -> dict[str, bytes]:
    manifest = {"name": "synth", "rm_id": "0x010000f0", "static_id": static_id,
                "note": "a demo overlay: the demo never programs a board from it"}
    return {"synth/manifest.json": json.dumps(manifest, indent=1).encode(),
            "synth/synth.bin": b"\0" * 256, "synth/synth_clear.bin": b"\0" * 128}


class _Assets:
    """``assets/<version>/<name>``: content-named, never rewritten (the published tree)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put(self, version: str, name: str, data: bytes) -> dict[str, Any]:
        path = self.root / "assets" / version / name
        if not (path.is_file() and path.read_bytes() == data):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return {"url": f"../../assets/{version}/{name}", "sha256": sha256_bytes(data),
                "size": len(data), "name": name}


def _release(assets: _Assets, version: str, *, static_id: str, usercode: str, impl: str,
             fw_sha: str, features: tuple[str, ...], proto: str, vivado: str,
             os_image: bytes = b"", kit: bytes = b"") -> dict[str, Any]:
    tag = f"mps3-harness-{version}"
    sd = _sd(static_id, fw_sha)
    comps: list[dict[str, Any]] = [
        {**assets.put(version, f"{tag}-sd-HBI0309C.zip", deterministic_zip(sd)),
         "name": "sd-HBI0309C", "target": "mcc-sd", "kind": "sd",
         "files": {k: sha256_bytes(v) for k, v in sd.items()}}]
    if os_image:
        comps.append({**assets.put(version, f"{tag}-linux_slot.img", os_image),
                      "name": "os-slot", "target": "user-usd", "kind": "os-slot",
                      "format": "raw"})
    comps.append({**assets.put(version, f"{tag}-overlays-open.zip",
                               deterministic_zip(_overlays(static_id))),
                  "name": "overlays-open", "target": "host-store", "kind": "overlays",
                  "ip_class": "open"})
    if kit:
        comps.append({**assets.put(version, f"mps3-kit-{static_id}.zip", kit),
                      "name": "kit", "target": "host-kit", "kind": "rm-kit", "vivado": vivado})
    return {"version": version, "status": "current",
            "identity": {"static_id": static_id, "usercode": usercode, "impl": impl,
                         "proto": proto, "features": list(features), "fw_sha": fw_sha,
                         "ver32": "0x01000000", "harness": "1.0.0"},
            "compat": {"min_app": "0.1.0", "board_revs": ["HBI0309C"],
                       "mcc_fw_tested": ["1.3.2"], "net_protocol": proto},
            "rekey": False, "components": comps, "released_at": "2026-09-24T12:00:00Z",
            "notes_url": f"https://example.invalid/demo/{version}", "notes": NOTES[version],
            "vivado": vivado, "source": {"demo": True}}


def _channel_doc(name: str, serial: int, key: Any, rels: list[dict[str, Any]], current: str,
                 now: float) -> dict[str, Any]:
    ordered = sorted((dict(r) for r in rels),
                     key=lambda r: tuple(int(x) for x in r["version"].split(".")), reverse=True)
    for i, r in enumerate(ordered):
        r["status"] = "current" if r["version"] == current else "superseded"
        older = ordered[i + 1] if i + 1 < len(ordered) else None
        r["rekey"] = bool(older) and older["identity"]["static_id"].lower() != \
            r["identity"]["static_id"].lower()
        if r["rekey"]:
            r["compat"] = {**r["compat"], "replaces_static_ids": [older["identity"]["static_id"]]}
    return {"schema": "harness-manager-channel", "schema_version": 1, "channel": name,
            "serial": serial, "issued_at": _iso(now), "expires_at": _iso(now + 180 * 86400),
            "signing_key_id": key.public.id_hex,
            "board": {"pack": "mps3", "part": "xcku115", "revisions": ["HBI0309C"]},
            "harness": {"current": current, "releases": ordered}}


def _publish(www: Path, doc: dict[str, Any], key: Any, now: float) -> None:
    from harness_manager.services.update import minisign
    from harness_manager.services.update.schema import parse_channel

    parse_channel(doc)                          # never sign what the app would refuse
    path = www / "channel" / doc["channel"] / "channel.json"
    data = json.dumps(doc, indent=1, sort_keys=True).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    comment = (f"timestamp:{int(now)}\tfile:channel.json\tchannel:{doc['channel']}\t"
               f"serial:{doc['serial']}")
    path.with_name("channel.json.minisig").write_text(minisign.sign(data, key,
                                                                    trusted_comment=comment))


def _os_image() -> bytes:
    """The Linux release's slot image: listed with its size, never pushed by the demo."""
    return b"DEMO-OS-SLOT-IMAGE\n" + b"\x5a" * 65536


class DemoCatalog:
    """The published tree and the key that signed it, rebuilt at every start."""

    def __init__(self, root: Path, *, now: float | None = None) -> None:
        from harness_manager.services.update import minisign

        self.root = Path(root)
        self.www = self.root / "www"
        self.key = minisign.SecretKey.generate()          # THROWAWAY, in memory only
        self.now = time.time() if now is None else now
        self.serial = max(int(self.now), self._last_serial() + 1)   # up at every start
        self.kit_dir = build_kit(self.root / "kit" / S_ILA, S_ILA)
        self.linux_kit_dir = build_kit(self.root / "kit" / S_LNX, S_LNX, usercode=U_LNX,
                                       release="2026.1", impl="linux", frames=None)
        self._publish()

    def _last_serial(self) -> int:
        """The serial the last start published (a new key signs every start, so the serial
        must go up even twice in one second: the channel client refuses a replay)."""
        try:
            doc = json.loads((self.www / "channel" / "stable" / "channel.json").read_bytes())
            return int(doc.get("serial") or 0)
        except (OSError, ValueError, TypeError):
            return 0

    @property
    def source(self) -> str:
        """The channel source (a local path with ``{channel}``): never a network URL."""
        return str(self.www / "channel" / "{channel}" / "channel.json")

    def trust(self) -> Any:
        from harness_manager.services.update.trust import ROLE_RELEASE, TrustedKey, TrustStore

        return TrustStore(pinned=(TrustedKey(self.key.public, ROLE_RELEASE,
                                             ("stable", "beta", "dev"),
                                             "the demo's throwaway key"),))

    def _publish(self) -> None:
        assets = _Assets(self.www)
        rels = {
            "1.0.0": _release(assets, "1.0.0", static_id=S_OLD, usercode=U_OLD,
                              impl="bare-metal", fw_sha=FW_OLD, features=V08, proto="0.10",
                              vivado="2024.1"),
            "1.1.0": _release(assets, "1.1.0", static_id=S_ILA, usercode=U_ILA,
                              impl="bare-metal", fw_sha=FW_ILA, features=V011, proto="0.11",
                              vivado="2024.1", kit=kit_zip(self.kit_dir)),
            "1.1.1": _release(assets, "1.1.1", static_id=S_ILA, usercode=U_ILA,
                              impl="bare-metal", fw_sha=FW_REBAKE, features=V011, proto="0.12",
                              vivado="2024.1", kit=kit_zip(self.kit_dir)),
            "2.0.0": _release(assets, "2.0.0", static_id=S_LNX, usercode=U_LNX, impl="linux",
                              fw_sha=FW_LNX, features=LINUX_FEATURES, proto="0.14",
                              vivado="2026.1", os_image=_os_image(),
                              kit=kit_zip(self.linux_kit_dir)),
        }
        _publish(self.www, _channel_doc("stable", self.serial, self.key,
                                        [rels[v] for v in STABLE], "1.1.1", self.now),
                 self.key, self.now)
        for name in ("beta", "dev"):            # dev = beta here: "beta and dev" lists both
            _publish(self.www, _channel_doc(name, self.serial, self.key,
                                            [rels[v] for v in BETA], "2.0.0", self.now),
                     self.key, self.now)


# --- the update service, offline ------------------------------------------------------------------


def _offline(what: str) -> UnreachableError:
    return UnreachableError(f"the demo never goes on the network (it was asked for {what})",
                            hint="the demo's catalogue is local; run `harness-manager app` "
                                 "without --demo for the real channels")


def demo_downloader(cache: Path) -> Any:
    """``Downloader`` that reads ``file:`` URLs and refuses everything else."""
    from harness_manager.services.update.download import Downloader

    class DemoDownloader(Downloader):
        def _opener(self, host: str) -> Any:                # every http(s) GET goes here
            raise _offline(host or "a URL")

        def _http(self, url: str, *args: Any, **kw: Any) -> Any:
            raise _offline(url)

        def _github_asset_url(self, rd: Any, what: str) -> str:
            raise _offline(f"GitHub ({what})")

    return DemoDownloader(Path(cache), token="", mirrors=())


def app_updater(state_dir: Path, *, staged: bool) -> Any:
    """The app side of the demo's update service: a pointer in the demo state dir that names
    this version, and (``staged``) a newer one ready. Applying anything is refused."""
    from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe
    from harness_manager.services.update.state import atomic_write_json

    class DemoAppUpdater(AppUpdater):
        def guard(self, what: str, *, rollback: bool = False) -> None:
            raise RefusedError(f"cannot {what}: this is the demo (scripted boards); it "
                               "installs and switches nothing",
                               hint="the banner is there for the review; run `harness-manager "
                                    "app` without --demo for real updates")

        def blocked(self) -> str:
            return "the demo installs and switches nothing"

    root = Path(state_dir) / "demo-fixtures" / "app"
    root.mkdir(parents=True, exist_ok=True)
    versions: dict[str, Any] = {__version__: {"state": "current", "at": time.time()}}
    if staged:
        versions[staged_version()] = {"state": "staged", "at": time.time(),
                                      "note": "a pretend update for the demo's banner"}
    atomic_write_json(root / "current.json", {"current": __version__, "previous": "",
                                              "versions": versions})
    dev = "" if staged else "the demo: scripted boards, it never updates itself"
    return DemoAppUpdater(AppLayout(root), LocalBusyProbe(Path(state_dir)), dev_install=dev,
                          state_dir=Path(state_dir))


def staged_version() -> str:
    """A version newer than this one (the patch number plus one)."""
    parts = __version__.split("+", 1)[0].split("-", 1)[0].split(".")
    try:
        nums = [int(p) for p in parts[:3]] + [0] * (3 - len(parts[:3]))
    except ValueError:
        return "99.0.0"
    return f"{nums[0]}.{nums[1]}.{nums[2] + 1}"


def staged_from_env() -> bool:
    return os.environ.get(UPDATE_ENV, "").strip().lower() == UPDATE_STAGED


def update_service(engine: Any, state_dir: Path, catalog: DemoCatalog, *,
                   staged: bool = False) -> Any:
    """A real ``UpdateService`` over the demo's catalogue: its channels, plans, pins and
    history are the product's; its source, trust, token and app side are the demo's."""
    from harness_manager.services.update import UpdateService
    from harness_manager.services.update.policy import Policy
    from harness_manager.services.update.schema import CATALOG_APP
    from harness_manager.services.update.state import UpdateState

    class DemoUpdateService(UpdateService):
        demo = True

        def fetch_channel(self, channel: str | None = None, source: str | None = None, *,
                          catalog: str | None = None) -> Any:
            if catalog == CATALOG_APP:
                raise AbsentError("the demo has no app catalogue",
                                  hint=f"its app update is scripted: {UPDATE_ENV}=staged")
            if source not in (None, "", catalog_source):
                raise _offline(f"the update source {source!r}")
            return super().fetch_channel(channel, catalog_source, catalog=catalog)

    catalog_source = catalog.source
    state = UpdateState.under(state_dir)
    svc = DemoUpdateService(engine, state_dir=state_dir, trust=catalog.trust(), token="",
                            downloader=demo_downloader(state.cache),
                            app_updater=app_updater(state_dir, staged=staged),
                            policy=Policy(), sd_ab=False)
    return svc


def kit_channel(update: Any, catalog: DemoCatalog) -> Any:
    """KIT-CORE's channel source over the demo's catalogue (never the GitHub one)."""
    from harness_manager.services.update.kits import ChannelKits, KitChannelSource

    return KitChannelSource(lambda: ChannelKits.for_service(
        update, pack="mps3", channels=("stable", "beta"), source=catalog.source))


def seed_kit(state_dir: Path, catalog: DemoCatalog, channel: Any) -> None:
    """The fixture kit for 0x72BB0A36 (and the Linux static's) in the demo's kit cache, once
    (the cache is content-addressed; a kit you removed in the demo comes back at a restart)."""
    from harness_manager.services.kit import KitService
    from harness_manager.services.store import ContentStore

    kits = KitService(ContentStore(Path(state_dir) / "store"), Path(state_dir) / "kits",
                      channel=channel)
    for sid, kit_dir in ((S_ILA, catalog.kit_dir), (S_LNX, catalog.linux_kit_dir)):
        if kits.get(sid) is None:
            kits.import_(kit_dir, source="demo")


def seed_history(state_dir: Path, bare_metal: str, linux: str) -> None:
    """The bare-metal board's install history and pin, and the Linux board's install: written
    only when the demo has none yet (what you do in the demo afterwards stays)."""
    from harness_manager.services.update.state import (
        InstallRecords,
        Pins,
        UpdateState,
        atomic_write_bytes,
        atomic_write_json,
        read_json,
    )

    state = UpdateState.under(state_dir)
    records = InstallRecords(state)
    day = 86400.0
    now = time.time()
    plan = {
        bare_metal: [
            {"version": "1.0.0", "result": "installed", "kind": "install", "from_version": "",
             "static_id": S_OLD, "fw_sha": FW_OLD, "doors": ["debug-usb"],
             "detail": "the first fielded harness (0x3F1A560F)", "recorded_at": now - 20 * day},
            {"version": "1.1.0", "result": "installed", "kind": "install",
             "from_version": "1.0.0", "static_id": S_ILA, "fw_sha": FW_ILA,
             "doors": ["debug-usb"], "rekey": True,
             "detail": "re-keyed 0x3F1A560F -> 0x72BB0A36 (the ILA mint)",
             "recorded_at": now - 2 * day}],
        linux: [
            {"version": "2.0.0", "result": "installed", "kind": "install",
             "from_version": "1.1.0", "static_id": S_LNX, "fw_sha": FW_LNX,
             "doors": ["debug-usb"], "rekey": True,
             "detail": "the Linux harness (mint 3), from the beta channel",
             "recorded_at": now - 1 * day}],
    }
    installed = read_json(state.installed, {}) or {}
    for bid, rows in plan.items():
        path = state.history(bid)
        if path.exists() or records.get(bid) is not None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, "".join(json.dumps(r, sort_keys=True) + "\n"
                                         for r in rows).encode("utf-8"))
        installed[bid] = rows[-1]
    atomic_write_json(state.installed, installed)
    pins = Pins(state)
    if bare_metal not in pins.all() and not (Path(state.pins).exists()
                                               and bare_metal in (read_json(state.pins, {}) or {})):
        pins.set(bare_metal, PIN, catalog=CATALOG, by="demo: hold on 1.1.x until the cutover")


def cleanup(path: Path | None) -> None:
    """Remove a temporary demo state dir (only one this module made)."""
    import shutil

    if path is not None:
        with contextlib.suppress(OSError):
            shutil.rmtree(path)


__all__ = ["DemoCatalog", "UPDATE_ENV", "S_ILA", "S_LNX", "S_OLD", "build_kit",
           "kit_channel", "seed_history", "seed_kit", "staged_from_env", "update_service"]
