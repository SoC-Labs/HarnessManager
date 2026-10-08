"""BRINGUP-USB over the REAL daemon, engine and MPS3 pack: a virtual board on its Debug USB
(``VirtualMps3(usb=True)``: the FakeMcc on ``fake://``, the FakeSdVolume as its V2M-MPS3
drive) found by the pack's own USB probe through injected port and volume listings. The
whole first install runs: scan, open, back up, check and write a bundle, MCC reboot, witness
the harness on Ethernet; each step has a negative twin that writes nothing.

No real port, volume or network is touched: the ports are the fakes', the drive is a temp
folder, and the Ethernet the witness probes is the virtual board's loopback shell."""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.daemon.app import create_app
from harness_manager.services import bringup
from harness_manager_mps3 import mcc as mccmod
from harness_manager_mps3 import usb as usbmod
from harness_manager_mps3.sd import VolumeInfo
from tests.fakes.cardwriter_fakes import guard  # noqa: F401 - the fixture
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t3_usb import FakePortInfo
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.virtual_board import VirtualMps3
from tests.unit.test_bringup_service import release_bundle, sd_tree


@pytest.fixture(autouse=True)
def _pool_from_its_first_address(monkeypatch):
    """These tests read the pool from its first address: ``--ip auto``'s start from the
    random MAC (``pool_start``) is tested in tests/unit/test_identity_assign.py."""
    from harness_manager.services import identity_assign as _IA

    monkeypatch.setattr(_IA, "pool_start", lambda addrs, mac=None: 0)

H = headers()


def phrase(bundle: Path | str, tmp_path: Path) -> str:
    """The typed INSTALL UNSIGNED <sha8> of a bundle, as POST /bringup/bundle shows it."""
    return bringup.check_bundle(str(bundle), tmp_path / "phrase-work").unsigned_phrase


def ports_of(vb: VirtualMps3) -> list[FakePortInfo]:
    """The FT4232H's four ports as pyserial lists them; interface 00 is the fake MCC."""
    devs = [vb.mcc_url, "fake://lane1-unused", "fake://lane2-unused", "fake://lane3-unused"]
    return [FakePortInfo(device=d, vid=0x0403, pid=0x6011, serial_number="FTVIRT",
                         location=f"1-4.2:1.{n}") for n, d in enumerate(devs)]


@pytest.fixture
def board(tmp_path: Path, monkeypatch) -> Iterator[VirtualMps3]:
    clock = FakeClock()
    monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
    monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
    monkeypatch.delenv("HARNESS_MANAGER_MPS3_OVERLAY_DIRS", raising=False)
    with VirtualMps3(tmp_path / "usb", usb=True) as vb:
        vb.mcc.clock = clock
        vb.mcc.down_s, vb.mcc.boot_s, vb.mcc.autoboot_window_s = 1.0, 25.0, 3.0
        counted = vb.mcc.on_reboot
        vb.mcc.on_reboot = lambda: (counted(), vb.shell.stop())
        vb.mcc.on_boot = vb.shell.start
        yield vb


def plug(monkeypatch, vb: VirtualMps3 | None, *, port: bool = True, drive: bool = True) -> None:
    ports = ports_of(vb) if vb is not None and port else []
    vols = [VolumeInfo(label="V2M-MPS3", root=str(vb.sd.root), device="/dev/fake-sdz1",
                       usb_path="1-4.1")] if vb is not None and drive else []
    monkeypatch.setattr(usbmod, "DEFAULT_ENV", usbmod.UsbEnv(lambda: list(ports),
                                                             lambda: list(vols)))


@pytest.fixture
def client(board, tmp_path) -> Iterator[tuple[TestClient, Any]]:
    eng = engine_for(board)
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None,
                                   state_dir=tmp_path / "svc")) as c:
            yield c, eng
    finally:
        eng.close_all()


def wait(c: TestClient, job: str, timeout: float = 60.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = c.get(f"/api/v1/jobs/{job}", headers=H).json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job} still running")


