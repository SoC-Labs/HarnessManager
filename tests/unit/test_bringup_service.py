"""BRINGUP-USB: the service (services/bringup.py) without a board: the bundle check, the scan
over a scripted engine, the witness, the switches and the overlay dirs. Each behaviour has a
negative twin. Nothing here opens a port, mounts a volume or writes a device."""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HeldError,
    NothingOnTargetError,
    UsageError,
)
from harness_manager.core.model import BoardIdentity, Candidate, Link, LinkKind
from harness_manager.services import bringup
from harness_manager.services.update.bitheader import build_bit

USERID = "0xC8551081"


def sd_tree(root: Path, *, bit: bytes | None = None, part: str = "xcku115-flvb2104-2-e",
            app: str = "Nanosoc") -> Path:
    mb = root / "MB" / "HBI0309C"
    (mb / app).mkdir(parents=True, exist_ok=True)
    (root / "config.txt").write_text("TITLE: V2M-MPS3 config\nUSB_REMOTE: TRUE\n")
    (mb / "board.txt").write_text(f"[MCCS]\nMBBIOS: mbb_v141.ebf ;stock\n[APPLICATION NOTE]\n"
                                  f"APPFILE: {app}\\{app.lower()}.txt\n")
    (mb / app / f"{app.lower()}.txt").write_text("[FPGAS]\nF0FILE: nanosoc.bit ;the base\n")
    (mb / app / "nanosoc.bit").write_bytes(
        bit if bit is not None else build_bit("shell_top", part, b"\xff" * 512, userid=USERID))
    return root


def release_bundle(root: Path, *, linux: bool = True, overlays: bool = True) -> Path:
    sd_tree(root / "sd")
    if linux:
        (root / "linux_bundle.json").write_text(json.dumps({"version": "2.0.0"}))
        (root / "linux_slot.img").write_bytes(b"S0LB" + b"\0" * 1020)
    else:
        (root / "mint.json").write_text(json.dumps({"version": "1.1.0"}))
    if overlays:
        o = root / "overlays" / "open" / "synth"
        o.mkdir(parents=True)
        (o / "manifest.json").write_text("{}")
    return root


def zip_dir(src: Path, dest: Path, top: str = "") -> Path:
    with zipfile.ZipFile(dest, "w") as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, f"{top}{p.relative_to(src).as_posix()}")
    return dest


# --- the bundle check ---------------------------------------------------------------------------


def test_a_config_sd_folder_names_its_base_bit_with_size_sha256_part_and_userid(tmp_path):
    chk = bringup.check_bundle(sd_tree(tmp_path / "b"), tmp_path / "work")
    assert not chk.refused and chk.layout == "sd-tree" and chk.kind == "dir"
    bit = chk.base_bit
    assert bit["path"] == "MB/HBI0309C/Nanosoc/nanosoc.bit"
    assert bit["size"] == (tmp_path / "b" / bit["path"]).stat().st_size
    assert bit["sha256"] == bringup.file_sha256(tmp_path / "b" / bit["path"])
    assert bit["part"].startswith("xcku115") and bit["userid"] == USERID.lower()
    assert sorted(chk.install_files) == ["MB/HBI0309C/Nanosoc/nanosoc.bit",
                                         "MB/HBI0309C/Nanosoc/nanosoc.txt",
                                         "MB/HBI0309C/board.txt", "config.txt"]
    assert chk.board["mb_bios"] == "mbb_v141.ebf"          # named, never written


def test_twin_an_ebf_in_the_tree_is_refused_and_nothing_is_offered_to_write(tmp_path):
    root = sd_tree(tmp_path / "b")
    (root / "MB" / "HBI0309C" / "mbb_v141.ebf").write_bytes(b"MB BIOS")
    chk = bringup.check_bundle(root, tmp_path / "work")
    assert chk.refused and chk.install_files == {}
    assert any(".ebf" in p and "never written" in p for p in chk.problems)


def test_twin_a_tree_without_a_bit_is_refused(tmp_path):
    root = sd_tree(tmp_path / "b")
    (root / "MB" / "HBI0309C" / "Nanosoc" / "nanosoc.bit").unlink()
    chk = bringup.check_bundle(root, tmp_path / "work")
    assert chk.refused
    assert "no .bit: the config SD's base bitstream is missing" in chk.problems


