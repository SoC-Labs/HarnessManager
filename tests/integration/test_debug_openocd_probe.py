"""DEBUG-OCD: an OpenOCD built without remote_bitbang is caught before ``debug up`` runs it.

On srv03335 the only OpenOCD was the SoC Labs build (jlink, buspirate, hostio4), and
``debug up`` failed deep inside OpenOCD. Now ``find_openocd`` asks each binary for its
adapters (``services/openocd_probe.py``: ``-c "adapter list" -c shutdown``) and refuses one
without the board's adapter, naming it, what it has and the fix.

The binaries are ``tests/fakes/ocd_adapter_fakes.py`` (every output style measured on a
real build) and ``stub_openocd`` (the debug service's own stand-in). The real xPack and
SoC Labs builds are asked too, when this host has them. Every check has a negative twin.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode, UnavailableError
from harness_manager.core.events import EventBus
from harness_manager.services import debug as dbg
from harness_manager.services import openocd_probe as P
from harness_manager.services.debug import DebugService, find_openocd, openocd_report
from harness_manager.settings import runtime
from tests.fakes.ocd_adapter_fakes import SOCLABS, make_fake, runs
from tests.fakes.t4_console_rig import BareSession, EventLog
from tests.fakes.t4_debug_rig import StaticDebugAdapter, StubRig, use_stub
from tests.fakes.t4_rbb_jtag import FakeJtagServer
from tests.fakes.t5_fake_engine import FakeEngine

XPACK = Path.home() / "opt/xpack-openocd/xpack-openocd-0.12.0-7/bin/openocd"
SOCLABS_BUILD = Path.home() / "SoCLabs/soclabs-openocd/install/bin/openocd"
FIX_WORDS = ("xPack OpenOCD 0.12", "harness-manager config set tools.openocd PATH",
             "Settings -> Tools")

# What the real builds print (srv03335, 2026-09-27), trimmed.
REAL_0120 = """Open On-Chip Debugger 0.12.0-g9ea7f3d-dirty (2026-09-24-08:41)
Licensed under GNU GPL v2
For bug reports, read
\thttp://openocd.org/doc/doxygen/bugs.html
The following debug adapters are available:
1: jlink
2: buspirate
3: hostio4

