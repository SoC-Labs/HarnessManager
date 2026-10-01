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
        eng._services["update"] = UpdateService(eng, downloader=dl, app_version="0.1.0", **kw)
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
    return f"mps3-harness-{version}-sd-HBI0309C.zip"


# --- the catalogue through GitHub -------------------------------------------------------------


def test_list_show_and_fetch_a_test_release_through_the_github_api(world, bare, capsys):
    rc, obj, err = world.run(capsys, "harness", "list", *src())
    assert rc == ExitCode.OK, err
    assert [r["version"] for r in obj["releases"]] == [BM]
    assert obj["channels"][0]["signed_by"].startswith(signer_mod.TEST_KEY_PREFIX)
    rc, obj, err = world.run(capsys, "harness", "show", BM, *src())
    assert rc == ExitCode.OK, err
    assert {c["name"] for c in obj["component_list"]} == {"sd-HBI0309C", "overlays-open"}
    assert obj["notes"].startswith("TEST BUILD")                    # the signed notes say so
    assert obj["identity"]["static_id"].lower() == S_ILA.lower() and obj["identity"]["fw_sha"] == "0e12a0b0"
    rc, obj, err = world.run(capsys, "harness", "fetch", BM, *src())
    assert rc == ExitCode.OK, err
    assert {c["name"]: c["result"] for c in obj["components"]} == {
        "sd-HBI0309C": "fetched", "overlays-open": "fetched"}
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
    assert {c["name"] for c in obj["component_list"]} == {"sd-HBI0309C", "os-slot",
                                                          "overlays-open"}
    rc, obj, err = world.run(capsys, "harness", "fetch", LX, *src())
    assert rc == ExitCode.OK, err
    assert {c["name"] for c in obj["components"] if c["result"] == "fetched"} == {
        "sd-HBI0309C", "os-slot", "overlays-open"}
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
