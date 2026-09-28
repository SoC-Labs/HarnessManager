"""KIT-LIC: the licence the build needs (``services/kit/licence.py``), the "does Vivado
start" probe (``services/kit/launch.py``) and both in the guide's Tools step.

The probe runs POSIX-sh fakes (``kit_fakes.fake_vivado_script``: a licensed start, exit 42
with no licence, exit 1, a hang) through the real runner (``core.proc.run_to_file``). One
test runs the real 2026.1 ``-version`` (skipped when it is not installed); the real launch
with no licence is opt-in (``HM_REAL_VIVADO=1``). Neither ever synthesises. Every check has
a negative twin.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from harness_manager.core.proc import run_to_file
from harness_manager.services.kit import guide, launch, licence, vivado
from harness_manager.services.kit.service import HubSource, KitService
from harness_manager.services.store import ContentStore
from tests.fakes import kit_fakes as kf

SID = kf.STATIC_ID
RC2 = "0x44EE76D5"
PART = "xcku115-flvb1760-1-c"
REAL_2026 = Path("/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado")
posix = pytest.mark.skipif(sys.platform == "win32", reason="the fake vivado is POSIX sh")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv(vivado.ENV, "off")
    launch.clear_cache()
    yield
    launch.clear_cache()


def no_licence_env(monkeypatch, home: Path | None = None) -> None:
    for k in launch.LICENCE_ENV:
        monkeypatch.delenv(k, raising=False)
    if home is not None:
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home))


# --- the licence line ------------------------------------------------------------------------------


def test_the_licence_line_is_worded_by_the_kits_release():
    assert licence.needs(PART, "2026.1", RC2) == (
        "a device licence for xcku115 is needed from synthesis on: Vivado 2026.1 Core or "
        "higher (2024.1: Enterprise); the static's IP needs none")
    assert licence.needs(PART, "2024.1.0", SID) == (
        "a device licence for xcku115 is needed from synthesis on: Vivado 2024.1 Enterprise "
        "(2026.1: Core or higher); the static's IP needs none")
    line = licence.line(PART, "2026.1", RC2)
    assert line.startswith("licence: unchecked (a device licence for xcku115") and line.endswith(")")
    assert "unchecked is not a pass" in line and "[Common 17-345]" in line


def test_negative_twins_an_unmeasured_release_device_or_static_is_never_guessed():
    t = licence.needs(PART, "2025.2", SID)
    assert "Vivado 2026.1 Core or higher, 2024.1 Enterprise" in t
    assert "the edition for 2025.2 was not checked" in t
    assert "(the kit names the part)" in licence.needs("", "", None)
    other = licence.needs("xc7z020-clg400-1", "2026.1")
    assert "xc7z020 may be needed" in other and "editions for xcku115 only" in other
    assert licence.needs(PART, "2026.1", "0x12345678").endswith(
        "the static's IP licences were not checked")
    assert "IP" not in licence.needs(PART, "2026.1", None)


def test_the_free_tier_is_named_and_a_paid_one_says_nothing():
    note = licence.tier_note(PART, "2026.1", "BASIC")
    assert "Basic tier does not cover the xcku115 (it needs Core or higher)" in note
    assert "[Common 17-345]" in note
    assert licence.tier_note(PART, "2026.1", "ENTERPRISE") == ""
    assert licence.tier_note(PART, "2026.1", "") == ""
    assert licence.tier_note("xc7z020-clg400-1", "2026.1", "BASIC") == ""


# --- the launch probe ------------------------------------------------------------------------------


def fake(tmp_path: Path, how: str, release: str = "2026.1", **kw) -> Path:
    return kf.fake_vivado_script(tmp_path / how / release / "Vivado" / "bin", release,
                                 launch=how, **kw)


def armed(monkeypatch, exe: Path) -> Path:
    """Vivado is on (the setting names the fake), so the probe may run it."""
    monkeypatch.setenv(vivado.ENV, str(exe))
    return exe


@posix
def test_a_licensed_vivado_starts_and_runs_the_probes_tcl(tmp_path, monkeypatch):
    exe = armed(monkeypatch, fake(tmp_path, "ok"))
    r = launch.probe(exe, release="2026.1")
    assert (r.state, r.rc, r.tier, r.no_licence) == ("ok", 0, "ENTERPRISE", False)
    assert r.detail == "Vivado 2026.1 starts (licence tier: ENTERPRISE)"
    assert r.check().state == "ok" and r.to_json()["ran"] is True
    assert launch.argv(str(exe), "t.tcl") == [str(exe), "-mode", "batch", "-nolog",
                                              "-nojournal", "-notrace", "-source", "t.tcl"]


@posix
def test_negative_twin_exit_0_without_the_tcl_is_not_a_start(tmp_path, monkeypatch):
    # a wrapper that exits 0 and never runs Vivado: the marker only comes from the Tcl
    exe = tmp_path / "wrapper" / "vivado"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\necho 'vivado v2026.1 (64-bit)'\nexit 0\n")
    exe.chmod(0o755)
    armed(monkeypatch, exe)
    r = launch.probe(exe, release="2026.1")
    assert r.state == "unchecked" and "did not run the probe's Tcl" in r.detail


@posix
def test_exit_42_with_no_licence_file_says_which_variable_to_set(tmp_path, monkeypatch):
    exe = armed(monkeypatch, fake(tmp_path, "no-licence"))
    no_licence_env(monkeypatch)
    r = launch.probe(exe, release="2026.1")
    assert (r.state, r.rc, r.no_licence) == ("failed", 42, True)
    assert r.detail == ("Vivado 2026.1 did not start: no licence file (set XILINXD_LICENSE_FILE "
                        "or LM_LICENSE_FILE); it exited 42")
    assert r.action == "export XILINXD_LICENSE_FILE=PORT@SERVER"
    assert r.check().state == "mismatch"


@posix
def test_negative_twin_exit_42_with_a_licence_variable_set_blames_the_server(tmp_path,
                                                                            monkeypatch):
    exe = armed(monkeypatch, fake(tmp_path, "no-licence"))
    no_licence_env(monkeypatch)
    monkeypatch.setenv("XILINXD_LICENSE_FILE", "2100@nowhere.example")
    r = launch.probe(exe, release="2026.1")
    assert r.state == "failed" and r.no_licence
    assert "no valid licence through XILINXD_LICENSE_FILE=2100@nowhere.example" in r.detail
    assert "no licence file" not in r.detail and r.action == ""
    assert "serves Vivado" in r.fix


@posix
def test_any_other_failure_is_reported_with_its_exit_code(tmp_path, monkeypatch):
    exe = armed(monkeypatch, fake(tmp_path, "error"))
    r = launch.probe(exe, release="2026.1")
    assert (r.state, r.rc, r.no_licence) == ("failed", 1, False)
    assert r.detail.startswith("Vivado 2026.1 did not start: it exited 1 (ERROR: [Common 17-39]")
    # a file that does not run at all
    bad = tmp_path / "notexec" / "vivado"
    bad.parent.mkdir()
    bad.write_text("")
    armed(monkeypatch, bad)
    r = launch.probe(bad, release="2026.1")
    assert r.state == "failed" and "does not run" in r.detail and r.rc is None


@posix
def test_a_hang_is_killed_with_its_children_and_is_unchecked(tmp_path, monkeypatch):
    exe = armed(monkeypatch, fake(tmp_path, "hang"))
    t0 = time.monotonic()
    r = launch.probe(exe, release="2026.1", timeout=1.0)
    assert time.monotonic() - t0 < 10
    assert (r.state, r.rc) == ("unchecked", None)
    assert "did not finish starting within 1 s" in r.detail
    child = int((exe.parent / "vivado.child").read_text())
    for _ in range(50):                               # the group was SIGKILLed; reaped by init
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"the hung launch's child {child} outlived the probe")


@posix
def test_nothing_lands_in_the_callers_dir_or_the_temp_dir(tmp_path, monkeypatch):
    exe = tmp_path / "dropper" / "vivado"
    exe.parent.mkdir()
    exe.write_text('#!/bin/sh\ntouch vivado.jou .Xil\necho HM_LAUNCH_OK\nexit 0\n')
    exe.chmod(0o755)
    armed(monkeypatch, exe)
    work, tmp = tmp_path / "work", tmp_path / "tmp"
    work.mkdir()
    tmp.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr("tempfile.tempdir", str(tmp))
    assert launch.probe(exe).state == "ok"
    assert list(work.iterdir()) == [] and list(tmp.iterdir()) == []


def test_off_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "run_to_file", lambda *a, **k: pytest.fail("ran with off"))
    r = launch.probe(tmp_path / "vivado")
    assert r.state == "unchecked" and not r.ran and "Vivado is off" in r.detail
    # on, but the file is gone: nothing to run either
    monkeypatch.setenv(vivado.ENV, str(tmp_path))
    r = launch.probe(tmp_path / "gone" / "vivado")
    assert r.state == "unchecked" and not r.ran and "is not there" in r.detail


@posix
def test_cached_per_binary_and_licence_variables_and_a_failure_only_briefly(tmp_path,
                                                                           monkeypatch):
    calls: list[list[str]] = []

    def counting(argv, timeout, **kw):
        calls.append(list(argv))
        return run_to_file(argv, timeout, **kw)

    monkeypatch.setattr(launch, "run_to_file", counting)
    ok = armed(monkeypatch, fake(tmp_path, "ok"))
    launch.probe(ok)
    launch.probe(ok)
    assert len(calls) == 1                                # a start is kept
    monkeypatch.setenv("XILINXD_LICENSE_FILE", "2100@other.example")
    launch.probe(ok)
    assert len(calls) == 2                                # another licence: asked again
    st = ok.stat()
    os.utime(ok, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    launch.probe(ok)
    assert len(calls) == 3                                # a replaced binary: asked again
    bad = armed(monkeypatch, fake(tmp_path, "no-licence"))
    launch.probe(bad)
    launch.probe(bad)
    assert len(calls) == 4                                # a failure is kept for a while ...
    monkeypatch.setattr(launch, "FAILURE_TTL_S", 0.0)
    launch.probe(bad)
    assert len(calls) == 5                                # ... then asked again


# --- the guide's Tools step ------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> ContentStore:
    return ContentStore(tmp_path / "store")


@pytest.fixture
def kits26(tmp_path, store) -> KitService:
    k = KitService(store, tmp_path / "kits", hub=HubSource(None))
    k.import_(kf.build_fixture(tmp_path / "kit26", SID, release="2026.1"))
    return k


@pytest.fixture
def kits24(tmp_path, store) -> KitService:
    k = KitService(store, tmp_path / "kits24", hub=HubSource(None))
    k.import_(kf.FIXTURE)
    return k


def found_at(exe: Path, release: str) -> vivado.VivadoFound:
    return vivado.VivadoFound(vivado.VivadoInstall(str(exe), release, 0, "env"),
                              want=(release,))


def tools_of(kits: KitService, exe: Path, release: str) -> tuple[guide.Guide, guide.Step]:
    g = guide.guide(kits, static_id=SID, vivado=found_at(exe, release))
    return g, g.steps[1]


@posix
def test_guide_2026_1_starts_and_the_device_licence_stays_unchecked(kits26, tmp_path,
                                                                     monkeypatch):
    exe = armed(monkeypatch, fake(tmp_path, "ok"))
    g, t = tools_of(kits26, exe, "2026.1")
    assert t.state == "done", t.detail
    assert ("Vivado 2026.1 starts (licence tier: ENTERPRISE); that does not prove the device "
            "licence for xcku115: synthesis still checks it") in t.detail
    assert ("; licence: unchecked (a device licence for xcku115 is needed from synthesis on: "
            "Vivado 2026.1 Core or higher (2024.1: Enterprise); the static's IP needs none; "
            "unchecked is not a pass") in t.detail
    assert {c.name: c.state for c in t.checks}["vivado_launch"] == "ok"
    assert g.to_json()["vivado"]["launch"]["state"] == "ok"


@posix
def test_negative_twin_guide_2026_1_with_no_licence_file_fails_tools(kits26, tmp_path,
                                                                      monkeypatch):
    exe = armed(monkeypatch, fake(tmp_path, "no-licence"))
    no_licence_env(monkeypatch)
    g, t = tools_of(kits26, exe, "2026.1")
    assert t.state == "failed"
    assert ("Vivado 2026.1 did not start: no licence file (set XILINXD_LICENSE_FILE or "
            "LM_LICENSE_FILE); it exited 42") in t.detail
    assert "licence: unchecked (" in t.detail                  # still named, never a pass
    assert t.reason.startswith("fix: export XILINXD_LICENSE_FILE=PORT@SERVER")
    assert t.actions[0]["text"] == "export XILINXD_LICENSE_FILE=PORT@SERVER"
    states = {s.id: s.state for s in g.steps}
    assert states["kit"] == "done" and states["build"] == "blocked"
    assert "2 tools" in g.steps[4].reason
    assert g.to_json()["vivado"]["launch"]["rc"] == 42


@posix
def test_guide_2024_1_names_enterprise_and_a_failed_start_its_exit_code(kits24, tmp_path,
                                                                         monkeypatch):
    exe = armed(monkeypatch, fake(tmp_path, "ok", release="2024.1"))
    _, t = tools_of(kits24, exe, "2024.1")
    assert t.state == "done"
    assert ("needed from synthesis on: Vivado 2024.1 Enterprise (2026.1: Core or higher); the "
            "static's IP needs none") in t.detail
    launch.clear_cache()
    bad = armed(monkeypatch, fake(tmp_path, "error", release="2024.1"))
    _, t = tools_of(kits24, bad, "2024.1")
    assert t.state == "failed" and "Vivado 2024.1 did not start: it exited 1" in t.detail
    assert t.reason.startswith("fix: run `") and "-mode tcl" in t.reason


@posix
def test_guide_a_slow_start_is_unchecked_not_failed_and_basic_is_named(kits26, tmp_path,
                                                                        monkeypatch):
    monkeypatch.setattr(launch, "LAUNCH_TIMEOUT_S", 1.0)
    exe = armed(monkeypatch, fake(tmp_path, "hang"))
    _, t = tools_of(kits26, exe, "2026.1")
    assert t.state == "done" and "did not finish starting within 1 s" in t.detail
    assert {c.name: c.state for c in t.checks}["vivado_launch"] == "unchecked"
    basic = armed(monkeypatch, fake(tmp_path, "ok", tier="BASIC"))
    _, t = tools_of(kits26, basic, "2026.1")
    assert "Vivado 2026.1 starts (licence tier: BASIC); the Basic tier does not cover the xcku115" \
        in t.detail


def test_guide_launches_nothing_for_another_release_or_when_off(kits26, tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "run_to_file", lambda *a, **k: pytest.fail("launched"))
    # a 2024.1 for a 2026.1 kit: the step is not done anyway; its Vivado is not launched
    g, t = tools_of(kits26, tmp_path / "vivado", "2024.1")
    assert t.state == "next" and "vivado_launch" not in {c.name for c in t.checks}
    assert g.to_json()["vivado"]["launch"] is None
    # the kit's release, but Vivado is off (the tests' default): nothing runs, nothing is said
    g, t = tools_of(kits26, tmp_path / "vivado", "2026.1")
    assert t.state == "done" and "starts" not in t.detail
    assert "Vivado 2026.1 Core or higher" in t.detail


# --- the real 2026.1 (read/execute only; never synthesises) ----------------------------------------


@posix
@pytest.mark.skipif(not REAL_2026.is_file(), reason=f"no Vivado 2026.1 at {REAL_2026}")
def test_real_2026_1_version_exits_0_even_with_no_licence(tmp_path, monkeypatch):
    """Why the probe launches instead of asking ``-version``: the real 2026.1 answers
    ``-version`` with no licence at all (measured 2026-09-28: exit 0, ~3 s)."""
    no_licence_env(monkeypatch, tmp_path / "home")
    rc, out = run_to_file([str(REAL_2026), "-version"], 60.0, cwd=tmp_path)
    assert rc == 0 and "vivado v2026.1" in out.lower(), out
    assert list(p.name for p in tmp_path.iterdir()) == ["home"]


@posix
@pytest.mark.skipif(os.environ.get("HM_REAL_VIVADO") != "1",
                    reason="opt-in: HM_REAL_VIVADO=1 launches the real 2026.1 with no licence "
                           "(~5 s; it cannot start, so it never synthesises)")
def test_opt_in_real_2026_1_launch_with_no_licence_exits_42(tmp_path, monkeypatch):
    if not REAL_2026.is_file():
        pytest.skip(f"no Vivado 2026.1 at {REAL_2026}")
    no_licence_env(monkeypatch, tmp_path / "home")
    monkeypatch.setenv(vivado.ENV, str(REAL_2026))
    monkeypatch.chdir(tmp_path)
    r = launch.probe(REAL_2026, release="2026.1")
    assert (r.state, r.rc, r.no_licence) == ("failed", 42, True), r
    assert r.detail == ("Vivado 2026.1 did not start: no licence file (set XILINXD_LICENSE_FILE "
                        "or LM_LICENSE_FILE); it exited 42")
