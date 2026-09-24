"""Team T5: golden output for EVERY verb, success and failure, over a scripted fake engine.

Each case runs three times, once per output format, and checks:

- ``--json``: stdout is exactly one JSON object; ``ok`` matches the exit code; a
  success has the expected keys; a failure carries ``error.code``/``error.name``;
- ``--tsv``: every row has exactly ``len(TSV_COLUMNS[layout])`` columns; a failure
  prints nothing on stdout;
- human: a success prints something; a failure prints
  ``harness-manager: <message> — <next action>`` on stderr;
- the exit code is the expected ``ExitCode`` in all three.

Every success case has a failure twin with the same verb. No network: the fake
engine never opens a socket, and the TARGET is 127.0.0.1.
"""

from __future__ import annotations

import io
import json
import re
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.main import main
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import (
    AbsentError,
    ExitCode,
    HeldError,
    PortBoundError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import Candidate, Check, Link, LinkKind
from harness_manager.core.pack import PreflightItem
from harness_manager.core.session import LockOwner, SessionLock
from tests.fakes.t5_fake_engine import FakeEngine

T = "127.0.0.1"
BID = f"mps3@{T}"
ERROR_LINE = re.compile(r"^harness-manager: .+ — .+$")


@pytest.fixture
def fake() -> FakeEngine:
    eng = FakeEngine()
    previous = set_engine_factory(lambda _args: eng)
    yield eng
    set_engine_factory(previous)


@pytest.fixture(autouse=True)
def _stdin_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prompt reads EOF (= not confirmed) unless a test gives it an answer."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


# --- setups ---------------------------------------------------------------------------------

Setup = Callable[[FakeEngine, Path], None]


def raises(key: str, exc: Exception) -> Setup:
    def _s(f: FakeEngine, _tmp: Path) -> None:
        f.st.raises[key] = exc
    return _s


def no_adapter(name: str) -> Setup:
    def _s(f: FakeEngine, _tmp: Path) -> None:
        f.st.adapters[name] = None
    return _s


def shell_attr(**kw: object) -> Setup:
    def _s(f: FakeEngine, _tmp: Path) -> None:
        for k, v in kw.items():
            setattr(f.st.adapters["shell"], k, v)
    return _s


def one_candidate(f: FakeEngine, _tmp: Path) -> None:
    f.st.candidates = [Candidate("mps3", BID, (Link(LinkKind.ETHERNET, f"{T}:6900"),),
                                 label="MPS3 greybox on shell 0x3f1a560f",
                                 evidence="answered ping")]


def stale_owner(f: FakeEngine, _tmp: Path) -> None:
    f.st.lock_owners[BID] = LockOwner("someone", socket.gethostname(), 2**22 + 17, 0.0, "gui")


def foreign_owner(f: FakeEngine, _tmp: Path) -> None:
    owner = LockOwner("bob", "another-host.example", 4242, time.time(), "bob's GUI")
    f.st.lock_owners[BID] = owner
    lock = SessionLock(BID)
    lock.lock_dir.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(json.dumps(owner.__dict__))


def bundle(with_ebf: bool = False, zip_ok: bool = True) -> Setup:
    def _s(f: FakeEngine, tmp: Path) -> None:
        root = tmp / "bundle" / "MB" / "HBI0309C"
        root.mkdir(parents=True)
        (tmp / "bundle" / "config.txt").write_text("TITLE: x\n")
        (root / "shell.bit").write_bytes(b"\x00" * 16)
        if with_ebf:
            (root / "mbb_v999.ebf").write_bytes(b"MCC")
        if zip_ok:
            f.st.adapters["storage"].backup(tmp)        # writes tmp/sd-backup.zip
            (tmp / "sd-backup.zip").rename(tmp / "b.zip")
        else:
            (tmp / "b.zip").write_text("not a zip")
        f.st.calls.clear()
    return _s


def frames(*fs: bytes) -> Setup:
    def _s(f: FakeEngine, _tmp: Path) -> None:
        f.st.adapters["shell"].frames = list(fs)
        f.st.adapters["shell"].rx = len(fs)
    return _s


# --- the table --------------------------------------------------------------------------------


@dataclass
class Case:
    id: str
    argv: list[str]
    rc: ExitCode
    keys: set[str] = field(default_factory=set)
    layout: str | None = None
    setup: Setup | None = None


CASES: list[Case] = [
    # system
    Case("version", ["version"], ExitCode.OK, {"version", "engine"}, "version"),
    Case("version-extra-arg", ["version", "extra"], ExitCode.USAGE),
    Case("packs", ["packs"], ExitCode.OK, {"packs"}, "packs"),
    Case("packs-broken", ["packs"], ExitCode.USAGE,
         setup=raises("engine.packs", UsageError("config overrides pack 'x', which is not "
                                                 "installed", hint="installed packs: mps3"))),
    Case("probe", ["probe", "--host", T, "--no-scan"], ExitCode.OK, {"candidates"}, "probe",
         one_candidate),
    Case("probe-none", ["probe", "--host", T, "--no-scan"], ExitCode.ABSENT),
    Case("info", ["info", T], ExitCode.OK,
         {"candidate", "identity", "health", "capabilities", "unavailable"}, "info"),
    Case("info-unreachable", ["info", T], ExitCode.UNREACHABLE,
         setup=raises("session.identity", UnreachableError(
             "shell at 127.0.0.1:6900 refused the connection", hint="is the board powered?"))),
    Case("info-held", ["info", T], ExitCode.HELD,
         setup=raises("engine.open", HeldError(f"{BID} is in use", holder="bob on hub (pid 7)",
                                               hint="held by bob on hub (pid 7)"))),
    Case("info-bad-target", ["info", "127.0.0.1:xx"], ExitCode.USAGE),
    Case("attach", ["attach", T, "--for", "0", "--note", "t5"], ExitCode.OK,
         {"board_id", "state", "holder", "lock"}, "attach"),
    Case("attach-held", ["attach", T, "--for", "0"], ExitCode.HELD,
         setup=raises("engine.open", HeldError(f"{BID} is in use", holder="bob",
                                               hint="held by bob"))),
    Case("detach-stale", ["detach", T], ExitCode.OK, {"board_id", "state", "holder"}, "detach",
         stale_owner),
    Case("detach-none", ["detach", T], ExitCode.ALREADY),
    Case("detach-foreign", ["detach", T], ExitCode.HELD, setup=foreign_owner),
    Case("telemetry", ["telemetry", T], ExitCode.OK, {"board_id", "readings"}, "telemetry"),
    Case("telemetry-unavailable", ["telemetry", T], ExitCode.UNAVAILABLE,
         setup=raises("telemetry.readings",
                      UnavailableError("telemetry", "not installed in this build"))),
    # program
    Case("overlays", ["overlays", T], ExitCode.OK, {"board_id", "compatible", "incompatible"},
         "overlays"),
    Case("overlays-unavailable", ["overlays", T], ExitCode.UNAVAILABLE,
         setup=raises("deploy.compatible", UnavailableError("deploy",
                                                            "not installed in this build"))),
    Case("program", ["program", T, "nanosoc", "--yes"], ExitCode.OK,
         {"board_id", "overlay", "preflight", "result"}, "program"),
    Case("program-by-rm-id", ["program", T, "0x01000001", "--yes"], ExitCode.OK,
         {"board_id", "overlay", "preflight", "result"}, "program"),
    Case("program-mismatch", ["program", T, "led_old", "--yes"], ExitCode.INCOMPATIBLE),
    Case("program-corrupt", ["program", T, "nanosoc", "--yes"], ExitCode.REFUSED,
         setup=lambda f, _t: f.st.preflight.update(nanosoc=[
             PreflightItem("shell_id matches", Check.OK),
             PreflightItem("crc and length", Check.MISMATCH, "partial crc differs")])),
    Case("program-unknown", ["program", T, "nosuch", "--yes"], ExitCode.ABSENT),
    Case("program-unverified", ["program", T, "nanosoc", "--yes"], ExitCode.ACTION_FAILED,
         setup=lambda f, _t: setattr(f.st, "deploy_verified", False)),
    Case("program-unconfirmed", ["program", T, "nanosoc"], ExitCode.REFUSED),
    Case("restore", ["restore", T], ExitCode.OK, {"board_id", "result"}, "restore"),
    Case("restore-no-baseline", ["restore", T], ExitCode.ABSENT,
         setup=raises("deploy.restore_baseline", AbsentError("no baseline overlay is known",
                                                             hint="import greybox"))),
    # consoles
    Case("console", ["console", T, "uart0", "--for", "0.3"], ExitCode.OK,
         {"board_id", "name", "text", "bytes"}, "console"),
    Case("console-unknown", ["console", T, "uart9", "--for", "0"], ExitCode.ABSENT),
    Case("console-export", ["console", T, "uart0", "--export", "0", "--for", "0"], ExitCode.OK,
         {"board_id", "name", "host", "port"}, "console --export"),
    Case("console-export-bound", ["console", T, "uart0", "--export", "4000", "--for", "0"],
         ExitCode.PORT_BOUND,
         setup=raises("consoles.export_tcp", PortBoundError("127.0.0.1:4000 is in use",
                                                            hint="pick another port"))),
    # debug
    Case("debug-status", ["debug", "status", T], ExitCode.OK, {"board_id", "status"},
         "debug up|down|status"),
    Case("debug-up", ["debug", "up", T, "--for", "0"], ExitCode.OK, {"board_id", "status"},
         "debug up|down|status"),
    Case("debug-up-failed", ["debug", "up", T, "--for", "0"], ExitCode.ACTION_FAILED,
         setup=lambda f, _t: setattr(f.st, "debug_state", "failed")),
    Case("debug-up-bound", ["debug", "up", T, "--for", "0"], ExitCode.PORT_BOUND,
         setup=raises("debug.up", PortBoundError("gdb port 29555 is in use", hint="free it"))),
    Case("debug-down", ["debug", "down", T], ExitCode.OK, {"board_id", "status"},
         "debug up|down|status"),
    Case("debug-detect", ["debug", "detect", T], ExitCode.OK, {"board_id", "idcode"},
         "debug detect"),
    Case("debug-detect-nothing", ["debug", "detect", T], ExitCode.NOTHING_ON_TARGET,
         setup=lambda f, _t: setattr(f.st, "idcode", "")),
    # reset and clock
    Case("reset", ["reset", T], ExitCode.OK, {"board_id", "target", "result"}, "reset"),
    Case("reset-unknown", ["reset", T, "rp"], ExitCode.USAGE),
    Case("reset-no-adapter", ["reset", T], ExitCode.UNAVAILABLE, setup=no_adapter("resets")),
    Case("clock-list", ["clock", T], ExitCode.OK, {"board_id", "readings", "set_mhz"}, "clock"),
    Case("clock-set", ["clock", T, "--dut-mhz", "25"], ExitCode.OK, {"board_id", "readings"},
         "clock"),
    Case("clock-preset", ["clock", T, "--preset", "100mhz"], ExitCode.OK,
         {"board_id", "readings"}, "clock"),
    Case("clock-bad-preset", ["clock", T, "--preset", "fast"], ExitCode.USAGE),
    Case("clock-no-adapter", ["clock", T], ExitCode.UNAVAILABLE, setup=no_adapter("clocks")),
    # lab
    Case("lab-link", ["lab", T, "link", "up"], ExitCode.OK, {"board_id", "event", "result"},
         "lab link"),
    Case("lab-link-refused", ["lab", T, "link", "down"], ExitCode.ACTION_FAILED,
         setup=shell_attr(refuse=True)),
    Case("lab-display-query", ["lab", T, "display", "query"], ExitCode.OK,
         {"board_id", "requested", "owner", "landed"}, "lab display"),
    Case("lab-display-flip", ["lab", T, "display", "dut"], ExitCode.OK,
         {"board_id", "requested", "owner", "landed", "polls"}, "lab display"),
    Case("lab-display-inconclusive", ["lab", T, "display", "dut"], ExitCode.ACTION_FAILED,
         setup=shell_attr(lands=False)),
    Case("lab-display-no-kvm", ["lab", T, "display", "query"], ExitCode.UNAVAILABLE,
         setup=shell_attr(kvm=False)),
    Case("lab-macgen", ["lab", T, "macgen", "--inject", "bad_fcs"], ExitCode.OK,
         {"board_id", "tx", "rx", "err"}, "lab macgen"),
    Case("lab-macgen-bad-inject", ["lab", T, "macgen", "--inject", "bogus"], ExitCode.USAGE),
    Case("lab-dutrx", ["lab", T, "dutrx", "--frames", "4"], ExitCode.OK,
         {"board_id", "frames", "rx", "drop_full", "drop_giant", "frames_waiting"}, "lab dutrx",
         frames(b"\x01\x02\x03", b"\xaa\xbb")),
    Case("lab-dutrx-empty", ["lab", T, "dutrx"], ExitCode.OK, {"board_id", "frames"},
         "lab dutrx"),
    Case("lab-dutrx-no-egress", ["lab", T, "dutrx"], ExitCode.UNAVAILABLE,
         setup=shell_attr(egress=False)),
    Case("lab-no-shell", ["lab", T, "link", "up"], ExitCode.UNAVAILABLE,
         setup=no_adapter("shell")),
    # mcc
    Case("mcc-temp", ["mcc", T, "temp"], ExitCode.OK, {"board_id", "readings"}, "mcc temp|osc"),
    Case("mcc-osc", ["mcc", T, "osc"], ExitCode.OK, {"board_id", "readings"}, "mcc temp|osc"),
    Case("mcc-reboot", ["mcc", T, "reboot", "--yes"], ExitCode.OK,
         {"board_id", "result", "phases"}, "mcc reboot"),
    Case("mcc-reboot-unconfirmed", ["mcc", T, "reboot"], ExitCode.REFUSED),
    Case("mcc-cmd", ["mcc", T, "cmd", "CFG", "R", "TEMP", "0"], ExitCode.OK,
         {"board_id", "command", "reply"}, "mcc cmd"),
    Case("mcc-cmd-denied", ["mcc", T, "cmd", "FORMAT"], ExitCode.REFUSED),
    Case("mcc-no-usb", ["mcc", T, "temp"], ExitCode.UNAVAILABLE,
         setup=no_adapter("controller")),
    # sd
    Case("sd-backup", ["sd", T, "backup", "{tmp}/bk"], ExitCode.OK, {"board_id", "backup"},
         "sd backup"),
    Case("sd-backup-no-usb", ["sd", T, "backup", "{tmp}/bk"], ExitCode.UNAVAILABLE,
         setup=no_adapter("storage")),
    Case("sd-install", ["sd", T, "install", "{tmp}/bundle", "--backup", "{tmp}/b.zip", "--yes"],
         ExitCode.OK, {"board_id", "files", "backup"}, "sd install", bundle()),
    Case("sd-install-no-backup", ["sd", T, "install", "{tmp}/bundle", "--backup",
                                  "{tmp}/missing.zip", "--yes"], ExitCode.ABSENT, setup=bundle()),
    Case("sd-install-ebf", ["sd", T, "install", "{tmp}/bundle", "--backup", "{tmp}/b.zip",
                            "--yes"], ExitCode.REFUSED, setup=bundle(with_ebf=True)),
    Case("sd-install-not-zip", ["sd", T, "install", "{tmp}/bundle", "--backup", "{tmp}/b.zip",
                                "--yes"], ExitCode.USAGE, setup=bundle(zip_ok=False)),
    Case("sd-restore", ["sd", T, "restore", "{tmp}/b.zip", "--yes"], ExitCode.OK,
         {"board_id", "backup"}, "sd restore", bundle()),
    Case("sd-restore-unconfirmed", ["sd", T, "restore", "{tmp}/b.zip"], ExitCode.REFUSED,
         setup=bundle()),
    # help
    Case("help-tabs", ["help", "--tabs"], ExitCode.OK, {"tabs"}, "help"),
    Case("help-tab", ["help", "--tabs", "exit codes"], ExitCode.OK, {"tabs"}, "help"),
    Case("help-tab-unknown", ["help", "--tabs", "Nonesuch"], ExitCode.USAGE),
    Case("help-verb", ["help", "program"], ExitCode.OK, {"verb", "text"}, "help"),
    Case("help-verb-unknown", ["help", "frobnicate"], ExitCode.USAGE),
    Case("help-list", ["help", "--list"], ExitCode.OK, {"tabs"}, "help"),
    # usage
    Case("no-verb", [], ExitCode.USAGE),
    Case("unknown-verb", ["frobnicate"], ExitCode.USAGE),
]


def test_every_verb_has_a_success_and_a_failure_case():
    verbs_ok = {c.argv[0] for c in CASES if c.rc == ExitCode.OK and c.argv}
    verbs_fail = {c.argv[0] for c in CASES if c.rc != ExitCode.OK and c.argv}
    expected = {"version", "packs", "probe", "info", "attach", "detach", "overlays", "program", "restore", "console",
                "debug", "reset", "clock", "lab", "mcc", "sd", "telemetry", "help"}
    assert expected <= verbs_ok
    assert expected <= verbs_fail
    # every TSV layout is exercised by some success case
    # daemon/ui run a real harness-manager-daemon process; their TSV is pinned in test_t13_process.py.
    # The update layouts need a signed channel; they are pinned in test_t7_cli.py.
    # pty and baud (L2) need a real PTY and a virtual board: pinned in test_l2_cli.py.
    # lease and share (L1, LR-C) talk to a hub: pinned in test_l1_cli.py and test_lrc_cli.py.
    # xdc (T10) needs the board pack's pin model: pinned in test_t10_cli.py.
    # panel and identify (P1) need a panel adapter: pinned in test_p1_cli.py.
    # kit (KIT-CORE) needs the fixture kit and a state dir: pinned in test_kit_cli.py.
    pinned_elsewhere = {"daemon", "ui", "app", "pty", "baud", "lease", "lease requests",
                        "lease respond", "lease leave", "lease dismiss", "share", "xdc",
                        "xdc info", "panel show", "panel mirror", "identify", "kit",
                        "kit list", "kit guide"} | {
        k for k in TSV_COLUMNS if k.startswith(("update ", "power "))}
    assert {c.layout for c in CASES if c.layout} == set(TSV_COLUMNS) - pinned_elsewhere


@pytest.mark.parametrize("fmt", ["json", "tsv", "human"])
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_golden(case: Case, fmt: str, fake: FakeEngine, tmp_path: Path, capsys):
    if case.setup:
        case.setup(fake, tmp_path)
    argv = [a.replace("{tmp}", str(tmp_path)) for a in case.argv]
    flag = [] if fmt == "human" else [f"--{fmt}"]
    rc, out, err = run(capsys, *flag, *argv)
    assert rc == case.rc, (rc, out, err)
    ok = rc == ExitCode.OK

    if fmt == "json":
        assert out.count("\n") == 1, out            # exactly one object, one line
        obj = json.loads(out)
        assert obj["ok"] is ok
        if ok:
            assert case.keys <= set(obj), set(obj)
        else:
            assert obj["error"]["code"] == int(case.rc)
            assert obj["error"]["name"] == case.rc.name
            assert obj["error"]["message"]
    elif fmt == "tsv":
        if ok:
            lines = out.splitlines()
            assert lines, "a TSV success prints at least one row"
            width = len(TSV_COLUMNS[case.layout])
            assert all(len(line.split("\t")) == width for line in lines), lines
        else:
            assert out == ""
    else:
        if ok:
            assert out.strip()
    if not ok:
        last = err.strip().splitlines()[-1]
        assert ERROR_LINE.match(last), last


# --- specific behaviours -----------------------------------------------------------------------


def test_program_refuses_a_mismatch_before_deploying(fake: FakeEngine, capsys):
    rc, out, err = run(capsys, "--json", "program", T, "led_old", "--yes")
    assert rc == ExitCode.INCOMPATIBLE
    assert "deploy.deploy" not in fake.calls              # the board was never written
    assert "MISMATCH" in err and "shell_id matches" in err  # the preflight table was shown
    data = json.loads(out)["error"]["data"]
    assert [i["check"] for i in data["preflight"]] == ["ok", "mismatch", "ok"]


def test_program_deploys_when_every_check_passes(fake: FakeEngine, capsys):
    # Twin of the refusal: same verb, compatible overlay.
    rc, out, err = run(capsys, "--json", "program", T, "nanosoc", "--yes")
    assert rc == ExitCode.OK and "deploy.deploy" in fake.calls
    assert json.loads(out)["result"]["rm_id"] == "0x01000001"
    assert "deploy: push" in err                            # progress went to stderr...
    assert "push" not in out                                # ...never to stdout


class _Unreadable(io.StringIO):
    def readline(self, *_a: object) -> str:
        raise AssertionError("the prompt read stdin although --yes was given")


def test_program_yes_skips_the_prompt(fake: FakeEngine, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Unreadable())
    rc, _, err = run(capsys, "program", T, "nanosoc", "--yes")
    assert rc == ExitCode.OK and "[y/N]" not in err


def test_program_prompt_yes_deploys(fake: FakeEngine, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    rc, _, err = run(capsys, "program", T, "nanosoc")
    assert rc == ExitCode.OK and "[y/N]" in err and "deploy.deploy" in fake.calls


def test_program_prompt_no_refuses_and_writes_nothing(fake: FakeEngine, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    rc, _, err = run(capsys, "program", T, "nanosoc")
    assert rc == ExitCode.REFUSED and "deploy.deploy" not in fake.calls
    assert "--yes" in err


def test_program_shows_unchecked_as_not_a_pass(fake: FakeEngine, capsys):
    fake.st.preflight["nanosoc"] = [PreflightItem("crc", Check.UNCHECKED, "no header")]
    rc, _, err = run(capsys, "program", T, "nanosoc", "--yes")
    assert rc == ExitCode.OK and "not a pass" in err


def test_unavailable_prints_the_reason_and_exit_12(fake: FakeEngine, capsys):
    fake.st.adapters["controller"] = None
    rc, out, err = run(capsys, "--json", "mcc", T, "reboot", "--yes")
    assert rc == ExitCode.UNAVAILABLE == 12
    assert err.strip() == "harness-manager: reboot_board is unavailable — needs the Debug USB cable"
    assert json.loads(out)["error"]["reason"] == "needs the Debug USB cable"


def test_held_names_the_holder(fake: FakeEngine, capsys):
    fake.st.raises["engine.open"] = HeldError(f"{BID} is in use", holder="bob on hub (pid 7)",
                                              hint="held by bob on hub (pid 7)")
    rc, out, err = run(capsys, "--json", "attach", T, "--for", "0")
    assert rc == ExitCode.HELD and "bob on hub" in err
    assert json.loads(out)["error"]["holder"] == "bob on hub (pid 7)"


def test_held_by_my_own_attach_says_how_to_detach(fake: FakeEngine, capsys):
    import os

    fake.st.raises["engine.open"] = HeldError(f"{BID} is in use", holder="me", hint="held")
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
    fake.st.lock_owners[BID] = LockOwner(user, socket.gethostname(), 1, 0.0, "[cli-hold] attach")
    rc, _, err = run(capsys, "info", T)
    assert rc == ExitCode.HELD and "harness-manager detach 127.0.0.1" in err
    rc, _, err = run(capsys, "attach", T, "--for", "0")
    assert rc == ExitCode.ALREADY and "already attached by you" in err


def test_json_and_tsv_work_after_the_verb(fake: FakeEngine, capsys):
    rc, out, _ = run(capsys, "info", T, "--json")
    assert rc == 0 and json.loads(out)["identity"]["shell_id"] == "0x3f1a560f"
    rc, out, _ = run(capsys, "lab", T, "display", "query", "--tsv")
    assert rc == 0 and out.split("\t")[2] == "harness"


def test_json_with_tsv_is_a_usage_error(fake: FakeEngine, capsys):
    rc, _, err = run(capsys, "--json", "info", T, "--tsv")
    assert rc == ExitCode.USAGE and "cannot be combined" in err


def test_console_streams_raw_bytes_to_stdout(fake: FakeEngine, capsys):
    rc, out, _ = run(capsys, "console", T, "uart0", "--for", "0.3")
    assert rc == 0 and out == "Hello world\r\nTEST PASSED\r\n"
    assert fake.consoles.streams[0].closed


def test_console_tsv_is_one_row_per_line(fake: FakeEngine, capsys):
    rc, out, _ = run(capsys, "--tsv", "console", T, "uart0", "--for", "0.3")
    assert rc == 0 and out.splitlines() == ["uart0\tHello world", "uart0\tTEST PASSED"]


def test_console_json_needs_a_time_limit(fake: FakeEngine, capsys):
    rc, _, _ = run(capsys, "--json", "console", T, "uart0")
    assert rc == ExitCode.USAGE and "consoles.subscribe" not in fake.calls


def test_console_export_prints_the_port_and_closes(fake: FakeEngine, capsys):
    rc, out, _ = run(capsys, "console", T, "uart1", "--export", "0", "--for", "0")
    assert rc == 0 and out.strip() == "41234"
    assert fake.st.exported == [("uart1", 41234)] and "consoles.close_all" in fake.calls


def test_debug_up_holds_then_takes_the_server_down(fake: FakeEngine, capsys):
    rc, out, _ = run(capsys, "--json", "debug", "up", T, "--for", "0")
    status = json.loads(out)["status"]
    assert rc == 0 and status["state"] == "up" and status["gdb_port"] == 29555
    assert fake.calls.index("debug.up") < fake.calls.index("debug.down")


def test_reset_rejects_an_unoffered_target_without_calling_reset(fake: FakeEngine, capsys):
    rc, _, err = run(capsys, "reset", T, "rp")
    assert rc == ExitCode.USAGE and "reset targets on this board: dut" in err
    assert "resets.reset" not in fake.calls
    rc, _, _ = run(capsys, "reset", T, "dut")
    assert rc == 0 and fake.st.adapters["resets"].done == ["dut"]


def test_clock_preset_sets_the_dut_clock(fake: FakeEngine, capsys):
    rc, out, _ = run(capsys, "--json", "clock", T, "--preset", "25MHz")
    assert rc == 0 and json.loads(out)["readings"][0]["value"] == 25.0
    assert fake.st.adapters["clocks"].mhz == 25.0


def test_mcc_denied_command_is_refused_by_the_adapter(fake: FakeEngine, capsys):
    rc, _, err = run(capsys, "mcc", T, "cmd", "FORMAT", "0")
    assert rc == ExitCode.REFUSED and "denied" in err
    assert fake.st.adapters["controller"].lines == []


def test_mcc_reboot_reports_the_witness_phases(fake: FakeEngine, capsys):
    rc, out, err = run(capsys, "--json", "mcc", T, "reboot", "--yes")
    assert rc == 0 and json.loads(out)["phases"] == ["sent", "down", "up"]
    assert "reboot: down" in err


def test_sd_install_refuses_ebf_before_touching_storage(fake: FakeEngine, tmp_path, capsys):
    bundle(with_ebf=True)(fake, tmp_path)
    rc, _, err = run(capsys, "sd", T, "install", str(tmp_path / "bundle"), "--backup",
                     str(tmp_path / "b.zip"), "--yes")
    assert rc == ExitCode.REFUSED and ".ebf" in err
    assert "storage.install" not in fake.calls


def test_sd_install_passes_the_bundle_and_the_backup(fake: FakeEngine, tmp_path, capsys):
    bundle()(fake, tmp_path)
    rc, out, _ = run(capsys, "--json", "sd", T, "install", str(tmp_path / "bundle"),
                     "--backup", str(tmp_path / "b.zip"), "--yes")
    assert rc == 0
    assert sorted(fake.st.adapters["storage"].installed) == ["MB/HBI0309C/shell.bit",
                                                             "config.txt"]
    assert json.loads(out)["backup"]["files"] == 2


def test_lab_dutrx_reads_every_waiting_frame(fake: FakeEngine, capsys):
    frames(b"\x01\x02", b"\x03")(fake, Path("."))
    rc, out, _ = run(capsys, "--json", "lab", T, "dutrx", "--frames", "5")
    obj = json.loads(out)
    assert rc == 0 and [f["data"] for f in obj["frames"]] == ["0102", "03"]


def test_lab_display_inconclusive_is_not_reported_as_done(fake: FakeEngine, capsys):
    shell_attr(lands=False)(fake, Path("."))
    rc, out, err = run(capsys, "--json", "lab", T, "display", "dut")
    assert rc == ExitCode.ACTION_FAILED and "INCONCLUSIVE" in err
    assert json.loads(out)["error"]["data"]["landed"] is False


def test_every_verb_closes_what_it_opened(fake: FakeEngine, capsys):
    run(capsys, "info", T)
    assert fake.calls.count("engine.open") == fake.calls.count("engine.close") == 1
    assert fake.closed_all


def test_internal_bug_is_exit_1_and_says_so(fake: FakeEngine, capsys):
    fake.st.raises["session.identity"] = RuntimeError("boom")   # type: ignore[assignment]
    rc, _, err = run(capsys, "info", T)
    assert rc == ExitCode.FAILED and "internal error" in err and "bug" in err
