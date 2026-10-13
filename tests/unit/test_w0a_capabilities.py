"""W0-A: the mint-4 and card capability declarations gate on feature NAMES only."""

from __future__ import annotations

import pytest

from harness_manager.core.capabilities import satisfied_route
from harness_manager.core.model import LinkKind as L
from harness_manager_mps3 import capabilities as caps

SPEC = {s.name: s for s in caps.SPECS}


def ok(name, links, *features):
    return satisfied_route(SPEC[name], frozenset(links), frozenset(features)) is not None


@pytest.mark.parametrize("cap,feature", [
    (caps.HOSTIO_CAP, "hostio"), (caps.SENSORS_CAP, "sysmon"), (caps.COSIM_CAP, "cosim"),
    (caps.OS_SLOTS_CAP, "slot"), (caps.DESIGN_STORE_CAP, "usd"),
])
def test_one_feature_name_lights_the_capability(cap, feature):
    assert ok(cap, {L.ETHERNET}, feature)
    assert not ok(cap, {L.ETHERNET}, "stats")
    assert not ok(cap, {L.ETHERNET})          # no feature, no capability
    assert not ok(cap, {L.USB_SERIAL}, feature)


@pytest.mark.parametrize("name", ["mccif", "mcc_local"])
def test_mcc_exists_when_either_name_is_present(name):
    assert ok(caps.MCC_CAP, {L.ETHERNET}, name)
    assert ok(caps.MCC_CAP, {L.HUB}, name)
    assert not ok(caps.MCC_CAP, {L.ETHERNET}, "hostio", "cosim")


def test_os_slots_also_reach_over_ssh():
    assert ok(caps.OS_SLOTS_CAP, {L.SSH}, "slot")


def test_no_route_names_a_protocol_version():
    for name in (caps.HOSTIO_CAP, caps.SENSORS_CAP, caps.MCC_CAP, caps.COSIM_CAP,
                 caps.OS_SLOTS_CAP, caps.DESIGN_STORE_CAP):
        for route in SPEC[name].routes:
            assert all(not f.startswith(("v0", "proto")) for f in route.features)


def test_names_are_unique():
    names = [s.name for s in caps.SPECS]
    assert len(names) == len(set(names))
