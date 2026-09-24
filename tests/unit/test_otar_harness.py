"""OTA-R: the harness front-end (HARNESS-DIST H13) of the release tool.

Fixtures are the HARNESS-DIST spike's fake mints (``tests/spikes/harness_dist_spike.py``:
real-format SD trees with a Xilinx header, T2 overlay triples, a Linux slot image) written
as bundle dirs (``tests/fakes/otar_release.write_bundle``). Each refusal has a twin that
passes, and a refusal writes nothing.

- a clean bare-metal mint: catalogue entry, doors ``mcc_sd``/``ethernet``, the Arm-IP
  overlays split to the AAA repo, and HM's own client + bundle checks read it back;
- refused: a dirty image (twin: --allow-dirty on beta, which promote never takes to
  stable), a USERID mismatch, Arm IP in an open component (declared, or a known AAA RM),
  an .ebf, a non-fieldable Linux bundle (twin: a fieldable one), a foreign overlay.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.services.update import trust
from harness_manager.services.update.service import UpdateService
from harness_manager.services.update.trust import ROLE_RELEASE, TrustedKey, TrustStore
from tests.fakes.otar_release import write_bundle
from tests.fakes.virtual_board import PRODUCT_V011_FEATURES
from tests.spikes.harness_dist_spike import (
    S_ILA,
    S_OLD,
    U_ILA,
    U_OLD,
    bare_metal_mint,
    linux_mint,
    overlays,
)
from tools.release import signer as signer_mod
from tools.release import smoke
from tools.release.cli import main
from tools.release.common import Layout

CAT = "mps3-harness"


@pytest.fixture
def keys(tmp_path: Path) -> tuple[Path, Path]:
    return signer_mod.keygen_throwaway(tmp_path / "keys")


@pytest.fixture
def ila(tmp_path: Path):
    return bare_metal_mint(tmp_path / "m", S_ILA, U_ILA, "d68dd0ed",
                           list(PRODUCT_V011_FEATURES), "0.11")


def harness(out: Path, bundle: Path, sk: Path, version: str = "1.1.0", *extra: str,
            ) -> tuple[int, str]:
    lines: list[str] = []
    rc = main(["harness", "--out", str(out), "--bundle", str(bundle), "--version", version,
               "--signer", "python", "--secret-key", str(sk), *extra], printer=lines.append)
    return rc, "\n".join(lines)


def nothing_written(out: Path) -> bool:
    return not (out / "SoC-Labs").exists()


def test_a_clean_bare_metal_mint_is_catalogued_and_reads_back(tmp_path, ila, keys):
    sk, pk = keys
    out = tmp_path / "dist"
    rc, text = harness(out, write_bundle(tmp_path / "b", ila), sk)
    assert rc == 0, text
    layout = Layout(out)
    doc = json.loads(layout.channel_file(CAT, "beta").read_bytes())
    assert doc["catalog"] == CAT and doc["board"]["part"] == "xcku115"
    rel = doc["harness"]["releases"][0]
    assert rel["identity"]["static_id"] == S_ILA and rel["identity"]["fw_sha"] == "d68dd0ed"
    assert rel["identity"]["harness"] == "1.0.0"                # what the firmware REPORTS
    assert rel["firmware"] == {"version": "1.0.0", "sha": "d68dd0ed", "stamped": False,
                               "elf_sha256": ""}               # U7: both recorded
    comps = {c["name"]: c for c in rel["components"]}
    assert comps["sd-HBI0309C"]["door"] == "mcc_sd" and comps["sd-HBI0309C"]["target"] == "mcc-sd"
    assert comps["overlays-open"]["door"] == "ethernet"
    aaa = comps["overlays-aaa"]
    assert aaa["ip_class"] == "arm-aaa" and aaa["access"] == "github-token"
    assert aaa["repo"] == "SoC-Labs/mps3-harness-aaa" and "mps3-harness-aaa" in aaa["url"]
    assert layout.asset_path("mps3-harness-v1.1.0", "mps3-harness-1.1.0-overlays-aaa.zip",
                             "SoC-Labs/mps3-harness-aaa").is_file()
    assert "U7 stamping pending" in text
    # HM's own client lists it, and its bundle checks pass on every component
    store = TrustStore(pinned=(TrustedKey(signer_mod.read_public_key(pk), ROLE_RELEASE,
                                          trust.CHANNELS, "t"),))
    svc = UpdateService(state_dir=tmp_path / "c", trust=store, token="", app_version="0.1.0")
    report = svc.check(channel="beta", source=str(layout.channel_file(CAT, "beta")))
    assert [r["version"] for r in report["releases"]["harness"]] == ["1.1.0"]
    rep = smoke.verify(layout, CAT, "beta", [signer_mod.read_public_key(pk)])
    assert any("USERID" in c and c.strip().startswith("ok") for c in rep.checks)
    assert any("overlays-aaa" in c and "static_id" in c for c in rep.checks)
    plan = (out / "plans" / f"{CAT}-beta" / "publish-plan.txt").read_text()
    assert "--repo SoC-Labs/mps3-harness-aaa" in plan


def test_check_only_prints_the_entry_and_writes_nothing(tmp_path, ila, keys):
    out = tmp_path / "dist"
    rc, text = harness(out, write_bundle(tmp_path / "b", ila), keys[0], "1.1.0", "--check-only")
    assert rc == 0 and '"static_id": "0x72BB0A36"' in text and nothing_written(out)


def test_a_dirty_image_is_refused(tmp_path, ila, keys):
    out = tmp_path / "dist"
    rc, text = harness(out, write_bundle(tmp_path / "b", ila, dirty=True), keys[0])
    assert rc == 15 and "[DIRTY]" in text and nothing_written(out)


def test_twin_a_waived_dirty_image_goes_to_beta_but_never_to_stable(tmp_path, ila, keys):
    out = tmp_path / "dist"
    rc, text = harness(out, write_bundle(tmp_path / "b", ila, dirty=True), keys[0], "1.1.0",
                       "--allow-dirty", "fielded 0x72BB0A36 was minted dirty")
    assert rc == 0, text
    rel = json.loads(Layout(out).channel_file(CAT, "beta").read_bytes())["harness"]["releases"][0]
    assert rel["source"]["dirty_waiver"] == "fielded 0x72BB0A36 was minted dirty"
    lines: list[str] = []
    rc = main(["promote", "--out", str(out), "--catalog", CAT, "--version", "1.1.0", "--signer",
               "python", "--secret-key", str(keys[0])], printer=lines.append)
    assert rc == 15 and "never reaches stable" in "\n".join(lines)


def test_a_userid_mismatch_is_refused(tmp_path, ila, keys):
    out = tmp_path / "dist"
    rc, text = harness(out, write_bundle(tmp_path / "b", ila, mint_usercode=U_OLD), keys[0])
    assert rc == 14 and "[USERID]" in text and "header USERID 0xc8551081" in text
    assert nothing_written(out)


def test_arm_ip_in_an_open_component_is_refused(tmp_path, ila, keys):
    out = tmp_path / "dist"
    aaa_in_open = {f"x{k}": v for k, v in ila.overlays_aaa.items()}    # declared arm-aaa
    rc, text = harness(out, write_bundle(tmp_path / "b", ila, extra_open=aaa_in_open), keys[0])
    assert rc == 15 and "[AAA_OPEN]" in text and "is Arm IP" in text and nothing_written(out)


def test_a_known_arm_ip_rm_is_refused_from_open_even_undeclared(tmp_path, ila, keys):
    from tests.fakes.t2_overlays import make_overlay

    root = tmp_path / "ovl"
    make_overlay(root, "nanosoc", rm_id=0x0100_00C1, static_id=int(S_ILA, 16),
                 static_usercode=int(U_ILA, 16))                    # no ip_class declared
    extra = {f"nanosoc/{p.name}": p.read_bytes() for p in (root / "nanosoc").iterdir()}
    rc, text = harness(tmp_path / "dist", write_bundle(tmp_path / "b", ila, extra_open=extra),
                       keys[0])
    assert rc == 15 and "overlay nanosoc (ip_class undeclared) is Arm IP" in text


def test_an_ebf_is_refused(tmp_path, ila, keys):
    out = tmp_path / "dist"
    rc, text = harness(out, write_bundle(tmp_path / "b", ila,
                                         extra_sd={"MB/HBI0309C/mbb_v141.ebf": b"\0" * 64}),
                       keys[0])
    assert rc == 15 and "[EBF]" in text and "mbb_v141.ebf" in text and nothing_written(out)


def test_a_foreign_overlay_is_refused(tmp_path, ila, keys):
    foreign = overlays(tmp_path / "f", S_OLD, U_OLD, ("synth2",))
    rc, text = harness(tmp_path / "dist",
                       write_bundle(tmp_path / "b", ila,
                                    extra_open={f"old{k}": v for k, v in foreign.items()}),
                       keys[0])
    assert rc == 14 and "[STATIC]" in text and "keyed to 0x3f1a560f" in text


def test_a_non_fieldable_linux_bundle_is_refused(tmp_path, keys):
    lnx = linux_mint(tmp_path / "m")
    out = tmp_path / "dist"
    rc, text = harness(out, write_bundle(tmp_path / "b", lnx,
                                         linux={"fieldable": False, "mint_kind": "prototype"}),
                       keys[0], "2.0.0")
    assert rc == 15 and "[NOT_FIELDABLE]" in text and "prototype" in text
    assert nothing_written(out)


def test_twin_a_fieldable_linux_bundle_goes_through_both_doors(tmp_path, keys):
    lnx = linux_mint(tmp_path / "m")
    out = tmp_path / "dist"
    rc, text = harness(out, write_bundle(tmp_path / "b", lnx), keys[0], "2.0.0")
    assert rc == 0, text
    rel = json.loads(Layout(out).channel_file(CAT, "beta").read_bytes())["harness"]["releases"][0]
    comps = {c["name"]: c for c in rel["components"]}
    assert comps["os-slot"]["target"] == "user-usd" and comps["os-slot"]["door"] == "ethernet"
    assert comps["sd-HBI0309C"]["door"] == "mcc_sd" and rel["identity"]["impl"] == "linux"
    assert rel["legal_info"]["name"] == "mps3-harness-2.0.0-linux_legal_info.tar"
    assert rel["vivado"] == "2026.1"


def test_a_lab_linux_image_counts_as_dirty(tmp_path, keys):
    lnx = linux_mint(tmp_path / "m")
    rc, text = harness(tmp_path / "dist",
                       write_bundle(tmp_path / "b", lnx, linux={"targets": {"ethernet": {
                           "components": {"image_kind": "lab"}}}}), keys[0], "2.0.0")
    assert rc == 15 and "image_kind 'lab'" in text


def test_a_rekey_is_derived_against_the_previous_release(tmp_path, keys):
    out = tmp_path / "dist"
    old = bare_metal_mint(tmp_path / "m0", S_OLD, U_OLD, "cb31b0f2", ["clcd"], "0.10")
    assert harness(out, write_bundle(tmp_path / "b0", old), keys[0], "1.0.0")[0] == 0
    ila = bare_metal_mint(tmp_path / "m1", S_ILA, U_ILA, "d68dd0ed",
                          list(PRODUCT_V011_FEATURES), "0.11")
    rc, text = harness(out, write_bundle(tmp_path / "b1", ila), keys[0], "1.1.0")
    assert rc == 0, text
    doc = json.loads(Layout(out).channel_file(CAT, "beta").read_bytes())
    new = doc["harness"]["releases"][0]
    assert doc["serial"] == 2 and new["version"] == "1.1.0" and new["rekey"] is True
    assert new["compat"]["replaces_static_ids"] == [S_OLD]
    assert [r["status"] for r in doc["harness"]["releases"]] == ["current", "superseded"]
