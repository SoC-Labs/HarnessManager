"""The build guide engine (KIT-GUIDE KG-A): Vivado discovery, the rendered build_rm.tcl,
the receipt read back, the receipt -> overlay manifest, and the six step states.

Vivado is never run: discovery gets a fake runner (``kit_fakes.FakeVivado``) or is off.
Every check has a negative twin.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from harness_manager.core.errors import RefusedError, UsageError
from harness_manager.core.model import BoardIdentity
from harness_manager.services.kit import build, guide, render, script, vivado
from harness_manager.services.kit.schema import load_receipt
from harness_manager.services.kit.service import HubSource, KitService
from harness_manager.services.store import ContentStore
from harness_manager_mps3 import kit as mkit
from tests.fakes import kit_fakes as kf


@pytest.fixture(autouse=True)
def _no_real_vivado(monkeypatch):
    monkeypatch.setenv(vivado.ENV, "off")


@pytest.fixture
def store(tmp_path: Path) -> ContentStore:
    return ContentStore(tmp_path / "store")


@pytest.fixture
def kits(tmp_path: Path, store: ContentStore) -> KitService:
    k = KitService(store, tmp_path / "kits", hub=HubSource(None))
    k.import_(kf.FIXTURE)
    return k


def found(release="2024.1", build=5076996, others=()) -> vivado.VivadoFound:
    return vivado.VivadoFound(vivado.VivadoInstall(f"/tools/Xilinx/Vivado/{release}/bin/vivado",
                                                   release, build, "path"), tuple(others))


def design_file(tmp_path: Path, **over) -> Path:
    d = {"kind": "rm", "name": "spike_rm", "rm_id": "0x010080F0",
         "use": {"clkrst": {}, "status": {}, "gpio": {"timed": True}},
         "build": {"sources": [str(kf.SPIKE_RM)], "top": "rm_spike_rm"}}
    d.update(over)
    p = tmp_path / "spike_rm.json"
    p.write_text(json.dumps(d))
    return p


# --- Vivado discovery (never runs Vivado) ------------------------------------------------------------


def test_discovery_reads_the_version_through_the_runner(tmp_path):
    exe = tmp_path / "bin" / "vivado"
    exe.parent.mkdir()
    exe.write_text("")
    fake = kf.FakeVivado("2024.1", 5076996)
    f = vivado.discover(runner=fake, env={vivado.ENV: str(exe)}, roots=())
    assert f.found and f.install.version == "2024.1" and f.install.build == 5076996
    assert fake.calls == [[str(exe), "-version"]]
    assert f.install.how == "env"


def test_discovery_off_runs_nothing():
    fake = kf.FakeVivado()
    f = vivado.discover(runner=fake, env={vivado.ENV: "off"})
    assert not f.found and f.disabled and fake.calls == []


def test_discovery_on_path_and_the_install_roots(tmp_path):
    root = tmp_path / "Xilinx" / "Vivado"
    for rel in ("2021.1", "2024.1"):
        b = root / rel / "bin"
        b.mkdir(parents=True)
        (b / "vivado").write_text("")
    fake = kf.FakeVivado("2021.1")
    on_path = str(root / "2021.1" / "bin" / "vivado")
    f = vivado.discover(runner=fake, env={}, which=lambda _: on_path, roots=(str(root),))
    assert f.install.path == on_path and f.install.version == "2021.1"
    assert [o.version for o in f.others] == ["2024.1"]     # listed, never run
    assert len(fake.calls) == 1
    # nothing on PATH: the newest release under the roots is the one
    g = vivado.discover(runner=kf.FakeVivado("2024.1"), env={}, which=lambda _: None,
                        roots=(str(root),))
    assert g.install.version == "2024.1" and g.install.how == "install root"
    none = vivado.discover(runner=fake, env={}, which=lambda _: None, roots=())
    assert not none.found and "not on PATH" in none.reason


def test_a_vivado_release_mismatch_warns_in_hm_never_refuses():
    ok = vivado.check_release(found("2024.1"), "2024.1", 5076996)
    assert ok.state == "ok"
    wrong = vivado.check_release(found("2021.1", others=[vivado.VivadoInstall(
        "/opt/Xilinx/Vivado/2024.1/bin/vivado", "2024.1")]), "2024.1")
    assert wrong.state == "warning"                       # HM warns (david K4)...
    assert "build_rm.tcl will refuse" in wrong.detail and "/opt/Xilinx/Vivado/2024.1" in wrong.detail
    build_only = vivado.check_release(found("2024.1", 5000000), "2024.1", 5076996)
    assert build_only.state == "warning" and "same release" in build_only.detail
    missing = vivado.check_release(vivado.VivadoFound(None, reason="none"), "2024.1")
    assert missing.state == "warning"
    assert vivado.check_release(found(), "").state == "unchecked"


# --- the rendered script -----------------------------------------------------------------------------


def test_the_script_refuses_a_different_major_minor_and_only_notes_the_build(kits, tmp_path):
    s = script.make_script(kits, pack="mps3", static_id="0x72BB0A36",
                           design=str(design_file(tmp_path)))
    tcl = s.files["build_rm.tcl"]
    # the parameters come from the kit, never hard-coded
    assert "    VIVADO_VERSION    {2024.1}" in tcl and "    VIVADO_BUILD      {5076996}" in tcl
    assert "    STATIC_ID         {0x72BB0A36}" in tcl and "    BOUNDARY_BITS     {148}" in tcl
    assert "    RM_ID             {0x010080F0}" in tcl
    assert "    STATIC_DCP        {kit/static/static_routed_locked.dcp}" in tcl
    assert "    RM_OOC_XDC        {xdc/spike_rm_ooc.xdc}" in tcl
    # K4: another major.minor is REFUSED by the gate, with this text...
    assert ("gate vivado_version [string equal [major_minor $vv] [major_minor "
            "$P(VIVADO_VERSION)]]") in tcl
    assert "a checkpoint opens only in the major.minor release that wrote it" in tcl
    # ...and a different build is only a NOTE
    assert 'note vivado_build "running build $vb; the static was written by build ' in tcl
    # the twin: the exact-string comparison the spike had is gone
    assert "gate vivado_version [string equal $vv $P(VIVADO_VERSION)]" not in tcl
    # nothing left unrendered, and no exec/python inside Vivado
    assert not re.search(r"\{\{[A-Z_]+\}\}", tcl)
    assert not re.search(r"^\s*exec |\[exec ", tcl, re.M)


def test_render_refuses_unknown_missing_and_brace_values():
    with pytest.raises(UsageError, match="no parameter"):
        render.render({"NOPE": "1"})
    with pytest.raises(UsageError, match="needs"):
        render.render({"RM_NAME": "x"})
    with pytest.raises(UsageError, match="brace"):
        render.tcl_list(["a}b.sv"], "RM_SOURCES")
    assert render.tcl_list(["C:\\Users\\A B\\x.sv", "y.sv"], "RM_SOURCES") == \
        "{C:/Users/A B/x.sv} y.sv"


def test_make_script_writes_the_build_dir(kits, tmp_path, store):
    out = tmp_path / "build" / "spike_rm"
    s = script.make_script(kits, pack="mps3", static_id="0x72BB0A36",
                           design=str(design_file(tmp_path)), out_dir=out, store=store)
    for rel in ("build_rm.tcl", "README.txt", "xdc/spike_rm_ooc.xdc",
                "xdc/spike_rm_wrapper_skeleton.sv", "kit/kit.json",
                "kit/static/static_routed_locked.dcp"):
        assert (out / rel).is_file(), rel
    assert (out / "out").is_dir()
    assert s.command[:4] == ["vivado", "-mode", "batch", "-source"]
    assert "kit check out/spike_rm_build.json" in (out / "README.txt").read_text()
    assert f"RM_SOURCES        {{{kf.SPIKE_RM.as_posix()}}}" in (out / "build_rm.tcl").read_text()


def test_make_script_proposes_an_rm_id_and_the_skeleton_agrees(kits, tmp_path):
    p = design_file(tmp_path)
    d = json.loads(p.read_text())
    del d["rm_id"]
    p.write_text(json.dumps(d))
    s = script.make_script(kits, pack="mps3", static_id="0x72BB0A36", design=str(p))
    assert s.rm_id_proposed and 0x8000 <= int(s.rm_id, 16) & 0xFFFF
    assert f"assign rm_id = 32'h{int(s.rm_id, 16):08X}" in s.files["xdc/spike_rm_wrapper_skeleton.sv"]
    assert f"RM_ID             {{{s.rm_id}}}" in s.files["build_rm.tcl"]
    assert any(c.name == "rm_id_proposed" and c.state == "warning" for c in s.checks)


def test_make_script_refuses_a_design_that_fails_the_xdc_checks(kits, tmp_path):
    bad = design_file(tmp_path, ports=[{"name": "dut_clk", "dir": "in"}])   # 1 of 47 ports
    with pytest.raises(RefusedError) as e:
        script.make_script(kits, pack="mps3", static_id="0x72BB0A36", design=str(bad))
    assert any(c["name"].startswith("xdc:") and c["state"] == "mismatch"
               for c in e.value.data["checks"])


def test_make_script_needs_the_kit(tmp_path):
    empty = KitService(ContentStore(tmp_path / "s"), tmp_path / "w", hub=HubSource(None))
    from harness_manager.core.errors import AbsentError

    with pytest.raises(AbsentError, match="kit fetch"):
        script.make_script(empty, pack="mps3", static_id="0x72BB0A36", design="minimal")


def test_log_markers_are_anchored_at_line_start():
    log = ("#   puts \"HM_RM_BUILD_FAILED gate=$name\"\n"          # Vivado echoing the script
           "HM_STAGE preflight\nHM_GATE static_id PASS CRC-32 is 0x72BB0A36\n"
           "HM_RM_BUILD_COMPLETE rm=spike_rm\n")
    marks = render.parse_markers(log)
    assert [m for m, _ in marks] == ["HM_STAGE", "HM_GATE", "HM_RM_BUILD_COMPLETE"]


# --- the receipt -> the overlay manifest (K5) --------------------------------------------------------


def test_a_passed_receipt_packs_into_an_overlay_the_store_accepts(tmp_path, store):
    r = load_receipt(kf.passed_build(tmp_path / "b", ltx=b"probes"))
    checks = build.receipt_checks(r)
    assert {c.name: c.state for c in checks} == {
        "build": "ok", "rm_id": "ok", "static_id": "ok", "partial": "ok", "clearing": "ok",
        "ltx": "ok"}
    a = mkit.make_kit_adapter()
    d = a.pack_receipt(r, tmp_path / "overlay")
    man = json.loads((d / "manifest.json").read_text())
    assert man["static_id"] == "0x72BB0A36" and man["rm_id"] == "0x010080F0"
    assert man["static_usercode"] == "0xC8551081"
    assert man["build_receipt"] == "spike_rm_build.json" and (d / "spike_rm_build.json").is_file()
    assert man["partial"] == {"file": "spike_rm.bin", "len": int(r.get("partial_len")),
                              "crc32": r.get("partial_crc32").lower()}
    assert man["ltx"] == "spike_rm.ltx"
    got = a.import_overlay(store, d)
    assert (got["name"], got["rm_id"], got["static_id"]) == ("spike_rm", "0x010080f0",
                                                            "0x72bb0a36")
    rec = store.find("overlay", rm_id="0x010080f0")
    assert rec and rec[0][1].get("receipt_sha256") and rec[0][1].get("ltx_sha256")


@pytest.mark.parametrize("kw, failing", [
    ({"state": "failed", "gates": [{"gate": "rm_timing", "verdict": "FAIL", "detail": "WNS -0.4"}]},
     "build"),
    ({"state": "stopped"}, "build"),
    ({"netlist_rm_id": "0x010080F1"}, "rm_id"),
    ({"rm_id": "0x00000000"}, "rm_id"),
])
def test_receipt_refusals(tmp_path, kw, failing):
    r = load_receipt(kf.passed_build(tmp_path / "b", **kw))
    checks = build.receipt_checks(r)
    assert {c.name: c.state for c in checks}[failing] == "mismatch"


def test_a_pair_from_another_build_is_refused_by_crc_and_len(tmp_path):
    p = kf.passed_build(tmp_path / "b")
    (p.parent / "spike_rm_partial.bin").write_bytes(kf.stream(fars=((0, 0, 101),)))
    checks = {c.name: c for c in build.receipt_checks(load_receipt(p))}
    assert checks["partial"].state == "mismatch" and "another build" in checks["partial"].detail
    assert checks["clearing"].state == "ok"


def test_find_receipts_in_a_build_dir(tmp_path):
    assert build.find_receipts(tmp_path) == []
    p = kf.passed_build(tmp_path)
    assert build.find_receipts(tmp_path) == [p]
    assert build.load(tmp_path).rm_name == "spike_rm"


# --- the step states ----------------------------------------------------------------------------------


def states(g: guide.Guide) -> dict[str, str]:
    return {s.id: s.state for s in g.steps}


def test_no_static_no_kit_nothing(tmp_path):
    empty = KitService(ContentStore(tmp_path / "s"), tmp_path / "w", hub=HubSource(None))
    g = guide.guide(empty, vivado=found())
    assert states(g) == {"target": "next", "tools": "unchecked", "kit": "blocked",
                         "wrapper": "blocked", "build": "blocked", "check": "blocked"}
    assert "waits for 1 target" in g.steps[2].reason


def test_a_static_with_no_kit_makes_fetch_the_next_step(tmp_path):
    empty = KitService(ContentStore(tmp_path / "s"), tmp_path / "w", hub=HubSource(None))
    g = guide.guide(empty, static_id="0x72BB0A36", vivado=found())
    assert states(g)["target"] == "done"
    assert states(g)["tools"] == "unchecked"               # no kit: the release is unknown
    assert states(g)["kit"] == "next"
    assert g.next.actions[0]["text"] == "harness-manager kit fetch --static-id 0x72BB0A36"


def test_the_whole_journey_to_done(kits, tmp_path, store):
    board = BoardIdentity(board_type="mps3", shell_id="0x72bb0a36", usercode="0xc8551081")
    dfile = str(design_file(tmp_path))
    bdir = tmp_path / "build"
    g = guide.guide(kits, identity=board, board_id="mps3-01", design=dfile, build_dir=bdir,
                    vivado=found(), store=store)
    assert states(g) == {"target": "done", "tools": "done", "kit": "done", "wrapper": "done",
                         "build": "next", "check": "blocked"}
    kf.passed_build(bdir)
    g = guide.guide(kits, identity=board, design=dfile, build_dir=bdir, vivado=found(),
                    store=store)
    assert states(g)["build"] == "done" and states(g)["check"] == "next"
    assert "kit pack" in g.next.actions[0]["text"]
    a = mkit.make_kit_adapter()
    a.import_overlay(store, a.pack_receipt(build.load(bdir), tmp_path / "ov"))
    g = guide.guide(kits, identity=board, design=dfile, build_dir=bdir, vivado=found(),
                    store=store)
    assert set(states(g).values()) == {"done"} and g.next is None


def test_a_failed_build_shows_the_gate_and_its_fix(kits, tmp_path):
    bdir = tmp_path / "build"
    kf.passed_build(bdir, state="failed", gates=[
        {"gate": "rm_timing", "verdict": "FAIL", "detail": "setup WNS -0.412 ns"}])
    g = guide.guide(kits, static_id="0x72BB0A36", design=str(design_file(tmp_path)),
                    build_dir=bdir, vivado=found())
    b = g.steps[4]
    assert b.state == "failed" and "rm_timing" in b.detail and "RM_XDC" in b.reason
    assert states(g)["check"] == "blocked"


def test_a_wrong_vivado_blocks_the_build_step_but_not_the_kit(kits, tmp_path):
    g = guide.guide(kits, static_id="0x72BB0A36", design=str(design_file(tmp_path)),
                    build_dir=tmp_path / "b", vivado=found("2021.1"))
    s = states(g)
    assert s["tools"] == "next" and s["kit"] == "done" and s["build"] == "blocked"
    assert "will refuse" in g.steps[1].detail and "licence: unchecked" in g.steps[1].detail


def test_a_board_on_another_static_fails_the_target(kits):
    board = BoardIdentity(board_type="mps3", shell_id="0x3f1a560f")
    g = guide.guide(kits, static_id="0x72BB0A36", identity=board, vivado=found())
    assert states(g)["target"] == "failed" and "FIELDED" in g.steps[0].detail


def test_the_kit_step_fails_when_the_board_runs_another_implementation(kits):
    board = BoardIdentity(board_type="mps3", shell_id="0x72bb0a36", usercode="0xdeadbeef")
    g = guide.guide(kits, identity=board, vivado=found())
    assert states(g)["kit"] == "failed" and "usercode" in g.steps[2].detail


def test_a_design_that_fails_its_xdc_checks_fails_the_wrapper_step(kits, tmp_path):
    bad = design_file(tmp_path, ports=[{"name": "dut_clk", "dir": "in"}])
    g = guide.guide(kits, static_id="0x72BB0A36", design=str(bad), vivado=found())
    assert states(g)["wrapper"] == "failed" and states(g)["build"] == "blocked"


def test_why_names_a_fix_for_every_gate_the_template_has():
    tcl = render.template_text()
    gates = set(re.findall(r"^\s+gate (\w+) ", tcl, re.M)) | {"tcl_error"}
    assert gates <= set(guide.GATE_HELP), gates - set(guide.GATE_HELP)
    with pytest.raises(UsageError):
        guide.why("nonesuch")


def test_fetching_the_kit_does_not_wait_for_vivado(tmp_path):
    empty = KitService(ContentStore(tmp_path / "s"), tmp_path / "w", hub=HubSource(None))
    g = guide.guide(empty, static_id="0x72BB0A36",
                    vivado=vivado.VivadoFound(None, reason="not on PATH"))
    s = states(g)
    assert s["tools"] == "next" and s["kit"] == "next" and s["build"] == "blocked"
    assert g.next.id == "tools"                          # the first of them
    assert "waits for 2 tools, 3 kit" in g.steps[4].reason
