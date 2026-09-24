"""The DUT build kit: kit.json v1 (K1), the cache and its sources (K2), the MPS3 adapter (K3).

Board-free and Vivado-free: the fixture kit's DCP is a tiny zip whose CRC-32 is forged to
0x72BB0A36 (tests/fakes/kit_fakes.py). Every check has a negative twin.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
import zlib
from pathlib import Path

import pytest

from harness_manager.core.errors import (
    AbsentError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.model import BoardIdentity
from harness_manager.core.pack import KitCheck, kit_refusal
from harness_manager.services.kit import schema
from harness_manager.services.kit.schema import KitFormatError, load_kit_json, parse_kit
from harness_manager.services.kit.service import ChannelSource, HubSource, KitService
from harness_manager.services.store import ContentStore
from harness_manager.services.update.schema import Asset
from harness_manager_mps3 import kit as mkit
from tests.fakes import kit_fakes as kf


@pytest.fixture
def kits(tmp_path: Path) -> KitService:
    return KitService(ContentStore(tmp_path / "store"), tmp_path / "kits",
                      hub=HubSource(None))


def doc() -> dict:
    return json.loads((kf.FIXTURE / "kit.json").read_text())


def st(checks: list[KitCheck]) -> dict[str, str]:
    return {c.name: c.state for c in checks}


# --- K1: the fixture and kit.json --------------------------------------------------------------------


def test_the_committed_fixture_is_what_the_builder_makes(tmp_path):
    made = kf.build_fixture(tmp_path / "k")
    for rel in ("kit.json", "static/static_routed_locked.dcp", "static/static_stamp.json"):
        assert (made / rel).read_bytes() == (kf.FIXTURE / rel).read_bytes(), rel


def test_the_fixture_dcp_crc_is_its_static_id_and_a_forged_twin_is_not():
    dcp = kf.FIXTURE / "static" / "static_routed_locked.dcp"
    assert zlib.crc32(dcp.read_bytes()) == 0x72BB0A36
    assert zlib.crc32(kf.fake_dcp("0x3F1A560F")) == 0x3F1A560F
    assert mkit.read_dcp_xml(dcp) == {"checkpoint_version": "22", "build": "5076996",
                                      "release": "2024.1", "part": "xcku115-flvb1760-1-c",
                                      "rp_inst": "u_rp_dut"}


def test_kit_json_v1_parses_with_every_field():
    m = load_kit_json(kf.FIXTURE / "kit.json")
    assert m.kit_id == "mps3/0x72BB0A36/vivado-2024.1"
    assert m.vivado.major_minor == "2024.1" and m.vivado.build == 5076996
    assert m.rp.clr_max == 262144 and m.rp.frames["idcode"] == "0x0390D093"
    assert m.access == "public" and "no Arm IP" in m.licence_note
    assert m.locked_static.crc32 == "0x72BB0A36"
    # round trip: to_json parses back to the same manifest
    assert parse_kit(json.loads(m.dumps())) == m


def test_kit_json_keeps_unknown_fields_and_refuses_a_newer_schema():
    d = doc() | {"targets": ["mcc_sd", "ethernet"]}
    assert parse_kit(d).extra == {"targets": ["mcc_sd", "ethernet"]}
    with pytest.raises(KitFormatError, match="newer kit format"):
        parse_kit(doc() | {"schema_version": 2})


@pytest.mark.parametrize("mutate, match", [
    (lambda d: d.update(schema="hm-overlay"), "not a build kit"),
    (lambda d: d["files"][0].update(crc32="0x3F1A560F"), "is not the kit's static_id"),
    (lambda d: d["files"][0].pop("crc32"), "needs its crc32"),
    (lambda d: d["files"][0].update(role="other"), "exactly one file"),
    (lambda d: d["files"][0].update(path="../escape.dcp"), "no '..'"),
    (lambda d: d["files"][0].update(path="C:/x.dcp"), "drive letter"),
    (lambda d: d["vivado"].update(release="latest"), "not a Vivado release"),
    (lambda d: d.update(access="anyone"), "access"),
    (lambda d: d.update(ip_class="arm-aaa"), "never public"),
    (lambda d: d.update(pr_verify_ref="static/other.dcp"), "not one of the kit's files"),
    (lambda d: d["rp"].pop("bits"), "bits"),
])
def test_kit_json_refusals(mutate, match):
    d = doc()
    mutate(d)
    with pytest.raises(KitFormatError, match=match):
        parse_kit(d)
    parse_kit(doc())                                      # the twin parses


def test_release_major_minor():
    assert schema.release_major_minor("2024.1.2") == "2024.1"
    assert schema.release_major_minor("2026.1") == "2026.1"
    assert schema.release_major_minor("v2024") == ""


# --- K2: import, cache, export, verify ---------------------------------------------------------------


def test_import_a_kit_dir_caches_it_and_export_writes_a_plain_dir(kits, tmp_path):
    res = kits.import_(kf.FIXTURE)
    assert st(res.checks) == {"files": "ok", "static_id": "ok"}
    kit = kits.require("0x72bb0a36")                     # any spelling of the id
    assert kit.manifest.kit_id == "mps3/0x72BB0A36/vivado-2024.1" and kit.source == "path"
    assert [k.static_id for k in kits.list()] == ["0x72BB0A36"]
    out = tmp_path / "exported"
    written = kits.export(kit, out)
    assert {p.relative_to(out).as_posix() for p in written} == {
        "kit.json", "static/static_routed_locked.dcp", "static/static_stamp.json"}
    manifest, checks = kits.verify_dir(out)
    assert st(checks) == {"files": "ok", "static_id": "ok"}
    # the same kit again: no second row
    assert kits.import_(kf.FIXTURE).already
    assert len(kits.list()) == 1


def test_a_corrupt_dcp_is_refused_on_import_and_nothing_is_cached(kits, tmp_path):
    bad = shutil.copytree(kf.FIXTURE, tmp_path / "bad")
    dcp = bad / "static" / "static_routed_locked.dcp"
    data = bytearray(dcp.read_bytes())
    data[10] ^= 0xFF
    dcp.write_bytes(bytes(data))
    with pytest.raises(RefusedError, match="sha256"):
        kits.import_(bad)
    assert kits.list() == []


def test_a_dcp_that_is_not_the_static_fails_the_crc_check_even_with_matching_sha(tmp_path):
    # kit.json re-hashed around a DCP of ANOTHER static: the sha256s agree, the CRC does not
    root = kf.build_fixture(tmp_path / "k")
    (root / "static" / "static_routed_locked.dcp").write_bytes(kf.fake_dcp("0x3F1A560F"))
    d = kf.kit_doc(root)
    d["files"][0]["crc32"] = "0x72BB0A36"                 # the lie
    m = parse_kit(d)
    from harness_manager.services.kit.service import verify_dir_files

    checks = verify_dir_files(m, root)
    assert st(checks) == {"files": "ok", "static_id": "mismatch"}
    assert "0x3F1A560F" in next(c.detail for c in checks if c.name == "static_id")


def test_verify_dir_notices_a_file_changed_after_export(kits, tmp_path):
    kits.import_(kf.FIXTURE)
    out = tmp_path / "exported"
    kits.export(kits.require("0x72BB0A36"), out)
    (out / "static" / "static_stamp.json").write_text("{}\n")
    _, checks = kits.verify_dir(out)
    assert st(checks)["files"] == "mismatch"


def test_verify_cached_rehashes_the_blobs(kits):
    kits.import_(kf.FIXTURE)
    kit = kits.require("0x72BB0A36")
    assert st(kits.verify_cached(kit)) == {"files": "ok", "static_id": "ok"}
    blob = kit.blob("locked_static")
    blob.chmod(0o644)
    blob.write_bytes(b"damaged")
    assert st(kits.verify_cached(kit))["files"] == "mismatch"


def test_export_refuses_a_dir_holding_another_statics_kit(kits, tmp_path):
    kits.import_(kf.FIXTURE)
    other = kf.build_fixture(tmp_path / "other", "0x3F1A560F")
    with pytest.raises(RefusedError, match="0x3F1A560F"):
        kits.export(kits.require("0x72BB0A36"), other)


def test_import_a_kit_zip_and_the_zip_round_trips(kits, tmp_path):
    kits.import_(kf.FIXTURE)
    z = kits.zip_to(kits.require("0x72BB0A36"), tmp_path / "kit.zip")
    with zipfile.ZipFile(z) as zf:
        assert "0x72BB0A36/kit.json" in zf.namelist()
    fresh = KitService(ContentStore(tmp_path / "store2"), tmp_path / "kits2", hub=HubSource(None))
    res = fresh.import_(z, source="zip")
    assert res.kit.static_id == "0x72BB0A36"
    junk = tmp_path / "junk.zip"
    with zipfile.ZipFile(junk, "w") as zf:
        zf.writestr("readme.txt", "no kit here")
    with pytest.raises(KitFormatError, match="no kit.json"):
        fresh.import_(junk)


def test_import_of_a_missing_path_or_a_plain_file(kits, tmp_path):
    with pytest.raises(AbsentError):
        kits.import_(tmp_path / "nope")
    f = tmp_path / "x.txt"
    f.write_text("x")
    with pytest.raises(UsageError):
        kits.import_(f)


# --- K2: kit import fielded/<sid> (loose files, through the MPS3 adapter) -----------------------------


def test_kit_import_of_a_fielded_dir_writes_the_kit_json(kits, tmp_path):
    d = kf.fielded_dir(tmp_path / "fielded" / "0x72BB0A36")
    res = kits.import_(d, source="fielded")
    m = res.kit.manifest
    assert m.static_id == "0x72BB0A36" and m.static_usercode == "0xC8551081"
    assert m.vivado.release == "2024.1" and m.vivado.build == 5076996 and \
        m.vivado.checkpoint_version == 22                 # from the DCP's own dcp.xml
    assert (m.rp.inst, m.rp.pblock, m.rp.ports, m.rp.bits, m.rp.clr_max) == \
        ("u_rp_dut", "pblock_rp_dut", 47, 148, 262144)
    assert m.rp.frames["partial"]["by_block"]["0"]["columns"] == [100, 150]   # from the pair
    assert {f.path for f in m.files} == {"static/static_routed_locked.dcp",
                                         "static/static_stamp.json", "static/mint.json",
                                         "static/static_id.txt"}   # only those; not the .bit
    assert m.access == "public" and m.licence_note == schema.DEFAULT_LICENCE_NOTE
    assert m.source["repo_sha"].startswith("c855108")


def test_a_fielded_dir_whose_static_id_txt_lies_is_refused(kits, tmp_path):
    d = kf.fielded_dir(tmp_path / "f", static_id_txt="0x3F1A560F")
    with pytest.raises(RefusedError, match="static_id.txt says 0x3F1A560F"):
        kits.import_(d)
    assert kits.list() == []


def test_a_fielded_dir_of_a_static_the_pin_model_lacks_is_unavailable(kits, tmp_path):
    d = kf.fielded_dir(tmp_path / "f", "0x3F1A560F")
    with pytest.raises(UnavailableError, match="partition of 0x3F1A560F is not known"):
        kits.import_(d)


def test_a_dir_that_is_no_kit_at_all(kits, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(UsageError, match="is not a kit"):
        kits.import_(tmp_path / "empty")


# --- K2: sources ---------------------------------------------------------------------------------


def test_fetch_prefers_the_cache(kits):
    kits.import_(kf.FIXTURE)
    kit, src = kits.fetch("0x72BB0A36")
    assert src == "cache" and kit.static_id == "0x72BB0A36"


def test_fetch_from_the_hub_archive_path_then_the_cache(tmp_path):
    hub = tmp_path / "mints"
    kf.fielded_dir(hub / "0x72BB0A36")
    kits = KitService(ContentStore(tmp_path / "s"), tmp_path / "w", hub=HubSource(hub))
    kit, src = kits.fetch("0x72bb0a36")
    assert src == "hub" and kit.source.startswith("hub:")
    assert kits.fetch("0x72BB0A36")[1] == "cache"


def test_the_hub_prefers_a_packed_kit_dir(tmp_path):
    hub = tmp_path / "mints"
    kf.build_fixture(hub / "0x72BB0A36" / "kit")
    kits = KitService(ContentStore(tmp_path / "s"), tmp_path / "w", hub=HubSource(hub))
    kit, _ = kits.fetch("0x72BB0A36", source="hub")
    assert kit.manifest.source == {"fixture": True}          # the packed kit.json, not loose


def test_fetch_with_no_source_says_what_it_tried(kits):
    with pytest.raises(AbsentError) as e:
        kits.fetch("0x72BB0A36")
    assert "cache: not cached" in e.value.hint and "OTA-C" in e.value.hint
    assert "HARNESS_MANAGER_KIT_HUB_DIR" in e.value.hint


def test_fetch_refuses_a_source_holding_another_statics_kit(kits, tmp_path):
    other = kf.build_fixture(tmp_path / "other", "0x3F1A560F")
    with pytest.raises(RefusedError, match="not 0x72BB0A36"):
        kits.fetch("0x72BB0A36", source=str(other))


def test_the_channel_seam_downloads_through_the_downloader(tmp_path):
    # OTA-C wires resolve(); here a file:// asset stands in for the release asset.
    src = KitService(ContentStore(tmp_path / "a"), tmp_path / "wa", hub=HubSource(None))
    src.import_(kf.FIXTURE)
    z = src.zip_to(src.require("0x72BB0A36"), tmp_path / "mps3-kit-0x72BB0A36.zip")
    data = z.read_bytes()
    asset = Asset(name=z.name, url=z.resolve().as_uri(), sha256=hashlib.sha256(data).hexdigest(),
                  size=len(data))
    asked = []

    def resolve(sid):
        asked.append(sid)
        return asset if sid == "0x72BB0A36" else None

    kits = KitService(ContentStore(tmp_path / "b"), tmp_path / "wb",
                      channel=ChannelSource(resolve), hub=HubSource(None))
    assert kits.sources()[1] == {"name": "channel", "available": True, "reason": ""}
    kit, src_name = kits.fetch("0x72BB0A36", source="channel")
    assert src_name == "channel" and kit.source == "channel" and asked == ["0x72BB0A36"]
    # the twin: a signed sha256 that the bytes do not match is refused, nothing cached
    bad = Asset(name="x.zip", url=asset.url, sha256="0" * 64, size=asset.size)
    kits2 = KitService(ContentStore(tmp_path / "c"), tmp_path / "wc",
                       channel=ChannelSource(lambda sid: bad), hub=HubSource(None))
    with pytest.raises(RefusedError, match="sha256"):
        kits2.fetch("0x72BB0A36", source="channel")
    assert kits2.list() == []


def test_the_channel_seam_is_unavailable_until_ota_c(kits):
    ch = kits.sources()[1]
    assert ch["name"] == "channel" and not ch["available"] and "OTA-C" in ch["reason"]


# --- K3: the MPS3 adapter against the live static ----------------------------------------------------


def ident(shell_id="0x72bb0a36", usercode="0xc8551081") -> BoardIdentity:
    return BoardIdentity(board_type="mps3", shell_id=shell_id, usercode=usercode)


def test_the_kit_matches_the_board_it_was_minted_for():
    m = load_kit_json(kf.FIXTURE / "kit.json")
    a = mkit.make_kit_adapter()
    assert st(a.check_kit(m, ident())) == {"shell_id": "ok", "usercode": "ok", "part": "ok",
                                          "boundary": "ok"}


def test_a_board_on_another_static_is_an_identity_mismatch():
    m = load_kit_json(kf.FIXTURE / "kit.json")
    checks = mkit.make_kit_adapter().check_kit(m, ident(shell_id="0x3f1a560f"))
    assert st(checks)["shell_id"] == "mismatch"
    assert isinstance(kit_refusal(checks, "the kit"), IncompatibleError)      # exit 14


def test_another_implementation_run_is_caught_by_the_usercode():
    m = load_kit_json(kf.FIXTURE / "kit.json")
    checks = mkit.make_kit_adapter().check_kit(m, ident(usercode="0xdeadbeef"))
    assert st(checks)["usercode"] == "mismatch" and st(checks)["shell_id"] == "ok"
    assert isinstance(kit_refusal(checks, "the kit"), IncompatibleError)


def test_no_usercode_from_the_board_is_unchecked_and_does_not_block():
    m = load_kit_json(kf.FIXTURE / "kit.json")
    checks = mkit.make_kit_adapter().check_kit(m, ident(usercode=""))
    assert st(checks)["usercode"] == "unchecked"
    assert kit_refusal(checks, "the kit") is None
    no_board = mkit.make_kit_adapter().check_kit(m, None)
    assert st(no_board)["shell_id"] == "unchecked"


def test_a_kit_for_another_part_or_boundary_is_refused():
    d = doc()
    d["part"] = "xcvu9p-flga2104-2-i"
    d["rp"]["bits"] = 136
    checks = mkit.make_kit_adapter().check_kit(parse_kit(d), ident())
    assert st(checks)["part"] == "mismatch" and st(checks)["boundary"] == "mismatch"
    assert isinstance(kit_refusal(checks, "the kit"), RefusedError)


def test_build_profile_from_the_kit_and_from_the_pin_model():
    a = mkit.make_kit_adapter()
    m = load_kit_json(kf.FIXTURE / "kit.json")
    p = a.build_profile("0x72BB0A36", m)
    assert (p.source, p.vivado, p.kit_id, p.boundary_bits, p.clr_max) == \
        ("kit", "2024.1", "mps3/0x72BB0A36/vivado-2024.1", 148, 262144)
    q = a.build_profile("0x72bb0a36")
    assert (q.source, q.vivado, q.rp_inst, q.rp_pblock, q.boundary_ports) == \
        ("pack", "", "u_rp_dut", "pblock_rp_dut", 47)
    assert a.build_profile("0x3F1A560F") is None          # the pack knows nothing of it


# --- K8: user rm_ids ---------------------------------------------------------------------------------


def test_the_proposed_rm_id_is_a_stable_user_id():
    a = mkit.make_kit_adapter()
    taken = dict(mkit.KNOWN_DESIGNS)
    rm = a.propose_rm_id("spike_rm", taken)
    assert rm == a.propose_rm_id("spike_rm", taken)       # the same name, the same proposal
    assert 0x8000 <= rm & 0xFFFF <= 0xFFFF and rm >> 16 == 0x0100     # v1.0
    assert st(a.rm_id_checks(rm, "spike_rm", taken)) == {"rm_id_clash": "ok"}
    # a clash steps past the taken id
    taken[rm & 0xFFFF] = "someone_else"
    other = a.propose_rm_id("spike_rm", taken)
    assert other != rm and 0x8000 <= other & 0xFFFF


def test_an_rm_id_clash_warns_and_its_twin_does_not():
    a = mkit.make_kit_adapter()
    taken = {0x80F0: "counter_demo", **mkit.KNOWN_DESIGNS}
    clash = st(a.rm_id_checks("0x010080F0", "spike_rm", taken))
    assert clash == {"rm_id_clash": "warning"}            # a warning, never a refusal
    assert st(a.rm_id_checks("0x010080F0", "counter_demo", taken)) == {"rm_id_clash": "ok"}
    platform = st(a.rm_id_checks("0x01000001", "mine", taken))
    assert platform == {"rm_id_range": "warning", "rm_id_clash": "warning"}   # nanosoc's id
    assert st(a.rm_id_checks("0x00000000", "mine", taken)) == {"rm_id": "mismatch"}


def test_taken_designs_reads_the_overlay_store(tmp_path):
    store = ContentStore(tmp_path / "store")
    a = mkit.make_kit_adapter()
    assert 0x80F0 not in a.taken_designs(store)
    receipt = kf.passed_build(tmp_path / "b")
    from harness_manager.services.kit.schema import load_receipt

    d = a.pack_receipt(load_receipt(receipt), tmp_path / "ov")
    a.import_overlay(store, d)
    assert a.taken_designs(store)[0x80F0] == "spike_rm"
