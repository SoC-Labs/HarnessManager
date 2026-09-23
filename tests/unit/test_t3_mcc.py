"""T3: the MCC driver against FakeMcc's traps. Every check has a negative twin.

Board-free: FakeMcc plays the MCC, a FakeClock makes pacing and boot timing
deterministic, and the Ethernet witness uses pyverify's FakeShell on 127.0.0.1.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyverify.testing.fakeshell import FakeShell

from socharness.core.errors import (
    ActionFailedError,
    ExitCode,
    NothingOnTargetError,
    RefusedError,
    UnreachableError,
)
from socharness.core.model import Candidate, Link, LinkKind
from socharness.core.pack import ControllerAdapter
from socharness_board_mps3 import mcc as mccmod
from socharness_board_mps3.mcc import DENIED, MccTiming, Mps3Controller, classify
from tests.fakes.fake_mcc import BOOT_BANNER, FakeMcc, SilentPort
from tests.fakes.t3_clock import FakeClock, RecordingPort

MCC_PACE_S = 0.05     # the MCC's real threshold (fpgahub 30ae4f3)


def make(mcc: FakeMcc | None = None, clock: FakeClock | None = None, **kw):
    clock = clock or FakeClock()
    mcc = mcc if mcc is not None else FakeMcc(clock=clock)
    port = RecordingPort(mcc, clock)
    ctl = Mps3Controller("fake://t3-mcc", clock=clock, sleep=clock.sleep,
                         opener=lambda url, baud: port, **kw)
    return ctl, mcc, port, clock


def raw_paced(mcc: FakeMcc, clock: FakeClock, text: str, gap: float = 0.06) -> None:
    for ch in text:
        clock.advance(gap)
        mcc.write(ch.encode())


# --- the adapter shape --------------------------------------------------------------


def test_controller_satisfies_the_protocol():
    ctl, *_ = make()
    assert isinstance(ctl, ControllerAdapter)


# --- trap 1: burst drop ---------------------------------------------------------------


def test_raw_burst_loses_the_command():
    clock = FakeClock()
    mcc = FakeMcc(clock=clock)
    mcc.write(b"HELP\r")                        # one burst: only the H lands
    assert mcc.accepted_lines == [] and mcc.dropped_chars == 4


def test_driver_pacing_lands_every_character():
    ctl, mcc, port, _ = make()
    reply = ctl.command("HELP")
    assert "REBOOT" in reply and mcc.accepted_lines == ["HELP"]
    assert mcc.dropped_chars == 0
    assert port.gaps() and min(port.gaps()) >= MCC_PACE_S


def test_under_paced_driver_loses_the_command():
    # Negative twin: the same driver with a 20 ms pace is the old burst bug.
    ctl, mcc, _, _ = make(timing=MccTiming(pace_s=0.02))
    with pytest.raises(UnreachableError):
        ctl.command("HELP")
    assert mcc.dropped_chars > 0 and "HELP" not in mcc.accepted_lines


# --- trap 2: CFG R only in the DEBUG menu ---------------------------------------------


def test_raw_cfg_read_outside_debug_is_a_command_error():
    clock = FakeClock()
    mcc = FakeMcc(clock=clock)
    raw_paced(mcc, clock, "CFG R TEMP 0\r")
    assert b"Command error" in mcc.read(4096)


def test_driver_enters_debug_for_cfg_and_leaves_with_exit():
    ctl, mcc, _, _ = make()
    (temp,) = ctl.temperatures()
    assert temp.available and temp.value == 35.5
    assert (temp.name, temp.unit, temp.source) == ("mcc_temp", "degC", "mcc-console")
    assert "unverified" in temp.reason and "IOFPGA_TMP" in temp.reason
    assert mcc.accepted_lines == ["DEBUG", "CFG R TEMP 0", "EXIT"]
    assert mcc.menu == "main"                   # the next REBOOT lands in the main menu


def test_menu_is_tracked_from_the_prompt():
    ctl, mcc, _, _ = make()
    assert ctl.command("DEBUG") == "" and mcc.menu == "debug"
    ctl.command("HELP")                         # HELP is a main-menu command: EXIT first
    assert mcc.accepted_lines == ["DEBUG", "EXIT", "HELP"] and mcc.menu == "main"
    ctl.command("DEBUG")
    (temp,) = ctl.temperatures()                # already in DEBUG: no second DEBUG
    assert temp.available and mcc.accepted_lines[-2:] == ["CFG R TEMP 0", "EXIT"]


def test_oscillators_are_setpoints_one_debug_visit():
    ctl, mcc, _, _ = make()
    oscs = ctl.oscillators()
    assert [r.name for r in oscs] == [f"osc{n}" for n in range(6)]
    assert [r.value for r in oscs] == [25.0, 50.0, 50.0, 50.0, 24.576, 23.75]
    assert all(r.source == "mcc-console setpoint" and r.unit == "MHz" for r in oscs)
    assert mcc.accepted_lines.count("DEBUG") == 1 and mcc.menu == "main"


def test_missing_oscillator_is_unavailable_not_zero():
    # Negative twin: an OSC the MCC does not answer is unavailable, never 0.
    clock = FakeClock()
    ctl, *_ = make(FakeMcc(clock=clock, osc_mhz={0: 25.0}), clock)
    oscs = ctl.oscillators()
    assert oscs[0].available and not any(r.available for r in oscs[1:])
    assert all(r.value is None and "Unable to perform" in r.reason for r in oscs[1:])


# --- trap 3: CFG R V is refused by the firmware ----------------------------------------


def test_voltage_read_is_an_unavailable_reading_not_a_crash():
    ctl, mcc, _, _ = make()
    readings = ctl.voltages([0, 1])
    assert [r.available for r in readings] == [False, False]
    assert all("Unable to perform requested function" in r.reason for r in readings)
    assert mcc.menu == "main"


def test_explicit_voltage_command_raises_action_failed():
    ctl, *_ = make()
    with pytest.raises(ActionFailedError, match="Unable to perform"):
        ctl.command("CFG R V 1")
    assert ctl.command("CFG R TEMP 0") == "MB Device 0 Temp: 35.5 degC"   # twin


def test_unreachable_mcc_gives_unavailable_temperature():
    ctl, *_ = make(SilentPort())
    (temp,) = ctl.temperatures()
    assert not temp.available and "no MCC prompt" in temp.reason


# --- trap 4: destructive commands never reach the port ----------------------------------


def _forms(name: str) -> list[str]:
    return [name, name.lower(), f"{name} foo.txt", f"HELP\r{name}", f"HELP; {name.lower()}"]


@pytest.mark.parametrize("line", [form for name in DENIED for form in _forms(name)])
def test_destructive_commands_are_refused_and_never_written(line):
    ctl, mcc, port, _ = make()
    with pytest.raises(RefusedError) as info:
        ctl.command(line)
    head = next(w for w in DENIED if w in line.upper())
    assert info.value.code == ExitCode.REFUSED and head in str(info.value)
    assert port.byte_times == [] and mcc.dangerous == [] and mcc.accepted_lines == []


def test_allowed_command_is_written():
    # Twin of the above: HELP goes through, so the refusals are not a dead port.
    ctl, mcc, port, _ = make()
    ctl.command("help")
    assert port.byte_times and mcc.accepted_lines == ["HELP"] and mcc.dangerous == []


@pytest.mark.parametrize("line", ["DIR", "TYPE config.txt", "RESET", "USB_ON", "CFG W V 1 3.3",
                                  "CFG R TEMP x", "CFG R FOO 0", "CFG W OSC 0 50", "HELP ME"])
def test_unlisted_commands_are_refused(line):
    ctl, mcc, port, _ = make()
    with pytest.raises(RefusedError):
        ctl.command(line)
    assert port.byte_times == [] and mcc.accepted_lines == []


def test_osc_write_needs_arm():
    ctl, mcc, _, _ = make()
    with pytest.raises(RefusedError, match="arm=True"):
        ctl.command("CFG W OSC 0 50")
    assert mcc.osc_mhz[0] == 25.0
    ctl.command("CFG W OSC 0 50", arm=True)         # twin: armed, it is sent
    assert mcc.osc_mhz[0] == 50.0 and mcc.menu == "main"


def test_classify_normalises_allowed_commands():
    assert classify("cfg r osc 3").text == "CFG R OSC 3"
    assert classify("cfg r osc 3").menu == "debug"
    assert classify("?").menu == "main"
    assert classify("REBOOT").head == "REBOOT"


# --- trap 5: the wrong tty (the historical no-op) ---------------------------------------


def test_silent_port_is_refused_before_reboot_is_typed():
    silent = SilentPort()
    ctl, *_ = make(silent)
    with pytest.raises(NothingOnTargetError, match="interface 00"):
        ctl.reboot(wait_s=10)
    assert bytes(silent.writes) == b"\r"            # the sync CR only; REBOOT never typed


# --- the reboot witness, console only ---------------------------------------------------


def slow_boot_mcc(clock: FakeClock, **kw) -> FakeMcc:
    return FakeMcc(clock=clock, down_s=1.0, boot_s=25.0, autoboot_window_s=3.0, **kw)


def test_reboot_is_witnessed_on_the_console():
    clock = FakeClock()
    ctl, mcc, port, _ = make(slow_boot_mcc(clock), clock)
    phases = []
    ctl.reboot(progress=lambda phase, done, total: phases.append((phase, done, total)), wait_s=120)
    assert phases == [("sent", 1, 3), ("down", 2, 3), ("up", 3, 3)]
    w = ctl.last_reboot
    assert w is not None and w.boot is not None and w.boot.fpga_configured and w.boot.complete
    assert "boot banner" in w.down_evidence[0] and w.up_after_s > 25
    assert w.boot.at_prompt                         # it waited for the MCC's prompt
    assert mcc.reboots == 1 and mcc.boots_completed == 1
    # Nothing was typed after REBOOT: the auto-boot window was never touched.
    assert not mcc.autoboot_aborted and mcc.ignored_while_booting == 0
    typed = bytes(b for _, b in port.byte_times)
    assert typed.endswith(b"REBOOT\r")


def test_noop_reboot_is_an_action_failure():
    # Negative twin: the MCC accepts REBOOT and nothing happens (the old no-op trap).
    clock = FakeClock()
    ctl, mcc, _, _ = make(FakeMcc(clock=clock, ignore_reboot=True), clock)
    phases = []
    with pytest.raises(ActionFailedError, match="REBOOT sent but no restart observed") as info:
        ctl.reboot(progress=lambda p, d, t: phases.append(p), wait_s=30)
    assert info.value.code == ExitCode.ACTION_FAILED
    assert "returned to its prompt" in str(info.value)
    assert phases == ["sent"] and mcc.ignored_reboots == 1


def test_reboot_without_fpga_configuration_fails():
    clock = FakeClock()
    banner = tuple(line for line in BOOT_BANNER
                   if not line.startswith(("Address:", "FPGA configuration complete")))
    banner = banner[:12] + ("ERROR: File not found \\MB\\HBI0309C\\Nanosoc\\nanosoc.bit",) + banner[12:]
    ctl, *_ = make(slow_boot_mcc(clock, boot_banner=banner), clock)
    with pytest.raises(ActionFailedError, match="did not configure the FPGA.*nanosoc.bit"):
        ctl.reboot(wait_s=120)


def test_reboot_survives_the_port_dropping_off_usb():
    clock = FakeClock()
    ctl, mcc, _, _ = make(slow_boot_mcc(clock, drop_port_on_reboot=True), clock)
    ctl.reboot(wait_s=120)
    w = ctl.last_reboot
    assert any("dropped off USB" in e for e in w.down_evidence)
    assert w.boot is not None and w.boot.fpga_configured and not mcc.autoboot_aborted


def test_reboot_command_returns_the_witness_summary():
    clock = FakeClock()
    ctl, *_ = make(slow_boot_mcc(clock), clock)
    assert ctl.command("reboot").startswith("REBOOT witnessed: down after")


def test_reboot_from_debug_menu_exits_first():
    clock = FakeClock()
    ctl, mcc, _, _ = make(slow_boot_mcc(clock), clock)
    ctl.command("DEBUG")
    ctl.reboot(wait_s=120)
    assert mcc.accepted_lines[-2:] == ["EXIT", "REBOOT"]


# --- the auto-boot window -------------------------------------------------------------


def test_a_keypress_in_the_autoboot_window_stops_the_boot():
    # The trap, shown on the fake: a naive client typing during the banner.
    clock = FakeClock()
    booted = []
    mcc = slow_boot_mcc(clock, on_boot=lambda: booted.append(1))
    raw_paced(mcc, clock, "REBOOT\r")
    heard = b""
    while b"Press Enter" not in heard:
        clock.advance(0.1)
        heard += mcc.read(4096)
    clock.advance(0.5)                              # inside the 3 s AUTORUNDELAY window
    mcc.write(b"\r")
    clock.advance(60)
    assert b"Cmd> " in mcc.read(4096)               # dropped straight to the prompt
    assert mcc.autoboot_aborted and booted == [] and mcc.boots_completed == 0


def test_driver_waits_out_a_boot_already_in_progress():
    # Twin: the driver starts an operation mid-boot and types nothing until it ends.
    clock = FakeClock()
    booted = []
    mcc = slow_boot_mcc(clock, on_boot=lambda: booted.append(1))
    raw_paced(mcc, clock, "REBOOT\r")               # someone else rebooted the board
    clock.advance(1.2)                              # the banner has started
    ctl, *_ = make(mcc, clock)
    (temp,) = ctl.temperatures()
    assert temp.available and not mcc.autoboot_aborted
    assert booted == [1] and mcc.boots_completed == 1 and mcc.ignored_while_booting == 0


def test_guard_gives_up_on_an_endless_boot():
    clock = FakeClock()
    mcc = FakeMcc(clock=clock, down_s=0.0, boot_s=10_000.0, autoboot_window_s=0.0)
    raw_paced(mcc, clock, "REBOOT\r")
    clock.advance(0.5)
    ctl, *_ = make(mcc, clock, timing=MccTiming(boot_guard_s=30.0))
    with pytest.raises(ActionFailedError, match="still booting"):
        ctl.command("HELP")
    assert not mcc.autoboot_aborted


# --- the reboot witness with an Ethernet shell ------------------------------------------


@pytest.fixture
def shell():
    fs = FakeShell.ephemeral(static_id=0x3F1A560F)
    fs.start()
    try:
        yield fs
    finally:
        fs.stop()


def eth_controller(shell: FakeShell, mcc: FakeMcc, clock: FakeClock):
    probe = mccmod._shell_probe_for(SimpleNamespace(host=shell.host, port=shell.control_port), 0.5)
    return make(mcc, clock, shell_probe=probe)


def test_reboot_is_witnessed_by_the_shell_going_down_and_up(shell):
    clock = FakeClock()
    mcc = slow_boot_mcc(clock, on_reboot=shell.stop, on_boot=shell.start)
    ctl, *_ = eth_controller(shell, mcc, clock)
    ctl.reboot(wait_s=120)
    w = ctl.last_reboot
    assert w.down_evidence[0] == "the shell stopped answering ping"
    assert w.shell_id_before == w.shell_id_after == "0x3f1a560f"
    assert "answers ping again" in w.up_evidence and not mcc.autoboot_aborted


def test_noop_reboot_with_the_shell_up_fails(shell):
    clock = FakeClock()
    mcc = FakeMcc(clock=clock, ignore_reboot=True)
    ctl, *_ = eth_controller(shell, mcc, clock)
    with pytest.raises(ActionFailedError, match="REBOOT sent but no restart observed"):
        ctl.reboot(wait_s=10)


def test_banner_without_the_shell_going_down_is_not_a_reboot(shell):
    # The MCC reboots but the shell never stops answering: not proven, so it fails.
    clock = FakeClock()
    ctl, *_ = eth_controller(shell, slow_boot_mcc(clock), clock)
    with pytest.raises(ActionFailedError, match="never stopped answering ping"):
        ctl.reboot(wait_s=40)


def _one_lost_ping(ctl):
    real, calls = ctl._shell_probe, {"n": 0}

    def glitchy():
        calls["n"] += 1
        return None if calls["n"] == 2 else real()   # 1 = before REBOOT, 2 = the lost ping

    ctl._shell_probe = glitchy
    return calls


def test_one_lost_ping_is_not_a_reboot(shell):
    # A network glitch during a no-op REBOOT must not be read as "went down".
    clock = FakeClock()
    ctl, *_ = eth_controller(shell, FakeMcc(clock=clock, ignore_reboot=True), clock)
    calls = _one_lost_ping(ctl)
    with pytest.raises(ActionFailedError, match="no restart observed"):
        ctl.reboot(wait_s=10)
    assert calls["n"] > 3


def test_a_one_ping_threshold_would_be_fooled(shell):
    # Twin: with down_pings=1 the same glitch passes as a reboot. The threshold is load-bearing.
    clock = FakeClock()
    ctl, *_ = eth_controller(shell, FakeMcc(clock=clock, ignore_reboot=True), clock)
    ctl.timing = MccTiming(down_pings=1)
    _one_lost_ping(ctl)
    ctl.reboot(wait_s=10)
    assert ctl.last_reboot.down_evidence[0] == "the shell stopped answering ping"


def test_shell_that_never_comes_back_fails(shell):
    clock = FakeClock()
    mcc = slow_boot_mcc(clock, on_reboot=shell.stop)
    ctl, *_ = eth_controller(shell, mcc, clock)
    with pytest.raises(ActionFailedError, match="did not come back.*never answered ping again"):
        ctl.reboot(wait_s=40)


# --- the pack hook -------------------------------------------------------------------


def _session(*links: Link):
    return SimpleNamespace(candidate=Candidate(pack="mps3", board_id="b", links=links))


def test_hook_needs_a_usb_serial_link():
    assert mccmod.make_controller_adapter(_session(Link(LinkKind.ETHERNET, "127.0.0.1:1"))) is None
    ctl = mccmod.make_controller_adapter(_session(Link(LinkKind.USB_SERIAL, "/dev/ttyUSB10", "MCC")))
    assert isinstance(ctl, Mps3Controller) and ctl.url == "serial:///dev/ttyUSB10"
    win = mccmod.make_controller_adapter(_session(Link(LinkKind.USB_SERIAL, "COM7", "MCC")))
    assert win.url == "serial://COM7"


def test_hook_never_takes_an_fpga_lane_for_the_mcc():
    lane = Link(LinkKind.USB_SERIAL, "serial:///dev/ttyUSB11", "FT4232H ? if01: FPGA UART lane 0 or 1")
    assert mccmod.make_controller_adapter(_session(lane)) is None
    mcc = Link(LinkKind.USB_SERIAL, "serial:///dev/ttyUSB10", "FT4232H ? if00: MCC console")
    assert mccmod.make_controller_adapter(_session(lane, mcc)).url == "serial:///dev/ttyUSB10"
