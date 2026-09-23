"""T7: channel.json v1 is validated strictly; unknown fields are ignored but kept."""

from __future__ import annotations

import copy

import pytest

from socharness.core.errors import IncompatibleError
from socharness.services.update.schema import ChannelFormatError, parse_channel
from socharness.services.update.version import at_least, compare, parse_version

SHA = "ab" * 32


def good() -> dict:
    return {
        "schema": "socharness-channel", "schema_version": 1, "channel": "stable", "serial": 3,
        "issued_at": "2026-09-23T12:00:00Z", "expires_at": "2027-01-01T00:00:00Z",
        "signing_key_id": "A1A1A1A1A1A1A1A1",
        "board": {"pack": "mps3", "part": "xcku115", "revisions": ["HBI0309C"]},
        "harness": {"current": "1.1.0", "releases": [{
            "version": "1.1.0", "status": "current",
            "identity": {"static_id": "0x3F1A560F", "usercode": "0xD46FCDCB", "harness": "1.1.0",
                         "impl": "bare-metal", "proto": "0.11", "features": ["windowed"]},
            "compat": {"min_app": "0.1.0", "board_revs": ["HBI0309C"], "mcc_fw_tested": ["1.3.2"],
                       "net_protocol": "0.11", "replaces_static_ids": ["0xA8C1C535"]},
            "rekey": False,
            "components": [
                {"name": "sd-HBI0309C", "target": "mcc-sd", "kind": "sd", "url": "https://x/sd.zip",
                 "sha256": SHA, "size": 10},
                {"name": "overlays-aaa", "target": "host-store", "kind": "overlays",
                 "url": "https://api.github.com/repos/o/r/releases/assets/1", "sha256": SHA,
                 "size": 10, "ip_class": "arm-aaa", "access": "github-token", "repo": "o/r"},
            ]}]},
        "app": {"current": "0.2.0", "releases": [{
            "version": "0.2.0", "status": "current", "requires_python": ">=3.10",
            "artifacts": [{"kind": "wheel", "name": "socharness-0.2.0-py3-none-any.whl",
                           "url": "https://x/socharness-0.2.0-py3-none-any.whl", "sha256": SHA,
                           "size": 5}]}]},
    }


def test_a_good_channel_parses():
    ch = parse_channel(good())
    rel = ch.harness_release()
    assert ch.serial == 3 and rel.version == "1.1.0" and rel.identity.static_id == "0x3f1a560f"
    assert rel.by_target("mcc-sd")[0].asset.sha256 == SHA
    assert rel.component("overlays-aaa").needs_token
    assert ch.app_release().wheel.name.endswith(".whl")


def test_unknown_fields_are_ignored_but_kept():
    doc = good()
    doc["future_field"] = {"x": 1}
    doc["harness"]["releases"][0]["provenance"] = {"commit": "abc"}
    doc["harness"]["releases"][0]["components"][0]["policy"] = "replace"
    ch = parse_channel(doc)
    assert ch.extra["future_field"] == {"x": 1} and ch.raw["future_field"] == {"x": 1}
    rel = ch.harness_release()
    assert rel.extra["provenance"] == {"commit": "abc"}
    assert rel.components[0].extra["policy"] == "replace"


def mutate(path: list, value) -> dict:
    doc = copy.deepcopy(good())
    obj = doc
    for key in path[:-1]:
        obj = obj[key]
    if value is KeyError:
        del obj[path[-1]]
    else:
        obj[path[-1]] = value
    return doc


R0 = ["harness", "releases", 0]
C0 = [*R0, "components", 0]


@pytest.mark.parametrize("path,value,match", [
    (["schema"], "other", "schema"),
    (["serial"], 0, "serial"),
    (["serial"], "3", "serial"),
    (["serial"], True, "serial"),
    (["channel"], KeyError, "channel"),
    (["issued_at"], "yesterday", "ISO-8601"),
    (["signing_key_id"], "xyz", "key id"),
    ([*R0, "version"], "one", "not a version"),
    ([*R0, "status"], KeyError, "status"),
    ([*R0, "status"], "live", "status"),
    ([*R0, "identity", "static_id"], "3F1A560F", "32-bit hex"),
    ([*R0, "identity", "impl"], "rtos", "impl"),
    ([*R0, "rekey"], "no", "true or false"),
    ([*R0, "components"], [], "non-empty"),
    ([*C0, "target"], "sd-card", "target"),
    ([*C0, "kind"], "overlays", "cannot go to target"),
    ([*C0, "sha256"], "abc", "sha256"),
    ([*C0, "size"], 0, "size"),
    ([*C0, "url"], "ftp://x/sd.zip", "https://"),
    ([*C0, "url"], "/etc/passwd", "absolute"),
    ([*C0, "access"], "secret", "access"),
    (["harness", "current"], "9.9.9", "names no release"),
])
def test_malformed_channels_are_refused(path, value, match):
    with pytest.raises(ChannelFormatError, match=match):
        parse_channel(mutate(path, value))


