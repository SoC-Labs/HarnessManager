"""RELEASE-PIPE end to end, no network: platform artifacts -> ``harness-release --test-key`` ->
a fake private GitHub (``tests/fakes/fake_github.py``, 127.0.0.1) -> the real CLI's
``harness list|show|fetch|install`` -> a VirtualMps3's config SD.

The TEST key reaches the client through the EXISTING test trust seam: the CLI's engine
factory (``cli.engine.set_engine_factory``) builds the ``UpdateService`` with an explicit
``trust=TrustStore(...)``, as tests/integration/test_hcat_cli.py does. Nothing a user can
set (no environment variable, no setting, no file) makes a client trust a TEST key: the
twin below runs the client with its default trust, ``trust.PINNED_KEYS``, and it refuses.

- bare metal: list, show, fetch through the GitHub API with the token; ``harness install -
  --door usb --volume V --serial S`` (a USB-only board) backs up the card, writes the
  release's config-SD tree, reboots through the MCC and witnesses the FPGA configure, then
  says "written, not running" because nothing can confirm without Ethernet; with Ethernet
  too, the same install is CONFIRMED;
- twins: a tampered asset (sha256 mismatch) is refused and the card is untouched; an unsigned
  channel, a wrong key and a real client's default trust are refused;
- Linux: list, show and fetch (the OS slot image's S0LB frames checked); on a USB-only board
  the install is refused today (the OS image needs the running harness: HARNESS-DIST L3).
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_manager.cli import main as climain
from harness_manager.cli.engine import set_engine_factory
from harness_manager.core.errors import ExitCode
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.update import UpdateService, trust
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.state import UpdateState
from harness_manager.services.update.trust import ROLE_RELEASE, TrustedKey, TrustStore
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.fake_github import FakeGitHub
from tests.fakes.hcat_catalog import BoardRig
from tests.fakes.test_release_platform import (
    S_ILA,
    S_LNX,
    bare_metal_platform,
    linux_platform,
    release_args,
)
from tools.release import signer as signer_mod
from tools.release.cli import main as release_main
from tools.release.harness import MIN_APP

REPO = "SoC-Labs/HarnessManager"
SRC = f"github:{REPO}"
BM, LX = "1.2.0-rc1", "2.0.0-rc1"


def build(tmp: Path, platform, version: str, *extra: str) -> Path:
    out = tmp / f"release-{version}"
    lines: list[str] = []
    rc = release_main(release_args(platform, out, version, "--test-key", *extra),
                      printer=lines.append)
    assert rc == 0, "\n".join(lines)
    return out


def trust_of(out: Path) -> TrustStore:
    pk = signer_mod.read_public_key(out / "TEST-KEY.pub")
    assert signer_mod.is_test_key(pk)
    return TrustStore(pinned=(TrustedKey(pk, ROLE_RELEASE, trust.CHANNELS, "RELEASE-PIPE TEST"),))


class World:
    """One fake GitHub serving release trees; a board; the CLI's engine factory (the seam)."""

    def __init__(self, tmp: Path, monkeypatch, gh: FakeGitHub) -> None:
        self.tmp, self.gh = tmp, gh
        self.trust: TrustStore | None = None              # None: the client's default trust
        # integ v1.1: a release's compat.min_app is 1.0.0 by default (the per-revision MBBIOS
        # rule a B+C config SD needs), so the client here is a 1.0.0 one.
        self.app_version = MIN_APP
        self.rig = BoardRig(tmp, SimpleNamespace(trust=lambda: TrustStore()), monkeypatch)
        self.monkeypatch = monkeypatch
        self.previous = set_engine_factory(self.factory)

    def factory(self, _args):
        rig = self.rig
        eng = Engine(EngineConfig(state_dir=rig.state_dir),
                     packs={"mps3": Mps3Pack(console_ports=rig.vb.console_ports)})
        dl = Downloader(UpdateState.under(rig.state_dir).cache, token=self.gh.token,
                        github_api=self.gh.api, mirrors=())
        kw = {"trust": self.trust} if self.trust is not None else {}
        eng._services["update"] = UpdateService(eng, downloader=dl, app_version=self.app_version,
                                                **kw)
        return eng

    def run(self, capsys, *argv: str) -> tuple[int, dict, str]:
        self.monkeypatch.setattr("sys.stdin", io.StringIO(""))
        rc = climain.main(["--json", *argv])
        out, err = capsys.readouterr()
        return rc, (json.loads(out) if out.strip() else {}), err

    def usb_only(self) -> list[str]:
        vb = self.rig.vb
        return ["-", "--serial", vb.mcc_url, "--volume", str(vb.sd.root)]

    def both_links(self) -> list[str]:
        vb = self.rig.vb
        return [vb.shell_endpoint, "--serial", vb.mcc_url, "--volume", str(vb.sd.root)]

    def close(self) -> None:
        set_engine_factory(self.previous)
        self.rig.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_MANAGER_GITHUB_TOKEN", raising=False)
    with FakeGitHub() as gh:
        w = World(tmp_path, monkeypatch, gh)
        try:
            yield w
        finally:
            w.close()


