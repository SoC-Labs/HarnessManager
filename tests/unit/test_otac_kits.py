"""OTA-C (P7, KIT-STORE K4): the DUT build kit rides the signed channel as ``kind: rm-kit`` on
``target: host-kit``; the harness planner never downloads it; KIT-CORE lists and fetches
it by static (``update.kits``). Also the doors' names: the platform's Linux-bundle
``mcc_sd`` / ``ethernet`` (FLOW_CONTRACT v1.6), with HM's old names read as them.
Every check has a negative twin. 127.0.0.1 only.
"""

from __future__ import annotations

import copy
import json

import pytest

from harness_manager.core.errors import AbsentError
from harness_manager.core.model import BoardIdentity
from harness_manager.services.update.channel import ChannelClient
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.kits import ChannelKits, kit_assets
from harness_manager.services.update.planner import BoardView, make_plan
from harness_manager.services.update.schema import (
    LINUX_BUNDLE_TARGETS,
    TARGET_ETHERNET,
    TARGET_HOST_KIT,
    TARGET_MCC_SD,
    ChannelFormatError,
    parse_channel,
)
from harness_manager.services.update.state import UpdateState
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t7_bundles import FIELDED_STATIC, Release

KEYS = TestKeys()
KIT = b"PK-kit-zip-" + b"\x00" * 64


def kit_component(b: ChannelBuilder, data: bytes = KIT, **fields) -> dict:
    return b.component("kit", "host-kit", AssetFile(f"mps3-kit-{len(data)}.zip", data),
                       kind="rm-kit", vivado="2024.1", **fields)


def publish(b: ChannelBuilder, tmp_path, *releases: tuple[str, str, bytes | None],
            serial: int = 1) -> None:
    """releases: (version, status, kit bytes or None)."""
    for version, status, kit in releases:
        comps = Release(version).components(b, tmp_path / "art")
        if kit is not None:
            comps.append(kit_component(b, kit))
        b.add_harness(version, Release(version).identity(), comps, status=status,
                      current=status == "current")
    b.publish(serial=serial)


def kits_for(srv, tmp_path, name: str = "c") -> ChannelKits:
    state = UpdateState(tmp_path / name / "update")
    client = ChannelClient(state, Downloader(state.cache, mirrors=()), KEYS.trust())
    return ChannelKits(client, client.downloader, source=srv.source())


@pytest.fixture
def srv(tmp_path):
    with FakeChannelServer(tmp_path / "www") as s:
        yield s


def test_a_kit_round_trips_through_the_channel(srv, tmp_path):
    b = ChannelBuilder(srv.root, KEYS)
    publish(b, tmp_path, ("1.1.0", "current", KIT))
    kits = kits_for(srv, tmp_path)
    [k] = kits.list(FIELDED_STATIC.upper().replace("0X", "0x"))
    assert (k.static_id, k.release, k.vivado, k.asset.access) == (FIELDED_STATIC, "1.1.0",
                                                                   "2024.1", "public")
    assert k.asset.url.startswith(srv.base)                       # absolute: KIT-CORE's seam
    assert kits.resolve(FIELDED_STATIC) == k.asset
    assert kits.fetch(FIELDED_STATIC).read_bytes() == KIT


def test_twin_the_harness_planner_never_downloads_the_kit(srv, tmp_path):
    b = ChannelBuilder(srv.root, KEYS)
    publish(b, tmp_path, ("1.1.0", "current", KIT))
    ch = kits_for(srv, tmp_path).verified()[0].channel
    ident = BoardIdentity(board_type="mps3", shell_id="0x5a11c0de", harness_version="0.9.0")
    plan = make_plan(ch, BoardView(board_id="b", pack="mps3", identity=ident, has_storage=True,
                                   has_controller=True), app_version="9.9.9")
    assert plan.components and "kit" not in plan.components         # a re-key, and no kit
    assert all(s.component != "kit" for s in plan.steps)


def test_twin_no_kit_for_another_static(srv, tmp_path):
    b = ChannelBuilder(srv.root, KEYS)
    publish(b, tmp_path, ("1.1.0", "current", KIT))
    kits = kits_for(srv, tmp_path)
    assert kits.list("0x12345678") == [] and kits.resolve("0x12345678") is None
    with pytest.raises(AbsentError, match="no signed channel lists"):
        kits.fetch("0x12345678")


