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
AS_BOARD1 = {"label": "MPS3-01", "ip": "192.168.10.101/24", "mac": "02:00:00:4d:50:53",
             "source": {"label": "default", "ip": "default", "mac": "default"}}
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
