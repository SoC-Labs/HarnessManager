"""KIT-RC2: the kit flow for the RC2 static (0x44EE76D5, Vivado 2026.1), board-free.

- Vivado 2025.1+ layout (``<root>/<rel>/Vivado/bin/vivado``), the kit's release preferred
  over another release on PATH, and the PATH one flagged (discovery, Detect, the guide);
- the printed command names the full path of the matching Vivado;
- ``minimal`` builds as its skeleton, which ties ``dut_lockup``/``irq_out`` off;
- ``kit check --static-id`` against a receipt of another static refuses;
- the pin model with more than one shell (a hand-made two-shell document here; the
  generator's own tests against the platform repo are in ``test_kit_rc2_pins.py``).

Vivado is never run: fake install trees and a runner that answers ``-version`` from the
path. Every test has its negative twin. The opt-in test at the end reads the real 2026.1
(``HM_REAL_VIVADO=1``) and skips otherwise.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from harness_manager.core.errors import IncompatibleError, RefusedError
from harness_manager.core.pack import kit_refusal
from harness_manager.services import xdc
from harness_manager.services.kit import build, guide, render, script, vivado
from harness_manager.services.kit.service import HubSource, KitService
from harness_manager.services.store import ContentStore
from harness_manager.services.xdc.model import PinModel
from harness_manager.settings import tooltest
from harness_manager_mps3 import pins
from tests.fakes import kit_fakes as kf

RC2 = "0x44EE76D5"
SID = kf.STATIC_ID                                     # 0x72BB0A36, in the committed model
REAL_2026 = Path("/research/CAD/Xilinx/Vivado/2026.1")


@pytest.fixture(autouse=True)
def _no_real_vivado(monkeypatch):
    monkeypatch.setenv(vivado.ENV, "off")


class ByPath:
    """A ``runner`` that answers ``vivado -version`` with the release in the exe's path."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, argv, **_):
        self.calls.append(argv[0])
        rel = vivado.release_of_path(argv[0]) or "2099.9"
        return subprocess.CompletedProcess(argv, 0, f"vivado v{rel} (64-bit)\nSW Build 1 on x\n", "")


def install(root: Path, rel: str, *, new_layout: bool) -> Path:
    """A fake install: ``<root>/<rel>/bin/vivado`` (old) or ``<root>/<rel>/Vivado/bin/vivado``."""
    exe = root / rel / ("Vivado/bin" if new_layout else "bin") / "vivado"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)                               # shutil.which wants an executable
    return exe


@pytest.fixture
def tree(tmp_path) -> dict[str, Path]:
    apps = tmp_path / "apps" / "Xilinx" / "Vivado"
    research = tmp_path / "research" / "CAD" / "Xilinx" / "Vivado"
    return {"apps": apps, "research": research,
            "2024.1": install(apps, "2024.1", new_layout=False),
            "2025.2": install(research, "2025.2", new_layout=True),
            "2026.1": install(research, "2026.1", new_layout=True)}


# --- item 2: the 2025.1+ layout, and the kit's release preferred ----------------------------------


def test_the_research_cad_root_is_searched():
    assert "/research/CAD/Xilinx/Vivado" in vivado.ROOTS_POSIX
    assert "/tools/Xilinx" in vivado.ROOTS_POSIX           # the 2025.1+ installer's default


def test_an_install_dir_in_the_new_layout_is_accepted(tree):
    for value in (tree["research"] / "2026.1", tree["research"] / "2026.1" / "Vivado",
                  tree["2026.1"]):
        f = vivado.discover(runner=ByPath(), env={vivado.ENV: str(value)}, which=lambda _: None,
                            roots=())
        assert f.install is not None and f.install.path == str(tree["2026.1"]), value
        assert f.install.version == "2026.1" and f.install.how == "env"
    # the old layout still works
    f = vivado.discover(runner=ByPath(), env={vivado.ENV: str(tree["apps"] / "2024.1")},
                        which=lambda _: None, roots=())
    assert f.install.path == str(tree["2024.1"])