def test_twin_files_outside_the_tree_and_mcc_command_files_are_refused(tmp_path):
    root = sd_tree(tmp_path / "b")
    (root / "notes.txt").write_text("hello")
    (root / "reboot.txt").write_text("")
    chk = bringup.check_bundle(root, tmp_path / "work")
    assert any(p.startswith("outside the config-SD tree") and "notes.txt" in p
               for p in chk.problems)
    assert any("reboot.txt" in p and "MCC command file" in p for p in chk.problems)


def test_twin_a_bit_for_another_part_or_not_a_bitstream_is_refused(tmp_path):
    wrong = bringup.check_bundle(sd_tree(tmp_path / "a", part="xc7z020clg400-1"), tmp_path / "w")
    assert any("built for xc7z020" in p for p in wrong.problems)
    junk = bringup.check_bundle(sd_tree(tmp_path / "b", bit=b"not a bitstream"), tmp_path / "w")
    assert any("is not a Xilinx bitstream" in p for p in junk.problems)


def test_twin_a_board_file_naming_a_bit_the_bundle_lacks_is_refused(tmp_path):
    root = sd_tree(tmp_path / "b")
    (root / "MB" / "HBI0309C" / "Nanosoc" / "nanosoc.txt").write_text("F0FILE: other.bit\n")
    chk = bringup.check_bundle(root, tmp_path / "w")
    assert any("names MB/HBI0309C/Nanosoc/other.bit" in p for p in chk.problems)


def test_desktop_files_are_ignored_not_refused(tmp_path):
    root = sd_tree(tmp_path / "b")
    (root / ".DS_Store").write_bytes(b"x")
    (root / "MB" / "Thumbs.db").write_bytes(b"x")
    chk = bringup.check_bundle(root, tmp_path / "w")
    assert not chk.refused and set(chk.ignored) == {".DS_Store", "MB/Thumbs.db"}
    assert ".DS_Store" not in chk.install_files


def test_a_release_bundle_zip_is_linux_with_a_slot_image_and_open_overlays(tmp_path):
    z = zip_dir(release_bundle(tmp_path / "rel"), tmp_path / "rel.zip", top="mps3-harness-2.0.0/")
    chk = bringup.check_bundle(z, tmp_path / "work")
    assert not chk.refused and chk.kind == "zip" and chk.layout == "release-bundle"
    assert chk.impl == "linux" and chk.version == "2.0.0"
    assert chk.os_image["kind"] == "slot"           # a SLOT image: never the card image
    assert chk.overlays["count"] == 1 and chk.overlays["names"] == ["synth"]
    assert Path(chk.sd_root).is_relative_to(tmp_path / "work")
    again = bringup.check_bundle(z, tmp_path / "work")           # the same zip: reused
    assert again.sd_root == chk.sd_root


def test_twin_a_bare_metal_release_bundle_is_not_linux_and_has_no_slot_image(tmp_path):
    chk = bringup.check_bundle(release_bundle(tmp_path / "rel", linux=False, overlays=False),
                               tmp_path / "w")
    assert chk.impl == "bare-metal" and chk.os_image is None and chk.overlays is None