@pytest.fixture
def bare(tmp_path, world):
    p = bare_metal_platform(tmp_path / "bm")
    out = build(tmp_path, p, BM)
    world.gh.publish_tree(out)
    world.trust = trust_of(out)
    return SimpleNamespace(platform=p, out=out)


def src(*extra: str) -> list[str]:
    return ["--source", SRC, "--channel", "beta", *extra]


def sd_zip_name(version: str) -> str:
    return f"mps3-harness-{version}-sd-HBI0309BC.zip"


# --- the catalogue through GitHub -------------------------------------------------------------


def test_list_show_and_fetch_a_test_release_through_the_github_api(world, bare, capsys):
    rc, obj, err = world.run(capsys, "harness", "list", *src())
    assert rc == ExitCode.OK, err
    assert [r["version"] for r in obj["releases"]] == [BM]
    assert obj["channels"][0]["signed_by"].startswith(signer_mod.TEST_KEY_PREFIX)
    rc, obj, err = world.run(capsys, "harness", "show", BM, *src())
    assert rc == ExitCode.OK, err
    assert {c["name"] for c in obj["component_list"]} == {"sd-HBI0309BC", "overlays-open"}
    assert obj["notes"].startswith("TEST BUILD")                    # the signed notes say so
    assert obj["identity"]["static_id"].lower() == S_ILA.lower() and obj["identity"]["fw_sha"] == "0e12a0b0"
    rc, obj, err = world.run(capsys, "harness", "fetch", BM, *src())
    assert rc == ExitCode.OK, err
    assert {c["name"]: c["result"] for c in obj["components"]} == {
        "sd-HBI0309BC": "fetched", "overlays-open": "fetched"}
    api = [r for r in world.gh.requests if r["path"].startswith("/repos/")]
    assert api and all(r["auth_ok"] for r in api)                   # the token went to the API
    assert not any(h["auth"] for h in world.gh.storage_hits)        # ... never to storage


def test_install_on_a_usb_only_board_writes_the_card_and_reboots_it(world, bare, capsys):
    vb, p = world.rig.vb, bare.platform
    ebf_before = vb.sd.snapshot()["MB/HBI0309C/mbb_v132.ebf"]
    rc, obj, err = world.run(capsys, "harness", "install", "-", BM, *world.usb_only()[1:],
                             "--door", "usb", "--consent", f"REKEY {S_ILA.lower()}", "--yes",
                             *src())
    # Without Ethernet nothing can confirm what runs: written + rebooted, not confirmed.
    assert rc == ExitCode.ACTION_FAILED, err
    outcome = obj["error"]["data"]["outcome"]
    assert outcome["result"] == "written-not-running"
    assert "nothing to confirm with" in outcome["detail"]
    assert outcome["evidence"]["fpga_configured"] is True           # the MCC REBOOT, witnessed
    assert Path(outcome["backup"]["path"]).is_file()                # the card was backed up first
    assert outcome["stored"] == ["overlays-open/greybox", "overlays-open/led"]   # no Arm IP
    root = vb.sd.root
    assert (root / "MB/HBI0309C/Nanosoc/nanosoc.bit").read_bytes() == p.bit.read_bytes()
    assert (root / "MB/HBI0309C/board.txt").read_text().startswith("BOARD: HBI0309C\n")
    assert (root / "MB/HBI0309C/Nanosoc/images.txt").read_text() == \
        (p.templates / "images.txt").read_text()
    assert (root / "config.txt").read_text() == (p.templates / "config.txt").read_text()
    assert vb.sd.snapshot()["MB/HBI0309C/mbb_v132.ebf"] == ebf_before   # never an .ebf
    assert world.rig.bound["booted"][-1]["sha"] == "0e12a0b0"        # it booted the new .bit


