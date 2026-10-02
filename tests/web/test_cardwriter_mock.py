"""Lane SD-FLASH in the T14 mock daemon (what the browser tests run against): the REAL
card-writer routes over ``--demo``'s simulated readers, the setting from the mock's own
settings. Off by default, as the product; on, two simulated cards; a write is a job.
"""

from __future__ import annotations

import time
import warnings
from pathlib import Path

import pytest

from harness_manager.demo import DemoEngine
from tests.fakes.cardwriter_fakes import card_image, guard  # noqa: F401 - the fixture
from tests.fakes.t14_mock_api import create_app

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("guard")
TOKEN = "t14-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client():
    eng = DemoEngine(speed=0.02)
    with TestClient(create_app(eng, token=TOKEN), raise_server_exceptions=False) as c:
        yield c
    eng.close_all()


def test_the_mock_says_off_by_default_with_the_reason(client):
    doc = client.get("/api/v1/cardwriter/devices", headers=AUTH).json()
    assert doc["enabled"] is False and doc["devices"] == []
    assert doc["reason"] == "SD flashing is turned off (Settings → Bring-up, bringup.sd_flash)"
    r = client.post("/api/v1/cardwriter/write", json={"device_id": "x", "kind": "card",
                                                      "source": "/x", "confirm": ""},
                    headers=AUTH)
    assert r.status_code == 422 and r.json()["error"]["name"] == "UNAVAILABLE"


def test_twin_on_through_settings_the_mock_lists_simulated_cards_and_writes_one(client,
                                                                             tmp_path: Path):
    assert client.put("/api/v1/settings", json={"bringup.sd_flash": "on"},
                      headers=AUTH).status_code == 200
    doc = client.get("/api/v1/cardwriter/devices", headers=AUTH).json()
    assert doc["enabled"] is True and doc["simulated"] is True
    by = {d["path"]: d for d in doc["devices"]}
    assert set(by) == {"/dev/sdb", "/dev/sdc"}
    assert by["/dev/sdb"]["kinds"]["files"]["ok"] and not by["/dev/sdb"]["kinds"]["card"]["ok"]
    blank = by["/dev/sdc"]
    assert blank["confirm"] == "WRITE MicroSD/M2 15.9 GB"
    src = card_image(tmp_path / "card.img")
    body = {"device_id": blank["id"], "kind": "card", "source": str(src),
            "confirm": blank["confirm"]}
    r = client.post("/api/v1/cardwriter/write", json=body, headers=AUTH)       # no unsigned phrase
    assert r.status_code == 409 and "this card image is unsigned" in r.json()["error"]["message"]
    r = client.post("/api/v1/cardwriter/check", json={"kind": "card", "source": str(src)},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    r = client.post("/api/v1/cardwriter/write", json={
        **body, "confirm_unsigned": r.json()["unsigned"]["phrase"]}, headers=AUTH)
    assert r.status_code == 202, r.text
    job_id = r.json()["job"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()
        if job["state"] != "running":
            break
        time.sleep(0.02)
    assert job["state"] == "done" and job["result"]["verified"] is True