def test_negative_twin_a_dir_with_no_vivado_is_refused_with_a_near_miss(tree, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    f = vivado.discover(runner=ByPath(), env={vivado.ENV: str(empty)}, which=lambda _: None,
                        roots=())
    assert f.install is None and "not a vivado executable or install" in f.reason
    # the runbook's 2026.1/bin/vivado does not exist: the reason names the real one
    wrong = tree["research"] / "2026.1" / "bin" / "vivado"
    f = vivado.discover(runner=ByPath(), env={vivado.ENV: str(wrong)}, which=lambda _: None,
                        roots=())
    assert f.install is None and f"did you mean {tree['2026.1']}?" in f.reason


def test_a_directory_of_releases_picks_the_kits_release(tree):
    f = vivado.discover(runner=ByPath(), env={vivado.ENV: str(tree["research"])},
                        which=lambda _: None, roots=(), want="2025.2")
    assert f.install.path == str(tree["2025.2"])
    # twin: no release asked for (or none of it): the newest
    f = vivado.discover(runner=ByPath(), env={vivado.ENV: str(tree["research"])},
                        which=lambda _: None, roots=(), want="2019.1")
    assert f.install.path == str(tree["2026.1"])


def test_the_kits_release_off_path_wins_over_another_on_path(tree):
    run = ByPath()
    roots = (str(tree["apps"]), str(tree["research"]))
    f = vivado.discover(runner=run, env={}, which=lambda _: str(tree["2024.1"]), roots=roots,
                        want="2026.1")
    assert f.install.path == str(tree["2026.1"]) and f.install.how == "install root"
    assert f.on_path is not None and f.on_path.version == "2024.1"
    assert str(tree["2024.1"]) in [o.path for o in f.others]
    assert run.calls == [str(tree["2024.1"]), str(tree["2026.1"])]    # only these two run
    assert vivado.command_vivado(f, "2026.1") == str(tree["2026.1"])
    # the twin: the kit wants PATH's release, PATH wins and nothing else runs
    run = ByPath()
    f = vivado.discover(runner=run, env={}, which=lambda _: str(tree["2024.1"]), roots=roots,
                        want="2024.1")
    assert f.install.path == str(tree["2024.1"]) and f.install.how == "path"
    assert run.calls == [str(tree["2024.1"])]
    # and no release asked for: PATH first, as before
    f = vivado.discover(runner=ByPath(), env={}, which=lambda _: str(tree["2024.1"]),
                        roots=roots)
    assert f.install.path == str(tree["2024.1"])
    assert {o.version for o in f.others} == {"2025.2", "2026.1"}


def test_a_setting_is_never_overridden_only_flagged(tree):
    f = vivado.discover(runner=ByPath(), env={vivado.ENV: str(tree["2024.1"])},
                        which=lambda _: None, roots=(str(tree["research"]),), want="2026.1")
    assert f.install.path == str(tree["2024.1"])
    c = vivado.check_release(f, "2026.1")
    assert c.state == "warning" and "Vivado 2026.1 is installed at" in c.detail
    assert str(tree["2026.1"]) in c.detail and "point $HARNESS_MANAGER_VIVADO" in c.detail
    assert vivado.command_vivado(f, "2026.1") == "vivado"          # no match: no full path


def test_check_path_flags_a_wrong_release_on_path(tree):
    f = vivado.discover(runner=ByPath(), env={vivado.ENV: str(tree["2026.1"])},
                        which=lambda _: str(tree["2024.1"]), roots=())
    assert f.on_path.version == "2024.1" and f.on_path.build == 0   # from its path, not run
    pc = vivado.check_path(f, "2026.1")
    assert pc.state == "warning" and "`vivado` on PATH is Vivado 2024.1" in pc.detail
    assert f"export PATH={tree['2026.1'].parent}:$PATH" in pc.detail
    # twins: PATH is the kit's release; no vivado on PATH; a PATH vivado with no release in it
    assert vivado.check_path(f, "2024.1").state == "ok"
    none = vivado.discover(runner=ByPath(), env={vivado.ENV: str(tree["2026.1"])},
                           which=lambda _: None, roots=())
    assert vivado.check_path(none, "2026.1") is None
    odd = vivado.discover(runner=ByPath(), env={vivado.ENV: str(tree["2026.1"])},
                          which=lambda _: "/usr/local/bin/vivado", roots=())
    assert vivado.check_path(odd, "2026.1").state == "unchecked"


def test_release_of_path_reads_both_layouts_and_symlinks(tree, tmp_path):
    assert vivado.release_of_path("/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado") == "2026.1"
    assert vivado.release_of_path("/apps/Xilinx/Vivado/2024.1/bin/vivado") == "2024.1"
    link = tmp_path / "usr" / "local" / "bin" / "vivado"
    link.parent.mkdir(parents=True)
    link.symlink_to(tree["2026.1"])
    assert vivado.release_of_path(link) == "2026.1"
    # twin: a path that names no release, and points nowhere that does
    plain = tmp_path / "usr" / "local" / "bin" / "wrapper"
    plain.write_text("#!/bin/sh\n")
    assert vivado.release_of_path(plain) == ""


def test_detect_prefers_and_flags_the_cached_kits_release(tree):
    env = {"PATH": str(tree["2024.1"].parent)}
    roots = (str(tree["apps"]), str(tree["research"]))
    step, found = tooltest.detect_one("vivado", "", env, runner=ByPath(),
                                      kits=[(RC2, "2026.1")], roots=roots)
    assert step["ok"] and found["path"] == str(tree["2026.1"]) and found["version"] == "2026.1"
    assert "the cached kits need 2026.1 (0x44EE76D5)" in step["detail"]
    assert "`vivado` on PATH is 2024.1" in step["detail"]
    assert found["on_path"]["version"] == "2024.1" and found["want"] == ["2026.1"]
    # twin: no kit cached: PATH first and the detail as before; the others are listed apart
    step, found = tooltest.detect_one("vivado", "", env, runner=ByPath(), kits=[], roots=roots)
    assert found["path"] == str(tree["2024.1"])
    assert step["detail"] == f"Vivado 2024.1 at {tree['2024.1']}"
    assert {o["version"] for o in found["others"]} == {"2025.2", "2026.1"}
    # twin: a setting of the wrong release is kept, and the hint names the right one
    step, found = tooltest.detect_one("vivado", str(tree["2024.1"]), env, runner=ByPath(),
                                      kits=[(RC2, "2026.1")], roots=roots)
    assert found["path"] == str(tree["2024.1"])
    assert "NOT the release the cached kits need" in step["detail"]
    assert str(tree["2026.1"]) in step["hint"]
    # an install DIR in the new layout as the setting
    step, found = tooltest.detect_one("vivado", str(tree["research"] / "2026.1"), env,
                                      runner=ByPath(), kits=[], roots=())
    assert step["ok"] and found["path"] == str(tree["2026.1"])


# --- items 3 and 6: the guide and the printed command ---------------------------------------------


@pytest.fixture
def store(tmp_path) -> ContentStore:
    return ContentStore(tmp_path / "store")


@pytest.fixture
def kits26(tmp_path, store) -> KitService:
    """A kit written by Vivado 2026.1 for a static the committed model describes."""
    k = KitService(store, tmp_path / "kits", hub=HubSource(None))
    k.import_(kf.build_fixture(tmp_path / "kit72_2026", kf.STATIC_ID, release="2026.1"))
    return k


@pytest.fixture
def rc2_kits(tmp_path, store) -> KitService:
    """The RC2 kit (0x44EE76D5, 2026.1): its static is NOT in the committed model yet."""
    k = KitService(store, tmp_path / "kits", hub=HubSource(None))
    k.import_(kf.build_fixture(tmp_path / "kit44", RC2, release="2026.1"))
    return k


def found_with(path_release: str | None, release: str = "2026.1") -> vivado.VivadoFound:
    chosen = vivado.VivadoInstall(f"/research/CAD/Xilinx/Vivado/{release}/Vivado/bin/vivado",
                                  release, 0, "env")
    on_path = (vivado.VivadoInstall(f"/apps/Xilinx/Vivado/{path_release}/bin/vivado",
                                    path_release, 0, "path") if path_release else None)
    return vivado.VivadoFound(chosen, on_path=on_path, want=(release,))


def states(g) -> dict[str, str]:
    return {s.id: s.state for s in g.steps}


def test_tools_is_not_done_when_path_has_the_wrong_release(kits26):
    g = guide.guide(kits26, static_id=SID, vivado=found_with("2024.1"))
    tools = g.steps[1]
    assert tools.state == "next", tools.detail
    assert "`vivado` on PATH is Vivado 2024.1" in tools.detail
    assert tools.actions[0]["text"] == "export PATH=/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin:$PATH"
    assert {c.name: c.state for c in tools.checks} == {"vivado": "ok", "vivado_path": "warning"}
    # twins: PATH has the kit's release; no vivado on PATH
    assert states(guide.guide(kits26, static_id=SID, vivado=found_with("2026.1")))["tools"] == "done"
    assert states(guide.guide(kits26, static_id=SID, vivado=found_with(None)))["tools"] == "done"
    # and the chosen one of the wrong release is not done either (as before)
    assert states(guide.guide(kits26, static_id=SID,
                              vivado=found_with(None, release="2024.1")))["tools"] == "next"


def test_the_guides_build_command_names_the_full_path(kits26, tmp_path, store):
    bdir = tmp_path / "b"
    script.make_script(kits26, pack="mps3", static_id=SID, design="minimal", out_dir=bdir,
                       store=store, vivado=found_with(None))
    g = guide.guide(kits26, static_id=SID, design="minimal", build_dir=bdir,
                    vivado=found_with(None))
    run = g.steps[4].actions[0]["text"]
    assert run.startswith("/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado -mode batch")
    # twin: no Vivado of the release: a bare vivado (the tools step says why)
    g = guide.guide(kits26, static_id=SID, design="minimal", build_dir=bdir,
                    vivado=vivado.VivadoFound(None, reason="none"))
    assert g.steps[4].actions[0]["text"].startswith("vivado -mode batch")


def test_make_script_prints_the_matching_vivado(kits26, tmp_path, store):
    out = tmp_path / "b"
    s = script.make_script(kits26, pack="mps3", static_id=SID, design="minimal", out_dir=out,
                           store=store, vivado=found_with("2024.1"))
    exe = "/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado"
    assert s.command[0] == exe and s.vivado == exe and s.vivado_release == "2026.1"
    assert f"     {exe} -mode batch -source build_rm.tcl" in (out / "README.txt").read_text()
    assert any(c.name == "vivado_path" and c.state == "warning" for c in s.checks)
    # twin: no Vivado of the kit's release here: bare vivado, and a warning that says so
    s = script.make_script(kits26, pack="mps3", static_id=SID, design="minimal", out_dir=out,
                           store=store, vivado=vivado.VivadoFound(None, reason="nothing here"))
    assert s.command[0] == "vivado"
    assert any(c.name == "vivado" and c.state == "warning" and "2026.1" in c.detail
               for c in s.checks)
    assert "`vivado` must be Vivado 2026.1" in (out / "README.txt").read_text()


def test_script_params_reads_the_rendered_block(kits26):
    s = script.make_script(kits26, pack="mps3", static_id=SID, design="minimal",
                           vivado=found_with(None))
    p = render.script_params(s.files["build_rm.tcl"])
    assert p["VIVADO_VERSION"] == "2026.1" and p["STATIC_ID"] == SID and p["RM_NAME"] == "minimal"
    assert render.script_params("puts hello\n") == {}


# --- item 4: the minimal skeleton, and RM_SOURCES when a design names no RTL ----------------------


def test_minimal_ties_dut_lockup_and_irq_out_like_the_template():
    k = xdc.export("mps3", "rm-kit", "minimal")
    sk = k.files["minimal_wrapper_skeleton.sv"]
    assert "  assign dut_lockup = 1'b0;" in sk and "  assign irq_out = 1'b0;" in sk
    assert "// assign dut_lockup" not in sk and "// assign irq_out" not in sk
    assert not k.errors


def test_negative_twin_a_used_group_without_tie_leaves_its_outputs_to_the_design():
    d = {"kind": "rm", "name": "mine", "use": {"clkrst": {}, "status": {}}}
    sk = xdc.export("mps3", "rm-kit", d).files["mine_wrapper_skeleton.sv"]
    assert "  // assign dut_lockup = ...;" in sk and "  // assign irq_out = ...;" in sk
    bad = {"kind": "rm", "name": "mine", "use": {"status": {"tie": ["rm_id", "dut_gpio_o"]}}}
    k = xdc.export("mps3", "rm-kit", bad)
    subjects = {(f.code, f.subject) for f in k.errors}
    assert ("missing_pin", "use.status.tie") in subjects and len(k.errors) == 2


def test_a_design_with_no_rtl_builds_as_its_skeleton(kits26, store):
    s = script.make_script(kits26, pack="mps3", static_id=SID, design="minimal",
                           store=store, vivado=found_with(None))
    assert s.params["RM_SOURCES"] == "xdc/minimal_wrapper_skeleton.sv"
    assert "RM_SOURCES        {xdc/minimal_wrapper_skeleton.sv}" in s.files["build_rm.tcl"]
    note = next(c for c in s.checks if c.name == "sources")
    assert "xdc/minimal_wrapper_skeleton.sv" in note.detail and note.state == "warning"
    assert s.params["RM_TOP"] == "rm_minimal"
    assert "module rm_minimal" in s.files["xdc/minimal_wrapper_skeleton.sv"]


def test_negative_twin_a_design_with_rtl_keeps_its_sources(kits26, store, tmp_path):
    d = {"kind": "rm", "name": "spike_rm", "rm_id": "0x010080F0",
         "use": {"clkrst": {}, "status": {}}, "build": {"sources": [str(kf.SPIKE_RM)]}}
    p = tmp_path / "spike_rm.json"
    p.write_text(json.dumps(d))
    s = script.make_script(kits26, pack="mps3", static_id=SID, design=str(p), store=store,
                           vivado=found_with(None))
    assert s.params["RM_SOURCES"] == kf.SPIKE_RM.as_posix()
    assert not any(c.name == "sources" for c in s.checks)


# --- item 5: kit check --static-id against a receipt -----------------------------------------------


def test_kit_check_refuses_a_receipt_for_another_static(tmp_path, rc2_kits):
    receipt = kf.passed_build(tmp_path / "b")                       # built for 0x72BB0A36
    checks, _facts, sid, _r = build.check_any(rc2_kits, receipt, static_id=RC2)
    c = next(x for x in checks if x.name == "expected_static")
    assert sid == kf.STATIC_ID and c.state == "mismatch" and c.identity
    assert "--static-id is 0x44EE76D5" in c.detail
    assert isinstance(kit_refusal(checks, "the build"), IncompatibleError)   # exit 14
    # twin: the same static (any spelling) passes that check; no flag adds no check
    checks, *_ = build.check_any(rc2_kits, receipt, static_id="0x72bb0a36")
    assert next(x for x in checks if x.name == "expected_static").state == "ok"
    checks, *_ = build.check_any(rc2_kits, receipt)
    assert not any(x.name == "expected_static" for x in checks)
    assert "expected_static" in guide.CHECK_HELP and "vivado_path" in guide.CHECK_HELP


def test_negative_twin_a_bare_partial_still_takes_static_id_as_the_kit(tmp_path, rc2_kits):
    part = tmp_path / "p.bin"
    part.write_bytes(kf.stream())
    clear = tmp_path / "c.bin"
    clear.write_bytes(kf.clearing_stream())
    checks, _f, sid, r = build.check_any(rc2_kits, part, clearing=clear, static_id=RC2)
    assert r is None and sid == RC2 and not any(x.name == "expected_static" for x in checks)
    assert not any(x.name == "kit" and x.state == "unchecked" for x in checks)   # RC2's kit used


# --- item 1: a pin model with two shells, read the way the kit flow reads it ------------------------


def two_shell_doc(extra: str = RC2) -> dict:
    """The committed model plus a copy of its shell relabelled: the shape the generator
    writes for 0x72BB0A36 + RC2 (tests/unit/test_kit_rc2_pins.py proves the generator)."""
    doc = pins.load_model()
    sh = copy.deepcopy(doc["shells"][doc["default_shell"]])
    sh["static_id"] = extra
    doc["shells"][extra] = sh
    return doc


def test_the_committed_model_names_each_shells_platform_commit():
    doc = pins.load_model()
    for sid, sh in doc["shells"].items():
        assert sh["platform"]["ref"] and len(sh["platform"]["commit"]) == 40, sid
    assert doc["shells"][doc["default_shell"]]["platform"]["commit"] == \
        doc["status"]["platform_commit"]


def test_a_second_shell_in_the_model_gets_its_own_rm_kit():
    model = PinModel(two_shell_doc(), pack="mps3")
    d = xdc.from_doc({"kind": "rm", "name": "minimal", "static_id": RC2,
                      "use": {"clkrst": {}, "status": {"tie": ["dut_lockup", "irq_out"]}}},
                     origin="inline")
    k = xdc.rm_kit(model, d)
    assert not [f for f in k.findings if f.code == "static_id"]
    assert k.facts["static_id"] == RC2 and k.facts["boundary"]["ports"] == 47
    assert f"every partition port of static {RC2}" in k.files["minimal_wrapper_skeleton.sv"]
    # twin: a static the model does not describe is a static_id finding (and falls back)
    d.doc["static_id"] = "0x12345678"
    k = xdc.rm_kit(model, d)
    assert [f.severity for f in k.findings if f.code == "static_id"] == ["error"]


def test_kit_script_for_rc2_needs_rc2_in_the_pin_model(rc2_kits, store, monkeypatch):
    # today (single-shell model): the XDC kit refuses the static it does not describe
    with pytest.raises(RefusedError, match="has no shell '0x44EE76D5'"):
        script.make_script(rc2_kits, pack="mps3", static_id=RC2, design="minimal", store=store,
                           vivado=found_with(None))
    # with RC2 in the model (what the generator writes once its record lands): it builds
    doc = two_shell_doc()
    monkeypatch.setattr(pins, "load_model", lambda: copy.deepcopy(doc))
    s = script.make_script(rc2_kits, pack="mps3", static_id=RC2, design="minimal", store=store,
                           vivado=found_with(None))
    assert s.static_id == RC2 and s.params["VIVADO_VERSION"] == "2026.1"
    assert s.params["RM_SOURCES"] == "xdc/minimal_wrapper_skeleton.sv"
    assert not [c for c in s.checks if c.name.startswith("xdc") and c.state == "mismatch"]


# --- opt-in: the real 2026.1, read-only ------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("HM_REAL_VIVADO") != "1",
                    reason="opt-in: HM_REAL_VIVADO=1 runs the real `vivado -version` (~5 s)")
def test_opt_in_the_real_2026_1_is_found_by_its_install_dir(monkeypatch, tmp_path):
    if not (REAL_2026 / "Vivado" / "bin" / "vivado").is_file():
        pytest.skip(f"no Vivado 2026.1 at {REAL_2026}")
    monkeypatch.chdir(tmp_path)                      # nothing Vivado writes lands in the repo
    f = vivado.discover(env={vivado.ENV: str(REAL_2026)}, roots=())
    assert f.install is not None and f.install.version.startswith("2026.1"), f
    assert f.install.path == str(REAL_2026 / "Vivado" / "bin" / "vivado")
    assert f.install.build > 0
