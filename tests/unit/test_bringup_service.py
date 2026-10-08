"""BRINGUP-USB: the service (services/bringup.py) without a board: the bundle check, the scan
over a scripted engine, the witness, the switches and the overlay dirs. Each behaviour has a
negative twin. Nothing here opens a port, mounts a volume or writes a device."""

from __future__ import annotations

import json
import sys
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


# --- an unsigned bundle: its sha256 and the typed INSTALL UNSIGNED <sha8> (david 2 Oct, D3a) ---


def _hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def test_a_folders_sha256_is_its_manifest_one_sha256sum_line_per_file_in_byte_order(tmp_path):
    root = tmp_path / "b"
    (root / "sd" / "MB").mkdir(parents=True)
    (root / "sd" / "config.txt").write_bytes(b"cfg")
    (root / "sd" / "MB" / "x.txt").write_bytes(b"x")
    (root / "B.txt").write_bytes(b"upper")              # "B" (0x42) sorts before "a" (0x61)
    (root / "a.txt").write_bytes(b"lower")
    (root / "odd\\name").write_bytes(b"odd")           # escaped as sha256sum escapes it
    manifest, sums, links = bringup.folder_manifest(root)
    assert manifest == b"".join([
        _hex(b"upper").encode() + b"  B.txt\n",
        _hex(b"lower").encode() + b"  a.txt\n",
        b"\\" + _hex(b"odd").encode() + b"  odd\\\\name\n",
        _hex(b"x").encode() + b"  sd/MB/x.txt\n",           # "M" (0x4D) before "c" (0x63)
        _hex(b"cfg").encode() + b"  sd/config.txt\n"])
    assert sums["sd/config.txt"] == _hex(b"cfg") and links == []
    chk = bringup.check_bundle(root, tmp_path / "work")
    assert chk.sha256 == _hex(manifest) and chk.sha256_of == "manifest"
    assert chk.manifest_files == 5


@pytest.mark.skipif(sys.platform == "win32", reason="the recipe is GNU find/sort/sha256sum")
def test_the_manifest_recipe_prints_the_same_sha256(tmp_path):
    import subprocess

    root = release_bundle(tmp_path / "rel")
    (root / "notes with space.txt").write_text("n")
    recipe = bringup.MANIFEST_RECIPE.replace("cd FOLDER", f"cd '{root}'")
    out = subprocess.run(["bash", "-c", recipe], capture_output=True, text=True, check=True)
    assert out.stdout.split()[0] == bringup.check_bundle(root, tmp_path / "w").sha256


def test_twin_any_change_to_any_file_changes_the_folders_sha256(tmp_path):
    root = release_bundle(tmp_path / "rel")
    first = bringup.check_bundle(root, tmp_path / "w").sha256
    (root / "overlays" / "open" / "synth" / "manifest.json").write_text('{"x": 1}')
    second = bringup.check_bundle(root, tmp_path / "w").sha256
    assert first != second
    (root / "overlays" / "open" / "synth" / "manifest.json").write_text("{}")
    assert bringup.check_bundle(root, tmp_path / "w").sha256 == first     # deterministic


def test_a_zips_sha256_is_the_zip_files_own(tmp_path):
    z = zip_dir(release_bundle(tmp_path / "rel"), tmp_path / "rel.zip")
    chk = bringup.check_bundle(z, tmp_path / "w")
    assert chk.sha256 == _hex(z.read_bytes()) and chk.sha256_of == "zip"
    u = chk.as_dict()["unsigned"]
    assert u["phrase"] == f"INSTALL UNSIGNED {chk.sha256[:8]}" and u["how"] == bringup.ZIP_RECIPE
    assert u["banner"] == ("Unsigned: Harness Manager cannot check where this came from; only "
                           "install a bundle you built or got from SoC Labs directly.")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_twin_a_symbolic_link_anywhere_in_a_folder_is_refused(tmp_path):
    root = release_bundle(tmp_path / "rel")
    (root / "overlays" / "open" / "linked").symlink_to(tmp_path)
    chk = bringup.check_bundle(root, tmp_path / "w")
    assert chk.refused and any("overlays/open/linked: a symbolic link; a bundle carries only "
                               "regular files" in p for p in chk.problems)
    (root / "overlays" / "open" / "linked").unlink()
    assert not bringup.check_bundle(root, tmp_path / "w").refused