def test_twin_with_ethernet_too_the_same_install_is_confirmed(world, bare, capsys):
    rc, obj, err = world.run(capsys, "harness", "install", *world.both_links()[:1], BM,
                             *world.both_links()[1:], "--door", "usb", "--yes", *src())
    assert rc == ExitCode.OK, err
    assert obj["result"] == "installed" and obj["version"] == BM


# --- integ v1.1: a release needs Harness Manager 1.0.0 by default (--min-app) ------------------


def test_a_release_needs_1_0_0_by_default_so_an_older_client_is_blocked(world, bare, capsys):
    assert MIN_APP == "1.0.0"
    world.app_version = "0.9.0"
    rc, obj, err = world.run(capsys, "harness", "show", BM, *world.both_links(), *src())
    assert rc == ExitCode.OK, err
    assert (f"harness {BM} needs harness-manager >= 1.0.0 (this is 0.9.0); run `harness-manager "
            "update app` first") in obj["plan"]["blockers"], obj["plan"]


def test_twin_a_1_0_0_client_is_not_blocked_by_min_app(world, bare, capsys):
    rc, obj, err = world.run(capsys, "harness", "show", BM, *world.both_links(), *src())
    assert rc == ExitCode.OK, err
    assert not [b for b in obj["plan"]["blockers"] if "needs harness-manager" in b], obj["plan"]


def test_twin_min_app_given_is_written_as_given(tmp_path, world, capsys):
    p = bare_metal_platform(tmp_path / "bm-old")
    out = build(tmp_path, p, BM, "--min-app", "0.9.0")
    world.gh.publish_tree(out)
    world.trust = trust_of(out)
    world.app_version = "0.9.0"
    rc, obj, err = world.run(capsys, "harness", "show", BM, *world.both_links(), *src())
    assert rc == ExitCode.OK, err
    assert not [b for b in obj["plan"]["blockers"] if "needs harness-manager" in b], obj["plan"]


# --- FIX-PACK-9: a B+C release (the default, david 2 Oct) onto a C card and onto a B card ------

B_NOTE = ("MBBIOS: HBI0309C mbb_v141.ebf from the bundle (the card has no mbb_v141.ebf, so the "
          "MCC will not update); MBBIOS kept: HBI0309B mbb_v132.ebf")
C_NOTE = ("MBBIOS kept: HBI0309C mbb_v132.ebf; MBBIOS: HBI0309B mbb_v141.ebf from the bundle "
          "(the card has no mbb_v141.ebf, so the MCC will not update)")
CARD_LINE = "MBBIOS: mbb_v132.ebf  ;the card's own\n"


def make_rev_b(world) -> None:
    """The rig's board becomes a Rev B: its card serves MB/HBI0309B only (with its own MBBIOS
    line), LOG.TXT says so, and its MCC reads that folder and prints rev B."""
    from tests.fakes.fake_mcc import BOOT_BANNER

    vb = world.rig.vb
    root = vb.sd.root
    (root / "MB" / "HBI0309C").rename(root / "MB" / "HBI0309B")
    board = root / "MB" / "HBI0309B" / "board.txt"
    board.write_text("BOARD: HBI0309B\n[MCCS]\n" + CARD_LINE + board.read_text())
    (root / "LOG.TXT").write_text("MotherBoard Revision B Variant A\r\n")
    vb.mcc.boot_banner = tuple(x.replace("rev C", "rev B").replace("HBI0309C", "HBI0309B")
                               for x in BOOT_BANNER)
    world.rig.bound["rev"] = "HBI0309B"


