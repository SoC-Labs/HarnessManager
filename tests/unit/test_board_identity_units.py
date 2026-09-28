"""BOARD-ID's pure rules (``services.board_identity``): each with its negative twin."""

from __future__ import annotations

import time

import pytest

from harness_manager.core.errors import UsageError
from harness_manager.services import board_identity as BI

HUB2 = {"target": "mps3_02_pl", "board": "mps3_02", "label": "MPS3-02",
        "board_ip": "192.168.11.101", "prefix": 24, "board_mac": "02:00:00:00:02:fe",
        "hostname": "mps3-02-pl", "discovered_mac": "", "mac_suspect": ""}
HUB1 = {"target": "mps3_01_pl", "board": "mps3_01", "label": "MPS3-01",
        "board_ip": "192.168.10.101", "board_mac": "00:e0:4c:46:dc:f8",
        "discovered_mac": "00:e0:4c:46:dc:f8"}
HUB1["mac_suspect"] = BI.hub_mac_suspect(HUB1)
#: Board 2 reporting board 1's identity. V7-ALIGN: on the shipped image a label with source
#: "default" is always ``MPS3`` (never ``MPS3-01``), so a board with board 1's LABEL has it from
#: a bake (here: board 1's stage0 bake); the MAC is still the image default.
AS_BOARD1 = {"label": "MPS3-01", "ip": "192.168.10.101/24", "mac": "02:00:00:4d:50:53",
             "source": {"label": "stage0", "ip": "stage0", "mac": "default"}}
AS_BOARD2 = {"label": "MPS3-02", "ip": "192.168.11.101/24", "mac": "02:00:00:00:02:fe",
             "source": {"label": "stage0", "ip": "stage0", "mac": "stage0"}}
OTHERS = [{**HUB1, "kind": "hub", "who": "mps3_01_pl", "ip": HUB1["board_ip"],
           "mac": HUB1["board_mac"]},
          {"kind": "board", "who": "mps3-01", "label": "MPS3-01", "ip": "192.168.10.101/24",
           "mac": "02:00:00:4d:50:53"}]


def test_board2_with_board1s_identity_is_a_clash_on_label_ip_and_mac():
    f = BI.compare(AS_BOARD1, HUB2, OTHERS)
    assert BI.summarise(f, AS_BOARD1) == "clash"
    clash = {(x["field"], x["other"]) for x in f if x["kind"] == "clash"}
    assert clash == {("ip", "mps3_01_pl"), ("label", "mps3_01_pl"), ("mac", "mps3-01"),
                     ("ip", "mps3-01"), ("label", "mps3-01")}
    assert BI.level_of("clash") == "err"


def test_twin_a_board_that_matches_its_hub_entry_has_no_findings():
    f = BI.compare(AS_BOARD2, HUB2, OTHERS)
    assert f == [] and BI.summarise(f, AS_BOARD2) == "ok" and BI.level_of("ok") == "ok"


def test_the_image_default_is_identity_not_set_and_a_stage0_value_is_not():
    assert BI.summarise(BI.compare(AS_BOARD1, None, []), AS_BOARD1) == "unset"
    old = {"label": "", "ip": "192.168.10.101", "mac": "02:00:00:4d:50:53"}      # no source
    assert [x["kind"] for x in BI.compare(old, None, [])] == ["unset"]
    assert BI.compare({**AS_BOARD2, "source": {"mac": "stage0"}}, None, []) == []


def test_a_hub_mac_that_is_the_hubs_adapter_is_noted_never_compared_nor_proposed():
    assert "hub's own USB bus" in HUB1["mac_suspect"]
    f = BI.compare(AS_BOARD2, HUB1, [])
    assert [x["kind"] for x in f if x["field"] == "mac"] == ["hub_suspect"]
    want, notes = BI.want_from_hub(HUB1)
    assert "mac" not in want and "not used" in notes[0]
    # twin: a harness-like (locally administered) hub MAC is fine and proposed
    assert BI.hub_mac_suspect(HUB2) == "" and BI.want_from_hub(HUB2)[0]["mac"] == \
        "02:00:00:00:02:fe"
    vendor = {"board_mac": "00:e0:4c:00:00:01"}                    # no discovered_mac
    assert "vendor" in BI.hub_mac_suspect(vendor)


