"""UI v2 drop A (lane UI2-API-HUB): the shapes of gaps G1b, G2, G3, G10, G11 and G12, served by the
real daemon over the showcase demo and by the T14 mock, so the UI lanes code against them.

docs/API.md "UI v2: hub leases, the Debug USB route, consoles and clashes". Each check has a
negative twin. Nothing here reaches a board or a hub: the demo's hub is in memory.
"""

from __future__ import annotations

import time
import warnings
from urllib.parse import quote

import pytest

from harness_manager import demo_showcase as show
from harness_manager.daemon import console_access as CA
from harness_manager.daemon import mcc_route
from harness_manager.daemon.app import create_app
from harness_manager.daemon.identity_api import clash_groups
from harness_manager.demo import BOARD_FIELDED, BOARD_USB, DemoEngine
from harness_manager.services.lease import NO_HUB_REASON

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

TOKEN = "ui2-api-hub"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
API = "/api/v1"


def enc(bid: str) -> str:
    return quote(bid, safe="")


@pytest.fixture
def showcase(tmp_path):
    eng = DemoEngine(showcase=True, state_dir=tmp_path / "demo", speed=0)
    with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as client:
        client.post(f"{API}/probe", json={}, headers=AUTH)
        yield client, eng
    eng.close_all()


def rows(client) -> dict[str, dict]:
    return {r["board_id"]: r for r in client.get(f"{API}/boards", headers=AUTH).json()["boards"]}


def open_board(client, bid: str) -> None:
    cand = rows(client)[bid]["candidate"]
    r = client.post(f"{API}/boards", json={"candidate": cand, "note": "ui2"}, headers=AUTH)
    assert r.status_code == 200, r.text


# --- G2 and G3 on GET /boards: the hub and the Debug USB route, with no contact ----------------------


def test_board_rows_name_their_hub_and_debug_usb_route_without_opening(showcase):
    client, _ = showcase
    got = rows(client)
    leased, spare = got[show.BOARD_LEASED], got[show.BOARD_SPARE]
    assert leased["open"] is False
    assert leased["hub"] == {"name": show.HUB_HOST, "host": show.HUB_HOST,
                             "target": show.HUB_TARGET, "transport": "demo"}
    assert spare["hub"]["target"] == show.SPARE_TARGET
    assert leased["mcc_route"] == "hub" and show.HUB_HOST in leased["mcc_route_reason"]
    assert got[show.BOARD_V011]["mcc_route"] == "pc"                   # the Debug USB here
    # twin: a board with no hub has none, and one whose links do not say is "unknown"
    assert got[show.BOARD_LINUX]["hub"] is None
    assert got[show.BOARD_LINUX]["mcc_route"] == "unknown"


def test_the_session_carries_the_debug_usb_route_and_info_keeps_its_shape(showcase):
    client, _ = showcase
    open_board(client, show.BOARD_LEASED)
    b = enc(show.BOARD_LEASED)
    s = client.get(f"{API}/boards/{b}/session", headers=AUTH).json()
    assert s["mcc_route"] == "hub" and "tty_00" in s["mcc_route_reason"]
    info = client.get(f"{API}/boards/{b}", headers=AUTH).json()
    assert "mcc_route" not in info                  # BoardInfo's shape stays (QUIET-POLL, T13)
    open_board(client, show.BOARD_LINUX)                              # twin: Ethernet only
    lx = client.get(f"{API}/boards/{enc(show.BOARD_LINUX)}/session", headers=AUTH).json()
    assert lx["mcc_route"] == "none" and "Ethernet only" in lx["mcc_route_reason"]


# --- G3: every target's lease on a hub, one read ------------------------------------------------------


