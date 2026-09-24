"""T7: bundle preparation and the domain checks (part, USERID, static_id, usercode, CRC, SD rules).

Every check has a negative twin: the good release passes all of them, and each
variant breaks exactly one thing. Nothing here touches a board. The channel is
read from a local mirror directory (file://), so no server is needed.
"""

from __future__ import annotations

import pytest

from harness_manager.core.errors import IncompatibleError, RefusedError
from harness_manager.core.model import Check
from harness_manager.services.update.bitheader import BitHeaderError, build_bit, parse_bit_header
from harness_manager.services.update.bundle import PackOverlayHandler, prepare_release, safe_extract
from harness_manager.services.update.channel import ChannelClient
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.state import UpdateState
from tests.fakes.fake_channel import ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t7_bundles import USERCODE, Release, zip_bytes

KEYS = TestKeys()


def prepare(tmp_path, release: Release, *, names=None, token=None, server=None):
    b = ChannelBuilder(server.root if server else tmp_path / "mirror", KEYS)
    release.add_to(b, tmp_path / "art")
    b.publish(serial=1)
    state = UpdateState(tmp_path / "state" / "update")
    dl = Downloader(state.cache, token=token, mirrors=())
    src = server.source() if server else str(b.root / "channel" / "stable")
    v = ChannelClient(state, dl, KEYS.trust()).fetch("stable", src)
    rel = v.channel.harness_release()
    names = names or [c.name for c in rel.components]
    return prepare_release(rel, names, downloader=dl, base_url=v.url, work=state.work(rel.version),
                           part=v.channel.board.part, overlay_handler=PackOverlayHandler("mps3"))


def test_a_good_release_passes_every_check(tmp_path):
    p = prepare(tmp_path, Release("1.1.0"))
    assert p.refusal() is None
    assert all(c.check == Check.OK for c in p.checks), [c for c in p.checks if c.check != Check.OK]
    assert sorted(p.sd_files) == ["MB/HBI0309C/Nanosoc/nanosoc.bit", "MB/HBI0309C/Nanosoc/nanosoc.txt",
                                  "MB/HBI0309C/board.txt", "config.txt"]
    assert [d.name for d in p.overlay_dirs()] == ["synth", "synth2"]


def test_a_bitstream_for_another_part_is_incompatible(tmp_path):
    with pytest.raises(IncompatibleError, match="built for xcvu9p"):
        prepare(tmp_path, Release("1.1.0", bit_part="xcvu9p-flga2104-2L-e"))


def test_a_bitstream_whose_userid_is_not_the_release_usercode_is_incompatible(tmp_path):
    with pytest.raises(IncompatibleError, match="USERID"):
        prepare(tmp_path, Release("1.1.0", bit_usercode="0x12345678"))


def test_an_unstamped_bitstream_is_incompatible(tmp_path):
    with pytest.raises(IncompatibleError, match="0xffffffff"):
        prepare(tmp_path, Release("1.1.0", bit_usercode="0xFFFFFFFF"))


@pytest.mark.parametrize("extra,match", [
    ({"MB/HBI0309C/mbb_v141.ebf": b"BIOS"}, r"\.ebf"),
    ({"reboot.txt": b""}, "MCC command"),
    ({"notes/readme.txt": b"hi"}, "outside config.txt"),
    ({"MB/HBI0309B/board.txt": b"x"}, "board revision"),
])
def test_sd_rules_refuse_the_bundle(tmp_path, extra, match):
    with pytest.raises(RefusedError, match=match):
        prepare(tmp_path, Release("1.1.0", sd_extra=extra))


def test_overlays_keyed_to_another_shell_are_incompatible(tmp_path):
    with pytest.raises(IncompatibleError, match="static_id"):
        prepare(tmp_path, Release("1.1.0", overlay_static="0x0badcafe"))


def test_overlays_of_another_implementation_are_incompatible(tmp_path):
    # Same static_id, other implementation run: the July "wiped the FPGA twice" case.
    with pytest.raises(IncompatibleError, match="static_usercode"):
        prepare(tmp_path, Release("1.1.0", overlay_usercode="0x11111111"))


def test_overlays_without_a_usercode_are_refused_when_the_release_has_one(tmp_path):
    with pytest.raises(IncompatibleError, match="no static_usercode"):
        prepare(tmp_path, Release("1.1.0", overlay_usercode=None))


def test_a_corrupt_overlay_payload_fails_its_crc(tmp_path):
    with pytest.raises(RefusedError, match="length \\+ CRC"):
        prepare(tmp_path, Release("1.1.0", corrupt_overlay="synth"))


def test_a_private_component_without_a_token_is_skipped_not_fatal(tmp_path):
    # From a host, a private component needs the token (OTA-C: a LOCAL copy, file://, is
    # read as it is: nothing leaves the machine; the planner still skips Arm IP without one).
    with FakeChannelServer(tmp_path / "www") as srv:
        p = prepare(tmp_path, Release("1.1.0", private_overlays=True), server=srv)
    assert "overlays-aaa" in p.skipped and "GitHub token" in p.skipped["overlays-aaa"]
    assert "overlays-open" in p.parts


def test_twin_a_private_component_in_a_local_copy_needs_no_token(tmp_path):
    p = prepare(tmp_path, Release("1.1.0", private_overlays=True))
    assert not p.skipped and "overlays-aaa" in p.parts


def test_the_signed_per_file_list_is_enforced(tmp_path):
    b = ChannelBuilder(tmp_path / "mirror", KEYS)
    rel = Release("1.1.0", with_overlays=False)
    comps = rel.components(b, tmp_path / "art")
    comps[0]["files"] = {"config.txt": "00" * 32}
    b.add_harness(rel.version, rel.identity(), comps)
    b.publish(serial=1)
    state = UpdateState(tmp_path / "state" / "update")
    dl = Downloader(state.cache)
    v = ChannelClient(state, dl, KEYS.trust()).fetch("stable", str(b.root / "channel" / "stable"))
    with pytest.raises(RefusedError, match="file list"):
        prepare_release(v.channel.harness_release(), ["sd-HBI0309C"], downloader=dl,
                        base_url=v.url, work=state.work("1.1.0"), part="xcku115")


@pytest.mark.parametrize("name", ["../evil.txt", "/abs/evil.txt", "C:/evil.txt"])
def test_archive_escapes_are_refused(tmp_path, name):
    z = tmp_path / "bad.zip"
    z.write_bytes(zip_bytes({name: b"x"}))
    with pytest.raises(RefusedError):
        safe_extract(z, tmp_path / "out")
    # Nothing next to the archive either: no "out", no ".out.*.tmp" left behind, and no
    # "evil.txt" written beside "out" (Q1: "out" alone can never exist here, since
    # safe_extract renames its temp directory to it only after a clean extract).
    assert [p.name for p in tmp_path.iterdir()] == ["bad.zip"]


def test_case_duplicates_are_refused_for_a_fat_target(tmp_path):
    z = tmp_path / "dup.zip"
    z.write_bytes(zip_bytes({"config.txt": b"a", "CONFIG.TXT": b"b"}))
    with pytest.raises(RefusedError, match="twice"):
        safe_extract(z, tmp_path / "out")


def test_bit_header_round_trip():
    hdr = parse_bit_header(build_bit("shell", "xcku115-flvb2104-2-e", b"\x00" * 16, userid=USERCODE))
    assert hdr.part_matches("xcku115") and hdr.userid == USERCODE and hdr.stamped
    assert not hdr.part_matches("xcvu9p")


def test_not_a_bitstream():
    with pytest.raises(BitHeaderError):
        parse_bit_header(b"\x00" * 64)