def test_the_plan_from_the_hub_and_its_phrase():
    plan = BI.plan_fix("mps3@x", AS_BOARD1, HUB2, from_hub=True)
    assert {c["field"]: c["to"] for c in plan["changes"]} == {
        "label": "MPS3-02", "ip": "192.168.11.101/24", "mac": "02:00:00:00:02:fe"}
    assert plan["phrase"] == "MPS3-02"
    # twin: nothing to change
    assert BI.plan_fix("mps3@x", AS_BOARD2, HUB2, from_hub=True)["changes"] == []
    # no label anywhere: the phrase names the board
    assert BI.plan_fix("mps3@x", {"mac": "02:00:00:4d:50:53"}, None,
                       want={"mac": "02:00:00:00:02:fe"})["phrase"] == "IDENTITY mps3@x"


@pytest.mark.parametrize("field,value,words", [
    ("mac", "01:00:5e:00:00:01", "unicast"), ("mac", "00:00:00:00:00:00", "unicast"),
    ("label", "X" * 24, "LCD row"), ("ip", "300.1.1.1", "IPv4"), ("hostname", "Bad_Name", "host"),
    # V7-ALIGN: the shipped rules (identity_core.c)
    ("label", "X" * 20, "1-19"), ("label", "mps3-02", "A-Z"), ("label", "MPS3 02", "LCD row"),
    ("hostname", "-a", "RFC 1123"), ("hostname", "a." + "b" * 62, "63"),
    ("ip", "10.0.0.0/8", "network address"), ("ip", "1.2.3.4/31", "prefix"),
    ("ip", "192.168.11.255", "broadcast"), ("ip", "127.0.0.2", "usable"),
    ("ip", "224.0.0.1", "usable"), ("ip", "10.1.1.1/7", "prefix"),
])
def test_values_the_board_would_refuse_are_usage_errors(field, value, words):
    with pytest.raises(UsageError, match=words):
        BI.validate_want({field: value})


def test_twin_good_values_are_normalised():
    assert BI.validate_want({"mac": "02-00-00-00-02-FE", "ip": "192.168.11.101",
                             "label": "MPS3-02", "hostname": "mps3-02"}) == {
        "mac": "02:00:00:00:02:fe", "ip": "192.168.11.101/24", "label": "MPS3-02",
        "hostname": "mps3-02"}
    assert BI.wire_mac("02:00:00:00:02:FE") == "0200000002fe"


def test_the_same_board_under_another_id_is_not_another_board(tmp_path):
    seen = BI.SeenIdentities(tmp_path)
    now = time.time()
    seen.update("mps3@a", label="MPS3-01", mac="02:00:00:4d:50:53", target="mps3_01_pl",
                address="a:6900", at=now)
    seen.update("mps3@b", label="MPS3-01", mac="02:00:00:4d:50:53", target="", address="b:6900",
                at=now - 30 * 24 * 3600)
    assert [o["board_id"] for o in seen.others("mps3@c")] == ["mps3@a"]       # b is stale
    assert seen.others("mps3@c", target="mps3_01_pl") == []                   # same target
    assert seen.others("mps3@c", address="a:6900") == []                      # same address
    assert seen.others("mps3@a") == []                                        # itself


# --- V7-ALIGN: the shipped contract (net-protocol v0.16, platform 18622e5) ----------------------


def test_v7_twin_the_shipped_rules_take_what_the_board_takes():
    assert BI.LABEL_MAX == 19 and BI.LABEL_BAKE_MAX == 8
    assert BI.validate_want({"label": "X" * 19})["label"] == "X" * 19
    assert BI.validate_want({"hostname": "a.b-c"}) == {"hostname": "a.b-c"}      # dots, as RFC 1123
    assert BI.validate_want({"hostname": "Mps3-02"}) == {"hostname": "Mps3-02"}  # any case
    assert BI.validate_want({"ip": "10.0.0.1"}) == {"ip": "10.0.0.1/24"}
    assert BI.validate_want({"ip": "10.0.0.1/8"}) == {"ip": "10.0.0.1/8"}
    assert BI.validate_want({"ip": "192.168.11.254/24"}) == {"ip": "192.168.11.254/24"}