shutdown command invoked
"""
REAL_XPACK = """xPack Open On-Chip Debugger 0.12.0+dev-02228-ge5888bda3-dirty (2025-10-04-22:42)
Licensed under GNU GPL v2
For bug reports, read
\thttp://openocd.org/doc/doxygen/bugs.html
amt_jtagaccel  { jtag }
cmsis-dap      { jtag swd }
remote_bitbang { jtag swd }
st-link        { jtag swd swim }
shutdown command invoked
"""
REAL_INTERFACE_LIST = """Open On-Chip Debugger 0.10.0
The following debug interfaces are available:
1: parport
2: remote_bitbang
"""


@pytest.fixture(autouse=True)
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    runtime.reset()
    P.clear_cache()
    monkeypatch.setattr(runtime, "POLICY_PATH", tmp_path / "policy.toml")
    monkeypatch.delenv(dbg.OPENOCD_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    log = tmp_path / "fake_openocd.jsonl"
    monkeypatch.setenv("FAKE_OPENOCD_LOG", str(log))
    yield log
    runtime.reset()
    P.clear_cache()


def settings(text: str) -> None:
    root = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.toml").write_text(text, encoding="utf-8")


def fake(tmp_path: Path, where: str, mode: str) -> Path:
    return make_fake(tmp_path / where, mode)


def on_path(monkeypatch: pytest.MonkeyPatch, *dirs: Path) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in dirs))


# --- parsing: every output style, measured -------------------------------------------------


def test_the_three_list_styles_parse_and_the_banner_does_not():
    assert P.parse_adapters(REAL_0120) == ("jlink", "buspirate", "hostio4")
    assert P.parse_adapters(REAL_XPACK) == ("amt_jtagaccel", "cmsis-dap", "remote_bitbang",
                                            "st-link")
    assert P.parse_adapters(REAL_INTERFACE_LIST) == ("parport", "remote_bitbang")
    # twin: the banner, the URL and the shutdown line are not adapters
    head = "\n".join(REAL_0120.splitlines()[:4] + ["shutdown command invoked", "Error: x"])
    assert P.parse_adapters(head) == ()


# --- the probe on fake binaries ---------------------------------------------------------------


@pytest.mark.parametrize(("mode", "has", "command"), [
    ("v012", True, "adapter list"),
    ("xpack", True, "adapter list"),
    ("v011", True, "interface_list"),
    ("soclabs", False, "adapter list"),
])
def test_each_build_style_is_read(tmp_path, fresh, mode, has, command):
    binary = fake(tmp_path, mode, mode)
    got = P.probe_adapters(binary)
    assert got.listed and got.error == ""
    assert got.has(P.REMOTE_BITBANG) is has
    assert got.command == command
    if mode == "soclabs":
        assert got.adapters == SOCLABS
    # only the probe ran: -c commands ending in shutdown, never a config (the fake exits 9)
    argvs = [r["argv"] for r in runs(fresh)]
    assert argvs[0] == ["-c", "adapter list", "-c", "shutdown"]
    assert all("-f" not in a and "-s" not in a for a in argvs)
    # twin: the interface_list fallback runs only when adapter list gave nothing
    assert len(argvs) == (2 if mode == "v011" else 1)
    if mode == "v011":
        assert argvs[1] == ["-c", "interface_list", "-c", "shutdown"]


def test_a_hanging_binary_times_out_with_a_clear_error(tmp_path, fresh, monkeypatch):
    assert P.PROBE_TIMEOUT_S == 10.0                    # the default the service uses
    binary = fake(tmp_path, "h", "hang")
    t0 = time.monotonic()
    got = P.probe_adapters(binary, timeout=1.0)
    took = time.monotonic() - t0
    assert not got.listed and got.adapters == ()
    assert "did not finish within 1 s" in got.error and 'adapter list' in got.error
    assert took < 8.0                                    # killed, not waited out (30 s)
    assert len(runs(fresh)) == 1                         # no interface_list after a timeout
    # a failure is remembered briefly (a polled `debug status` never waits it out again) ...
    assert P.probe_adapters(binary, timeout=1.0).error == got.error
    assert len(runs(fresh)) == 1
    # ... twin: and asked again once FAILURE_TTL_S has passed
    monkeypatch.setattr(P, "FAILURE_TTL_S", 0.0)
    P.probe_adapters(binary, timeout=1.0)
    assert len(runs(fresh)) == 2


def test_a_binary_that_exits_non_zero_says_so(tmp_path, fresh):
    got = P.probe_adapters(fake(tmp_path, "f", "fail"))
    assert not got.listed
    assert "exited 2" in got.error and "this build cannot start" in got.error
    # twin: a good one beside it lists
    assert P.probe_adapters(fake(tmp_path, "g", "v012")).listed


def test_a_binary_that_does_not_run_is_an_error_not_a_crash(tmp_path):
    bad = tmp_path / "notexe" / "openocd"
    bad.parent.mkdir()
    bad.write_text("not a program\n")
    got = P.probe_adapters(bad)
    assert not got.listed and got.error


def test_the_list_is_cached_until_the_binary_changes(tmp_path, fresh):
    binary = fake(tmp_path, "c", "v012")
    first = P.probe_adapters(binary)
    assert P.probe_adapters(binary) == first
    assert len(runs(fresh)) == 1                         # cached: not run again
    st = binary.stat()
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert P.probe_adapters(binary).has(P.REMOTE_BITBANG)
    assert len(runs(fresh)) == 2                         # twin: a new mtime is asked again
    binary.write_text(binary.read_text() + "\n")         # same mtime second, new size
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    P.probe_adapters(binary)
    assert len(runs(fresh)) == 3                         # a new size is asked again too


def test_a_callers_runner_is_used_and_never_cached(tmp_path):
    seen: list[list[str]] = []

    def runner(argv, **kw):
        seen.append(argv)
        assert kw["timeout"] == P.PROBE_TIMEOUT_S and kw["capture_output"]
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": REAL_0120})()

    binary = fake(tmp_path, "r", "v012")
    assert P.probe_adapters(binary, runner=runner).adapters == ("jlink", "buspirate", "hostio4")
    P.probe_adapters(binary, runner=runner)
    assert len(seen) == 2 and seen[0][1:] == ["-c", "adapter list", "-c", "shutdown"]


def test_every_openocd_on_path_is_a_candidate_in_order(tmp_path, monkeypatch):
    a, b = fake(tmp_path, "a", "soclabs"), fake(tmp_path, "b", "v012")
    empty = tmp_path / "nothing"
    empty.mkdir()
    path = os.pathsep.join(map(str, (a.parent, empty, b.parent, a.parent)))
    assert P.candidates("openocd", path) == [str(a), str(b)]     # a twice: once
    assert shutil.which("openocd", path=path) == str(a)          # twin: which sees only one
    if os.name == "posix":
        link = tmp_path / "link"
        link.mkdir()
        (link / "openocd").symlink_to(b)
        assert P.candidates("openocd", f"{b.parent}{os.pathsep}{link}") == [str(b)]
        plain = tmp_path / "plain" / "openocd"
        plain.parent.mkdir()
        plain.write_text("#!/bin/sh\n")                           # not executable
        assert P.candidates("openocd", str(plain.parent)) == []


# --- find_openocd: the PATH search -----------------------------------------------------------


def test_the_path_search_skips_a_build_without_the_adapter(tmp_path, monkeypatch, fresh):
    bad, good = fake(tmp_path, "first", "soclabs"), fake(tmp_path, "second", "v012")
    on_path(monkeypatch, bad.parent, good.parent)
    assert find_openocd() == str(good)
    assert [r["mode"] for r in runs(fresh)] == ["soclabs", "v012"]


def test_negative_twin_a_good_first_candidate_is_taken_and_the_rest_never_run(
        tmp_path, monkeypatch, fresh):
    good, bad = fake(tmp_path, "first", "xpack"), fake(tmp_path, "second", "soclabs")
    on_path(monkeypatch, good.parent, bad.parent)
    assert find_openocd() == str(good)
    assert [r["mode"] for r in runs(fresh)] == ["xpack"]


def test_no_candidate_with_the_adapter_lists_each_one(tmp_path, monkeypatch):
    bad, broken = fake(tmp_path, "one", "soclabs"), fake(tmp_path, "two", "fail")
    on_path(monkeypatch, bad.parent, broken.parent)
    with pytest.raises(UnavailableError) as exc:
        find_openocd()
    err = exc.value
    assert err.code == ExitCode.UNAVAILABLE == 12
    assert "no OpenOCD on PATH has the remote_bitbang adapter" in err.reason
    assert f"{bad} (jlink, buspirate, hostio4)" in err.reason
    assert str(broken) in err.reason and "exited 2" in err.reason
    assert all(w in err.hint for w in FIX_WORDS)


def test_no_binary_at_all_keeps_the_not_found_error(tmp_path, monkeypatch):
    with pytest.raises(UnavailableError) as exc:
        find_openocd()
    assert exc.value.reason == "OpenOCD not found — install it or set HARNESS_MANAGER_OPENOCD"


# --- find_openocd: the configured one, and SET-WIRE's precedence -------------------------------


def test_a_configured_build_without_the_adapter_is_refused_naming_it(tmp_path, monkeypatch):
    bad, good = fake(tmp_path, "cfg", "soclabs"), fake(tmp_path, "path", "v012")
    on_path(monkeypatch, good.parent)
    settings(f'[tools]\nopenocd = "{bad}"\n')
    with pytest.raises(UnavailableError) as exc:
        find_openocd()
    err = exc.value
    assert err.reason == (f"tools.openocd={bad} (settings.toml) has no remote_bitbang adapter "
                          "(it has: jlink, buspirate, hostio4)")
    assert all(w in err.hint for w in FIX_WORDS)
    assert "HARNESS_MANAGER_OPENOCD" not in err.hint
    # twin: the configured one is the only one tried (the good one on PATH is not taken),
    # and a configured good one is taken
    settings(f'[tools]\nopenocd = "{good}"\n')
    assert find_openocd() == str(good)


def test_the_setting_wins_over_path(tmp_path, monkeypatch, fresh):
    mine, path_one = fake(tmp_path, "mine", "xpack"), fake(tmp_path, "onpath", "v012")
    on_path(monkeypatch, path_one.parent)
    settings(f'[tools]\nopenocd = "{mine}"\n')
    assert find_openocd() == str(mine)
    assert [r["mode"] for r in runs(fresh)] == ["xpack"]          # PATH's never asked
    settings("")                                                  # twin: unset: PATH's
    assert find_openocd() == str(path_one)


def test_the_variable_wins_over_the_setting(tmp_path, monkeypatch):
    good, bad = fake(tmp_path, "setting", "v012"), fake(tmp_path, "env", "soclabs")
    settings(f'[tools]\nopenocd = "{good}"\n')
    monkeypatch.setenv(dbg.OPENOCD_ENV, str(bad))
    with pytest.raises(UnavailableError) as exc:
        find_openocd()
    assert exc.value.reason.startswith(f"HARNESS_MANAGER_OPENOCD={bad} has no remote_bitbang")
    assert "$HARNESS_MANAGER_OPENOCD" in exc.value.hint and "unset it" in exc.value.hint
    monkeypatch.setenv(dbg.OPENOCD_ENV, str(good))                # twin: a good variable
    settings(f'[tools]\nopenocd = "{bad}"\n')
    assert find_openocd() == str(good)


def test_a_configured_binary_that_hangs_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROBE_TIMEOUT_S", 1.0)
    hung = fake(tmp_path, "hung", "hang")
    monkeypatch.setenv(dbg.OPENOCD_ENV, str(hung))
    t0 = time.monotonic()
    with pytest.raises(UnavailableError) as exc:
        find_openocd()
    assert time.monotonic() - t0 < 8.0
    assert "could not be asked for its adapters" in exc.value.reason
    assert "did not finish within 1 s" in exc.value.reason


def test_a_configured_name_on_path_is_probed_by_its_path(tmp_path, monkeypatch):
    bad = make_fake(tmp_path / "named", "soclabs", name="my-openocd")
    on_path(monkeypatch, bad.parent)
    monkeypatch.setenv(dbg.OPENOCD_ENV, "my-openocd")
    with pytest.raises(UnavailableError) as exc:
        find_openocd()
    assert f"HARNESS_MANAGER_OPENOCD=my-openocd ({bad}) has no remote_bitbang" in exc.value.reason


# --- the report `debug status` shows -----------------------------------------------------------


def test_the_report_says_which_openocd_and_whether_it_has_the_adapter(tmp_path, monkeypatch):
    good = fake(tmp_path, "good", "v012")
    monkeypatch.setenv(dbg.OPENOCD_ENV, str(good))
    rep = openocd_report()
    assert rep["ok"] and rep["path"] == str(good) and "remote_bitbang" in rep["adapters"]
    assert rep["detail"] == f"{good}: has remote_bitbang (4 adapters)" and rep["hint"] == ""
    bad = fake(tmp_path, "bad", "soclabs")                         # twin: never raises
    monkeypatch.setenv(dbg.OPENOCD_ENV, str(bad))
    rep = openocd_report()
    assert not rep["ok"] and rep["path"] == ""
    assert str(bad) in rep["detail"] and "jlink, buspirate, hostio4" in rep["detail"]
    assert "xPack" in rep["hint"]


# --- debug up refuses before anything starts ---------------------------------------------------


@pytest.fixture
def rig(monkeypatch, tmp_path) -> StubRig:
    return use_stub(monkeypatch, tmp_path)


def test_debug_up_refuses_a_build_without_the_adapter_before_any_process(rig, monkeypatch):
    from tests.fakes.stub_openocd import read_log

    monkeypatch.setenv("STUB_OPENOCD_ADAPTERS", "jlink,buspirate,hostio4")
    bus = EventBus()
    events = EventLog(bus, "debug.state")
    asked: list[str] = []

    class Recording(StaticDebugAdapter):
        def openocd_config(self) -> tuple[str, ...]:
            asked.append("config")                    # the board's identity, in the MPS3 pack
            return super().openocd_config()

    with FakeJtagServer() as jtag:
        svc = DebugService(bus, start_timeout=15)
        session = BareSession("mps3@ocd-test", debug=Recording(rig.cfg_dir, jtag.port))
        try:
            with pytest.raises(UnavailableError) as exc:
                svc.up(session)
            assert exc.value.code == ExitCode.UNAVAILABLE
            assert str(rig.binary) in exc.value.reason and "hostio4" in exc.value.reason
            assert rig.runs() == []                                  # no OpenOCD session
            assert [e for e in read_log(rig.log) if "probe" in e]    # only the probe ran
            assert jtag.accepted == 0 and asked == []                # no board, no config
            assert not (svc.registry_dir.is_dir() and list(svc.registry_dir.glob("*.json")))
            failed = events.wait_for(lambda e: e.data["state"] == "failed", timeout=2.0)
            assert "hostio4" in failed.data["detail"]                # the GUI is told why
            # twin: the same service with the stub's default list (remote_bitbang) comes up
            monkeypatch.delenv("STUB_OPENOCD_ADAPTERS")
            P.clear_cache()
            st = svc.up(session)
            assert st.state == "up" and len(rig.runs()) == 1 and jtag.accepted == 1
            assert asked == ["config"]
        finally:
            svc.close()


def test_debug_detect_refuses_it_too(rig, monkeypatch):
    monkeypatch.setenv("STUB_OPENOCD_ADAPTERS", "jlink,hostio4")
    with FakeJtagServer() as jtag:
        svc = DebugService(None)
        session = BareSession("mps3@ocd-detect", debug=StaticDebugAdapter(rig.cfg_dir, jtag.port))
        with pytest.raises(UnavailableError, match="no remote_bitbang adapter"):
            svc.detect(session)
        assert rig.runs() == [] and jtag.accepted == 0


# --- the CLI: refused before the board (and its tunnel) is opened ------------------------------


@pytest.fixture
def cli_fake() -> Iterator[FakeEngine]:
    eng = FakeEngine()
    previous = set_engine_factory(lambda _args: eng)
    yield eng
    set_engine_factory(previous)


def test_cli_debug_up_refuses_before_opening_the_board(cli_fake, capsys):
    def refuse() -> str:
        raise UnavailableError("debug_dut", "HARNESS_MANAGER_OPENOCD=/x/openocd has no "
                                            "remote_bitbang adapter (it has: jlink)",
                               hint=P.fix_hint())

    cli_fake.debug.openocd = refuse
    rc = main(["debug", "up", "127.0.0.1", "--for", "0"])
    _, err = capsys.readouterr()
    assert rc == ExitCode.UNAVAILABLE
    assert err.strip() == ("harness-manager: debug_dut is unavailable — HARNESS_MANAGER_OPENOCD="
                           "/x/openocd has no remote_bitbang adapter (it has: jlink); "
                           + P.fix_hint())
    assert "engine.open" not in cli_fake.calls and "debug.up" not in cli_fake.calls
    # twin: an OpenOCD with the adapter goes on to open the board and start the server
    cli_fake.debug.openocd = lambda: "/good/openocd"
    assert main(["debug", "up", "127.0.0.1", "--for", "0"]) == ExitCode.OK
    assert "engine.open" in cli_fake.calls and "debug.up" in cli_fake.calls


def test_cli_debug_status_shows_the_adapter_verdict(cli_fake, capsys):
    import json

    cli_fake.debug.openocd_report = lambda _session=None: {
        "ok": False, "path": "", "need": "remote_bitbang", "adapters": [],
        "detail": "/x/openocd has no remote_bitbang adapter (it has: jlink)",
        "hint": P.fix_hint()}
    assert main(["debug", "status", "127.0.0.1"]) == ExitCode.OK   # read-only: still OK
    out, _ = capsys.readouterr()
    assert "openocd    /x/openocd has no remote_bitbang" in out and "fix: use an OpenOCD" in out
    assert main(["--json", "debug", "status", "127.0.0.1"]) == ExitCode.OK
    body = json.loads(capsys.readouterr()[0])
    assert body["openocd"]["ok"] is False and "hint" in body["openocd"]
    # twin: a service without the report (an older daemon) prints no openocd line
    cli_fake.debug.openocd_report = lambda _session=None: None
    assert main(["debug", "status", "127.0.0.1"]) == ExitCode.OK
    assert "openocd" not in capsys.readouterr()[0]
    del cli_fake.debug.openocd_report
    assert main(["debug", "status", "127.0.0.1"]) == ExitCode.OK
    assert "openocd" not in capsys.readouterr()[0]


# --- the real builds on this host (read-only: adapter list, then shutdown) ---------------------


@pytest.mark.skipif(not XPACK.is_file(), reason=f"no xPack OpenOCD at {XPACK}")
def test_real_xpack_has_remote_bitbang():
    got = P.probe_adapters(XPACK)
    assert got.listed and got.has("remote_bitbang") and got.command == "adapter list"
    assert got.version.startswith("0.12.0")


@pytest.mark.skipif(not SOCLABS_BUILD.is_file(), reason=f"no SoC Labs OpenOCD at {SOCLABS_BUILD}")
def test_real_soclabs_build_lacks_remote_bitbang():
    got = P.probe_adapters(SOCLABS_BUILD)
    assert got.listed and not got.has("remote_bitbang") and got.has("hostio4")


@pytest.mark.skipif(sys.platform == "win32" or not (XPACK.is_file() and SOCLABS_BUILD.is_file()),
                    reason="needs both real builds")
def test_real_builds_on_path_pick_xpack_after_the_soclabs_one(monkeypatch):
    on_path(monkeypatch, SOCLABS_BUILD.parent, XPACK.parent)
    assert find_openocd() == str(XPACK)


# --- through the daemon: the same refusal, the same words ---------------------------------------


def test_the_api_error_keeps_the_fix_hint():
    from harness_manager.cli.output import error_json, error_line
    from harness_manager.client.codec import error_from_json

    err = UnavailableError("debug_dut", "/x/openocd has no remote_bitbang adapter",
                           hint=P.fix_hint())
    back = error_from_json(error_json(err)["error"])
    assert isinstance(back, UnavailableError) and back.hint == err.hint
    assert error_line(back) == error_line(err)
    # twin: without a hint the line is unchanged from before (no trailing "; ")
    plain = error_from_json(error_json(UnavailableError("reboot_board", "needs the cable"))["error"])
    assert error_line(plain) == "harness-manager: reboot_board is unavailable — needs the cable"


def test_the_daemon_refuses_the_same_way_and_status_carries_the_verdict(
        rig, monkeypatch, tmp_path, capsys):
    import json

    from harness_manager.cli.engine import ENV_ENGINE as CLI_ENGINE_ENV
    from harness_manager.cli.engine import ENV_NO_DAEMON
    from tests.fakes.t13_daemon import LiveDaemon, engine_for, run_cli
    from tests.fakes.virtual_board import VirtualMps3

    monkeypatch.delenv(CLI_ENGINE_ENV, raising=False)
    monkeypatch.delenv(ENV_NO_DAEMON, raising=False)
    previous = set_engine_factory(None)
    monkeypatch.setenv("STUB_OPENOCD_ADAPTERS", ",".join(SOCLABS))
    try:
        with VirtualMps3(tmp_path / "nanosoc", boot_rm_id=0x01000001) as vb, \
                FakeJtagServer() as jtag:
            eng = engine_for(vb, rbb_port=jtag.port)
            try:
                with LiveDaemon(eng):
                    ep = vb.shell_endpoint
                    rc, out, _ = run_cli(capsys, "--json", "debug", "up", ep, "--for", "0")
                    remote = json.loads(out)["error"]
                    with monkeypatch.context() as m:
                        m.setenv(ENV_NO_DAEMON, "1")
                        rc_local, out_local, _ = run_cli(capsys, "--json", "debug", "up", ep,
                                                         "--for", "0")
                    local = json.loads(out_local)["error"]
                    assert rc == rc_local == ExitCode.UNAVAILABLE
                    assert remote["reason"] == local["reason"] and "hostio4" in local["reason"]
                    assert remote["hint"] == local["hint"] == P.fix_hint(env_var=dbg.OPENOCD_ENV)
                    assert rig.runs() == [] and jtag.accepted == 0      # nothing started
                    rc, out, _ = run_cli(capsys, "--json", "debug", "status", ep)
                    ocd = json.loads(out)["openocd"]
                    assert rc == 0 and ocd["ok"] is False and "hostio4" in ocd["detail"]
                    # twin: with remote_bitbang the daemon's verdict flips (a new list: the
                    # binary is the same, so the cache is dropped as a changed binary would be)
                    monkeypatch.delenv("STUB_OPENOCD_ADAPTERS")
                    P.clear_cache()
                    rc, out, _ = run_cli(capsys, "--json", "debug", "status", ep)
                    ocd = json.loads(out)["openocd"]
                    assert ocd["ok"] is True and ocd["path"] == str(rig.binary)
            finally:
                eng.close_all()
    finally:
        set_engine_factory(previous)
