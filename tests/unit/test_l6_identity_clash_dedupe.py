"""Lane L6: a board opened by address and its own hub entry are one board, not a clash."""

from __future__ import annotations

import time

from harness_manager.daemon.identity_api import clash_groups

NOW = time.time()
OWN = {"label": "MPS3-02", "label_source": "override", "ip": "10.0.0.5/24",
       "mac": "02:00:00:00:00:01", "target": "", "address": "10.0.0.5:7000", "at": NOW}
HUB_ENTRY = {"target": "mps3_02_pl", "ip": "10.0.0.5", "mac": "02:00:00:00:00:01",
             "label": "MPS3-02"}


def test_a_board_and_its_own_hub_entry_are_not_a_clash():
    rec = {**OWN, "hub_others": [HUB_ENTRY]}
    assert clash_groups({"b1": rec}, now=NOW) == []


def test_twin_another_hub_target_with_the_same_mac_still_clashes():
    rec = {**OWN, "target": "t1", "hub_others": [HUB_ENTRY]}
    got = clash_groups({"b1": rec}, now=NOW)
    assert {c["field"] for c in got} == {"mac", "ip", "label"}


def test_twin_an_address_board_and_a_hub_entry_with_another_mac_and_the_same_ip_clash():
    other = {**HUB_ENTRY, "mac": "02:00:00:00:00:09", "label": "MPS3-09"}
    got = clash_groups({"b1": {**OWN, "hub_others": [other]}}, now=NOW)
    assert [c["field"] for c in got] == ["ip"]


def test_twin_two_board_records_with_the_same_mac_and_ip_still_clash():
    a = {**OWN, "name": "a"}
    other = {**OWN, "address": "", "target": "", "name": "b"}
    got = clash_groups({"b1": a, "b2": other}, now=NOW)
    assert {c["field"] for c in got} >= {"mac", "ip"}