def test_v7_an_empty_string_drops_the_key_and_none_is_not_given():
    """The board: ``""`` drops that key from the override. HM keeps it (``DROP``)."""
    assert BI.validate_want({"hostname": "", "label": "MPS3-02"}) == {"hostname": BI.DROP,
                                                                      "label": "MPS3-02"}
    # twin: None is "not given", as before
    assert BI.validate_want({"hostname": None, "label": "MPS3-02"}) == {"label": "MPS3-02"}


def test_v7_a_drop_is_a_change_only_when_the_override_holds_the_key():
    rep = {**AS_BOARD2, "hostname": "bench", "source": {**AS_BOARD2["source"],
                                                       "hostname": "override"},
           "override": {"hostname": "bench"}}
    plan = BI.plan_fix("mps3@x", rep, None, want={"hostname": ""})
    assert plan["changes"] == [{"field": "hostname", "from": "bench", "to": "", "drop": True}]
    assert plan["want"] == {"hostname": ""} and "dropped" in BI.change_text(plan["changes"][0])
    # twin: nothing in the override to drop: no change, nothing sent
    plan = BI.plan_fix("mps3@x", {**rep, "override": None}, None, want={"hostname": ""})
    assert plan["changes"] == [] and plan["want"] == {}


def test_v7_dropping_the_label_asks_for_the_board_phrase_not_the_old_label():
    rep = {**AS_BOARD2, "override": {"label": "MPS3-02"}}
    assert BI.plan_fix("mps3@x", rep, None, want={"label": ""})["phrase"] == "IDENTITY mps3@x"
    # twin: setting a label asks for the new label
    assert BI.plan_fix("mps3@x", rep, None, want={"label": "BENCH-2"})["phrase"] == "BENCH-2"


def test_v7_a_drop_is_verified_by_the_field_no_longer_coming_from_the_override():
    plan = {"want": {"hostname": ""}}
    after = {**AS_BOARD2, "hostname": "mps3-02", "override": None, "pending": None,
             "source": {**AS_BOARD2["source"], "hostname": "label"}}
    assert BI._verified(plan, after, clear=False) == (True, [])
    # twin: still the override's after the reboot
    still = {**after, "hostname": "bench", "override": {"hostname": "bench"},
             "source": {**after["source"], "hostname": "override"}}
    assert BI._verified(plan, still, clear=False) == (False, ["hostname"])


#: Board 2 on rc2_v7 before its identity bake is fielded: the generic label, the old MAC, its
#: IP already from stage0 (.11.101).
BOARD2_TONIGHT = {"label": "MPS3", "hostname": "mps3", "ip": "192.168.11.101/24",
                  "mac": "02:00:00:4d:50:53",
                  "source": {"label": "default", "hostname": "label", "ip": "stage0",
                             "mac": "default"}}


def seen_board1(**kw):
    return {"kind": "board", "who": "mps3-01", "label": "MPS3", "label_source": "default",
            "ip": "192.168.10.101/24", "mac": "02:00:00:4d:50:53", **kw}


def test_v7_board2_default_label_is_identity_not_set_never_a_label_clash():
    f = BI.compare(BOARD2_TONIGHT, HUB2, [seen_board1()])
    clashes = {(x["field"], x["other"]) for x in f if x["kind"] == "clash"}
    assert clashes == {("mac", "mps3-01")}, "a duplicate MAC is still a real clash today"
    unset = [x for x in f if x["kind"] == "unset"]
    assert unset and unset[0]["text"].startswith("identity not set (default label, MAC)")
    # board 1 after tonight's bake: its own label; still the old MAC
    f = BI.compare(BOARD2_TONIGHT, HUB2, [seen_board1(label="MPS3-01", label_source="stage0")])
    assert {(x["field"]) for x in f if x["kind"] == "clash"} == {"mac"}