def test_require_unsigned_takes_the_phrase_and_refuses_anything_else(tmp_path):
    from harness_manager.core.errors import RefusedError

    chk = bringup.check_bundle(sd_tree(tmp_path / "t"), tmp_path / "w")
    want = chk.unsigned_phrase
    bringup.require_unsigned(chk, want)
    bringup.require_unsigned(chk, f"  INSTALL UNSIGNED   {want[-8:].upper()} ")
    for typed in (None, "", "yes", want[:-1], "INSTALL UNSIGNED", "install unsigned "
                  + want[-8:], f"INSTALL UNSIGNED {'0' * 8 if want[-8:] != '0' * 8 else '1' * 8}"):
        with pytest.raises(RefusedError) as e:
            bringup.require_unsigned(chk, typed)
        assert e.value.data["unsigned"]["phrase"] == want, typed
        assert "nothing was written" in e.value.hint


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


def test_releases_are_refused_while_no_signing_key_is_pinned_in_the_stores_own_words():
    from harness_manager.services.update.trust import TrustStore

    for eng in (object(), type("E", (), {"update": Upd(0)})()):
        got = bringup.signing(eng)
        assert got["refused"] is True and got["keys"] == 0
    real = type("U", (), {"trust": TrustStore(pinned=())})()
    got = bringup.signing(type("E", (), {"update": real})())
    assert got["name"] == "REFUSED"
    assert got["reason"] == "cannot verify channel.json: this build has no pinned update-signing keys"
    assert "install updates by hand" in got["hint"]


def test_twin_a_pinned_key_lets_releases_through():
    got = bringup.signing(type("E", (), {"update": Upd(1)})())
    assert got == {"keys": 1, "refused": False, "name": "", "hint": "", "reason": ""}