def test_a_bc_release_installs_onto_a_c_card_with_both_folders(world, bare, capsys):
    vb, p = world.rig.vb, bare.platform
    board = vb.sd.root / "MB" / "HBI0309C" / "board.txt"
    board.write_text("BOARD: HBI0309C\n[MCCS]\n" + CARD_LINE + board.read_text())
    rc, obj, err = world.run(capsys, "harness", "install", *world.both_links()[:1], BM,
                             *world.both_links()[1:], "--door", "usb", "--yes", *src())
    assert rc == ExitCode.OK, err
    assert obj["result"] == "installed" and obj["notes"] == [C_NOTE]
    root = vb.sd.root
    for rev in "BC":
        assert (root / f"MB/HBI0309{rev}/Nanosoc/nanosoc.bit").read_bytes() == p.bit.read_bytes()
    assert CARD_LINE in (root / "MB/HBI0309C/board.txt").read_text()     # the card's, kept
    b_txt = (root / "MB/HBI0309B/board.txt").read_text()
    assert b_txt.startswith("BOARD: HBI0309B\n") and "MBBIOS: mbb_v141.ebf" in b_txt
    assert world.rig.bound["booted"][-1]["sha"] == "0e12a0b0"          # read MB/HBI0309C


def test_twin_the_same_release_installs_onto_a_rev_b_card(world, bare, capsys):
    make_rev_b(world)
    vb, p = world.rig.vb, bare.platform
    rc, obj, err = world.run(capsys, "harness", "show", BM, *world.both_links(), *src())
    assert rc == ExitCode.OK, err
    assert any(x.startswith("Rev B: boots, untested. This board is HBI0309B (LOG.TXT on its "
                            "config SD)") for x in obj["plan"]["warnings"]), obj
    rc, obj, err = world.run(capsys, "harness", "install", *world.both_links()[:1], BM,
                             *world.both_links()[1:], "--door", "usb", "--yes", *src())
    assert rc == ExitCode.OK, err
    assert obj["result"] == "installed" and obj["notes"] == [B_NOTE]
    root = vb.sd.root
    assert CARD_LINE in (root / "MB/HBI0309B/board.txt").read_text()     # the card's, kept
    assert (root / "MB/HBI0309C/board.txt").read_text().startswith("BOARD: HBI0309C\n")
    for rev in "BC":
        assert (root / f"MB/HBI0309{rev}/Nanosoc/nanosoc.bit").read_bytes() == p.bit.read_bytes()
    assert world.rig.bound["booted"][-1]["sha"] == "0e12a0b0"          # read MB/HBI0309B


def test_twin_a_c_only_release_onto_the_rev_b_card_is_refused_and_writes_nothing(
        tmp_path, world, capsys):
    p = bare_metal_platform(tmp_path / "bm-c")
    out = build(tmp_path, p, BM, "--board-rev", "C")
    world.gh.publish_tree(out)
    world.trust = trust_of(out)
    make_rev_b(world)
    before = world.rig.vb.sd.snapshot()
    rc, obj, err = world.run(capsys, "harness", "install", *world.both_links()[:1], BM,
                             *world.both_links()[1:], "--door", "usb", "--yes", *src())
    assert rc == ExitCode.REFUSED, err
    assert ("this board is HBI0309B (LOG.TXT on its config SD), and harness 1.2.0-rc1 carries "
            "MB/HBI0309C only: the MCC reads only MB/HBI0309B/, so the board would stay "
            "unprogrammed") in obj["error"]["message"]
    assert world.rig.vb.sd.snapshot() == before and world.rig.bound["booted"] == []