def test_v7_a_default_label_is_not_also_differs_from_the_hub():
    """"identity not set (default label)" says it: no "differs" for the label. The MAC that is
    not the hub's still differs, and the duplicate MAC is still a clash."""
    f = BI.compare(BOARD2_TONIGHT, HUB2, [seen_board1()])
    differs = [x["field"] for x in f if x["kind"] == "differs"]
    assert "label" not in differs and differs == ["mac"]
    assert [x["field"] for x in f if x["kind"] == "clash"] == ["mac"]
    sourceless = {**BOARD2_TONIGHT, "source": {}}                    # identify: no source
    assert "label" not in [x["field"] for x in BI.compare(sourceless, HUB2, [])
                           if x["kind"] == "differs"]


def test_v7_twin_a_set_label_unlike_the_hubs_still_differs():
    baked = {**BOARD2_TONIGHT, "label": "BENCH-2",
             "source": {**BOARD2_TONIGHT["source"], "label": "stage0"}}
    f = BI.compare(baked, HUB2, [])
    differs = {x["field"]: x["text"] for x in f if x["kind"] == "differs"}
    assert differs["label"] == "label BENCH-2 differs from the hub's mps3_02_pl: MPS3-02"
    assert BI.summarise(f, baked) == "unset"            # the MAC is still the image default
    fixed = {**baked, "mac": "02:00:00:00:02:fe", "source": {**baked["source"], "mac": "stage0"}}
    assert BI.summarise(BI.compare(fixed, HUB2, []), fixed) == "differs"


def test_v7_twin_board1_with_its_own_mac_leaves_board2_unset_not_clashing():
    f = BI.compare(BOARD2_TONIGHT, HUB2, [seen_board1(mac="02:00:00:00:01:fe")])
    assert [x for x in f if x["kind"] == "clash"] == []
    assert BI.summarise(f, BOARD2_TONIGHT) == "unset"


def test_v7_twin_a_baked_label_that_matches_another_board_is_a_clash():
    baked = {**BOARD2_TONIGHT, "label": "MPS3-01", "source": {**BOARD2_TONIGHT["source"],
                                                              "label": "stage0"}}
    f = BI.compare(baked, None, [seen_board1(label="MPS3-01", label_source="stage0",
                                             mac="02:00:00:00:01:fe")])
    assert {(x["field"], x["other"]) for x in f if x["kind"] == "clash"} == {("label", "mps3-01")}
    # the other side's default label is not a clash either (both "MPS3", one baked so)
    as_mps3 = {**baked, "label": "MPS3"}
    other = seen_board1(mac="02:00:00:00:01:fe")
    assert not [x for x in BI.compare(as_mps3, None, [other]) if x["kind"] == "clash"]
    # ... but a baked "MPS3" on both is
    other = seen_board1(mac="02:00:00:00:01:fe", label_source="stage0")
    assert [x["field"] for x in BI.compare(as_mps3, None, [other]) if x["kind"] == "clash"] == \
        ["label"]


def test_v7_a_sourceless_label_mps3_is_the_default_and_mps3_02_is_not():
    """identify (``info``'s cheap read) carries ``label`` but no ``source``."""
    rep = {"label": "MPS3", "ip": "", "mac": "02:00:00:00:02:fe", "source": {}}
    f = BI.compare(rep, None, [seen_board1(label_source="")])
    assert [x["field"] for x in f] == ["label"] and f[0]["kind"] == "unset"
    rep2 = {**rep, "label": "MPS3-02"}
    assert BI.compare(rep2, None, []) == []


def test_v7_the_seen_record_keeps_the_label_source(tmp_path):
    seen = BI.SeenIdentities(tmp_path)
    seen.update("mps3@a", label="MPS3", label_source="default", mac="02:00:00:4d:50:53",
                at=time.time())
    assert seen.others("mps3@b")[0]["label_source"] == "default"
    seen.update("mps3@c", label="MPS3-01", mac="02:00:00:00:01:fe", at=time.time())
    assert {o["board_id"]: o["label_source"] for o in seen.others("mps3@b")} == {
        "mps3@a": "default", "mps3@c": ""}