def test_twin_a_zip_escaping_its_folder_is_refused(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../etc/passwd", "x")
    chk = bringup.check_bundle(z, tmp_path / "work")
    assert chk.refused and "escapes the archive" in chk.problems[0]


def test_twin_an_ebf_given_as_the_bundle_and_bad_paths(tmp_path):
    ebf = tmp_path / "mbb_v141.ebf"
    ebf.write_bytes(b"x")
    assert bringup.check_bundle(ebf, tmp_path / "w").refused
    with pytest.raises(UsageError):
        bringup.check_bundle("relative/dir", tmp_path / "w")
    with pytest.raises(AbsentError):
        bringup.check_bundle(tmp_path / "nothing", tmp_path / "w")
    (tmp_path / "empty").mkdir()
    assert "holds no config-SD tree" in bringup.check_bundle(tmp_path / "empty",
                                                             tmp_path / "w").problems[0]


# --- the drive ----------------------------------------------------------------------------------


def test_the_drive_says_what_it_loads(tmp_path):
    facts = bringup.drive_facts(str(sd_tree(tmp_path / "d", app="AN536")))
    assert facts["readable"] and facts["revisions"] == ["HBI0309C"]
    assert facts["fpga_file"] == "MB/HBI0309C/AN536/nanosoc.bit" and not facts["journal"]


def test_twin_a_missing_drive_is_not_readable(tmp_path):
    assert bringup.drive_facts(str(tmp_path / "gone")) == {
        "readable": False, "why": f"{tmp_path / 'gone'} is not a mounted folder"}


# --- the scan over a scripted engine ---------------------------------------------------------------


@dataclass
class Owner:
    who: str

    def describe(self) -> str:
        return self.who


@dataclass
class Ctl:
    reply: str = "CAP COPY DEBUG HELP REBOOT"
    error: Exception | None = None
    asked: list[str] = field(default_factory=list)
    last_transcript: bytes = b""

    def command(self, line: str) -> str:
        self.asked.append(line)
        if self.error is not None:
            raise self.error
        return self.reply


class Session:
    def __init__(self, ctl: Ctl | None) -> None:
        self.controller = ctl


class Engine:
    """engine.probe/open/close/session/lock_owner/open_boards, scripted."""

    def __init__(self, usb: list[Candidate], eth: list[Candidate] = (), *,
                 ctl: Ctl | None = None, owner: Owner | None = None) -> None:
        self.usb, self.eth, self.ctl, self.owner = list(usb), list(eth), ctl or Ctl(), owner
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.hints: list[Any] = []

    def probe(self, hints: Any) -> list[Candidate]:
        self.hints.append(hints)
        return list(self.usb) if hints.scan_usb else list(self.eth)

    def open_boards(self) -> list[str]:
        return []

    def lock_owner(self, bid: str) -> Owner | None:
        return self.owner

    def open(self, cand: Candidate, note: str = "") -> Session:
        self.opened.append(cand.board_id)
        return Session(self.ctl)

    def close(self, bid: str) -> None:
        self.closed.append(bid)


def usb_cand(n: int = 0, *, serial: bool = True, volume: str | None = "/media/me/V2M-MPS3"
             ) -> Candidate:
    links = []
    if serial:
        links.append(Link(LinkKind.USB_SERIAL, f"serial:///dev/ttyUSB{n}",
                          f"FT4232H X{n} if00: MCC console"))
        links.append(Link(LinkKind.USB_SERIAL, f"serial:///dev/ttyUSB{n + 1}",
                          f"FT4232H X{n} if01: FPGA UART lane 0 or 1 (MCC UARTMODE mux)"))
    if volume:
        links.append(Link(LinkKind.USB_MSD, volume, "V2M-MPS3 volume"))
    return Candidate("mps3", f"mps3@usb:{links[0].address}", tuple(links))


def eth_cand(host: str = "192.168.10.101", rescue: bool = False) -> Candidate:
    detail = "stage0 rescue: TFTP and identify only, no control channel" if rescue else \
        "shell control channel"
    return Candidate("mps3", f"mps3@{host}:6900", (Link(LinkKind.ETHERNET, f"{host}:6900", detail),),
                     identity=BoardIdentity("mps3", harness_version="1.0.0",
                                            harness_impl="" if rescue else "bare-metal"))


def test_scan_one_board_lists_its_mcc_drive_and_ethernet_and_asks_the_mcc(tmp_path):
    drive = sd_tree(tmp_path / "drive")
    eng = Engine([usb_cand(volume=str(drive))], [eth_cand()])
    out = bringup.scan(eng, ask=True)
    (row,) = out["boards"]
    assert row["mcc"]["port"] == "/dev/ttyUSB0" and row["lanes"][0]["port"] == "/dev/ttyUSB1"
    assert row["volume"]["path"] == str(drive) and row["drive"]["fpga_file"].endswith("nanosoc.bit")
    assert row["mcc_answer"]["state"] == "answers" and eng.ctl.asked == ["?"]
    assert eng.opened == eng.closed == [row["board_id"]]           # opened for the question only
    assert row["ethernet"]["state"] == "running" and row["problems"] == []
    usb_hints, eth_hints = eng.hints
    assert usb_hints.scan_usb and not usb_hints.scan_network and not usb_hints.hosts
    assert eth_hints.hosts == ("192.168.10.101",) and not eth_hints.scan_usb   # never a broadcast


def test_twin_scan_none_says_what_to_check():
    out = bringup.scan(Engine([]))
    assert out["boards"] == [] and out["empty"]["text"] == "No MPS3 Debug USB found on this PC."
    assert any("cable" in c for c in out["empty"]["check"])
    assert any("power" in c for c in out["empty"]["check"])
    assert any("mounted" in c for c in out["empty"]["check"])


def test_scan_two_boards_pairs_no_ethernet_and_says_why():
    out = bringup.scan(Engine([usb_cand(0), usb_cand(4, volume="/media/me/V2M-MPS3_")],
                              [eth_cand()]))
    assert len(out["boards"]) == 2 and all(b["ethernet"] is None for b in out["boards"])
    assert any("which one it is cannot be told" in n for n in out["notes"])
    assert any("2 Debug USBs" in n for n in out["notes"])


def test_scan_a_drive_without_a_port_and_a_port_without_a_drive_say_what_is_lost():
    no_port = bringup.scan(Engine([usb_cand(serial=False)]))["boards"][0]
    assert no_port["mcc"] is None and no_port["mcc_answer"]["state"] == "not-asked"
    assert any("no MCC serial port" in p for p in no_port["problems"])
    no_drive = bringup.scan(Engine([usb_cand(volume=None)]))["boards"][0]
    assert no_drive["volume"] is None and no_drive["drive"] is None
    assert any("no V2M-MPS3 drive" in p for p in no_drive["problems"])


def test_twin_an_mcc_without_a_prompt_or_held_elsewhere_is_said_never_raised():
    silent = Engine([usb_cand()], ctl=Ctl(error=NothingOnTargetError("no Cmd> prompt on /dev/x")))
    assert bringup.scan(silent, ask=True)["boards"][0]["mcc_answer"]["state"] == "silent"
    held = Engine([usb_cand()], owner=Owner("bob on lab-pc (pid 7)"))
    row = bringup.scan(held, ask=True)["boards"][0]
    assert row["mcc_answer"]["state"] == "held" and held.opened == []
    assert row["holder"] == "bob on lab-pc (pid 7)"
    unasked = Engine([usb_cand()])
    assert bringup.scan(unasked)["boards"][0]["mcc_answer"]["state"] == "not-asked"
    assert unasked.ctl.asked == []


def test_ethernet_tells_rescue_from_running_and_matches_the_host():
    assert bringup.ethernet(Engine([], [eth_cand(rescue=True)]))["state"] == "rescue"
    assert bringup.ethernet(Engine([], [eth_cand("192.168.10.104")]))["state"] == "none"
    assert bringup.ethernet(Engine([], [eth_cand("127.0.0.1")]), "127.0.0.1:6900")["state"] == \
        "running"


# --- the witness ---------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class LateEngine(Engine):
    def __init__(self, after: int, cand: Candidate) -> None:
        super().__init__([], [])
        self.after, self.cand, self.calls = after, cand, 0

    def probe(self, hints: Any) -> list[Candidate]:
        self.calls += 1
        return [self.cand] if self.calls > self.after else []


def test_the_witness_waits_until_the_harness_answers():
    c = Clock()
    seen = []
    out = bringup.witness(LateEngine(3, eth_cand()), wait_s=60, poll_s=3, now=c.now,
                          sleep=c.sleep, progress=lambda p, d, t: seen.append(p))
    assert out["state"] == "running" and out["tries"] == 4 and out["took_s"] == 9.0
    assert seen == ["waiting", "waiting", "waiting", "running"]


def test_twin_the_witness_times_out_with_the_restore_hint():
    c = Clock()
    with pytest.raises(ActionFailedError) as e:
        bringup.witness(LateEngine(10**6, eth_cand()), wait_s=10, poll_s=3, now=c.now,
                        sleep=c.sleep)
    assert e.value.data["timeout"] is True and e.value.data["host"] == "192.168.10.101"
    assert "restore the backup" in e.value.hint and "192.168.10.0/24" in e.value.hint


def test_the_witness_reports_stage0_rescue():
    c = Clock()
    out = bringup.witness(LateEngine(0, eth_cand(rescue=True)), wait_s=5, now=c.now,
                          sleep=c.sleep)
    assert out["state"] == "rescue" and "RESCUE" in out["text"]


# --- the switches --------------------------------------------------------------------------------


def test_sd_flash_is_off_by_default_and_says_how_to_turn_it_on(tmp_path):
    got = bringup.sd_flash(tmp_path, env={})
    assert got["enabled"] is False and got["value"] == "off"
    assert "bringup.sd_flash" in got["reason"] and "HARNESS_MANAGER_BRINGUP_SD_FLASH" in got["reason"]


def test_twin_sd_flash_on_by_its_variable(tmp_path):
    got = bringup.sd_flash(tmp_path, env={"HARNESS_MANAGER_BRINGUP_SD_FLASH": "on"})
    assert got["enabled"] is True and got["reason"] == ""


class Trust:
    def __init__(self, n: int) -> None:
        self.n = n

    def all_keys(self) -> list[int]:
        return list(range(self.n))


class Upd:
    def __init__(self, n: int) -> None:
        self.trust = Trust(n)


def test_releases_are_refused_while_no_signing_key_is_pinned():
    for eng in (object(), type("E", (), {"update": Upd(0)})()):
        got = bringup.signing(eng)
        assert got["refused"] is True and got["keys"] == 0
    assert "docs/KEYS.md" in bringup.signing(type("E", (), {"update": Upd(0)})())["reason"]


def test_twin_a_pinned_key_lets_releases_through():
    got = bringup.signing(type("E", (), {"update": Upd(1)})())
    assert got == {"keys": 1, "refused": False, "reason": ""}


def test_status_carries_the_switches_the_network_door_and_the_card_image_hint():
    st = bringup.status(object())
    assert st["default_host"] == "192.168.10.101"
    assert st["rescue_network"] == {"available": False,
                                    "reason": "comes with Linux v2.1 (HARNESS-DIST L3)",
                                    "note": bringup.RESCUE_NETWORK_NOTE}
    assert "stage0_mkcard.py card --card-img" in st["card_image"]["hint"]
    assert st["usb_write_warning"].startswith("A USB write can take 5 minutes")


# --- the overlay dirs ---------------------------------------------------------------------------


def test_a_bundles_overlays_join_mps3_overlay_dirs_once(tmp_path, monkeypatch):
    from harness_manager.demo import DemoEngine
    from harness_manager.settings import ops

    monkeypatch.delenv("HARNESS_MANAGER_MPS3_OVERLAY_DIRS", raising=False)
    eng = DemoEngine()
    try:
        sctx = ops.SettingsContext(state_dir=tmp_path / "state", engine=eng)
        first = bringup.add_overlay_dir(sctx, tmp_path / "b" / "overlays" / "open")
        assert first["added"] is True and first["dirs"] == [str(tmp_path / "b/overlays/open")]
        assert first["shadowed"] is False and "result" in first
        again = bringup.add_overlay_dir(sctx, tmp_path / "b" / "overlays" / "open")
        assert again["added"] is False and again["dirs"] == first["dirs"]       # the twin
        toml = (tmp_path / "state" / "settings.toml").read_text()
        assert "overlay_dirs" in toml and str(tmp_path / "b" / "overlays" / "open") in toml
    finally:
        eng.close_all()


def test_twin_a_zips_overlays_are_kept_outside_the_cleared_work_area(tmp_path):
    z = zip_dir(release_bundle(tmp_path / "rel"), tmp_path / "rel.zip")
    chk = bringup.check_bundle(z, tmp_path / "work")
    kept = bringup.keep_overlays(chk, tmp_path / "keep")
    assert kept.is_relative_to(tmp_path / "keep") and (kept / "synth" / "manifest.json").is_file()
    folder = bringup.check_bundle(release_bundle(tmp_path / "rel2"), tmp_path / "work")
    assert bringup.keep_overlays(folder, tmp_path / "keep") == tmp_path / "rel2/overlays/open"
    plain = bringup.check_bundle(sd_tree(tmp_path / "plain"), tmp_path / "work")
    assert bringup.keep_overlays(plain, tmp_path / "keep") is None


def test_held_error_is_not_swallowed_by_ask_mcc_import_check():
    # ask_mcc maps every HarnessError to a state; a HELD from the gate is one of them
    eng = Engine([usb_cand()], ctl=Ctl(error=HeldError("busy", holder="job x")))
    assert bringup.ask_mcc(eng, usb_cand())["state"] == "error"
