"""RELEASE-PIPE: ``python -m tools.release harness-release`` (platform artifacts -> a signed
harness release) and ``publish-check`` (may a built tree go to OWNER/REPO).

Every behaviour has a negative twin, and a refusal writes no release. Fixtures are a fake
platform tree (``tests/fakes/test_release_platform.py``); keys are TEST or throwaway keys
under pytest's temp dir; nothing reaches the network, the hub or a device.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

import pytest

from harness_manager.services.update import minisign, trust
from tests.fakes.test_release_platform import (
    S_LNX,
    U_LNX,
    bare_metal_platform,
    linux_platform,
    release_args,
)
from tools.release import signer as signer_mod
from tools.release.cli import main
from tools.release.common import Layout, ReleaseError
from tools.release.publish_check import check

CAT, REPO, AAA = "mps3-harness", "SoC-Labs/HarnessManager", "SoC-Labs/mps3-harness-aaa"
V = "2.0.0-rc1"


def run(argv: list[str]) -> tuple[int, str]:
    lines: list[str] = []
    rc = main(argv, printer=lines.append)
    return rc, "\n".join(lines)


def channel(out: Path) -> dict:
    return json.loads(Layout(out).channel_file(CAT, "beta").read_bytes())


def entry(out: Path) -> dict:
    return channel(out)["harness"]["releases"][0]


def nothing_released(out: Path) -> bool:
    return not (out / "SoC-Labs").exists()


@pytest.fixture
def lx(tmp_path):
    return linux_platform(tmp_path / "plat")


# --- assembling a Linux release --------------------------------------------------------------


def test_a_linux_release_from_platform_artifacts_with_a_test_key(tmp_path, lx):
    out = tmp_path / "out"
    rc, text = run(release_args(lx, out, V, "--test-key"))
    assert rc == 0, text
    rel = entry(out)
    comps = {c["name"]: c for c in rel["components"]}
    assert set(comps) == {"sd-HBI0309C", "os-slot", "overlays-open"}      # no Arm IP, no kit
    assert rel["identity"]["static_id"] == S_LNX and rel["identity"]["usercode"] == U_LNX
    assert rel["legal_info"]["name"].endswith("linux_legal_info.tar")
    # the SD tree is the templates, stamped as assemble_sd.sh does, with the stage0 .bit
    sd = Layout(out).asset_path(rel["tag"], Path(comps["sd-HBI0309C"]["url"]).name)
    with zipfile.ZipFile(sd) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    assert set(files) == {"config.txt", "MB/HBI0309C/board.txt", "MB/HBI0309C/Nanosoc/nanosoc.txt",
                          "MB/HBI0309C/Nanosoc/images.txt", "MB/HBI0309C/Nanosoc/nanosoc.bit"}
    assert files["MB/HBI0309C/Nanosoc/nanosoc.bit"] == lx.bit.read_bytes()
    assert files["MB/HBI0309C/board.txt"].startswith(b"BOARD: HBI0309C\n")
    assert files["config.txt"] == (lx.templates / "config.txt").read_bytes()
    # the Arm-IP RMs and the stray file are named as left out
    report = json.loads((out / "plans" / f"{CAT}-beta" / "report.json").read_text())
    left = report["assembled"]["left_out"]
    assert set(left) == {"nanosoc", "eth_ss", "mps3_shell_static_id.c"}
    assert left["nanosoc"].startswith("Arm IP")
    assert {a["name"] for a in report["assets"]} == {
        Path(c["url"]).name for c in comps.values()} | {rel["legal_info"]["name"]}
    assert all(len(a["sha256"]) == 64 and a["size"] > 0 for a in report["assets"])
    assert "assets:" in text and "TEST BUILD: never published" in text


def test_the_test_key_marks_everything_and_does_not_outlive_the_run(tmp_path, lx):
    out = tmp_path / "out"
    key_dirs = set(Path(tempfile.gettempdir()).glob("hm-test-key-*"))
    assert run(release_args(lx, out, V, "--test-key"))[0] == 0
    assert set(Path(tempfile.gettempdir()).glob("hm-test-key-*")) == key_dirs   # deleted
    doc = channel(out)
    kid = doc["signing_key_id"]
    assert kid.startswith(signer_mod.TEST_KEY_PREFIX) and doc["test"]["key_id"] == kid
    sig = minisign.parse_signature((Layout(out).channel_file(CAT, "beta").with_name(
        "channel.json.minisig")).read_bytes())
    assert "test:1" in sig.trusted_comment
    assert entry(out)["notes"].startswith("TEST BUILD")
    pub = signer_mod.read_public_key(out / "TEST-KEY.pub")
    assert pub.id_hex == kid and (out / "TEST-BUILD.txt").is_file()
    assert not any(p.suffix == ".key" for p in out.rglob("*"))           # no secret key left


def test_twin_a_release_key_by_path_signs_without_a_test_marker(tmp_path, lx):
    sk, pk = signer_mod.keygen_throwaway(tmp_path / "keys", "release")    # release.key/.pub
    out = tmp_path / "out"
    rc, text = run(release_args(lx, out, V, "--key", str(sk), "--signer", "python"))
    assert rc == 0, text
    doc = channel(out)
    assert "test" not in doc and doc["signing_key_id"] == signer_mod.read_public_key(pk).id_hex
    assert not (out / "TEST-KEY.pub").exists()
    assert "not pinned" in text                                           # U2: no key pinned yet


@pytest.mark.parametrize("why, extra", [
    ("no signing key", []),
    ("never", ["--test-key", "--publish"]),
    ("is a TEST key", ["--key", "TESTKEY"]),
])
def test_key_refusals_write_nothing(tmp_path, lx, why, extra):
    if "TESTKEY" in extra:
        sk, _pk = signer_mod.keygen_test(tmp_path / "tk")
        extra = ["--key", str(sk), "--signer", "python"]
    out = tmp_path / "out"
    rc, text = run(release_args(lx, out, V, *extra))
    assert rc != 0 and why in text and nothing_released(out)


def test_a_test_key_is_never_pinned_and_lives_only_in_the_temp_dir(tmp_path):
    assert not [k.id_hex for k in trust.PINNED_KEYS if signer_mod.is_test_key(k.key)]
    sk, pk = signer_mod.keygen_test(tmp_path / "k")
    assert signer_mod.is_test_key(signer_mod.read_public_key(pk))
    assert oct(sk.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ReleaseError, match="temp dir only"):
        signer_mod.keygen_test(Path.home() / "hm-release-pipe-never-here")
    # a random (non-TEST) throwaway key is not taken for one
    _sk2, pk2 = signer_mod.keygen_throwaway(tmp_path / "k2")
    assert not signer_mod.is_test_key(signer_mod.read_public_key(pk2))   # 1 in 2**32 if not


def test_twin_a_real_release_is_never_built_on_a_test_channel(tmp_path, lx):
    out = tmp_path / "out"
    assert run(release_args(lx, out, V, "--test-key"))[0] == 0
    sk, _pk = signer_mod.keygen_throwaway(tmp_path / "keys", "release")
    rc, text = run(release_args(lx, out, "2.0.0-rc2", "--key", str(sk), "--signer", "python",
                                "--trust-key", str(out / "TEST-KEY.pub")))
    assert rc != 0 and "TEST channel" in text
    assert [r["version"] for r in channel(out)["harness"]["releases"]] == [V]


# --- the stage0 re-bake, the overlays, the notes --------------------------------------------------


def test_a_stage0_re_bake_replaces_the_flashable_bit_only_with_its_record(tmp_path, lx):
    out = tmp_path / "out"
    rc, text = run(release_args(lx, out, V, "--test-key", "--bit", str(lx.rebake_bit)))
    assert rc != 0 and "is not the flashable .bit" in text and nothing_released(out)
    rc, text = run(release_args(lx, out, V, "--test-key", "--bit", str(lx.rebake_bit),
                                "--stage0-bake", str(lx.rebake_record), "--keep-bundle"))
    assert rc == 0, text
    assert "the OS image records stage0 e1a2172d" in text            # the image's own record
    lb = json.loads((out / "bundles" / f"{CAT}-{V}" / "linux_bundle.json").read_text())
    mcc = lb["targets"]["mcc_sd"]
    assert mcc["flashable_bit"]["name"] == lx.rebake_bit.name
    assert mcc["rebake"]["replaces"]["name"] == "config_rm_greybox_stage0.bit"
    assert json.loads(lx.prod.joinpath("linux_bundle.json").read_text())["targets"]["mcc_sd"][
        "flashable_bit"]["name"] == "config_rm_greybox_stage0.bit"     # the input is untouched


@pytest.mark.parametrize("field, value, why", [
    ("static_id", "0x12345678", "static_id"),
    ("static_usercode", "0xDEADBEEF", "static_usercode"),
    ("stage0_identity_checked", False, "stage0_identity_checked"),
    ("mint_kind", "prototype", "prototype"),
])
def test_twin_a_re_bake_of_another_static_or_kind_is_refused(tmp_path, lx, field, value, why):
    rec = json.loads(lx.rebake_record.read_text())
    rec[field] = value
    lx.rebake_record.write_text(json.dumps(rec))
    out = tmp_path / "out"
    rc, text = run(release_args(lx, out, V, "--test-key", "--bit", str(lx.rebake_bit),
                                "--stage0-bake", str(lx.rebake_record)))
    assert rc != 0 and why in text and nothing_released(out)


def test_include_aaa_puts_the_arm_ip_rms_in_the_private_repo_only(tmp_path, lx):
    out = tmp_path / "out"
    rc, text = run(release_args(lx, out, V, "--test-key", "--include-aaa", "--kit", str(lx.kit)))
    assert rc == 0, text
    comps = {c["name"]: c for c in entry(out)["components"]}
    aaa = comps["overlays-aaa"]
    assert aaa["access"] == "github-token" and aaa["repo"] == AAA
    assert "/mps3-harness-aaa/releases/download/" in aaa["url"]
    with zipfile.ZipFile(Layout(out, aaa_repo=AAA).asset_path(
            entry(out)["tag"], Path(aaa["url"]).name, AAA)) as zf:
        assert {n.split("/")[0] for n in zf.namelist()} == {"nanosoc", "eth_ss"}
    with zipfile.ZipFile(Layout(out).asset_path(entry(out)["tag"],
                                                Path(comps["overlays-open"]["url"]).name)) as zf:
        assert {n.split("/")[0] for n in zf.namelist()} == {"greybox", "led"}
    kit = comps["kit"]
    assert kit["access"] == "github-token" and Path(kit["url"]).name == f"mps3-kit-{S_LNX}.zip"
    assert "AMD IP" in text                                             # the licence call named


def test_twin_an_overlay_the_bundle_was_not_packed_with_is_refused(tmp_path, lx):
    man = lx.overlays / "led" / "manifest.json"
    m = json.loads(man.read_text())
    m["partial"]["crc32"] = "0x00000001"
    man.write_text(json.dumps(m))
    out = tmp_path / "out"
    rc, text = run(release_args(lx, out, V, "--test-key"))
    assert rc != 0 and "overlay led is not the one linux_bundle.json was packed with" in text
    assert nothing_released(out)


def test_notes_with_placeholders_are_refused_and_filled_ones_are_signed(tmp_path, lx):
    notes = tmp_path / "notes.md"
    notes.write_text("v2.0.0 at «RELEASE_SHA»\n")
    out = tmp_path / "out"
    rc, text = run(release_args(lx, out, V, "--test-key", "--notes", str(notes)))
    assert rc != 0 and "placeholders" in text and nothing_released(out)
    notes.write_text("v2.0.0: the Linux harness.\n")
    assert run(release_args(lx, out, V, "--test-key", "--notes", str(notes)))[0] == 0
    assert entry(out)["notes"].endswith("v2.0.0: the Linux harness.")


def test_a_bare_metal_release_from_a_mint_record(tmp_path):
    p = bare_metal_platform(tmp_path / "plat")
    out = tmp_path / "out"
    rc, text = run(release_args(p, out, "1.2.0", "--test-key"))
    assert rc == 0, text
    rel = entry(out)
    assert rel["identity"]["impl"] == "bare-metal" and rel["identity"]["fw_sha"] == "0e12a0b0"
    assert {c["name"] for c in rel["components"]} == {"sd-HBI0309C", "overlays-open"}


def test_twin_a_dirty_mint_is_refused(tmp_path):
    p = bare_metal_platform(tmp_path / "plat", dirty=True)
    out = tmp_path / "out"
    rc, text = run(release_args(p, out, "1.2.0", "--test-key"))
    assert rc == 15 and "[DIRTY]" in text and nothing_released(out)


def test_harness_release_never_publishes(tmp_path, lx):
    sk, _pk = signer_mod.keygen_throwaway(tmp_path / "keys", "release")
    out = tmp_path / "out"
    rc, text = run(release_args(lx, out, V, "--key", str(sk), "--signer", "python", "--publish"))
    assert rc == 2 and "publish_harness_release.sh" in text and nothing_released(out)


# --- publish-check ------------------------------------------------------------------------------


@pytest.fixture
def built(tmp_path, lx):
    out = tmp_path / "out"
    assert run(release_args(lx, out, V, "--test-key"))[0] == 0
    return out


def test_publish_check_dry_run_warns_and_writes_the_plan_inputs(tmp_path, built):
    pc = check(built, REPO)
    assert pc.test and not pc.pinned and pc.version == V and pc.tag == f"{CAT}-v{V}"
    assert any("TEST release" in w for w in pc.warnings)
    env = pc.write(tmp_path / "plan").read_text()
    assert "TEST=1" in env and "PINNED=0" in env and f"TAG={CAT}-v{V}" in env
    assets = (tmp_path / "plan" / "assets.txt").read_text().split()
    assert len(assets) == 4 and all(Path(a).is_file() for a in assets)   # 3 components + legal
    assert any("every one where the channel says" in c for c in pc.checks)


def test_twin_publish_check_for_publish_refuses_a_test_release(built):
    with pytest.raises(ReleaseError, match="TEST release"):
        check(built, REPO, for_publish=True)


def test_twin_publish_check_refuses_an_unpinned_real_key(tmp_path, lx):
    sk, pk = signer_mod.keygen_throwaway(tmp_path / "keys", "release")
    out = tmp_path / "out"
    assert run(release_args(lx, out, V, "--key", str(sk), "--signer", "python"))[0] == 0
    assert not check(out, REPO, public_key=pk).test
    with pytest.raises(ReleaseError, match="not pinned"):
        check(out, REPO, for_publish=True, public_key=pk)


def test_twin_publish_check_refuses_another_repo_and_names_the_one_built(built):
    with pytest.raises(ReleaseError) as exc:
        check(built, "Other/Repo")
    assert "built for SoC-Labs/HarnessManager" in exc.value.hint


def test_twin_publish_check_refuses_a_tampered_or_extra_file(built):
    rel = entry(built)
    d = Layout(built).release_dir(rel["tag"])
    (d / "stray.txt").write_text("not signed")
    with pytest.raises(ReleaseError, match="does not name"):
        check(built, REPO)
    (d / "stray.txt").unlink()
    img = d / f"mps3-harness-{V}-linux_slot.img"
    data = bytearray(img.read_bytes())
    data[-1] ^= 1
    img.write_bytes(bytes(data))
    with pytest.raises(ReleaseError, match="sha256|refuse"):
        check(built, REPO)


def test_publish_check_against_the_live_channel(tmp_path, lx, built):
    first = channel(built)                                 # serial 1: 2.0.0-rc1
    rc, text = run(release_args(lx, built, "2.0.0-rc2", "--test-key", "--trust-key",
                                str(built / "TEST-KEY.pub")))
    assert rc == 0, text                                   # ours: serial 2, rc1 + rc2
    live = tmp_path / "live.json"
    live.write_text(json.dumps(first))
    pc = check(built, REPO, live=live)
    assert pc.version == "2.0.0-rc2" and any("every live release kept" in c for c in pc.checks)
    # twins: the live channel is as new as ours, or holds a release ours lost
    live.write_text(json.dumps({**first, "serial": 2}))
    with pytest.raises(ReleaseError, match="rollback"):
        check(built, REPO, live=live)
    gone = json.loads(json.dumps(first))
    gone["harness"]["releases"].append({**gone["harness"]["releases"][0], "version": "1.9.0",
                                        "status": "superseded"})
    live.write_text(json.dumps(gone))
    with pytest.raises(ReleaseError, match="missing 1.9.0"):
        check(built, REPO, live=live)


def test_the_cli_runs_publish_check(tmp_path, built):
    rc, text = run(["publish-check", "--root", str(built), "--repo", REPO, "--env-out",
                    str(tmp_path / "plan")])
    assert rc == 0 and "TEST release" in text and (tmp_path / "plan" / "publish.env").is_file()
    rc, text = run(["publish-check", "--root", str(built), "--repo", REPO, "--for-publish"])
    assert rc == 15 and "never published" in text
