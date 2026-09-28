"""Lane P1: ``harness-manager panel show|mirror`` and ``harness-manager identify`` end to end.

The real CLI ``main()``, in-process (the real Engine and MPS3 pack over ``PanelVirtualMps3``)
and over a real harness-manager-daemon (``LiveDaemon``: the verbs then read its ``/panel``
routes). The TSV layouts are pinned here (test_t5_cli_golden.py lists them as pinned
elsewhere). Every check has a negative twin.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.main import main
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ExitCode
from harness_manager_mps3.capabilities import NEEDS_LOCATE
from tests.fakes.clcd_panel_shell import LINUX_PANEL, V011_BARE_METAL, PanelVirtualMps3
from tests.fakes.t13_daemon import LiveDaemon, engine_for
from tests.fakes.virtual_board import LINUX_HARNESSD


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def in_process(monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    yield


def board(tmp_path: Path, profile) -> Iterator[PanelVirtualMps3]:
    with PanelVirtualMps3(tmp_path, profile) as vb:
        previous = set_engine_factory(lambda _args: engine_for(vb))
        try:
            yield vb
        finally:
            set_engine_factory(previous)


@pytest.fixture
def linux(tmp_path, in_process) -> Iterator[PanelVirtualMps3]:
    yield from board(tmp_path, LINUX_PANEL)


@pytest.fixture
def bare(tmp_path, in_process) -> Iterator[PanelVirtualMps3]:
    yield from board(tmp_path, V011_BARE_METAL)


@pytest.fixture
def lx_nopanel(tmp_path, in_process) -> Iterator[PanelVirtualMps3]:
    """PANEL-TRUTH: the Linux harness without 'panel', 'presence', 'locate' (rc2_v6)."""
    yield from board(tmp_path, LINUX_HARNESSD)


# --- the verbs exist, and `identify` was free ---------------------------------------------------


def test_panel_and_identify_are_verbs_with_help_and_tsv_columns(capsys):
    rc, out, _ = run(capsys, "help", "identify")
    assert rc == 0 and "harness-manager identify" in out and "--seconds" in out
    assert " ".join(TSV_COLUMNS["identify"]) in " ".join(out.split())
    rc, out, _ = run(capsys, "help", "panel")
    assert rc == 0 and "show" in out and "mirror" in out
    rc, _, err = run(capsys, "panel", "blink", "127.0.0.1")
    assert rc == ExitCode.USAGE and "invalid choice" in err


# --- panel show ------------------------------------------------------------------------------


def test_panel_show_on_linux_reads_the_panel(linux, capsys):
    linux.shell.tap("request")
    rc, out, _ = run(capsys, "--json", "panel", "show", linux.shell_endpoint)
    body = json.loads(out)
    assert rc == 0 and body["panel"]["source"] == "panel" and body["panel"]["page"] == "status"
    assert body["identify"]["available"] is True
    assert body["presence"]["active"] is False, "a CLI run announces nothing"
    assert [e["on"] for e in body["panel"]["events"]] == ["request"]
    rc, out, _ = run(capsys, "panel", "show", linux.shell_endpoint)
    assert "panel      status page · harness owns it" in out and "identify   available" in out
    assert linux.shell.hellos == [], "the CLI reads with `panel`, never `hello`"


def test_panel_show_on_bare_metal_says_rebuilt_and_why_identify_is_off(bare, capsys):
    rc, out, _ = run(capsys, "--json", "panel", "show", bare.shell_endpoint)
    body = json.loads(out)
    assert rc == 0 and body["panel"]["source"] == "rebuilt" and body["panel"]["owner"] == "harness"
    assert body["identify"] == {"available": False, "until": None, "reason": NEEDS_LOCATE}
    rc, out, _ = run(capsys, "panel", "show", bare.shell_endpoint)
    assert "rebuilt from what Harness Manager read" in out
    assert f"identify   unavailable: {NEEDS_LOCATE}" in out
    # PANEL-TRUTH: what it does not report, on one line, by feature; the type from its impl
    assert ("missing    not reported by this image: page, who is connected, recent taps "
            "(harness feature 'panel' and 'presence')") in out
    assert "harness    bare-metal harness" in out and "sessions   " not in out


def test_panel_truth_panel_show_on_a_linux_image_without_panel_never_says_bare_metal(
        lx_nopanel, capsys):
    rc, out, _ = run(capsys, "panel", "show", lx_nopanel.shell_endpoint)
    assert rc == 0 and "harness    Linux harness" in out
    assert "bare metal" not in out and "bare-metal" not in out
    assert "not reported by this image: page, who is connected, recent taps" in out
    rc, out, _ = run(capsys, "panel", "mirror", lx_nopanel.shell_endpoint)
    assert "?" not in out and "127.0.0.1" not in out and "NET : \u2014" in out


def test_panel_show_tsv_is_one_row_of_its_columns(linux, capsys):
    rc, out, _ = run(capsys, "--tsv", "panel", "show", linux.shell_endpoint)
    [row] = out.splitlines()
    cols = row.split("\t")
    assert rc == 0 and len(cols) == len(TSV_COLUMNS["panel show"])
    assert cols[1:4] == ["panel", "status", "harness"] and cols[9] == "true"


# --- panel mirror ---------------------------------------------------------------------------


def test_panel_mirror_prints_the_grid(linux, bare, capsys):
    rc, out, _ = run(capsys, "panel", "mirror", linux.shell_endpoint)
    lines = out.splitlines()
    assert rc == 0 and len(lines) == 17 and lines[1].startswith("|MPS3-01")
    rc, out, _ = run(capsys, "--tsv", "panel", "mirror", linux.shell_endpoint)
    rows = [r.split("\t") for r in out.splitlines()]
    assert len(rows) == 15 and all(len(r) == len(TSV_COLUMNS["panel mirror"]) for r in rows)
    assert rows[0][4] == "panel"
    rc, out, _ = run(capsys, "panel", "mirror", bare.shell_endpoint)
    assert rc == 0 and out.rstrip().endswith("(rebuilt from what Harness Manager read, not "
                                             "read from the panel)")


# --- identify -----------------------------------------------------------------------------------


def test_identify_blinks_a_linux_board(linux, capsys):
    rc, out, _ = run(capsys, "--json", "identify", linux.shell_endpoint, "--seconds", "5")
    body = json.loads(out)
    assert rc == 0 and body["seconds"] == 5 and linux.shell.locates[-1]["s"] == 5
    rc, out, _ = run(capsys, "identify", linux.shell_endpoint, "--seconds", "0")
    assert rc == 0 and "stopped" in out and linux.shell.locates[-1] == {"op": "locate", "s": 0}


def test_identify_on_bare_metal_is_exit_12_with_the_reason(bare, capsys):
    rc, out, err = run(capsys, "identify", bare.shell_endpoint)
    assert rc == ExitCode.UNAVAILABLE and out == ""
    assert NEEDS_LOCATE in err
    assert bare.shell.locates == []


def test_identify_refuses_a_bad_duration_before_the_board(linux, capsys):
    rc, _, err = run(capsys, "identify", linux.shell_endpoint, "--seconds", "31")
    assert rc == ExitCode.USAGE and "0 to 30" in err and linux.shell.locates == []


# --- over harness-manager-daemon ------------------------------------------------------------------


def test_the_verbs_go_through_a_running_daemon(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("HARNESS_MANAGER_NO_DAEMON", raising=False)
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        eng = engine_for(vb)
        with LiveDaemon(eng) as d:
            d.app.state.daemon.presence.ride_wait_s = 0.0
            rc, out, _ = run(capsys, "--json", "panel", "show", vb.shell_endpoint)
            body = json.loads(out)
            assert rc == 0 and body["panel"]["source"] == "panel"
            assert body["presence"]["reason"] != "presence runs in the Harness Manager service " \
                                                 "(harness-manager-daemon)", "the daemon answered"
            rc, out, _ = run(capsys, "--json", "identify", vb.shell_endpoint, "--seconds", "3")
            assert rc == 0 and json.loads(out)["seconds"] == 3 and vb.shell.locates[-1]["s"] == 3
        eng.close_all()
