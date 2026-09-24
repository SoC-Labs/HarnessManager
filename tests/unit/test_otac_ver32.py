"""OTA-C (H1): ``identity.ver32`` / ``usr_access`` in the channel, ``BoardIdentity.ver32`` on
the wire, and ``match_release`` / ``base_differs`` / the confirm using it when BOTH sides
have one. Every check has a negative twin. Pure, except one VirtualMps3 read (127.0.0.1).

Why ver32 matters (HARNESS-DIST §3.2, R4): once the firmware's VERSION is stamped with
the release tag at bake, two releases can share a firmware sha (a re-stamp) and differ
only in the packed HARNESS_VER32 the firmware reports.
"""

from __future__ import annotations

import copy

import pytest

from harness_manager.core.model import BoardIdentity
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.update.executor import confirm_wire_identity, journaled_release_runs
from harness_manager.services.update.planner import base_differs, match_release, ver32_match
from harness_manager.services.update.schema import (
    BoardSpec,
    Channel,
    ChannelFormatError,
    Compat,
    HarnessIdentity,
    HarnessRelease,
    parse_channel,
)
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.virtual_board import FIELDED_3F1A560F, VirtualMps3, ila_v011_profile

S, U, FW = "0x72bb0a36", "0xc8551081", "d68dd0ed"
V110, V111 = "0x01010000", "0x01010100"          # VERSION stamped 1.1.0 / 1.1.1 (R4)


def rel(version: str, *, ver32: str = "", fw_sha: str = FW, harness: str = "1.0.0") -> HarnessRelease:
    return HarnessRelease(version=version, status="superseded",
                          identity=HarnessIdentity(static_id=S, usercode=U, harness=harness,
                                                   fw_sha=fw_sha, ver32=ver32),
                          compat=Compat(), components=())


def chan(*rels: HarnessRelease) -> Channel:
    return Channel(channel="stable", serial=1, issued_at="2026-09-24T00:00:00Z",
                   signing_key_id="0" * 16, board=BoardSpec(pack="mps3"),
                   harness_current=rels[-1].version, harness=rels, app_current="", app=())


def board(*, ver32: str = "", sha: str = FW, harness: str = "1.0.0") -> BoardIdentity:
    return BoardIdentity(board_type="mps3", shell_id=S, usercode=U, harness_version=harness,
                         firmware_sha=sha, ver32=ver32)


RESTAMP = chan(rel("1.1.0", ver32=V110), rel("1.1.1", ver32=V111))


@pytest.mark.parametrize("want,have,expect", [
    (V110, V110, True), ("0x01010000", "0x1010000", True), (V110, V111, False),
    ("", V110, None), (V110, "", None), ("0x00000000", V110, None),   # 0: "not stamped"
])
def test_ver32_match(want, have, expect):
    assert ver32_match(want, have) is expect


def test_match_release_tells_a_restamp_apart_by_ver32():
    assert match_release(RESTAMP, board(ver32=V111)).version == "1.1.1"
    assert match_release(RESTAMP, board(ver32=V110)).version == "1.1.0"


def test_twin_without_a_ver32_on_the_board_a_restamp_is_unrecorded():
    assert match_release(RESTAMP, board()) is None           # one sha, two releases: no guess


def test_a_differing_ver32_never_fits_even_with_a_matching_sha():
    ch = chan(rel("1.1.1", ver32=V111))
    assert match_release(ch, board(ver32=V110)) is None
    assert match_release(ch, board(ver32=V111)).version == "1.1.1"   # twin


def test_ver32_is_decisive_over_a_tag_in_the_harness_string():
    ch = chan(rel("1.1.1", ver32=V111, fw_sha="", harness="1.1.1"))   # the tag, no sha
    assert match_release(ch, board(ver32=V111, sha="")).version == "1.1.1"
    assert match_release(ch, board(sha="")) is None           # twin: no ver32, "1.0.0" != tag


def test_base_differs_by_ver32():
    r = rel("1.1.1", ver32=V111)
    assert base_differs(r, board(ver32=V110)) is True        # a re-stamp is a new base
    assert base_differs(r, board(ver32=V111)) is False       # twin
    no_sha = rel("1.1.1", ver32=V111, fw_sha="", harness="1.1.1")
    assert base_differs(no_sha, board(ver32=V111, sha="")) is False
    assert base_differs(no_sha, board(sha="")) is True       # twin: only the string to go by


def test_confirm_uses_ver32():
    want = rel("1.1.1", ver32=V111, harness="1.1.1").identity    # tag in harness (P6 shape)
    ok, items = confirm_wire_identity(want, board(ver32=V111))
    assert ok and {i.name for i in items} >= {"ver32", "firmware sha", "harness version"}
    bad, items = confirm_wire_identity(want, board(ver32=V110))   # twin: the old stamp
    assert not bad and next(i for i in items if i.name == "ver32").check.value == "mismatch"


def test_the_journal_recognises_the_release_by_ver32():
    j = {"version": "1.1.1", "static_id": S,
         "identity": {"harness": "1.1.1", "fw_sha": "", "usercode": U, "impl": "", "ver32": V111}}
    assert journaled_release_runs(j, board(ver32=V111, sha=""))
    assert not journaled_release_runs(j, board(ver32=V110, sha=""))   # twin


# --- the schema ------------------------------------------------------------------------------


DOC = {"schema": "harness-manager-channel", "schema_version": 1, "channel": "stable", "serial": 1,
       "issued_at": "2026-09-24T00:00:00Z", "signing_key_id": "0" * 16,
       "harness": {"current": "1.1.1", "releases": [{
           "version": "1.1.1", "status": "current",
           "identity": {"static_id": "0x72BB0A36", "ver32": "0x01010100",
                        "usr_access": "0x01010100"},
           "components": [{"name": "sd", "target": "mcc_sd", "url": "sd.zip",
                           "sha256": "0" * 64, "size": 1}]}]}}


def test_identity_ver32_and_usr_access_are_parsed():
    ident = parse_channel(DOC).harness_release().identity
    assert ident.ver32 == "0x01010100" and ident.usr_access == "0x01010100"
    assert "ver32" not in ident.extra


@pytest.mark.parametrize("key", ["ver32", "usr_access"])
def test_twin_a_malformed_ver32_refuses_the_channel(key):
    doc = copy.deepcopy(DOC)
    doc["harness"]["releases"][0]["identity"][key] = "1.1.1"
    with pytest.raises(ChannelFormatError, match="32-bit hex"):
        parse_channel(doc)


# --- the wire: the MPS3 shell reports it ------------------------------------------------------


@pytest.mark.parametrize("profile,want", [(ila_v011_profile(), "0x01000000"),
                                          (FIELDED_3F1A560F, "")])   # twin: ver32 0 = unstamped
def test_the_mps3_identity_carries_ver32(tmp_path, profile, want):
    with VirtualMps3(tmp_path, profile) as vb:
        pack = Mps3Pack(console_ports=vb.console_ports)
        eng = Engine(EngineConfig(state_dir=tmp_path / "state"), packs={"mps3": pack})
        try:
            s = eng.open(vb.candidate(), note="otac ver32")
            assert s.identity().ver32 == want
        finally:
            eng.close_all()