def test_the_newest_non_withdrawn_kit_wins(srv, tmp_path):
    b = ChannelBuilder(srv.root, KEYS)
    publish(b, tmp_path, ("1.0.0", "superseded", b"PK-kit-1.0.0"),
            ("1.1.0", "withdrawn", b"PK-kit-1.1.0"), ("1.2.0", "current", None))
    got = kits_for(srv, tmp_path).list(FIELDED_STATIC)
    assert [k.release for k in got] == ["1.0.0", "1.1.0"]           # withdrawn last
    assert kit_assets(kits_for(srv, tmp_path, "d").verified()[0])[0].release == "1.0.0"


# --- the schema --------------------------------------------------------------------------------


DOC = {"schema": "harness-manager-channel", "schema_version": 1, "channel": "stable", "serial": 1,
       "issued_at": "2026-09-24T00:00:00Z", "signing_key_id": "0" * 16,
       "harness": {"current": "1.1.0", "releases": [{
           "version": "1.1.0", "status": "current", "vivado": "2024.1",
           "identity": {"static_id": "0x72BB0A36"},
           "components": [
               {"name": "sd", "target": "mcc_sd", "url": "sd.zip", "sha256": "0" * 64, "size": 1},
               {"name": "os", "target": "ethernet", "kind": "os-slot", "url": "os.img",
                "sha256": "1" * 64, "size": 1},
               {"name": "legal", "target": "ethernet", "kind": "legal-info",
                "url": "legal.tar", "sha256": "2" * 64, "size": 1},
               {"name": "kit", "target": "host-kit", "kind": "rm-kit", "url": "kit.zip",
                "sha256": "3" * 64, "size": 1}]}]}}


def test_the_kit_and_the_platform_door_names_parse():
    rel = parse_channel(DOC).harness_release()
    kit = rel.component("kit")
    assert (kit.target, kit.kind, kit.static_id, kit.vivado) == (TARGET_HOST_KIT, "rm-kit",
                                                                 "0x72bb0a36", "2024.1")
    assert rel.component("os").fmt == "raw" and rel.component("legal").fmt == "raw"
    assert [c.name for c in rel.kits()] == ["kit"]
    assert LINUX_BUNDLE_TARGETS == {"mcc_sd": TARGET_MCC_SD, "ethernet": TARGET_ETHERNET}


def test_hms_old_door_names_are_read_as_the_platforms():
    doc = copy.deepcopy(DOC)
    doc["harness"]["releases"][0]["components"][0]["target"] = "mcc-sd"
    doc["harness"]["releases"][0]["components"][1]["target"] = "user-usd"
    rel = parse_channel(doc).harness_release()
    assert rel.component("sd").target == "mcc_sd" and rel.component("os").target == "ethernet"
    assert rel.by_target("mcc-sd") == rel.by_target("mcc_sd") == [rel.component("sd")]


@pytest.mark.parametrize("path,value,why", [
    (["components", 3, "target"], "host-store", "cannot go to target"),     # kit on host-store
    (["components", 3, "kind"], "overlays", "cannot go to target"),         # not a kit on host-kit
    (["components", 3, "format"], "raw", "is a zip"),
    (["components", 3, "static_id"], "0x3F1A560F", "not the release's static_id"),
    (["components", 1, "kind"], "sd", "cannot go to target"),               # ethernet: no SD
    (["components", 0, "target"], "mcc_sdx", "is not one of"),
])
def test_twin_a_contradicting_component_is_refused(path, value, why):
    doc = copy.deepcopy(DOC)
    obj = doc["harness"]["releases"][0]
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
    with pytest.raises(ChannelFormatError, match=why):
        parse_channel(doc)


# --- the release tool publishes the kit now (OTA-R's H13 front-end) -----------------------------