def test_arm_ip_marked_public_is_refused():
    doc = good()
    comp = doc["harness"]["releases"][0]["components"][1]
    comp["access"] = "public"
    with pytest.raises(ChannelFormatError, match="arm-aaa component must be"):
        parse_channel(doc)


def test_duplicate_component_names_are_refused():
    doc = good()
    comps = doc["harness"]["releases"][0]["components"]
    comps[1]["name"] = comps[0]["name"]
    with pytest.raises(ChannelFormatError, match="unique"):
        parse_channel(doc)


def test_withdrawn_release_cannot_be_current():
    with pytest.raises(ChannelFormatError, match="withdrawn"):
        parse_channel(mutate([*R0, "status"], "withdrawn"))


def test_a_newer_schema_version_asks_for_an_app_update():
    with pytest.raises(IncompatibleError, match="schema_version"):
        parse_channel(mutate(["schema_version"], 2))


def test_a_wheel_must_be_listed_exactly_once():
    doc = good()
    doc["app"]["releases"][0]["artifacts"] = [{"kind": "sdist", "url": "https://x/a.tar.gz",
                                               "sha256": SHA, "size": 1}]
    with pytest.raises(ChannelFormatError, match="exactly one wheel"):
        parse_channel(doc)


def test_expiry():
    ch = parse_channel(good())
    assert not ch.expired(1.0) and ch.expired(4_102_444_800.0)


# --- versions -------------------------------------------------------------------------


@pytest.mark.parametrize("a,b,expect", [
    ("1.0.0", "1.1.0", -1), ("1.10.0", "1.9.0", 1), ("1.0", "1.0.0", 0),
    ("1.1.0rc1", "1.1.0", -1), ("1.1.0-rc.2", "1.1.0rc1", 1), ("0.4.0.dev3", "0.4.0a1", -1),
    ("v2.0.0", "2.0.0", 0), ("1.0.0+local", "1.0.0", 0),
])
def test_version_order(a, b, expect):
    assert compare(a, b) == expect


@pytest.mark.parametrize("bad", ["", "one", "1.0.x", "1..0", None])
def test_bad_versions_are_refused(bad):
    with pytest.raises(ValueError):
        parse_version(bad)


def test_at_least():
    assert at_least("0.2.0", "0.1.0") and at_least("0.1.0", "") and not at_least("0.0.1", "0.1.0")


# --- the sample the hand-back gives harness Lane G (build_bundle.py / sign_and_publish) ------