def test_status_carries_the_switches_the_network_door_and_the_card_image_hint():
    st = bringup.status(object())
    assert st["default_host"] == "192.168.10.101"
    assert st["rescue_network"] == {"available": False,
                                    "reason": "comes with Linux v2.1",
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


# --- the proposed identity (david 2 Oct, D4a) ------------------------------------------------------


def _usb_cand(detail: str = "FT4232H FT6ABC12 if00: MCC console", evidence: str = "",
              address: str = "serial:///dev/ttyUSB7") -> Candidate:
    return Candidate(pack="mps3", board_id="mps3@usb:/dev/ttyUSB7", evidence=evidence,
                     links=(Link(LinkKind.USB_SERIAL, address, detail),))


def test_the_mcc_serial_is_the_debug_usbs_ft4232h_serial_from_the_probe():
    assert bringup.mcc_serial(_usb_cand()) == "FT6ABC12"
    assert bringup.mcc_serial(_usb_cand(detail="MCC console (given explicitly)",
                                        evidence="FT4232H 0403:6011 serial FTEVID9 at USB 1-4"),
                              list_ports=lambda: []) == "FTEVID9"


def test_a_port_given_by_hand_is_looked_up_in_this_pcs_port_list_windows_letters_too():
    from tests.fakes.t3_usb import FakePortInfo

    given = _usb_cand(detail="MCC console (given explicitly)", address="COM7")
    win = [FakePortInfo(device=f"COM{7 + n}", vid=0x0403, pid=0x6011,
                        serial_number=f"FTWIN42{'ABCD'[n]}", location="") for n in range(4)]
    assert bringup.mcc_serial(given, list_ports=lambda: win) == "FTWIN42"
    linux = [FakePortInfo(device=f"/dev/ttyUSB{7 + n}", vid=0x0403, pid=0x6011,
                          serial_number="FTLNX1", location=f"1-4.2:1.{n}") for n in range(4)]
    assert bringup.mcc_serial(_usb_cand(detail="MCC console (given explicitly)"),
                              list_ports=lambda: linux) == "FTLNX1"


def test_twin_no_serial_anywhere_is_empty_never_a_guess():
    assert bringup.mcc_serial(None) == ""
    assert bringup.mcc_serial(_usb_cand(detail="FT4232H ? if00: MCC console"),
                              list_ports=lambda: []) == ""
    from tests.fakes.t3_usb import FakePortInfo

    other = [FakePortInfo(device="/dev/ttyUSB0", vid=0x0403, pid=0x6011, serial_number="OTHER",
                          location="1-1:1.0")]                     # another board's MCC
    assert bringup.mcc_serial(_usb_cand(detail="MCC console (given explicitly)"),
                              list_ports=lambda: other) == ""


# --- lane IDENTITY (david 2 Oct): a random MAC and the pool's next free IP, not the serial's ---


def _mps3_policy(**kw):
    from harness_manager.services.identity_assign import IdentityPolicy

    return IdentityPolicy(pack="mps3", reserved_mac_prefixes=("02:00:00",),
                          ip_pool="192.168.10.110-199", reserved_ips=("192.168.10.101",), **kw)


def _bytes(*rolls: bytes):
    it = iter(rolls)
    return lambda n: next(it)


def test_the_mac_is_random_locally_administered_and_unicast():
    from harness_manager.services import board_identity as BI

    p = bringup.propose_identity("FT6ABC12", policy=_mps3_policy(),
                                 urandom=_bytes(bytes.fromhex("ff5e3a91c017")))
    assert p["mac"] == "02:5e:3a:91:c0:17" and p["mac_how"] == "random"     # byte 0 forced
    assert BI.mac_is_local(p["mac"]) and BI.mac_is_unicast_nonzero(p["mac"])
    # two proposals for the same serial differ: nothing is derived from it any more
    a = bringup.propose_identity("FT6ABC12", policy=_mps3_policy())["mac"]
    b = bringup.propose_identity("FT6ABC12", policy=_mps3_policy())["mac"]
    assert a != b and a.startswith("02:") and not a.startswith("02:00:00:")


def test_twin_the_image_range_and_a_mac_in_the_registry_are_rolled_again():
    rolls = _bytes(bytes.fromhex("0200004d5053"), bytes.fromhex("02000012abcd"),
                   bytes.fromhex("021111111111"), bytes.fromhex("022222222222"))
    p = bringup.propose_identity("FT6ABC12", policy=_mps3_policy(),
                                 taken_macs={"02:11:11:11:11:11": ["mps3@other"]}, urandom=rolls)
    assert p["mac"] == "02:22:22:22:22:22"           # 02:00:00:* twice, then the registry's


def test_the_proposal_name_ip_and_notes_fit_the_identity_writer():
    from harness_manager.services import board_identity as BI

    # the MAC's last byte 0x00: the pool's first address (110 + 0 mod 90)
    p = bringup.propose_identity("FT6ABC12", policy=_mps3_policy(rescue_note="rescue: .101"),
                                 urandom=_bytes(bytes.fromhex("025e3a91c000")))
    assert p["label"] == "MPS3-BC12" and p["hostname"] == "mps3-bc12"
    assert p["ip"] == "192.168.10.110/24" and p["ip_how"] == "auto" and p["ip_error"] == ""
    assert p["same_net"] == "this PC must be on the same /24 (e.g. 192.168.10.1/24)"
    assert BI.validate_want({k: p[k] for k in ("label", "ip", "mac")}) == {
        "label": "MPS3-BC12", "ip": "192.168.10.110/24", "mac": p["mac"]}
    assert p["notes"] == [
        "every board gets its own IP: a free address of the pool, searched from its MAC (the "
        "image default 192.168.10.101 is never given)", "rescue: .101"]
    assert bringup.propose_identity("ab-1", policy=_mps3_policy())["label"] == "MPS3-AB1"
    assert bringup.propose_identity("x", policy=_mps3_policy(),
                                    taken_ips={"192.168.10.110": ["b"]},
                                    urandom=_bytes(bytes.fromhex("025e3a91c05a")))["ip"] == \
        "192.168.10.111/24"                          # 0x5a = 90 -> .110, taken: skipped
    assert bringup.identity_command("192.168.10.101", p) == (
        f"harness-manager board identity 192.168.10.101 --label MPS3-BC12 --ip "
        f"192.168.10.110 --mac {p['mac']} --consent MPS3-BC12")


def test_the_proposal_ip_starts_from_the_random_mac():
    # the lab's seat sheet (david 2 Oct): 192.168.10.(110 + mac[5] mod 90), then the skips
    p = bringup.propose_identity("FT6ABC12", policy=_mps3_policy(),
                                 urandom=_bytes(bytes.fromhex("025e3a91c017")))
    assert p["mac"].endswith(":17") and p["ip"] == "192.168.10.133/24"     # 110 + 23
    p = bringup.propose_identity("FT6ABC12", policy=_mps3_policy(),
                                 urandom=_bytes(bytes.fromhex("025e3a91c0ff")))
    assert p["ip"] == "192.168.10.185/24"                                  # 110 + 255 mod 90


def test_twin_the_mac_start_skips_the_registry_and_wraps_round_the_pool():
    taken = {"192.168.10.199": ["a"]}
    p = bringup.propose_identity("FT6ABC12", policy=_mps3_policy(), taken_ips=taken,
                                 urandom=_bytes(bytes.fromhex("025e3a91c059")))   # 89 -> .199
    assert p["ip"] == "192.168.10.110/24"                  # .199 taken: wraps to the start


def test_twin_no_serial_proposes_no_name_and_says_why():
    p = bringup.propose_identity("", policy=_mps3_policy(), urandom=_bytes(bytes.fromhex("025e3a91c000")))
    assert p["label"] == p["hostname"] == "" and p["mac"].startswith("02:")
    assert p["notes"][0] == ("the MCC's USB serial number is not known (the Debug USB did not "
                             "report one): give the board a name yourself")
    assert bringup.identity_command("h", p) == (f"harness-manager board identity h --ip "
                                                f"192.168.10.110 --mac {p['mac']}")


def test_twin_an_exhausted_pool_proposes_no_ip_and_says_why():
    full = {f"192.168.10.{n}": ["b"] for n in range(110, 200)}
    p = bringup.propose_identity("FT6ABC12", policy=_mps3_policy(), taken_ips=full)
    assert p["ip"] == "" and p["ip_how"] == ""
    assert p["ip_error"].startswith("no free address in the pool 192.168.10.110-199: all 90 "
                                    "are taken (90 given to or seen on boards here")
    assert "--ip" not in bringup.identity_command("h", p)


# --- WIZARD-FIT: the v2.0 release bundle layout (config-sd/ + overlays/ + linux_bundle.json) -------


def v2_bundle(root: Path, *, overlays: dict[str, dict] | None = None, aaa: bool = False) -> Path:
    sd_tree(root / "config-sd")
    (root / "linux_bundle.json").write_text(json.dumps({"static_id": "0x44EE76D5",
                                                        "static_usercode": "0xFB1F8C76"}))
    (root / "linux_slot.img").write_bytes(b"S0LB" + b"\0" * 1020)
    keyed = {"static_id": "0x44EE76D5", "static_usercode": "0xFB1F8C76", "ip_class": "open"}
    for name, man in (overlays if overlays is not None else {"led": keyed, "uart_echo": keyed}).items():
        o = root / "overlays" / name
        o.mkdir(parents=True)
        (o / "manifest.json").write_text(json.dumps(man))
    if aaa:
        (root / "overlays" / "aaa").mkdir()
    return root


def test_the_v2_bundle_root_is_accepted_linux_with_its_overlays(tmp_path):
    chk = bringup.check_bundle(v2_bundle(tmp_path / "b"), tmp_path / "w")
    assert not chk.refused and chk.layout == "release-bundle"
    assert chk.sd_root == str(tmp_path / "b" / "config-sd")
    assert chk.impl == "linux" and chk.os_image["kind"] == "slot"
    assert chk.overlays["names"] == ["led", "uart_echo"]
    assert chk.overlays["path"] == str(tmp_path / "b" / "overlays")
    assert chk.warnings == [] and "config.txt" in chk.install_files


def test_the_v2_bundle_as_a_zip_with_a_top_folder(tmp_path):
    z = zip_dir(v2_bundle(tmp_path / "b"), tmp_path / "b.zip", top="mps3-bundle/")
    chk = bringup.check_bundle(z, tmp_path / "w")
    assert not chk.refused and chk.impl == "linux" and chk.overlays["count"] == 2


def test_the_old_sd_and_overlays_open_layout_still_works(tmp_path):
    chk = bringup.check_bundle(release_bundle(tmp_path / "old"), tmp_path / "w")
    assert not chk.refused and chk.sd_root == str(tmp_path / "old" / "sd")
    assert chk.overlays["path"] == str(tmp_path / "old" / "overlays" / "open")


def test_a_bare_sd_folder_is_accepted_and_has_no_overlays_nor_linux(tmp_path):
    chk = bringup.check_bundle(sd_tree(tmp_path / "card"), tmp_path / "w")
    assert not chk.refused and chk.layout == "sd-tree" and chk.overlays is None and chk.impl == ""


def test_the_config_sd_folder_of_a_v2_bundle_finds_its_bundle_one_folder_up(tmp_path):
    root = v2_bundle(tmp_path / "b")
    chk = bringup.check_bundle(root / "config-sd", tmp_path / "w")
    assert not chk.refused and chk.impl == "linux" and chk.overlays["count"] == 2


def test_twin_a_folder_that_is_neither_is_refused_naming_both_layouts(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "readme.txt").write_text("hi")
    chk = bringup.check_bundle(tmp_path / "x", tmp_path / "w")
    assert chk.refused and "config-sd/ or sd/" in chk.problems[0]


def test_overlays_are_checked_ip_class_and_keying(tmp_path):
    keyed = {"static_id": "0x44EE76D5", "static_usercode": "0xFB1F8C76", "ip_class": "open"}
    chk = bringup.check_bundle(v2_bundle(tmp_path / "b", overlays={
        "led": keyed,
        "wrong": {**keyed, "static_id": "0x11111111"},
        "secret": {**keyed, "ip_class": "aaa"}}, aaa=True), tmp_path / "w")
    assert not chk.refused
    assert chk.overlays["names"] == ["led", "wrong"] and chk.overlays["excluded"] == ["secret"]
    assert any("secret is ip_class aaa" in w for w in chk.warnings)
    assert any("wrong is keyed to static_id 0x11111111" in w and "0x44EE76D5" in w
               for w in chk.warnings)
    assert any("overlays/aaa" in w for w in chk.warnings)
    kept = bringup.keep_overlays(chk, tmp_path / "keep")        # only the open ones join
    assert sorted(p.name for p in kept.iterdir()) == ["led", "wrong"]


def test_twin_a_clean_bundle_has_no_overlay_warnings_and_joins_in_place(tmp_path):
    chk = bringup.check_bundle(v2_bundle(tmp_path / "b"), tmp_path / "w")
    assert chk.warnings == [] and "excluded" not in chk.overlays
    assert bringup.keep_overlays(chk, tmp_path / "keep") == tmp_path / "b" / "overlays"


def test_a_v2_bundle_without_overlays_has_none(tmp_path):
    chk = bringup.check_bundle(v2_bundle(tmp_path / "b", overlays={}), tmp_path / "w")
    assert not chk.refused and chk.overlays is None


# --- WIZARD-FIT: the witness waits the Linux time for a Linux bundle --------------------------------


def test_the_witness_wait_is_the_linux_budget_for_a_linux_bundle_else_bare_metal(tmp_path):
    from harness_manager.services.update.planner import LINUX_REBOOT_WAIT_S

    assert bringup.witness_wait_s(True) == bringup.LINUX_WITNESS_S == LINUX_REBOOT_WAIT_S
    assert bringup.witness_wait_s(False) == bringup.DEFAULT_WITNESS_S
    assert bringup.LINUX_WITNESS_S > 240 > bringup.DEFAULT_WITNESS_S - 1     # 3-4 min fits
    linux = bringup.check_bundle(v2_bundle(tmp_path / "b"), tmp_path / "w")
    bare = bringup.check_bundle(sd_tree(tmp_path / "card"), tmp_path / "w")
    assert bringup.witness_wait_s(linux.impl == "linux") == 300.0
    assert bringup.witness_wait_s(bare.impl == "linux") == 180.0
    assert bringup.status(None)["witness_s"] == {"bare-metal": 180.0, "linux": 300.0}
    assert "3 to 4 minutes" in bringup.status(None)["linux_boot_note"]
