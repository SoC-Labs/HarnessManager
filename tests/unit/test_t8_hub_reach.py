"""T8: ``via = "hub"``, the SSH-free data plane, planned against the fake hub. Each check has a twin.

Routes and probes are injected: nothing here reads this machine's routing table, pings
anything, or opens a connection beyond 127.0.0.1.
"""

from __future__ import annotations

import pytest

from harness_manager.core.errors import UnreachableError
from harness_manager.transports.hub_reach import (
    DirectReach,
    Route,
    best_route,
    open_hub_reach,
    parse_proc_route,
    plan_reach,
)
from tests.fakes.t8_hub_rest import DEFAULT_BOARDS, FakeFpgahub, client_for

BOARD_IP = "192.168.10.101"
VIA_HUB = Route("192.168.10.0/24", "10.10.0.1", "eth0")
PORTS = {"push": 6910, "rbb": 6921, "uart0": 6930, "uart1": 6931, "swo": 6932, "xvc": 2542}

# /proc/net/route: a default via 10.10.0.254, the campus /16, and 192.168.10.0/24 via 10.10.0.1
PROC_ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\tFE000A0A\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t00000A0A\t00000000\t0001\t0\t0\t100\t0000FFFF\t0\t0\t0
eth0\t000AA8C0\t01000A0A\t0003\t0\t0\t100\t00FFFFFF\t0\t0\t0
eth1\t0014A8C0\t00000000\t0000\t0\t0\t100\t00FFFFFF\t0\t0\t0
"""


@pytest.fixture(autouse=True)
def _no_fpgahub_login(tmp_path, monkeypatch):
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)


def _boards(gate: bool) -> dict:
    boards = {k: {r: dict(v) for r, v in m.items()} for k, m in DEFAULT_BOARDS.items()}
    boards["mps3_01"]["pl"]["gate_ethernet"] = gate
    return boards


@pytest.fixture
def gated():
    with FakeFpgahub(boards=_boards(True)) as h:
        yield h


@pytest.fixture
def ungated():
    with FakeFpgahub(boards=_boards(False)) as h:
        yield h


# --- the routing table -----------------------------------------------------------------------


def test_the_proc_route_table_parses_and_the_longest_specific_prefix_wins():
    routes = parse_proc_route(PROC_ROUTE)
    assert Route("192.168.10.0/24", "10.10.0.1", "eth0") in routes
    assert all(r.iface != "eth1" for r in routes)                 # a route that is not up
    assert best_route(BOARD_IP, routes) == VIA_HUB


def test_only_a_default_route_is_no_route_to_the_board():
    routes = parse_proc_route(PROC_ROUTE)
    assert best_route("192.168.30.5", routes) is None             # the default does not count
    assert best_route("10.10.3.4", routes).network == "10.10.0.0/16"


# --- the plan ----------------------------------------------------------------------------------


def _holding(hub):
    c = client_for(hub, hub.add_token("alice"))
    c.lease_acquire("x", ttl=600)
    return c


def test_direct_when_a_route_the_gate_the_lease_and_an_echo_all_line_up(gated):
    c = _holding(gated)
    plan = plan_reach(BOARD_IP, client=c, ssh_host="mapstone-dev",
                      route_fn=lambda ip: VIA_HUB, probe_fn=lambda ip: True)
    assert plan.mode == "direct" and "10.10.0.1" in plan.reason
    assert plan.checks == {"route": "192.168.10.0/24", "gate_ethernet": True,
                           "lease_holder": "alice@mapstone-dev", "probe": True}


@pytest.mark.parametrize("case, word", [
    ("no_route", "no route"),
    ("gate_off", "gate is off"),
    ("not_holder", "does not hold"),
    ("no_echo", "IPForward=no"),
])
def test_each_missing_piece_falls_back_to_ssh_and_says_which(gated, ungated, case, word):
    hub = ungated if case == "gate_off" else gated
    c = client_for(hub, hub.add_token("alice")) if case == "not_holder" else _holding(hub)
    plan = plan_reach(BOARD_IP, client=c, ssh_host="mapstone-dev",
                      route_fn=(lambda ip: None) if case == "no_route" else (lambda ip: VIA_HUB),
                      probe_fn=lambda ip: case != "no_echo")
    assert plan.mode == "ssh" and word in plan.reason


def test_without_an_ssh_host_the_plan_has_nowhere_to_fall_back(ungated):
    c = _holding(ungated)
    plan = plan_reach(BOARD_IP, client=c, route_fn=lambda ip: VIA_HUB, probe_fn=lambda ip: True)
    assert plan.mode == "none"


def test_a_hub_that_cannot_be_asked_falls_back(gated):
    c = client_for(gated, None)                  # 401: the gate cannot be read
    plan = plan_reach(BOARD_IP, client=c, ssh_host="h", route_fn=lambda ip: VIA_HUB,
                      probe_fn=lambda ip: True)
    assert plan.mode == "ssh" and "401" in plan.reason


def test_direct_always_skips_the_checks_and_never_always_tunnels(ungated):
    probed = []
    plan = plan_reach(BOARD_IP, client=None, direct="always", route_fn=lambda ip: None,
                      probe_fn=probed.append)
    assert plan.mode == "direct" and probed == []
    plan = plan_reach(BOARD_IP, client=_holding(ungated), direct="never", ssh_host="h",
                      route_fn=lambda ip: VIA_HUB, probe_fn=lambda ip: True)
    assert plan.mode == "ssh" and "never" in plan.reason


# --- the reach ------------------------------------------------------------------------------------


def test_a_direct_reach_talks_to_the_board_address_on_the_board_ports(gated):
    c = _holding(gated)
    reach = open_hub_reach(f"{BOARD_IP}:6900", PORTS, client=c, ssh_host="mapstone-dev",
                           ssh_fallback=lambda plan: pytest.fail("no tunnel wanted"),
                           route_fn=lambda ip: VIA_HUB, probe_fn=lambda ip: True)
    assert isinstance(reach, DirectReach)
    assert reach.host == BOARD_IP and reach.ports == {**PORTS, "control": 6900}
    st = reach.status()
    assert st["via"] == "hub" and st["mode"] == "direct" and st["state"] == "up"
    assert reach.tunnel is None
    reach.close()


def test_a_reach_that_cannot_go_direct_uses_the_ssh_fallback_with_the_reason(ungated):
    c = _holding(ungated)

    class Tunnelled:
        tunnel = object()

    got = open_hub_reach(f"{BOARD_IP}:6900", PORTS, client=c, ssh_host="mapstone-dev",
                         ssh_fallback=lambda plan: Tunnelled(), route_fn=lambda ip: VIA_HUB,
                         probe_fn=lambda ip: True)
    assert isinstance(got, Tunnelled) and "gate is off" in got.hub_plan.reason


def test_no_direct_path_and_no_ssh_host_is_unreachable_with_the_reason(ungated):
    c = _holding(ungated)
    with pytest.raises(UnreachableError) as ei:
        open_hub_reach(f"{BOARD_IP}:6900", PORTS, client=c, route_fn=lambda ip: VIA_HUB,
                       probe_fn=lambda ip: True)
    assert "gate is off" in ei.value.message and "hub.host" in ei.value.hint
