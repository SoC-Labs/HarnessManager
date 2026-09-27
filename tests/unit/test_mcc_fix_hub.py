"""MCC-FIX phase 2: the MCC of a hub board runs ON the hub, never through a share on tty_00.
Each check has a negative twin.

- no code path starts (or uses) a tty_00 share: a spy on every share start;
- a hub-mode REBOOT runs pyverify's writer on the hub (the fake ssh runner, and pyverify's own
  ssh quoting), with a Python 3.10+ and never the hub's 3.6 ``python3``;
- the post-SD-write quirk (a bare CR answered with only CR/LF) is retried, a real refusal is not,
  and the retry is bounded; locally too;
- the hub door's REBOOT is pyverify's ``sd field --already-written`` on the hub;
- SLOT-TIMING's reset guard (the real module, reading a held card job) refuses every REBOOT
  and harness restart mid-job, and a check nested in ``guarded`` neither refuses twice nor
  blocks ``--force``;
- the Linux-only restart is harnessd's ``reboot`` verb;
- the hub-side reader runs under a real Python 3.6 (skipped only when none is installed).

Nothing reaches a hub, a board or a network beyond 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyverify.bootrate import HUB_MCC_REBOOT_PY
from pyverify.testing.fakeshell import FakeShell

from harness_manager.core.errors import (
    HarnessError,
    HeldError,
    NothingOnTargetError,
    RefusedError,
    UnavailableError,
)
from harness_manager.core.model import Candidate, Link, LinkKind
from harness_manager.services import reset_guard
from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3 import hub_mcc
from harness_manager_mps3 import mcc as mccmod
from harness_manager_mps3.hub_mcc import HUB_MCC_READ_PY, PY310_PROBE, HubMccController
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from harness_manager_mps3.mcc import POST_WRITE_GAP_S, POST_WRITE_TRIES, Mps3Controller
from harness_manager_mps3.shell import Mps3Shell, ShellResets
from tests.fakes.fake_mcc import FakeMcc, SilentPort
from tests.fakes.hub_mcc_fakes import MCC_TTY, HubTool, PtyMcc
from tests.fakes.l1_fake_hub import FakeHub, FakeLane
from tests.fakes.lxslots_board import LINUX_SID, board_session, slot_board
from tests.fakes.t3_clock import FakeClock, RecordingPort

HUB = "mapstone-dev.ecs.soton.ac.uk"
TARGET = "mps3_01_pl"
LANE2 = "/dev/mps3_01_pl/tty_02"


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, s: float) -> None:
        self.calls.append(s)


def hub_controller(tool: HubTool, **kw) -> HubMccController:
    sleep = kw.pop("sleep", Sleeps())
    return HubMccController(tool, target=TARGET, tty=MCC_TTY, host=HUB, sleep=sleep, **kw)


# --- 1. no code path starts a tty_00 share ---------------------------------------------------------


@pytest.fixture
def share_spy(monkeypatch):
    """Every share start, whoever asks: the SSH client, the REST client, and the hub itself."""
    from harness_manager.transports import hub_rest

    seen: list[str] = []
    for cls in (hubmod.HubClient, hub_rest.RestHubClient):
        real = cls.share_start

        def spy(self, tty, baud=115200, _real=real):
            out = _real(self, tty, baud)
            seen.append(tty)                                     # only a start that happened
            return out

        monkeypatch.setattr(cls, "share_start", spy)
    return seen


def test_no_code_path_starts_a_tty_00_share(share_spy, monkeypatch):
    fake = FakeHub(TARGET)
    fake.add_tty(MCC_TTY, FakeMcc())
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", lambda host, group, **_k: fake)
    cfg = hubmod.HubConfig(HUB, TARGET, shares={"mcc": MCC_TTY}, start_shares=True)
    hubmod.SHARES.configure(cfg)
    ref = hubmod.ShareRef(HUB, TARGET, MCC_TTY)
    try:
        assert hubmod.share_links(cfg) == []                     # no hub:// link for the MCC
        with pytest.raises(RefusedError):
            hubmod.resolve_share(ref)                            # the opener, start_shares on
        with pytest.raises(RefusedError):
            hubmod.open_hub_share(ref.url.split("://", 1)[1])
        with pytest.raises(RefusedError):
            hubmod.HubClient(HUB, TARGET, runner=fake).share_start(MCC_TTY)
        with pytest.raises(RefusedError):
            hubmod.Mps3Hub(cfg, client=hubmod.HubClient(HUB, TARGET, runner=fake)).refuse_share(
                "mcc", MCC_TTY)
    finally:
        hubmod.SHARES.close_all()
    assert MCC_TTY not in fake.shares
    assert not any(c[:3] == ["fpgahub", "share", "start"] for c in fake.calls)
    assert all(t != MCC_TTY for t in share_spy)


def test_twin_a_lane_share_still_starts(share_spy, monkeypatch):
    fake = FakeHub(TARGET)
    fake.add_tty(LANE2, FakeLane())
    info = hubmod.HubClient(HUB, TARGET, runner=fake).share_start(LANE2)
    assert info.tty == LANE2 and share_spy == [LANE2] and LANE2 in fake.shares
    fake.close()


def test_a_hub_board_has_the_mcc_as_a_hub_link_and_no_serial_link():
    cfg = hubmod.HubConfig(HUB, TARGET, shares={"mcc": MCC_TTY})
    link = hubmod.mcc_link(cfg)
    assert link.kind == LinkKind.HUB and link.via == "hub" and link.address == (
        f"hub-mcc://{HUB}/{TARGET}{MCC_TTY}")
    # twin: a REST-only hub (no SSH login) cannot reach tty_00: no MCC link at all
    from harness_manager.transports.hub_rest import parse_rest_table

    rest = parse_rest_table({"url": "https://hub.invalid:7246"}, where="hub")
    assert hubmod.mcc_link(hubmod.HubConfig("hub.invalid", TARGET, rest=rest)) is None


# --- 2. a hub-mode REBOOT is pyverify's writer, run on the hub ---------------------------------------


def test_a_hub_reboot_runs_pyverifys_writer_on_the_hub_with_python_310():
    mcc = FakeMcc()
    tool = HubTool(mcc=mcc)
    ctl = hub_controller(tool)
    out = ctl.reboot(wait_s=180)
    assert mcc.reboots == 1 and ctl.attempts == 1
    (run,) = tool.writer_runs
    assert run["python"] == "/usr/bin/python3.11" and run["mode"] == "reboot"
    assert run["tty"] == MCC_TTY and run["pace"] == 0.1 and run["capture_s"] == 150.0
    assert run["log"].startswith("/tmp/harness-manager-mcc-")
    assert out["route"] == "pyverify paced REBOOT" and out["via"] == "hub"
    assert out["fpga_file"] == "MB/HBI0309C/Nanosoc/nanosoc.bit" and out["fpga_configured"]
    assert run["log"] not in tool.files                           # read back, then removed
    assert not any(c[:1] == ["python3"] for c in tool.calls)      # never the hub's bare python3
    assert tool.share_starts() == []


def test_twin_a_hub_with_no_python_310_is_refused_and_nothing_is_typed():
    mcc = FakeMcc()
    tool = HubTool(mcc=mcc, hub_python="")
    with pytest.raises(UnavailableError, match="no Python 3.10\\+"):
        hub_controller(tool).reboot(wait_s=180)
    assert tool.writer_runs == [] and mcc.reboots == 0


def test_the_hub_argv_survives_pyverifys_own_ssh_quoting():
    """What goes to ssh: ``sg fpga -c '<env> <argv>'``, two shells deep. Both scripts arrive
    byte for byte, so the hub runs exactly the reader/writer that was tested here."""
    from pyverify.bootrate import MccTtyResetter
    from pyverify.lease import SshHubRunner

    runner = SshHubRunner(HUB, group="fpga")
    writer = MccTtyResetter(lambda *a, **k: None, tty_path=MCC_TTY, pace_s=0.1,
                            python="/usr/bin/python3.11").argv("reboot")
    reader = ["sh", "-c", hub_mcc._py_pick(), HUB_MCC_READ_PY, json.dumps({"tty": MCC_TTY})]
    for argv in (writer, reader, ["sh", "-c", PY310_PROBE]):
        cmd = runner.build(argv)
        assert cmd[0] == "ssh" and cmd[-2] == HUB
        outer = shlex.split(cmd[-1])
        assert outer[:3] == ["sg", "fpga", "-c"]
        inner = shlex.split(outer[3])
        while inner and "=" in inner[0] and not inner[0].startswith(("/", "-")):
            inner.pop(0)                                          # the render env
        assert inner == argv
    assert HUB_MCC_REBOOT_PY in writer


@pytest.mark.skipif(os.name != "posix" or not Path("/bin/sh").exists(),
                    reason="the probe is an sh script run by /bin/sh, with symlinked pythons "
                           "(the hub's side; REVIEW-W5 16)")
def test_the_python_probe_takes_310_and_never_the_bare_python3(tmp_path):
    assert "python3" not in hub_mcc.PY310_CANDIDATES              # never the bare one
    assert hub_mcc.PY310_CANDIDATES[0] == "python3.11"            # the hub's, first
    assert "/opt/fpgahub/bin/python3.11" in hub_mcc.PY310_CANDIDATES
    names = [c for c in hub_mcc.PY310_CANDIDATES if "/" not in c]
    probe = hub_mcc.py310_probe(names)       # this box may have fpgahub's own 3.11 in /opt
    py36 = shutil.which("python3.6") or "/usr/libexec/platform-python"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "python3.11").symlink_to(sys.executable)
    got = subprocess.run(["/bin/sh", "-c", probe], env={"PATH": str(fresh)},
                         capture_output=True, text=True, check=False)
    assert got.returncode == 0 and got.stdout.strip() == str(fresh / "python3.11")
    # twin: only a 3.6 behind the 3.11 name, and a bare python3 that IS new: refused (127)
    old = tmp_path / "old"
    old.mkdir()
    if Path(py36).exists():
        (old / "python3.11").symlink_to(py36)
    (old / "python3").symlink_to(sys.executable)
    got = subprocess.run(["/bin/sh", "-c", probe], env={"PATH": str(old)},
                         capture_output=True, text=True, check=False)
    assert got.returncode == 127 and got.stdout.strip() == ""


def test_hub_reads_refuse_state_changes_before_the_hub_is_asked():
    tool = HubTool(mcc=FakeMcc())
    ctl = hub_controller(tool)
    for line in ("CFG W OSC 0 30 ", "DEBUG", "EXIT"):
        with pytest.raises(RefusedError):
            ctl.command(line.strip(), arm=True)
    with pytest.raises(RefusedError):
        ctl.command("FORMAT")
    assert tool.calls == []


# --- 3. the post-SD-write quirk: retried while bare CR/LF, never otherwise -------------------------


def test_the_post_write_bare_crlf_is_retried_and_succeeds_on_attempt_2():
    mcc = FakeMcc()
    mcc.bare_crlf_crs = 1                       # straight after an SD write
    tool, sleeps = HubTool(mcc=mcc), Sleeps()
    ctl = hub_controller(tool, sleep=sleeps)
    out = ctl.reboot(wait_s=180)
    assert ctl.attempts == 2 and mcc.reboots == 1 and out["attempts"] == 2
    assert sleeps.calls == [POST_WRITE_GAP_S] and "post-SD-write" in out["notes"][0]
    assert [r["mode"] for r in tool.writer_runs] == ["reboot", "reboot"]


def test_twin_a_real_refusal_is_not_retried():
    mcc = FakeMcc()
    mcc.menu = "debug"                          # Debug> is a refusal, not the quirk
    tool, sleeps = HubTool(mcc=mcc), Sleeps()
    with pytest.raises(NothingOnTargetError, match="Nothing was sent"):
        hub_controller(tool, sleep=sleeps).reboot(wait_s=180)
    assert len(tool.writer_runs) == 1 and sleeps.calls == [] and mcc.reboots == 0


def test_twin_the_quirk_is_not_retried_forever():
    mcc = FakeMcc()
    mcc.bare_crlf_crs = 99
    tool, sleeps = HubTool(mcc=mcc), Sleeps()
    ctl = hub_controller(tool, sleep=sleeps)
    with pytest.raises(NothingOnTargetError):
        ctl.reboot(wait_s=180)
    assert len(tool.writer_runs) == POST_WRITE_TRIES and ctl.attempts == POST_WRITE_TRIES
    assert sleeps.calls == [POST_WRITE_GAP_S] * (POST_WRITE_TRIES - 1) and mcc.reboots == 0


def test_twin_a_second_reader_is_refused_at_once():
    mcc = FakeMcc()
    tool = HubTool(mcc=mcc)
    tool.others = [[4242, f"cat {MCC_TTY}"]]
    with pytest.raises(HeldError, match="pid 4242"):
        hub_controller(tool).reboot(wait_s=180)
    assert len(tool.writer_runs) == 1 and mcc.reboots == 0


def local(mcc: FakeMcc | SilentPort, clock: FakeClock) -> Mps3Controller:
    port = RecordingPort(mcc, clock)
    return Mps3Controller("fake://mcc-fix-local", clock=clock, sleep=clock.sleep,
                          opener=lambda url, baud: port)


def test_local_usb_reboot_retries_the_post_write_quirk_too():
    clock = FakeClock()
    mcc = FakeMcc(clock=clock, down_s=1.0, boot_s=25.0, autoboot_window_s=3.0)
    mcc.bare_crlf_crs = 1
    ctl = local(mcc, clock)
    out = ctl.reboot(wait_s=120)
    assert mcc.reboots == 1 and out["fpga_file"] == "MB/HBI0309C/Nanosoc/nanosoc.bit"
    assert mcc.accepted_lines[-1] == "REBOOT" and not mcc.autoboot_aborted


def test_twin_local_silence_is_not_the_quirk_and_is_not_retried():
    clock = FakeClock()
    silent = SilentPort()
    with pytest.raises(NothingOnTargetError, match="interface 00"):
        local(silent, clock).reboot(wait_s=10)
    assert bytes(silent.writes) == b"\r"                  # one sync CR; REBOOT never typed


def test_twin_local_quirk_gives_up_after_the_tries():
    clock = FakeClock()
    mcc = FakeMcc(clock=clock)
    mcc.bare_crlf_crs = 99
    with pytest.raises(NothingOnTargetError):
        local(mcc, clock).reboot(wait_s=10)
    assert mcc.reboots == 0 and mcc.bare_crlf_crs == 99 - POST_WRITE_TRIES


# --- 4. a local USB reboot is unchanged ------------------------------------------------------------


def test_a_local_debug_usb_link_keeps_the_local_controller(monkeypatch):
    called = []
    monkeypatch.setattr(hub_mcc, "make_hub_controller", lambda s: called.append(s))
    cand = Candidate(pack="mps3", board_id="mps3@usb",
                     links=(Link(LinkKind.USB_SERIAL, "/dev/ttyUSB0", "FT4232H if00 (MCC)"),))
    ctl = mccmod.make_controller_adapter(SimpleNamespace(candidate=cand, shell=None))
    assert isinstance(ctl, Mps3Controller) and ctl.url == "serial:///dev/ttyUSB0"
    assert ctl.timing.pace_s == mccmod.DEFAULT_TIMING.pace_s and called == []


def test_twin_a_legacy_hub_share_link_is_never_the_mcc(monkeypatch):
    called = []
    monkeypatch.setattr(hub_mcc, "make_hub_controller", lambda s: called.append(s) or "hub")
    cand = Candidate(pack="mps3", board_id="mps3@hub",
                     links=(Link(LinkKind.USB_SERIAL, f"hub://{HUB}/{TARGET}{MCC_TTY}",
                                 "MCC console over the hub share", via="hub"),))
    assert mccmod.make_controller_adapter(SimpleNamespace(candidate=cand, shell=None)) == "hub"
    assert len(called) == 1


# --- 5. the hub door's REBOOT is `sd field --already-written` on the hub ------------------------------


def test_the_hub_doors_reboot_is_sd_field_already_written(tmp_path):
    from harness_manager_mps3.hub_sd import HubSdDoor, SshSdBackend
    from tests.fakes.hubsd_fakes import MCC_TTY as DOOR_TTY
    from tests.fakes.hubsd_fakes import FakeBoard, FakeHubHandle, FakeSdHub, HubSession, ssh_client

    clock = FakeClock(1_758_000_000.0)
    hub = FakeSdHub(clock, sd_bit=b"old")
    handle = FakeHubHandle(ssh_client(hub))
    door = HubSdDoor(handle, backend=SshSdBackend(handle.client, uploader=hub.upload, clock=clock),
                     clock=clock, sleep=clock.sleep)
    session = HubSession(hub, FakeBoard(loaded=b"old"), door, handle)
    from tests.fakes.hubsd_fakes import sha

    bit = tmp_path / "new.bit"
    bit.write_bytes(b"new image")
    ref = door._backend().stage(bit, sha(b"new image"), lambda *a: None)
    # the hub's own record of the write (what the door proved before handing over)
    hub.sd_bit = b"new image"
    hub.journal.append((clock() - 5, f"program dispatched: board={TARGET} method=sd plugin="
                        f"sd_install ok=True part=x sha256={sha(b'new image')[:12]} dur=68.0s"))
    door.written = {"ref": ref, "sha256": sha(b"new image"), "source": "journal"}
    out = session.controller.reboot(wait_s=180)
    assert out["route"] == "pyverify sd field --already-written"
    assert session.board.loaded == b"new image" and hub.mcc_reboots == 1
    assert ["sha256sum", ref] in hub.calls                        # the witness's key
    assert any(c[:2] == ["journalctl", "-u"] and "-n" in c for c in hub.calls)   # its probe
    assert [r["mode"] for r in hub.mcc_runs] == ["scan", "reboot"]
    assert all(r["python"] == "/usr/bin/python3.11" and r["tty"] == DOOR_TTY for r in hub.mcc_runs)
    assert door.take_written() is None                            # used once


def test_twin_without_a_door_write_the_reboot_is_the_plain_paced_reboot():
    from tests.fakes.hubsd_fakes import FakeBoard, FakeHubHandle, FakeSdHub, HubSession, ssh_client

    clock = FakeClock(1_758_000_000.0)
    hub = FakeSdHub(clock, sd_bit=b"img")
    handle = FakeHubHandle(ssh_client(hub))
    session = HubSession(hub, FakeBoard(loaded=b"img"), SimpleNamespace(), handle)
    out = session.controller.reboot(wait_s=180)
    assert out["route"] == "pyverify paced REBOOT" and hub.mcc_reboots == 1
    assert not any(c[:2] == ["journalctl", "-u"] for c in hub.calls)
    assert [r["mode"] for r in hub.mcc_runs] == ["reboot"]


# --- 6. SLOT-TIMING's reset guard before every reboot: the REAL module --------------------------------
#
# ``harness_manager.services.reset_guard`` exists now (INTEG-W4 merged SLOT-TIMING first), so
# ``mcc.guard_reset`` imports it outright: no stand-in. The card job is a Linux SlotBoard's
# (``hold_job``), read through the real MPS3 session's ``Mps3OsSlots.busy_job``.

A_RECORDED = {"state": "valid", "hdr_crc": 0x3E5E9C2C, "len": 24354312, "sid": LINUX_SID}
MB29 = 29_000_000


@pytest.fixture
def card_board(monkeypatch):
    """A Linux board whose card job a test holds, and the real MPS3 session on it."""
    fake = slot_board(slots={"a": A_RECORDED})
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    session = board_session(fake)
    try:
        yield fake, session
    finally:
        session.close()
        fake.stop()


def local_mcc() -> tuple[FakeMcc, FakeClock]:
    clock = FakeClock()
    return FakeMcc(clock=clock, down_s=1.0, boot_s=25.0, autoboot_window_s=3.0), clock


def test_guard_reset_is_the_real_module_not_an_optional_import():
    assert mccmod.reset_guard is reset_guard
    assert not hasattr(mccmod, "RESET_GUARD_MODULE")


def test_the_real_guard_refuses_a_local_reboot_mid_card_job(card_board):
    fake, session = card_board
    mcc, clock = local_mcc()
    ctl = local(mcc, clock)
    ctl.session = session
    fake.hold_job("writing")
    with pytest.raises(reset_guard.CardBusyError, match="slot B is being written") as exc:
        ctl.reboot(wait_s=120)
    assert exc.value.message.startswith("MCC REBOOT refused") and exc.value.code == 4
    with pytest.raises(reset_guard.CardBusyError):
        ctl.command("REBOOT")                                    # `mcc cmd REBOOT`: the same
    assert mcc.accepted_lines == [] and mcc.reboots == 0
    fake.end_job()                                               # twin: the job ended
    ctl.reboot(wait_s=120)
    assert mcc.reboots == 1


def test_the_real_guard_refuses_a_hub_reboot_mid_card_job(card_board):
    fake, session = card_board
    mcc = FakeMcc()
    tool = HubTool(mcc=mcc)
    fake.hold_job("verifying", got=MB29, length=MB29)
    with pytest.raises(reset_guard.CardBusyError, match="slot B is being read back"):
        hub_controller(tool, session=session).reboot(wait_s=180)
    with pytest.raises(reset_guard.CardBusyError):
        hub_controller(tool, session=session).command("REBOOT")
    assert tool.calls == [] and mcc.reboots == 0
    fake.end_job()                                               # twin: idle card, it reboots
    hub_controller(tool, session=session).reboot(wait_s=180)
    assert mcc.reboots == 1


def test_twin_a_board_with_no_card_job_to_protect_reboots_freely():
    # bare metal (no OS slots): the real guard has nothing to read, so the reboot goes ahead
    mcc = FakeMcc()
    hub_controller(HubTool(mcc=mcc), session=SimpleNamespace(candidate=None)).reboot(wait_s=180)
    assert mcc.reboots == 1


def test_a_check_nested_in_guarded_does_not_refuse_twice_and_force_reaches_through(card_board):
    fake, session = card_board
    bid = session.candidate.board_id
    mcc, clock = local_mcc()
    ctl = local(mcc, clock)
    ctl.session = session
    reads: list[int] = []
    probe = session.os_slots.busy_job

    def counted():
        reads.append(1)
        return probe()

    session.os_slots.busy_job = counted
    # the CLI/daemon layer checked (idle) and holds the scope; a job that starts after that
    # is not read again by the pack's own check: one read, one reboot
    with reset_guard.guarded(session, reset_guard.ACTION_MCC_REBOOT):
        fake.hold_job("writing")
        ctl.reboot(wait_s=120)
    assert mcc.reboots == 1 and len(reads) == 1 and reset_guard.scope_of(bid) is None
    # --force with the typed phrase (the CLI's --force/--consent): the outer layer reads the
    # job and lets it go; the inner check passes, so the forced reboot happens
    with reset_guard.guarded(session, reset_guard.ACTION_MCC_REBOOT, force=True,
                             consent=f"RESET {bid}") as job:
        assert job is not None and job.state == "writing"
        ctl.reboot(wait_s=120)
    assert mcc.reboots == 2 and len(reads) == 2
    # twin: another thread is not inside this thread's scope: its check reads and refuses
    seen: list[BaseException] = []

    def other() -> None:
        try:
            mccmod.guard_reset(session, "ACTION_MCC_REBOOT")
        except reset_guard.CardBusyError as exc:
            seen.append(exc)

    with reset_guard.guarded(session, reset_guard.ACTION_MCC_REBOOT, force=True,
                             consent=f"RESET {bid}"):
        t = threading.Thread(target=other)
        t.start()
        t.join(10)
    assert len(seen) == 1
    # twin: --force without the phrase is refused by the outer layer; the MCC is untouched
    with pytest.raises(RefusedError, match="typed phrase"):
        with reset_guard.guarded(session, reset_guard.ACTION_MCC_REBOOT, force=True,
                                 consent="RESET"):
            ctl.reboot(wait_s=120)
    # twin: outside any guarded block the pack's own check reads the job and refuses
    with pytest.raises(reset_guard.CardBusyError):
        ctl.reboot(wait_s=120)
    assert mcc.reboots == 2


# --- 7. the Linux-only restart is harnessd's `reboot` verb ------------------------------------------


def test_restart_the_shell_on_linux_is_the_reboot_verb_without_an_fpga_reload(card_board):
    fake, session = card_board
    resets = ShellResets(Mps3Shell(fake.host, fake.control_port), session)
    assert "shell" in resets.reset_targets()
    resets.reset("shell")
    assert len(fake.reboots) == 1                                   # harnessd restarts itself
    deadline = time.monotonic() + 10
    while not fake.boots and time.monotonic() < deadline:          # wait for it to come back
        time.sleep(0.02)
    assert fake.boots == ["A"]
    fake.hold_job("writing")                                        # twin: mid card job, refused
    with pytest.raises(reset_guard.CardBusyError, match="harness reboot refused"):
        resets.reset("shell")
    assert len(fake.reboots) == 1 and not fake.wedged


def test_twin_bare_metal_has_no_reboot_verb_restart():
    fs = FakeShell.ephemeral(static_id=0x3F1A560F)
    fs.start()
    try:
        resets = ShellResets(Mps3Shell(fs.host, fs.control_port))
        assert "shell" not in resets.reset_targets()
        with pytest.raises(HarnessError):
            resets.reset("shell")
        assert fs.reboots == []
    finally:
        fs.stop()


# --- 8. the hub-side reader under a real Python 3.6 ---------------------------------------------------


def _python36() -> str | None:
    for cand in ("/usr/bin/python3.6", "/usr/libexec/platform-python", shutil.which("python3.6")):
        if cand and os.path.exists(cand):
            v = subprocess.run([cand, "-c", "import sys; print(sys.version_info[:2])"],
                               capture_output=True, text=True, check=False).stdout.strip()
            if v == "(3, 6)":
                return cand
    return None


PY36 = _python36()


@pytest.mark.skipif(PY36 is None, reason="no Python 3.6 on this machine (the lab hub's python3)")
def test_the_hub_reader_compiles_and_runs_under_python_36():
    got = subprocess.run([PY36, "-c", "import sys; compile(sys.stdin.read(), 'reader', 'exec')"],
                         input=HUB_MCC_READ_PY, capture_output=True, text=True, check=False)
    assert got.returncode == 0, got.stderr
    mcc = FakeMcc()
    with PtyMcc(mcc) as pty:
        tool = HubTool(pty=pty, python=PY36)
        ctl = hub_controller(tool)
        (temp,) = ctl.temperatures()
        assert temp.available and temp.value == 35.5, temp.reason
        oscs = ctl.oscillators()
        assert [o.value for o in oscs] == [25.0, 50.0, 50.0, 50.0, 24.576, 23.75]
    assert mcc.menu == "main" and mcc.dropped_chars == 0 and mcc.reboots == 0
    assert ctl.last_info["rc"] == 0 and ctl.last_info["menu"] == "main"


@pytest.mark.skipif(PY36 is None, reason="no Python 3.6 on this machine (the lab hub's python3)")
def test_twin_under_python_36_a_talking_mcc_is_not_typed_into():
    mcc = FakeMcc(down_s=0.0, boot_s=3.0)
    with PtyMcc(mcc) as pty:
        mcc._start_boot()                           # the banner keeps coming, as in a boot
        tool = HubTool(pty=pty, python=PY36)
        (temp,) = hub_controller(tool).temperatures()
        assert not temp.available and "talking" in temp.reason
    assert mcc.accepted_lines == []
