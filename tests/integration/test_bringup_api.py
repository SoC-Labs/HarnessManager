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
from harness_manager_mps3 import mcc as mccmod
from harness_manager_mps3 import usb as usbmod
from harness_manager_mps3.sd import VolumeInfo
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t3_usb import FakePortInfo
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.virtual_board import VirtualMps3
from tests.unit.test_bringup_service import release_bundle, sd_tree

H = headers()


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
    r = c.post(f"{B}/bringup/install", json={"bundle": str(bundle), "backup_path": backup},
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
    r = c.post(f"{B}/bringup/install", json={"bundle": str(bad), "backup_path": backup},
               headers=H)
    assert r.status_code == 409 and r.json()["error"]["data"]["check"]["problems"]
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


def test_the_status_says_releases_are_refused_and_the_reader_is_off(client):
    c, _ = client
    st = c.get("/api/v1/bringup", headers=H).json()
    assert st["signing"]["refused"] is True and st["signing"]["name"] == "REFUSED"
    assert st["signing"]["reason"] == ("cannot verify channel.json: this build has no pinned "
                                       "update-signing keys")
    assert st["sd_flash"]["enabled"] is False and st["sd_flash"]["routes"] is False
    assert st["rescue_network"]["available"] is False