def test_one_read_lists_every_target_on_the_hub_and_seeds_lease_known(showcase):
    client, _ = showcase
    assert "lease_known" not in rows(client)[show.BOARD_LEASED]       # nothing read yet
    r = client.get(f"{API}/hubs/{show.HUB_HOST}/leases", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    by = {t["target"]: t for t in body["targets"]}
    assert set(by) == {show.HUB_TARGET, show.SPARE_TARGET}
    assert by[show.HUB_TARGET]["state"] == "held"
    assert by[show.HUB_TARGET]["holder"] == "alice@lab-pc-07"
    assert by[show.HUB_TARGET]["queue_length"] == 2 and not by[show.HUB_TARGET]["here"]
    assert by[show.HUB_TARGET]["boards"] == [show.BOARD_LEASED]
    assert by[show.SPARE_TARGET]["state"] == "free"
    assert body["cached"] is False and body["transport"] == "demo"
    known = rows(client)
    assert known[show.BOARD_LEASED]["lease_known"]["source"] == "overview"
    assert known[show.BOARD_LEASED]["lease_known"]["holder"] == "alice@lab-pc-07"
    assert known[show.BOARD_SPARE]["lease_known"]["state"] == "free"
    again = client.get(f"{API}/hubs/{show.HUB_HOST}/leases", headers=AUTH).json()
    assert again["cached"] is True                                   # one read per 20 s


def test_twin_a_hub_no_listed_board_uses_is_absent(showcase):
    client, _ = showcase
    r = client.get(f"{API}/hubs/no-such-hub/leases", headers=AUTH)
    assert r.status_code == 404 and r.json()["error"]["name"] == "ABSENT"


# --- G11: the full queue ---------------------------------------------------------------------------------


def test_the_lease_view_has_tiers_messages_how_long_and_the_background_queue(showcase):
    client, _ = showcase
    open_board(client, show.BOARD_LEASED)
    view = client.get(f"{API}/boards/{enc(show.BOARD_LEASED)}/lease", headers=AUTH).json()
    assert view["lease"]["tier"] == "interactive"
    q = view["queue"]
    assert [w["tier"] for w in q] == ["interactive", "interactive"]
    mine, bob = q
    assert mine["mine"] and mine["want_s"] == show.MY_WANT_S and mine["message"]
    assert bob["holder"] == "bob@lab-pc-03" and bob["want_s"] == 1800
    assert bob["message"] == "a quick uart_echo check" and bob["since"]
    assert view["request"]["want_s"] == show.MY_WANT_S
    assert view["background_known"] is True
    assert [w["holder"] for w in view["background_queue"]] == ["hil-runner@mapstone-dev"]
    assert view["background_queue"][0]["tier"] == "background"


def test_twin_a_board_with_no_hub_has_no_background_queue(showcase):
    client, _ = showcase
    open_board(client, show.BOARD_LINUX)
    view = client.get(f"{API}/boards/{enc(show.BOARD_LINUX)}/lease", headers=AUTH).json()
    assert view["lease"] is None and view["background_queue"] == []
    assert view["background_known"] is False and view["background_reason"] == NO_HUB_REASON


def test_a_request_says_how_long_and_a_bad_want_is_refused_before_any_job(showcase):
    client, _ = showcase
    open_board(client, show.BOARD_SPARE)
    b = enc(show.BOARD_SPARE)
    r = client.post(f"{API}/boards/{b}/lease/request", json={"want_s": 5}, headers=AUTH)
    assert r.status_code == 400 and "want_s" in r.json()["error"]["message"]
    assert client.get(f"{API}/jobs", headers=AUTH).json()["jobs"] == []
    r = client.post(f"{API}/boards/{b}/lease/request", json={"want_s": 3600}, headers=AUTH)
    assert r.status_code == 202


# --- G1b: who may type ------------------------------------------------------------------------------------


def test_console_rows_say_the_role_and_who_may_type(showcase):
    client, _ = showcase
    open_board(client, show.BOARD_LEASED)                             # alice holds its lease
    cons = {c["name"]: c for c in client.get(
        f"{API}/boards/{enc(show.BOARD_LEASED)}/consoles", headers=AUTH).json()["consoles"]}
    assert cons["uart0"]["role"] == "dut" and cons["shell"]["role"] == "shell"   # bare metal
    assert cons["uart0"]["writable"] is False
    assert "alice@lab-pc-07" in cons["uart0"]["read_only_reason"]
    open_board(client, show.BOARD_LINUX)          # twin: no hub, so no lease: all writable
    lx = {c["name"]: c for c in client.get(
        f"{API}/boards/{enc(show.BOARD_LINUX)}/consoles", headers=AUTH).json()["consoles"]}
    assert lx["shell"]["role"] == "linux-root" and lx["shell"]["writable"] is True
    assert lx["shell"]["read_only_reason"] == ""


def test_the_linux_root_console_is_for_the_holder_here_only():
    lease_alice = {"holder": "alice@lab-pc-07", "mine": False, "here": False}
    mine_elsewhere = {"holder": "me@hub", "mine": True, "here": False}
    here = {"holder": "me@hub", "mine": True, "here": True}
    assert CA.rule("linux-root", True, here, "") == (True, "")
    for lease, err in ((None, ""), (lease_alice, ""), (mine_elsewhere, ""), (None, "reset")):
        ok, why = CA.rule("linux-root", True, lease, err)
        assert not ok and why.startswith("read-only")
    # twin: a DUT console stays writable on a free lease; read-only only when held elsewhere
    assert CA.rule("dut", True, None, "") == (True, "")
    assert CA.rule("dut", True, mine_elsewhere, "")[0] is False
    assert CA.rule("dut", False, lease_alice, "") == (True, "")      # no hub, no lease


def test_roles_follow_the_lane_and_the_harness():
    assert CA.role_of("fpga_uart2", "shell", "linux") == "linux-root"
    assert CA.role_of("fpga_uart2", "fpga_uart2", "bare-metal") == "shell"
    assert CA.role_of("uart0", "uart0", "linux") == "dut"
    assert CA.role_of("fpga_uart3", "fpga_uart3", "linux") == "lane"
    assert CA.role_of("x", "x", "linux") == ""                       # twin: unknown


# --- G10: identity clashes ---------------------------------------------------------------------------------


def test_the_showcase_clash_is_listed_with_no_contact(showcase):
    client, _ = showcase
    body = client.get(f"{API}/identity/clashes", headers=AUTH).json()
    assert body["boards_seen"] == 2
    (clash,) = body["clashes"]
    assert clash["field"] == "mac" and clash["value"] == "02:00:00:4d:50:53"
    assert {b["name"] for b in clash["boards"]} == {"mps3-02", "mps3-03"}


def test_clash_rules_default_labels_one_board_twice_and_old_records():
    now = time.time()
    rec = {"label": "MPS3", "label_source": "default", "ip": "10.0.0.5/24",
           "mac": "02:00:00:00:00:01", "target": "t1", "address": "a1", "at": now}
    # twin 1: the image-default label is "not set", never a clash
    got = clash_groups({"b1": rec, "b2": {**rec, "ip": "10.0.0.6", "mac": "02:00:00:00:00:02",
                                          "target": "t2", "address": "a2"}}, now=now)
    assert got == []
    # a baked label shared by two boards is one
    got = clash_groups({"b1": {**rec, "label": "MPS3-01", "label_source": "stage0"},
                        "b2": {**rec, "label": "mps3-01", "label_source": "override",
                               "ip": "10.0.0.6", "mac": "02:00:00:00:00:02", "target": "t2",
                               "address": "a2"}}, now=now)
    assert [(c["field"], c["value"]) for c in got] == [("label", "MPS3-01")]
    # twin 2: the same board under two ids (same target) is one board
    assert clash_groups({"b1": rec, "b1-by-name": {**rec, "address": "other"}}, now=now) == []
    # twin 3: a record older than 14 days is left out
    assert clash_groups({"b1": rec, "b2": {**rec, "target": "t2", "address": "a2",
                                           "at": now - 15 * 86400}}, now=now) == []
    # the hub's other targets count; one whose MAC is the hub's own adapter does not
    hub = {"target": "t9", "ip": "10.0.0.5", "mac": "02:00:00:00:00:01", "label": "MPS3-09"}
    got = clash_groups({"b1": {**rec, "hub_others": [hub]}}, now=now)
    assert {(c["field"], c["boards"][1]["kind"]) for c in got} == {("mac", "hub"), ("ip", "hub")}
    got = clash_groups({"b1": {**rec, "hub_others": [{**hub, "mac_suspect": "the hub's"}]}}, now=now)
    assert [c["field"] for c in got] == ["ip"]


# --- G2: the rules on links ----------------------------------------------------------------------------------


def test_the_route_from_links():
    from harness_manager.core.model import Link, LinkKind

    hub = Link(LinkKind.HUB, "hub-mcc://hubhost/t/dev/t/tty_00", "mcc on the hub", via="hub")
    mcc = Link(LinkKind.USB_SERIAL, "/dev/ttyUSB0", "FT4232H X if00 (MCC)")
    lane = Link(LinkKind.USB_SERIAL, "/dev/ttyUSB2", "FT4232H X if02: FPGA UART lane 2")
    eth = Link(LinkKind.ETHERNET, "10.0.0.5:6900", "shell")
    assert mcc_route.from_links((eth, hub))[0] == "hub"
    assert mcc_route.from_links((eth, mcc, lane))[0] == "pc"
    assert mcc_route.from_links((eth,), ("mcc_local",))[0] == "self"
    assert mcc_route.from_links((eth, lane))[0] == "unknown"        # twin: a lane is not the MCC


# --- G12 ------------------------------------------------------------------------------------------------------


def test_open_on_is_a_general_setting_and_refuses_another_tab(showcase):
    client, _ = showcase
    row = next(r for r in client.get(f"{API}/settings/schema", headers=AUTH).json()["rows"]
               if r["key"] == "general.open_on")
    assert row["default"] == "workbench" and row["choices"] == ["workbench", "overview"]
    assert row["section"] == "General" and row["apply"] == "live"
    bad = client.put(f"{API}/settings", json={"general.open_on": "board"}, headers=AUTH)
    assert bad.status_code == 400                                     # twin: not a tab


# --- the T14 mock serves the same shapes ------------------------------------------------------------------------


@pytest.fixture
def mock():
    from tests.fakes.t14_mock_api import create_app as mock_app

    eng = DemoEngine(speed=0)
    app = mock_app(eng, token=TOKEN, serve_ui=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, app, eng
    eng.close_all()


def test_the_mock_serves_the_hub_overview_consoles_and_routes(mock):
    client, app, _eng = mock
    cands = client.post(f"{API}/probe", json={}, headers=AUTH).json()["candidates"]
    cand = next(c for c in cands if c["board_id"] == BOARD_FIELDED)
    client.post(f"{API}/boards", json={"candidate": cand}, headers=AUTH)
    app.state.sim.behind_hub(BOARD_FIELDED, lease="other")
    app.state.sim.requests.add_background(BOARD_FIELDED)
    over = client.get(f"{API}/hubs/mapstone-dev/leases", headers=AUTH).json()
    (t,) = over["targets"]
    assert t["state"] == "held" and t["boards"] == [BOARD_FIELDED]
    view = client.get(f"{API}/boards/{enc(BOARD_FIELDED)}/lease", headers=AUTH).json()
    assert view["background_queue"][0]["tier"] == "background"
    row = next(r for r in client.get(f"{API}/boards", headers=AUTH).json()["boards"]
               if r["board_id"] == BOARD_FIELDED)
    assert row["hub"]["name"] == "mapstone-dev" and row["mcc_route"] == "none"
    cons = client.get(f"{API}/boards/{enc(BOARD_FIELDED)}/consoles", headers=AUTH).json()
    assert all(c["writable"] is False for c in cons["consoles"])      # alice's lease
    usb = next(r for r in client.get(f"{API}/boards", headers=AUTH).json()["boards"]
               if r["board_id"] == BOARD_USB)
    assert usb["hub"] is None and usb["mcc_route"] == "pc"            # twin: no hub, USB here
    assert client.get(f"{API}/hubs/nowhere/leases", headers=AUTH).status_code == 404


def test_the_mock_lists_the_identity_sims_clash(mock):
    client, app, _eng = mock
    app.state.identity.board2_as_board1(BOARD_FIELDED)
    body = client.get(f"{API}/identity/clashes", headers=AUTH).json()
    assert {c["field"] for c in body["clashes"]} >= {"mac", "ip"}
    app.state.identity.matching(BOARD_FIELDED)                         # twin: board 2 fixed
    assert client.get(f"{API}/identity/clashes", headers=AUTH).json()["clashes"] == []
