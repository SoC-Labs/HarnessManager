"""Team T5: output formats, error lines, progress, and small parsers. Each check has a twin."""

from __future__ import annotations

import io
import os
import signal
import threading
import time
from enum import Enum

import pytest

from harness_manager.cli.cmd_board import bundle_files, preset_mhz
from harness_manager.cli.context import hold, serial_url
from harness_manager.cli.output import (
    TSV_COLUMNS,
    StderrProgress,
    error_json,
    error_line,
    jsonable,
    tsv_field,
    tsv_line,
    with_data,
)
from harness_manager.core.errors import (
    ActionFailedError,
    ExitCode,
    HarnessError,
    HeldError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.model import Check, Reading

# The TSV columns as first shipped. Columns may be APPENDED; these prefixes never change.
PINNED = {
    "info": ("BOARD_ID", "BOARD_TYPE", "SHELL_ID", "RM_ID", "RM_NAME", "HARNESS",
             "BUILD_CHECK", "CONTROL"),                     # the Wave 0 scaffold's 8
    "probe": ("BOARD_ID", "PACK", "LABEL", "EVIDENCE", "LINKS"),
    "program": ("BOARD_ID", "OVERLAY", "RM_ID", "VERIFIED", "SECONDS", "TRANSPORT"),
    "telemetry": ("BOARD_ID", "NAME", "VALUE", "UNIT", "SOURCE", "AGE_S", "REASON"),
    "debug up|down|status": ("BOARD_ID", "STATE", "GDB", "TELNET", "TCL", "PID", "CONFIG",
                             "DETAIL"),
    "help": ("TAB", "LINE", "TEXT"),
}


def test_tsv_columns_are_append_only():
    for layout, pinned in PINNED.items():
        assert TSV_COLUMNS[layout][:len(pinned)] == pinned, layout


def test_tsv_columns_have_no_duplicates_or_blanks():
    for layout, cols in TSV_COLUMNS.items():
        assert len(set(cols)) == len(cols) and all(c and c.isupper() for c in cols), layout


def test_tsv_field_never_breaks_a_row():
    assert tsv_field("a\tb\nc\rd") == "a b c d"
    assert tsv_field(None) == "-" and tsv_field("") == "-" and tsv_field([]) == "-"
    assert tsv_field(True) == "true" and tsv_field(False) == "false"
    assert tsv_field(["x", "y"]) == "x;y" and tsv_field(0.5) == "0.5" and tsv_field(0) == "0"


def test_tsv_field_leaves_plain_text_alone():
    assert tsv_field("0x3f1a560f") == "0x3f1a560f"


def test_tsv_line_refuses_a_row_of_the_wrong_width():
    with pytest.raises(AssertionError):
        tsv_line("reset", ["mps3@x", "dut"])
    assert tsv_line("reset", ["mps3@x", "dut", "done"]) == "mps3@x\tdut\tdone"


class _E(Enum):
    A = "a"


def test_jsonable_handles_every_shape_the_verbs_emit():
    r = Reading.unavailable("board_power", "W", "no sensor")
    out = jsonable({"b": b"\x01\xff", "s": frozenset({"z", "a"}), "e": _E.A, "c": Check.OK,
                    "r": r, "t": (1, 2)})
    assert out["b"] == "01ff" and out["s"] == ["a", "z"] and out["e"] == "a"
    assert out["c"] == "ok" and out["r"]["value"] is None and out["t"] == [1, 2]


def test_error_line_always_names_a_next_action():
    assert error_line(UsageError("bad")) == (
        "harness-manager: bad — run `harness-manager help` for the verbs and the TARGET forms")
    assert error_line(ActionFailedError("x", hint="do y")) == "harness-manager: x — do y"
    assert error_line(UnavailableError("reboot_board", "needs the Debug USB cable")) == (
        "harness-manager: reboot_board is unavailable — needs the Debug USB cable")
    assert "bug" in error_line(HarnessError("internal error: KeyError"))


def test_error_json_carries_the_structured_fields():
    held = error_json(HeldError("in use", holder="bob (pid 7)", hint="wait"))["error"]
    assert held["code"] == 4 and held["name"] == "HELD" and held["holder"] == "bob (pid 7)"
    un = error_json(UnavailableError("debug", "not installed in this build"))["error"]
    assert un["capability"] == "debug" and un["reason"] == "not installed in this build"
    data = error_json(with_data(RefusedError("no"), items=[Check.MISMATCH]))["error"]
    assert data["data"] == {"items": ["mismatch"]} and data["code"] == ExitCode.REFUSED
    assert "data" not in error_json(RefusedError("no"))["error"]


def test_progress_is_throttled_but_every_phase_is_shown():
    err = io.StringIO()
    p = StderrProgress("sd install", err)
    for done in range(0, 1001):
        p("write", done, 1000)
    p("verify", 0, 0)
    lines = err.getvalue().splitlines()
    assert 10 <= len(lines) <= 13 and lines[-1] == "sd install: verify"
    assert p.phases == ["write", "verify"]


def test_preset_names_parse_to_mhz():
    assert preset_mhz("25mhz") == 25.0 and preset_mhz("100MHz") == 100.0
    assert preset_mhz("12.5") == 12.5


@pytest.mark.parametrize("bad", ["fast", "mhz", "25 khz", ""])
def test_preset_that_is_not_a_frequency_is_usage(bad):
    with pytest.raises(UsageError):
        preset_mhz(bad)


def test_serial_url_accepts_bare_device_names():
    assert serial_url("/dev/ttyUSB0") == "serial:///dev/ttyUSB0"
    assert serial_url("COM7") == "serial://COM7"
    assert serial_url("fake://mcc") == "fake://mcc"


def test_bundle_files_maps_sd_paths(tmp_path):
    (tmp_path / "MB").mkdir()
    (tmp_path / "MB" / "a.txt").write_text("x")
    (tmp_path / "config.txt").write_text("y")
    assert sorted(bundle_files(tmp_path)) == ["MB/a.txt", "config.txt"]


def test_bundle_files_refuses_ebf_in_any_case(tmp_path):
    (tmp_path / "MBB_V200.EBF").write_bytes(b"x")
    with pytest.raises(RefusedError):
        bundle_files(tmp_path)


def test_hold_zero_returns_at_once_and_a_limit_expires():
    assert hold(0) == "no hold requested"
    t0 = time.monotonic()
    assert hold(0.05) == "time limit" and time.monotonic() - t0 < 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery")
def test_hold_ends_on_sigterm_instead_of_dying():
    before = signal.getsignal(signal.SIGTERM)
    timer = threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        why = hold(None)
    finally:
        timer.cancel()
    assert why == f"signal {int(signal.SIGTERM)}"
    assert signal.getsignal(signal.SIGTERM) == before       # the handler is removed again