def test_twin_a_tampered_asset_is_refused_and_the_card_is_untouched(world, bare, capsys):
    zname = sd_zip_name(BM)
    good = (bare.out / REPO / "releases/download" / f"mps3-harness-v{BM}" / zname).read_bytes()
    bad = bytearray(good)
    bad[len(bad) // 2] ^= 0x01                   # same size, other bytes: only sha256 can tell
    world.gh.replace_bytes(zname, bytes(bad))
    rc, obj, err = world.run(capsys, "harness", "fetch", BM, *src())
    assert rc == ExitCode.REFUSED and "sha256" in obj["error"]["message"], err
    before = world.rig.vb.sd.snapshot()
    rc, obj, err = world.run(capsys, "harness", "install", "-", BM, *world.usb_only()[1:],
                             "--door", "usb", "--consent", f"REKEY {S_ILA.lower()}", "--yes",
                             *src())
    assert rc != ExitCode.OK
    assert world.rig.vb.sd.snapshot() == before and world.rig.bound["booted"] == []


def test_twin_an_unsigned_channel_is_refused(tmp_path, world, capsys):
    out = build(tmp_path, bare_metal_platform(tmp_path / "bm"), BM)
    world.trust = trust_of(out)
    (out / REPO / "releases/download/channel-mps3-harness-beta/channel.json.minisig").unlink()
    world.gh.publish_tree(out)
    rc, obj, err = world.run(capsys, "harness", "list", *src())
    assert rc != ExitCode.OK and not obj.get("releases")
    assert "minisig" in json.dumps(obj) + err


def test_twin_a_wrong_key_is_refused(tmp_path, world, bare, capsys):
    other = build(tmp_path / "other", bare_metal_platform(tmp_path / "bm2"), BM)
    world.trust = trust_of(other)                # trusts ANOTHER test key, not the signer
    rc, obj, err = world.run(capsys, "harness", "list", *src())
    assert rc == ExitCode.REFUSED and "does not trust" in obj["error"]["message"]


def test_twin_a_real_client_refuses_a_test_release(world, bare, capsys):
    world.trust = None                           # the client's own trust: trust.PINNED_KEYS
    rc, obj, err = world.run(capsys, "harness", "list", *src())
    assert rc == ExitCode.REFUSED
    assert "no pinned update-signing keys" in obj["error"]["message"] or \
        "does not trust" in obj["error"]["message"]


# --- the Linux harness ---------------------------------------------------------------------------


def test_a_linux_release_lists_fetches_and_says_what_a_usb_only_install_lacks(tmp_path, world,
                                                                             capsys):
    p = linux_platform(tmp_path / "lx")
    out = build(tmp_path, p, LX, "--bit", str(p.rebake_bit), "--stage0-bake",
                str(p.rebake_record))
    world.gh.publish_tree(out)
    world.trust = trust_of(out)
    rc, obj, err = world.run(capsys, "harness", "show", LX, *src())
    assert rc == ExitCode.OK, err
    assert obj["identity"]["static_id"].lower() == S_LNX.lower() and obj["identity"]["impl"] == "linux"
    assert {c["name"] for c in obj["component_list"]} == {"sd-HBI0309BC", "os-slot",
                                                          "overlays-open"}
    rc, obj, err = world.run(capsys, "harness", "fetch", LX, *src())
    assert rc == ExitCode.OK, err
    assert {c["name"] for c in obj["components"] if c["result"] == "fetched"} == {
        "sd-HBI0309BC", "os-slot", "overlays-open"}
    before = world.rig.vb.sd.snapshot()
    rc, obj, err = world.run(capsys, "harness", "install", "-", LX, *world.usb_only()[1:],
                             "--door", "usb", "--consent", f"REKEY {S_LNX.lower()}", "--yes",
                             *src())
    assert rc == ExitCode.REFUSED
    assert "OS slot" in obj["error"]["message"]          # L3: provisioning from rescue, not built
    assert world.rig.vb.sd.snapshot() == before


def test_the_release_tree_is_a_mirror_the_client_reads_without_github(tmp_path, world, bare,
                                                                      capsys):
    """The same tree, copied as a directory (a USB stick, the hub), is a source too."""
    copy = tmp_path / "stick"
    shutil.copytree(bare.out, copy)
    rc, obj, err = world.run(capsys, "harness", "list", "--source", str(copy), "--channel",
                             "beta")
    assert rc == ExitCode.OK, err
    assert [r["version"] for r in obj["releases"]] == [BM]