def test_the_release_tool_publishes_the_kit_and_the_client_finds_it(tmp_path):
    from tests.fakes.otar_release import write_bundle
    from tests.spikes.harness_dist_spike import S_ILA, U_ILA, bare_metal_mint
    from tools.release import signer as signer_mod
    from tools.release.cli import main
    from tools.release.common import Layout

    sk, pk = signer_mod.keygen_throwaway(tmp_path / "keys")
    mint = bare_metal_mint(tmp_path / "m", S_ILA, U_ILA, "d68dd0ed", ["windowed"], "0.11")
    bundle = write_bundle(tmp_path / "b", mint)
    (bundle / "kit").mkdir()
    (bundle / "kit" / f"mps3-kit-{S_ILA}.zip").write_bytes(mint.kit_zip)
    (bundle / "kit" / "kit.json").write_text(json.dumps({
        "schema": "hm-rm-kit", "schema_version": 1, "static_id": S_ILA,
        "vivado": {"release": "2024.1"}, "ip_class": "open", "access": "public",
        "source": {"dirty": False}}))
    lines: list[str] = []
    rc = main(["harness", "--out", str(tmp_path / "dist"), "--bundle", str(bundle),
               "--version", "1.1.0", "--signer", "python", "--secret-key", str(sk)],
              printer=lines.append)
    assert rc == 0, "\n".join(lines)
    assert not any("kit NOT published" in ln for ln in lines)
    from harness_manager.services.update import minisign, trust
    from harness_manager.services.update.trust import ROLE_RELEASE, TrustedKey, TrustStore

    store = TrustStore(pinned=(TrustedKey(minisign.PublicKey.from_text(pk.read_text()),
                                          ROLE_RELEASE, trust.CHANNELS, "otar"),))
    state = UpdateState(tmp_path / "client" / "update")
    client = ChannelClient(state, Downloader(state.cache, mirrors=()), store)
    kits = ChannelKits(client, client.downloader, channels=("beta",),
                       source=str(Layout(tmp_path / "dist").channel_file("mps3-harness", "beta")),
                       catalog="mps3-harness")
    [k] = kits.list(S_ILA)
    assert k.vivado == "2024.1" and k.asset.access == "public"
    assert kits.fetch(S_ILA).read_bytes() == mint.kit_zip


# --- wired into KIT-CORE's KitService (the CLI's and the daemon's) --------------------------------


@pytest.fixture
def real_kit(tmp_path):
    """KIT-CORE's fixture kit (its DCP's CRC-32 is 0x72BB0A36), zipped as the release asset."""
    from harness_manager.services.kit.service import HubSource, KitService
    from harness_manager.services.store import ContentStore
    from tests.fakes import kit_fakes as kf

    src = KitService(ContentStore(tmp_path / "a"), tmp_path / "wa", hub=HubSource(None))
    src.import_(kf.FIXTURE)
    return src.zip_to(src.require("0x72BB0A36"), tmp_path / "mps3-kit-0x72BB0A36.zip")


def wired(tmp_path, monkeypatch, zip_path, *, tamper: bool = False):
    from harness_manager.services.kit.service import HubSource, KitService
    from harness_manager.services.update import trust as trust_mod

    b = ChannelBuilder(tmp_path / "www", KEYS)
    comp = b.component("kit", "host-kit", AssetFile(zip_path.name, zip_path.read_bytes()),
                       kind="rm-kit", vivado="2024.1")
    b.add_harness("1.1.0", {"static_id": "0x72BB0A36"}, [comp])
    b.publish(serial=1)
    if tamper:
        f = tmp_path / "www" / "assets" / zip_path.name
        data = f.read_bytes()
        f.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    monkeypatch.setattr(trust_mod, "PINNED_KEYS", KEYS.trust().pinned)   # the test release key
    monkeypatch.setenv("HARNESS_MANAGER_UPDATE_SOURCE",
                       str(tmp_path / "www" / "channel" / "{channel}" / "channel.json"))
    return KitService.for_state_dir(tmp_path / "state", hub=HubSource(None))


def test_the_product_kit_service_fetches_from_the_signed_channel(tmp_path, monkeypatch, real_kit):
    kits = wired(tmp_path, monkeypatch, real_kit)
    assert kits.sources()[1] == {"name": "channel", "available": True, "reason": ""}
    kit, source = kits.fetch("0x72BB0A36")                       # cache miss -> channel
    assert source == "channel" and kit.static_id.lower() == "0x72bb0a36"
    assert kits.fetch("0x72BB0A36")[1] == "cache"                 # imported: cached now


def test_twin_a_tampered_channel_kit_is_refused_loudly(tmp_path, monkeypatch, real_kit):
    from harness_manager.core.errors import RefusedError

    kits = wired(tmp_path, monkeypatch, real_kit, tamper=True)
    with pytest.raises(RefusedError, match="fails its sha256 check"):
        kits.fetch("0x72BB0A36")
    assert kits.list() == []


def test_twin_no_kit_in_the_channel_falls_through_and_says_why(tmp_path, monkeypatch, real_kit):
    kits = wired(tmp_path, monkeypatch, real_kit)
    with pytest.raises(AbsentError) as exc:
        kits.fetch("0x3F1A560F")
    assert "channel: no signed channel lists a DUT build kit for static 0x3F1A560F" in \
        exc.value.hint
