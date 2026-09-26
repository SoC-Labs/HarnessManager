"""LINUX-SLOTS: ``GET /boards/{bid}/card`` and ``/slots`` on the real daemon, over pyverify's
FakeShell through the real engine and MPS3 pack. Twins: a harness without a card store or
OS slots answers ``available: false`` with the reason, and nothing is sent to change it."""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")      # starlette: httpx with the TestClient is deprecated
    from fastapi.testclient import TestClient

from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from tests.fakes.lxslots_board import LINUX_SID, slot_board
from tests.fakes.t13_daemon import TOKEN, bid_path, headers, state_dir

A_RECORDED = {"state": "valid", "hdr_crc": 0x3E5E9C2C, "len": 24354312, "sid": LINUX_SID}


def client_for(fake) -> tuple[Engine, TestClient]:
    eng = Engine(EngineConfig(state_dir=state_dir(), pack_overrides={"mps3": {
        "console_ports": fake.console_ports, "push_port": fake.raw_tcp_port,
        "tftp_port": fake.tftp_port}}))
    return eng, TestClient(create_app(eng, token=TOKEN, static_dir=None))


@pytest.fixture
def api(request, monkeypatch) -> Iterator[tuple]:
    fake = slot_board(**request.param)
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    eng, client = client_for(fake)
    with client:
        r = client.post("/api/v1/boards", json={"target": f"{fake.host}:{fake.control_port}",
                                                "note": "lxslots"}, headers=headers())
        assert r.status_code == 200, r.text
        yield fake, client, r.json()["board_id"]
    eng.close_all()
    fake.stop()


@pytest.mark.parametrize("api", [{"usd_card": "da", "slots": {"a": A_RECORDED}}], indirect=True)
def test_the_card_line_names_the_store_and_the_os_slots(api):
    fake, client, bid = api
    r = client.get(bid_path(bid) + "/card", headers=headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] and body["card"]["present"] and body["card"]["state"] == "empty"
    assert body["card"]["os_slots"]["running"] == "A"
    assert body["line"] == "empty · OS A:valid* B:empty"
    s = client.get(bid_path(bid) + "/slots", headers=headers()).json()
    assert s["available"] and s["slots"]["target"] == "B"
    assert s["slots"]["slots"]["A"]["verified"] == "boot"


@pytest.mark.parametrize("api", [{}], indirect=True)
def test_twin_no_card_is_a_line_not_an_error(api):
    fake, client, bid = api
    body = client.get(bid_path(bid) + "/card", headers=headers()).json()
    assert body["available"] and not body["card"]["present"]
    assert body["line"] == "none (boots as always)"


@pytest.mark.parametrize("api", [{"profile": "bare-metal", "slots": None,
                                  "features": ("clcd", "clcd_kvm", "touch", "hwicap_fifo",
                                               "windowed")}], indirect=True)
def test_twin_bare_metal_has_neither_and_says_why(api):
    fake, client, bid = api
    card = client.get(bid_path(bid) + "/card", headers=headers()).json()
    assert card["ok"] and not card["available"] and "no user-microSD store" in card["reason"]
    slots = client.get(bid_path(bid) + "/slots", headers=headers()).json()
    assert not slots["available"] and "bare-metal harness has no OS slots" in slots["reason"]
    assert fake.commits == [] and fake.slots is None


def test_the_board_tile_reads_the_card_route_and_hides_it_on_an_older_daemon():
    from pathlib import Path

    import harness_manager.web as web

    js = Path(web.static_dir()) / "js"
    api_js = (js / "api.js").read_text(encoding="utf-8")
    assert 'cardStatus: ["GET", "/boards/{bid}/card"]' in api_js
    assert "|card)\\b/" in api_js                 # routeMissing: a 404 for /card hides the line
    tile = (js / "sections" / "overview.js").read_text(encoding="utf-8")
    assert 'data-testid="tile-card"' in tile and "<${CardRow} b=${b} />" in tile
    store = (js / "store.js").read_text(encoding="utf-8")
    assert "b.cardMissing = true" in store and 'call("cardStatus", { bid })' in store