H = "0" * 63
SAMPLE_CHANNEL = {
    "schema": "socharness-channel", "schema_version": 1,
    "channel": "stable", "serial": 12,
    "issued_at": "2026-10-12T10:00:00Z", "expires_at": "2027-04-12T00:00:00Z",
    "signing_key_id": "E7620F1842B4E81F",
    "board": {"pack": "mps3", "part": "xcku115", "revisions": ["HBI0309C"]},
    "harness": {"current": "2.0.0", "releases": [
        {"version": "2.0.0", "status": "current", "released_at": "2026-10-12T09:00:00Z",
         "notes_url": "https://github.com/SoC-Labs/mps3-platform-dist/releases/tag/harness-v2.0.0",
         "identity": {"static_id": "0x11C30003", "usercode": "0x6A7B8C9D", "harness": "2.0.0",
                      "impl": "linux", "proto": "0.12", "fw_sha": "a1b2c3d4",
                      "features": ["clcd", "clcd_kvm", "touch", "hwicap_fifo", "dut_egress",
                                   "jtag_server", "xvc_dbgbr", "stats", "log", "reboot",
                                   "touch_cal"]},
         "compat": {"min_app": "0.2.0", "board_revs": ["HBI0309C"], "mcc_fw_tested": ["1.3.2"],
                    "net_protocol": "0.12", "replaces_static_ids": ["0x72BB0A36"]},
         "rekey": True,
         "components": [
             {"name": "sd-HBI0309C", "target": "mcc-sd", "kind": "sd",
              "url": "https://github.com/SoC-Labs/mps3-platform-dist/releases/download/"
                     "harness-v2.0.0/mps3-harness-2.0.0-sd-HBI0309C.zip",
              "sha256": H + "1", "size": 1170000, "ip_class": "open", "access": "public",
              "files": {"config.txt": H + "2", "MB/HBI0309C/board.txt": H + "3",
                        "MB/HBI0309C/Nanosoc/nanosoc.txt": H + "4",
                        "MB/HBI0309C/Nanosoc/nanosoc.bit": H + "5"}},
             {"name": "os-2.0.0", "target": "user-usd", "kind": "os-slot", "format": "raw",
              "url": "https://github.com/SoC-Labs/mps3-platform-dist/releases/download/"
                     "harness-v2.0.0/mps3-harness-2.0.0-os.s0",
              "sha256": H + "6", "size": 22000000, "ip_class": "open"},
             {"name": "overlays-open", "target": "host-store", "kind": "overlays",
              "url": "https://github.com/SoC-Labs/mps3-platform-dist/releases/download/"
                     "harness-v2.0.0/mps3-harness-2.0.0-overlays-open.zip",
              "sha256": H + "7", "size": 2500000, "ip_class": "open"},
             {"name": "overlays-aaa", "target": "host-store", "kind": "overlays",
              "url": "https://api.github.com/repos/SoC-Labs/mps3-platform-dist-aaa/releases/"
                     "assets/123456789",
              "sha256": H + "8", "size": 1900000, "ip_class": "arm-aaa",
              "access": "github-token", "repo": "SoC-Labs/mps3-platform-dist-aaa"},
             {"name": "openocd", "target": "host-store", "kind": "openocd",
              "url": "https://github.com/SoC-Labs/mps3-platform-dist/releases/download/"
                     "harness-v2.0.0/mps3-harness-2.0.0-openocd.zip",
              "sha256": H + "9", "size": 40000, "ip_class": "open"}],
         "provenance": {"source_repo": "SoC-Labs/MPS3-NanoSoC-Verification-Platform",
                        "commit": "c855108", "dirty": False, "vivado": "2024.1"}},
        {"version": "1.0.0", "status": "superseded",
         "identity": {"static_id": "0x3F1A560F", "usercode": "0xD46FCDCB", "harness": "1.0.0",
                      "impl": "bare-metal", "fw_sha": "cb31b0f2",
                      "features": ["clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed"]},
         "compat": {"min_app": "0.1.0", "board_revs": ["HBI0309C"]},
         "rekey": False,
         "components": [
             {"name": "sd-HBI0309C", "target": "mcc-sd", "kind": "sd",
              "url": "https://github.com/SoC-Labs/mps3-platform-dist/releases/download/"
                     "harness-v1.0.0/mps3-harness-1.0.0-sd-HBI0309C.zip",
              "sha256": H + "a", "size": 1150000}]}]},
    "app": {"current": "0.2.0", "releases": [
        {"version": "0.2.0", "status": "current", "requires_python": ">=3.10",
         "min_harness": "1.0.0",
         "artifacts": [{"kind": "wheel", "name": "socharness-0.2.0-py3-none-any.whl",
                        "url": "https://github.com/SoC-Labs/mps3-platform-dist/releases/"
                               "download/app-v0.2.0/socharness-0.2.0-py3-none-any.whl",
                        "sha256": H + "b", "size": 412000}],
         "lock": {"name": "socharness-0.2.0-requirements.lock",
                  "url": "https://github.com/SoC-Labs/mps3-platform-dist/releases/download/"
                         "app-v0.2.0/socharness-0.2.0-requirements.lock",
                  "sha256": H + "c", "size": 9000}}]},
}


def test_the_sample_channel_in_the_hand_back_is_valid():
    ch = parse_channel(SAMPLE_CHANNEL)
    rel = ch.harness_release()
    assert rel.rekey and rel.identity.impl == "linux"
    assert {c.target for c in rel.components} == {"mcc-sd", "user-usd", "host-store"}
    assert rel.component("os-2.0.0").fmt == "raw" and rel.component("overlays-aaa").needs_token
    assert rel.extra["provenance"]["commit"] == "c855108"
    assert ch.harness_release("1.0.0").status == "superseded"
