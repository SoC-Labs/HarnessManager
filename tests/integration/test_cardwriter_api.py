"""Lane SD-FLASH over the REAL daemon (``cardwriter_api.py``): the contract BRINGUP-USB codes to.

The daemon's ``CardWriter`` is the rig's (fixture lsblk, temp-file devices through the
``FileAccess`` seam), but the setting is the real one, read where ``settings_api`` writes it.
Each route has its negative twin; nothing touches a block device (``guard``).
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager.services import cardwriter as cw
from tests.fakes.cardwriter_fakes import (
    BUNDLE_BOARD,
    CARD_BOARD,
    MCC,
    NO_LINE_BOARD,
    Rig,
    guard,  # noqa: F401 - the fixture
    phrase,
)
from tests.fakes.t13_daemon import TOKEN, headers, state_dir

pytestmark = pytest.mark.usefixtures("guard")

OFF = "SD flashing is turned off (Settings → Bring-up, bringup.sd_flash)"


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple]:
    rig = Rig(tmp_path / "rig")

    def factory(*, state_dir=None, publish=None, **kw):  # noqa: ANN001, ANN003
        rig.writer.publish = publish
        rig.writer.state_dir = Path(state_dir)
        rig.writer._enabled = lambda: cw.is_enabled(state_dir)        # the real setting
        return rig.writer

    monkeypatch.setattr(cw, "CardWriter", factory)
    eng = Engine(EngineConfig(state_dir=state_dir()))
    events: list = []
    eng.bus.subscribe("*", events.append)
    client = TestClient(create_app(eng, token=TOKEN, static_dir=None))
    with client:
        yield rig, client, events
    eng.close_all()


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cw.ENABLE_ENV, "on")


def wait_job(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/jobs/{job_id}", headers=headers()).json()
        if body.get("state") != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} still running")


def devices(client: TestClient) -> dict:
    r = client.get("/api/v1/cardwriter/devices", headers=headers())
    assert r.status_code == 200, r.text
    return r.json()


def device(client: TestClient, name: str) -> dict:
    return next(d for d in devices(client)["devices"] if d["path"] == f"/dev/{name}")


_AUTO = object()


def write(client: TestClient, *, unsigned: object = _AUTO, **body) -> object:  # noqa: ANN003
    """POST /cardwriter/write. ``unsigned``: the INSTALL UNSIGNED phrase (by default the right
    one for ``source``, so the tests of other behaviours stay about them; None: not sent)."""
    if unsigned is _AUTO:
        try:
            body["confirm_unsigned"] = phrase(Path(str(body.get("source"))))
        except Exception:  # noqa: BLE001 - a bad source: the route says why
            pass
    elif unsigned is not None:
        body["confirm_unsigned"] = unsigned
    return client.post("/api/v1/cardwriter/write", json=body, headers=headers())


# --- the setting -------------------------------------------------------------------------------


def test_off_by_default_devices_says_why_and_write_is_422(api):
    rig, client, _ = api
    doc = devices(client)
    assert doc["ok"] is True and doc["enabled"] is False and doc["reason"] == OFF
    assert doc["devices"] == [] and rig.listed == 0
    r = write(client, device_id="sdc-x", kind="card", source=str(rig.card_image()),
              confirm="WRITE x")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["name"] == "UNAVAILABLE" and OFF in err["message"]


def test_twin_turned_on_through_the_settings_route_it_lists_at_once(api):
    rig, client, _ = api
    r = client.put("/api/v1/settings", json={"bringup.sd_flash": "on"}, headers=headers())
    assert r.status_code == 200, r.text
    doc = devices(client)
    assert doc["enabled"] is True and {d["path"] for d in doc["devices"]} == {
        "/dev/sdb", "/dev/sdc", "/dev/mmcblk0"}
    assert all("id" not in e for e in doc["excluded"])
    assert {e["path"] for e in doc["excluded"]} >= {"/dev/nvme0n1", "/dev/sdd", "/dev/sde"}
    row = next(d for d in doc["devices"] if d["path"] == "/dev/sdb")
    assert {"id", "path", "model", "size_bytes", "removable", "mounted", "writable"} <= set(row)
    assert row["fs"] == "vfat" and MCC in row["labels"] and row["kinds"]["card"]["ok"] is False


# --- card --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("on")
def test_a_card_write_is_a_job_with_progress_and_done_events(api):
    rig, client, events = api
    dev = device(client, "sdc")
    src = rig.card_image()
    r = write(client, device_id=dev["id"], kind="card", source=str(src), confirm=dev["confirm"])
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "done" and job["kind"] == "cardwriter_write" and job["board_id"] == ""
    res = job["result"]
    assert res["outcome"] == "written" and res["verified"] is True
    assert res["sha256"] == cw.file_sha256(src)
    assert rig.devices["/dev/sdc"].read_bytes()[:src.stat().st_size] == src.read_bytes()
    prog = [e.data for e in events if e.topic == "cardwriter.progress"]
    assert [p["phase"] for p in prog][0] == "unmount" and prog[-1]["phase"] == "verify"
    assert {"phase", "bytes", "total"} <= set(prog[-1])
    done = [e.data for e in events if e.topic == "cardwriter.done"]
    assert done == [{"device_id": dev["id"], "kind": "card", "verified": True,
                     "sha256": res["sha256"], "outcome": "written"}]
    assert all(e.board_id == "" for e in events if e.topic.startswith("cardwriter."))


@pytest.mark.usefixtures("on")
def test_twin_a_wrong_phrase_is_409_with_the_phrase_and_nothing_starts(api):
    rig, client, events = api
    dev = device(client, "sdc")
    r = write(client, device_id=dev["id"], kind="card", source=str(rig.card_image()),
              confirm="WRITE it")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["name"] == "REFUSED" and err["data"]["confirm"] == "WRITE MicroSD/M2 15.9 GB"
    assert not [e for e in events if e.topic.startswith(("job.", "cardwriter."))]


@pytest.mark.usefixtures("on")
def test_a_single_os_slot_is_409_before_the_job(api):
    rig, client, _ = api
    dev = device(client, "sdc")
    r = write(client, device_id=dev["id"], kind="card", source=str(rig.slot()),
              confirm=dev["confirm"])
    assert r.status_code == 409
    assert "that is a single OS slot (linux_slot.img), not a whole-card image" in \
        r.json()["error"]["message"]


@pytest.mark.usefixtures("on")
def test_a_swapped_card_is_409(api):
    rig, client, _ = api
    dev = device(client, "sdc")
    rig.node("sdc")["size"] *= 2
    r = write(client, device_id=dev["id"], kind="card", source=str(rig.card_image()),
              confirm=dev["confirm"])
    assert r.status_code == 409 and "changed since it was listed" in r.json()["error"]["message"]


@pytest.mark.usefixtures("on")
def test_needs_privilege_ends_the_job_done_with_the_commands(api):
    rig, client, events = api
    rig.access.writable = False
    dev = device(client, "sdc")
    assert dev["needs_privilege"] is True
    src = rig.card_image()
    r = write(client, device_id=dev["id"], kind="card", source=str(src), confirm=dev["confirm"])
    job = wait_job(client, r.json()["job"])
    res = job["result"]
    assert job["state"] == "done" and res["outcome"] == "needs_privilege"
    assert res["privileged_command"] == (f"sudo dd if={src} of=/dev/sdc bs=4M conv=fsync "
                                         f"status=progress")
    assert res["verify_command"].startswith(f"sudo cmp -n {src.stat().st_size} {src} /dev/sdc")
    done = [e.data for e in events if e.topic == "cardwriter.done"][-1]
    assert done["outcome"] == "needs_privilege" and done["verified"] is False


@pytest.mark.usefixtures("on")
def test_a_failed_read_back_fails_the_job(api):
    rig, client, _ = api

    def flip(path: Path) -> None:
        data = bytearray(path.read_bytes())
        data[4200] ^= 0xFF
        path.write_bytes(bytes(data))

    rig.access.after_write = flip
    dev = device(client, "sdc")
    r = write(client, device_id=dev["id"], kind="card", source=str(rig.card_image()),
              confirm=dev["confirm"])
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "failed" and job["error"]["name"] == "ACTION_FAILED"
    assert "read-back mismatch" in job["error"]["message"]


@pytest.mark.usefixtures("on")
def test_bad_requests_are_400(api):
    rig, client, _ = api
    dev = device(client, "sdc")
    for body in ({"device_id": dev["id"], "kind": "image", "source": str(rig.card_image()),
                  "confirm": dev["confirm"]},
                 {"device_id": dev["id"], "kind": "card", "source": "card.img",
                  "confirm": dev["confirm"]},
                 {"device_id": dev["id"], "kind": "card", "source": str(rig.card_image()),
                  "confirm": 1}):
        r = write(client, **body)
        assert r.status_code == 400 and r.json()["error"]["name"] == "USAGE", body


# --- files -------------------------------------------------------------------------------------


@pytest.mark.usefixtures("on")
def test_files_back_up_into_the_services_backups_and_keep_mbbios(api):
    rig, client, _ = api
    rig.card_board_txt(CARD_BOARD)
    dev = device(client, "sdb")
    r = write(client, device_id=dev["id"], kind="files",
              source=str(rig.bundle(board_txt=BUNDLE_BOARD)), confirm=dev["confirm"])
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job"])
    res = job["result"]
    assert job["state"] == "done" and res["verified"] is True
    assert Path(res["backup"]["path"]).parent == state_dir() / "backups"
    assert res["mbbios"][0]["note"] == "MBBIOS kept: mbb_v141.ebf"
    assert b"mbb_v141.ebf" in (rig.root / "MB" / "HBI0309C" / "board.txt").read_bytes()
    assert set(job["phases"]) >= {"backup", "write", "verify"}


@pytest.mark.usefixtures("on")
def test_twin_files_that_would_update_the_mcc_are_409_unless_allowed(api):
    rig, client, _ = api
    rig.card_board_txt(NO_LINE_BOARD, ebf="mbb_v999.ebf")
    dev = device(client, "sdb")
    body = {"device_id": dev["id"], "kind": "files",
            "source": str(rig.bundle(board_txt=BUNDLE_BOARD)), "confirm": dev["confirm"]}
    r = write(client, **body)
    assert r.status_code == 409 and "--allow-mcc-update" in r.json()["error"]["message"]
    r = write(client, **body, allow_mcc_update=True)
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "done" and job["result"]["mbbios"][0]["action"] == "allowed"


@pytest.mark.usefixtures("on")
def test_an_ebf_in_the_bundle_is_409(api):
    rig, client, _ = api
    dev = device(client, "sdb")
    r = write(client, device_id=dev["id"], kind="files", source=str(rig.bundle(ebf=True)),
              confirm=dev["confirm"])
    assert r.status_code == 409 and ".ebf" in r.json()["error"]["hint"]


# --- the unsigned phrase (david 2 Oct: every unsigned write, the same rule) -----------------------


@pytest.mark.usefixtures("on")
def test_a_card_image_needs_install_unsigned_and_its_sha8(api):
    rig, client, events = api
    dev = device(client, "sdc")
    src = rig.card_image()
    sha = cw.file_sha256(src)
    r = write(client, unsigned=None, device_id=dev["id"], kind="card", source=str(src),
              confirm=dev["confirm"])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["message"] == (f"not confirmed: this card image is unsigned; type exactly "
                              f"'INSTALL UNSIGNED {sha[:8]}' to install it")
    assert err["data"]["unsigned"]["of"] == "file" and err["data"]["unsigned"]["sha256"] == sha
    wrong = "INSTALL UNSIGNED " + ("0" * 8 if sha[:8] != "0" * 8 else "1" * 8)
    r = write(client, unsigned=wrong, device_id=dev["id"], kind="card", source=str(src),
              confirm=dev["confirm"])
    assert r.status_code == 409 and "does not name this card image" in \
        r.json()["error"]["message"]
    assert not [e for e in events if e.topic.startswith(("job.", "cardwriter."))]
    assert rig.devices["/dev/sdc"].read_bytes() == b"\xee" * 4096          # untouched
    r = write(client, unsigned=f"INSTALL UNSIGNED {sha[:8].upper()}", device_id=dev["id"],
              kind="card", source=str(src), confirm=dev["confirm"])
    assert r.status_code == 202 and wait_job(client, r.json()["job"])["state"] == "done"


@pytest.mark.usefixtures("on")
def test_twin_a_bundle_folder_or_zip_needs_it_too(api, tmp_path):
    from tests.unit.test_bringup_service import zip_dir

    rig, client, _ = api
    dev = device(client, "sdb")
    bundle = rig.bundle()
    r = write(client, unsigned=None, device_id=dev["id"], kind="files", source=str(bundle),
              confirm=dev["confirm"])
    assert r.status_code == 409 and "this bundle is unsigned" in r.json()["error"]["message"]
    assert r.json()["error"]["data"]["unsigned"]["of"] == "manifest"
    z = zip_dir(bundle, tmp_path / "bundle.zip")
    r = write(client, unsigned=None, device_id=dev["id"], kind="files", source=str(z),
              confirm=dev["confirm"])
    assert r.status_code == 409
    assert r.json()["error"]["data"]["unsigned"]["sha256"] == cw.file_sha256(z)
    assert (rig.root / "MB" / "HBI0309C" / "images.txt").read_text() == "old\n"
    r = write(client, device_id=dev["id"], kind="files", source=str(z), confirm=dev["confirm"])
    assert r.status_code == 202, r.text
    assert wait_job(client, r.json()["job"])["state"] == "done"
    assert (rig.root / "MB" / "HBI0309C" / "images.txt").read_text() == "new harness\n"


@pytest.mark.usefixtures("on")
def test_check_says_the_phrase_before_any_device_and_refuses_a_slot_image(api):
    rig, client, _ = api
    src = rig.card_image()
    r = client.post("/api/v1/cardwriter/check", json={"kind": "card", "source": str(src)},
                    headers=headers())
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["unsigned"]["phrase"] == f"INSTALL UNSIGNED {cw.file_sha256(src)[:8]}"
    assert doc["unsigned"]["how"] == "sha256sum CARD.img" and doc["card"]["bytes"] > 0
    r = client.post("/api/v1/cardwriter/check", json={"kind": "card", "source": str(rig.slot())},
                    headers=headers())
    assert r.status_code == 409 and "single OS slot" in r.json()["error"]["message"]
