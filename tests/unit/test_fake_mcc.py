"""The FakeMcc must reproduce the real MCC's traps, or it will hide driver bugs."""

from __future__ import annotations

from tests.fakes.fake_mcc import FakeMcc


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def paced_write(mcc: FakeMcc, clock: Clock, text: str, gap: float = 0.06) -> None:
    for ch in text:
        clock.t += gap
        mcc.write(ch.encode())


def test_burst_write_loses_all_but_first_char():
    clock = Clock()
    mcc = FakeMcc(clock=clock)
    mcc.write(b"REBOOT\r")          # one burst, as the old fpgahub plugin did
    assert mcc.reboots == 0 and mcc.dropped_chars == 6


def test_paced_reboot_lands():
    clock = Clock()
    fired = []
    mcc = FakeMcc(clock=clock, on_reboot=lambda: fired.append(1))
    paced_write(mcc, clock, "REBOOT\r")
    assert mcc.reboots == 1 and fired == [1]


def test_cfg_read_needs_debug_menu_and_voltage_is_refused():
    clock = Clock()
    mcc = FakeMcc(clock=clock)
    paced_write(mcc, clock, "CFG R TEMP 0\r")
    assert b"Command error" in mcc.read(4096)
    paced_write(mcc, clock, "DEBUG\r")
    mcc.reset_input_buffer()
    paced_write(mcc, clock, "CFG R TEMP 0\r")
    assert b"MB Device 0 Temp: 35.5 degC" in mcc.read(4096)
    paced_write(mcc, clock, "CFG R V 1\r")
    assert b"ERROR: Unable to perform requested function" in mcc.read(4096)


def test_dangerous_commands_are_recorded():
    clock = Clock()
    mcc = FakeMcc(clock=clock)
    paced_write(mcc, clock, "FORMAT\r")
    assert mcc.dangerous == ["FORMAT"]