def scan(c: TestClient, board: VirtualMps3, **body: Any) -> dict:
    r = c.post("/api/v1/bringup/scan", json={"host": board.shell_endpoint, **body}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


# --- the scan ----------------------------------------------------------------------------------


def test_the_scan_finds_the_debug_usb_asks_its_mcc_and_sees_the_harness(client, board,
                                                                         monkeypatch):
    c, eng = client
    plug(monkeypatch, board)
    out = scan(c, board, ask_mcc=True)
    (row,) = out["boards"]
    assert row["board_id"].startswith("mps3@usb:") and row["mcc"]["url"] == board.mcc_url
    assert row["mcc_answer"]["state"] == "answers", row["mcc_answer"]
    assert any("HELP" in line for line in row["mcc_answer"]["reply"])
    assert row["volume"]["path"] == str(board.sd.root)
    assert row["drive"]["fpga_file"] == "MB/HBI0309C/Nanosoc/nanosoc.bit"
    assert row["ethernet"]["state"] == "running" and row["problems"] == []
    assert eng.open_boards() == []                     # opened for the question, closed again
    listed = c.get("/api/v1/boards", headers=H).json()["boards"]
    assert row["board_id"] in {b["board_id"] for b in listed}


def test_twin_nothing_plugged_says_what_to_check(client, board, monkeypatch):
    c, _ = client
    plug(monkeypatch, None)
    out = scan(c, board)
    assert out["boards"] == [] and out["empty"]["text"] == "No MPS3 Debug USB found on this PC."


def test_twin_a_drive_without_its_port_and_a_port_without_its_drive(client, board, monkeypatch):
    c, _ = client
    plug(monkeypatch, board, port=False)
    (row,) = scan(c, board)["boards"]
    assert row["mcc"] is None and any("no MCC serial port" in p for p in row["problems"])
    plug(monkeypatch, board, drive=False)
    (row,) = scan(c, board)["boards"]
    assert row["volume"] is None and any("no V2M-MPS3 drive" in p for p in row["problems"])


# --- the bundle ---------------------------------------------------------------------------------


def test_a_bundle_is_checked_and_an_ebf_bundle_refused(client, board, tmp_path):
    c, _ = client
    ok = c.post("/api/v1/bringup/bundle", json={"path": str(sd_tree(tmp_path / "good"))},
                headers=H)
    assert ok.status_code == 200 and ok.json()["check"]["base_bit"]["part"].startswith("xcku115")
    bad = sd_tree(tmp_path / "bad")
    (bad / "MB" / "HBI0309C" / "mbb_v141.ebf").write_bytes(b"MB BIOS")
    r = c.post("/api/v1/bringup/bundle", json={"path": str(bad)}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    assert r.json()["error"]["data"]["check"]["refused"] is True


# --- the first install, end to end -------------------------------------------------------------


def open_usb(c: TestClient, board: VirtualMps3, monkeypatch) -> str:
    plug(monkeypatch, board)
    (row,) = scan(c, board)["boards"]
    r = c.post("/api/v1/boards", json={"candidate": row["candidate"]}, headers=H)
    assert r.status_code == 200, r.text
    return row["board_id"]


def test_the_first_install_backs_up_writes_reboots_and_witnesses(client, board, tmp_path,
                                                                 monkeypatch):
    c, _ = client
    bid = open_usb(c, board, monkeypatch)
    B = bid_path(bid)
    bundle = release_bundle(tmp_path / "rel", linux=False)
    ebf_before = board.sd.ebf.read_bytes()
    # no backup: refused, nothing written
    r = c.post(f"{B}/bringup/install", json={"bundle": str(bundle)}, headers=H)
    assert r.status_code == 409 and "needs a backup" in r.json()["error"]["message"]
    assert (board.sd.root / "MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes() == b"\0" * 64
    # back up, then write
    job = c.post(f"{B}/storage/backup", json={"dest_dir": str(tmp_path / "bk")},
                 headers=H).json()["job"]
    backup = wait(c, job)["result"]["path"]
    # no INSTALL UNSIGNED <sha8>: refused, nothing written (the twin of the write below)
    r = c.post(f"{B}/bringup/install", json={"bundle": str(bundle), "backup_path": backup},
               headers=H)
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    err = r.json()["error"]
    assert err["message"] == (f"not confirmed: this bundle is unsigned; type exactly "
                              f"{phrase(bundle, tmp_path)!r} to install it")
    assert err["data"]["unsigned"]["phrase"] == phrase(bundle, tmp_path)
    assert (board.sd.root / "MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes() == b"\0" * 64
    r = c.post(f"{B}/bringup/install", json={"bundle": str(bundle), "backup_path": backup,
                                             "confirm_unsigned": phrase(bundle, tmp_path)},
               headers=H)
    assert r.status_code == 202, r.text
    done = wait(c, r.json()["job"])
    assert done["state"] == "done" and done["kind"] == "sd_install", done
    written = (board.sd.root / "MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes()
    assert written == (bundle / "sd/MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes()
    assert board.sd.ebf.read_bytes() == ebf_before                 # never an .ebf
    ovl = done["result"]["overlays"]
    assert ovl["added"] is True and ovl["path"] == str(bundle / "overlays" / "open")
    toml = (tmp_path / "svc" / "settings.toml").read_text()
    assert str(bundle / "overlays" / "open") in toml
    # reboot through the MCC, then witness the harness on Ethernet
    job = c.post(f"{B}/controller/reboot", json={}, headers=H).json()["job"]
    assert wait(c, job)["state"] == "done" and board.reboots == 1
    job = c.post(f"{B}/bringup/witness", json={"host": board.shell_endpoint, "wait_s": 30,
                                                "poll_s": 0.2}, headers=H).json()["job"]
    seen = wait(c, job)
    assert seen["state"] == "done" and seen["result"]["state"] == "running", seen


def test_twin_a_refused_bundle_writes_nothing_even_with_a_backup(client, board, tmp_path,
                                                                 monkeypatch):
    c, _ = client
    bid = open_usb(c, board, monkeypatch)
    B = bid_path(bid)
    job = c.post(f"{B}/storage/backup", json={"dest_dir": str(tmp_path / "bk")},
                 headers=H).json()["job"]
    backup = wait(c, job)["result"]["path"]
    bad = sd_tree(tmp_path / "bad")
    (bad / "MB" / "HBI0309C" / "Nanosoc" / "nanosoc.bit").unlink()
    before = board.sd.snapshot()
    r = c.post(f"{B}/bringup/install", json={"bundle": str(bad), "backup_path": backup,
                                             "confirm_unsigned": phrase(bad, tmp_path)},
               headers=H)
    assert r.status_code == 409 and r.json()["error"]["data"]["check"]["problems"]
    assert board.sd.snapshot() == before


def test_twin_a_wrong_sha8_or_a_bundle_changed_since_its_check_writes_nothing(
        client, board, tmp_path, monkeypatch):
    c, _ = client
    bid = open_usb(c, board, monkeypatch)
    B = bid_path(bid)
    job = c.post(f"{B}/storage/backup", json={"dest_dir": str(tmp_path / "bk")},
                 headers=H).json()["job"]
    backup = wait(c, job)["result"]["path"]
    bundle = sd_tree(tmp_path / "good")
    shown = c.post("/api/v1/bringup/bundle", json={"path": str(bundle)},
                   headers=H).json()["check"]["unsigned"]
    assert shown["phrase"] == phrase(bundle, tmp_path) and shown["of"] == "manifest"
    before = board.sd.snapshot()
    wrong = "INSTALL UNSIGNED " + ("0" * 8 if shown["sha256"][:8] != "0" * 8 else "1" * 8)
    r = c.post(f"{B}/bringup/install", json={"bundle": str(bundle), "backup_path": backup,
                                             "confirm_unsigned": wrong}, headers=H)
    assert r.status_code == 409 and "does not name this bundle" in r.json()["error"]["message"]
    (bundle / "config.txt").write_text("TITLE: changed after the check\n")
    r = c.post(f"{B}/bringup/install", json={"bundle": str(bundle), "backup_path": backup,
                                             "confirm_unsigned": shown["phrase"]}, headers=H)
    assert r.status_code == 409 and "does not name this bundle" in r.json()["error"]["message"]
    assert r.json()["error"]["data"]["unsigned"]["phrase"] != shown["phrase"]
    assert board.sd.snapshot() == before


def test_twin_the_witness_times_out_on_a_dark_address(client, board, monkeypatch):
    c, _ = client
    bid = open_usb(c, board, monkeypatch)
    board.shell.stop()                                     # the harness never answers
    job = c.post(f"{bid_path(bid)}/bringup/witness",
                 json={"host": board.shell_endpoint, "wait_s": 1, "poll_s": 0.2},
                 headers=H).json()["job"]
    out = wait(c, job)
    assert out["state"] == "failed" and out["error"]["data"]["timeout"] is True
    assert "restore the backup" in out["error"]["hint"]


def test_the_witness_default_wait_is_the_linux_one_when_the_bundle_is_linux(client, board,
                                                                           monkeypatch):
    c, _ = client
    bid = open_usb(c, board, monkeypatch)
    board.shell.stop()
    monkeypatch.setattr(bringup, "DEFAULT_WITNESS_S", 0.6)
    monkeypatch.setattr(bringup, "LINUX_WITNESS_S", 2.4)
    waited = {}
    for linux in (False, True):
        job = c.post(f"{bid_path(bid)}/bringup/witness",
                     json={"host": board.shell_endpoint, "linux": linux, "poll_s": 0.2},
                     headers=H).json()["job"]
        out = wait(c, job)
        assert out["state"] == "failed" and out["error"]["data"]["timeout"] is True
        waited[linux] = out["error"]["data"]["waited_s"]
    assert waited[False] < 1.5 < 2.3 <= waited[True]


def test_the_status_says_releases_are_refused_and_the_reader_is_off(client):
    c, _ = client
    st = c.get("/api/v1/bringup", headers=H).json()
    assert st["signing"]["refused"] is True and st["signing"]["name"] == "REFUSED"
    assert st["signing"]["reason"] == ("cannot verify channel.json: this build has no pinned "
                                       "update-signing keys")
    assert st["sd_flash"]["enabled"] is False
    assert st["sd_flash"]["routes"] is True        # integ: SD-FLASH's writer is merged
    assert st["rescue_network"]["available"] is False


# --- the hub lease: a board behind a hub keeps its gate; a USB-only board has none --------------


def test_a_board_behind_a_hub_needs_its_lease_to_write_and_a_usb_only_board_none(tmp_path):
    from harness_manager.demo import DemoEngine
    from harness_manager.demo_showcase import BOARD_LEASED, BOARD_NEW_USB

    eng = DemoEngine(speed=0.05, showcase=True, state_dir=tmp_path / "demo")
    bundle = str(sd_tree(tmp_path / "good"))
    try:
        with TestClient(create_app(eng, token=TOKEN, static_dir=None,
                                   state_dir=tmp_path / "demo")) as c:
            cands = {x["board_id"]: x for x in c.post("/api/v1/probe", json={},
                                                       headers=H).json()["candidates"]}
            assert c.post("/api/v1/boards", json={"candidate": cands[BOARD_LEASED]},
                          headers=H).status_code == 200
            r = c.post(f"{bid_path(BOARD_LEASED)}/bringup/install",
                       json={"bundle": bundle, "backup_path": str(tmp_path / "b.zip"),
                             "confirm_unsigned": phrase(bundle, tmp_path)}, headers=H)
            assert r.status_code == 409 and r.json()["error"]["name"] == "HELD", r.text
            assert "write the configuration SD" in r.json()["error"]["message"]
            assert eng.called("storage.install") == []
            # the twin: the new board on USB only has no lease to ask for
            (row,) = c.post("/api/v1/bringup/scan", json={}, headers=H).json()["boards"]
            assert row["board_id"] == BOARD_NEW_USB
            assert c.post("/api/v1/boards", json={"candidate": row["candidate"]},
                          headers=H).status_code == 200
            job = c.post(f"{bid_path(BOARD_NEW_USB)}/storage/backup", json={},
                         headers=H).json()["job"]
            backup = wait(c, job)["result"]["path"]
            r = c.post(f"{bid_path(BOARD_NEW_USB)}/bringup/install",
                       json={"bundle": bundle, "backup_path": backup,
                             "confirm_unsigned": phrase(bundle, tmp_path)}, headers=H)
            assert r.status_code == 202, r.text
            assert wait(c, r.json()["job"])["state"] == "done"
    finally:
        eng.close_all()


# --- POST /bringup/card-reader: the card-reader door for a bundle (the rig's card writer) --------


@pytest.fixture
def reader(tmp_path: Path, monkeypatch, guard) -> Iterator[tuple]:  # noqa: F811 - the fixture
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine
    from harness_manager.services import cardwriter as cw
    from tests.fakes.cardwriter_fakes import Rig
    from tests.fakes.t13_daemon import state_dir

    rig = Rig(tmp_path / "rig")

    def factory(*, state_dir=None, publish=None, **kw):  # noqa: ANN001, ANN003
        rig.writer.publish = publish
        rig.writer.state_dir = Path(state_dir)
        rig.writer._enabled = lambda: cw.is_enabled(state_dir)        # the real setting
        return rig.writer

    monkeypatch.setattr(cw, "CardWriter", factory)
    eng = Engine(EngineConfig(state_dir=state_dir()))
    with TestClient(create_app(eng, token=TOKEN, static_dir=None,
                               state_dir=tmp_path / "svc")) as c:
        yield rig, c
    eng.close_all()


def card_write(c: TestClient, rig: Any, bundle: Path, tmp_path: Path, **over: Any) -> Any:
    body = {"bundle": str(bundle), "device_id": rig.card("sdb").id,
            "confirm": "WRITE SD/MMC 31.9 GB", "confirm_unsigned": phrase(bundle, tmp_path),
            "backup_dir": str(tmp_path / "bk"), **over}
    return c.post("/api/v1/bringup/card-reader", json={k: v for k, v in body.items()
                                                       if v is not None}, headers=H)


def test_the_card_reader_door_writes_a_bundle_with_both_phrases(reader, tmp_path, monkeypatch):
    rig, c = reader
    monkeypatch.setenv("HARNESS_MANAGER_BRINGUP_SD_FLASH", "on")
    bundle = release_bundle(tmp_path / "rel", linux=False)
    r = card_write(c, rig, bundle, tmp_path)
    assert r.status_code == 202, r.text
    done = wait(c, r.json()["job"])
    assert done["state"] == "done" and done["kind"] == "cardwriter_write", done
    res = done["result"]
    assert res["kind"] == "files" and res["verified"] is True and res["source"].endswith("/sd")
    assert (rig.root / "MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes() == \
        (bundle / "sd/MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes()
    assert Path(res["backup"]["path"]).parent == tmp_path / "bk"


def test_twin_the_card_reader_door_refuses_without_either_phrase(reader, tmp_path, monkeypatch):
    rig, c = reader
    monkeypatch.setenv("HARNESS_MANAGER_BRINGUP_SD_FLASH", "on")
    bundle = sd_tree(tmp_path / "good")
    before = sorted(p.relative_to(rig.root).as_posix() for p in rig.root.rglob("*"))
    r = card_write(c, rig, bundle, tmp_path, confirm_unsigned=None)
    assert r.status_code == 409 and "this bundle is unsigned" in r.json()["error"]["message"]
    r = card_write(c, rig, bundle, tmp_path, confirm_unsigned="INSTALL UNSIGNED 0000000g")
    assert r.status_code == 409 and "does not name this bundle" in r.json()["error"]["message"]
    r = card_write(c, rig, bundle, tmp_path, confirm="WRITE SD/MMC 31914983424")
    assert r.status_code == 409 and "type exactly 'WRITE SD/MMC 31.9 GB'" in \
        r.json()["error"]["message"]
    ebf = sd_tree(tmp_path / "ebf")
    (ebf / "MB" / "HBI0309C" / "mbb_v141.ebf").write_bytes(b"MB BIOS")
    r = card_write(c, rig, ebf, tmp_path, confirm_unsigned="INSTALL UNSIGNED 00000000")
    assert r.status_code == 409 and r.json()["error"]["data"]["check"]["refused"] is True
    assert sorted(p.relative_to(rig.root).as_posix() for p in rig.root.rglob("*")) == before
    assert not (tmp_path / "bk").exists()


def test_twin_the_card_reader_door_is_unavailable_while_sd_flash_is_off(reader, tmp_path):
    rig, c = reader
    rig.enabled = True                                          # the rig would; the setting not
    r = card_write(c, rig, sd_tree(tmp_path / "good"), tmp_path, device_id="sdb-x")
    assert r.status_code == 422 and r.json()["error"]["name"] == "UNAVAILABLE"
    assert "bringup.sd_flash" in r.json()["error"]["message"]


# --- GET .../bringup/proposal: the identity proposed for the new board ---------------------------


def test_the_proposed_identity_names_the_board_from_its_serial_with_a_random_mac_and_ip(
        client, board, monkeypatch):
    """Lane IDENTITY (david 2 Oct): the name from the MCC's USB serial, a RANDOM MAC (never the
    serial's) and the pool's next free IP; the command sets exactly what was shown."""
    c, _ = client
    bid = open_usb(c, board, monkeypatch)
    r = c.get(f"{bid_path(bid)}/bringup/proposal", headers=H)
    assert r.status_code == 200, r.text
    p = r.json()["proposal"]
    assert p["serial"] == "FTVIRT" and p["label"] == "MPS3-VIRT"
    assert p["mac"].startswith("02:") and not p["mac"].startswith("02:00:00:")
    assert p["mac_how"] == "random" and p["ip"] == "192.168.10.110/24" and p["ip_how"] == "auto"
    assert p["pool"] == "192.168.10.110-199" and "derivation" not in p
    assert r.json()["command"] == (f"harness-manager board identity 192.168.10.101 --label "
                                   f"MPS3-VIRT --ip 192.168.10.110 --mac {p['mac']} "
                                   "--consent MPS3-VIRT")
    again = c.get(f"{bid_path(bid)}/bringup/proposal", params={"ip": "192.168.10.102"},
                  headers=H).json()
    assert again["proposal"]["mac"] != p["mac"]                 # random, every time
    assert again["command"].startswith("harness-manager board identity 192.168.10.102 ")


def test_twin_a_board_not_open_here_gets_no_proposal(client, board, monkeypatch):
    c, _ = client
    plug(monkeypatch, board)
    (row,) = scan(c, board)["boards"]
    r = c.get(f"{bid_path(row['board_id'])}/bringup/proposal", headers=H)
    assert r.status_code in (404, 409) and "proposal" not in r.json()
