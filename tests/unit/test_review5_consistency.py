"""REVIEW-W5 items 10-12, board-free. Every check has a negative twin.

10. ONE tty_00 rule (``transports.tcp_serial.mcc_tty_reason``) on normalised paths, used by
    ``hub.is_mcc_share``/``share_links``, ``hub_mcc.is_mcc_tty``, the settings ``_tty`` check,
    ``HubClient.share_start``, ``RestHubClient.share_start`` and ``parse_hub_table``;
11. ``POST /hubs/{name}/boards`` refuses a target that is not a target name before the job;
12. tools Detect: probes run the ``openocd_probe`` way (a file, the group killed on timeout),
    the demo refuses a request ``table`` that names a path, ``_exe`` tries ``PATHEXT`` on
    Windows, ``hw_server.bat`` beside Vivado on Windows.

Items 13-16 (Windows): ``test_review5_windows.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness_manager.core.errors import RefusedError, UsageError
from harness_manager.transports import tcp_serial as TS

MCC = "/dev/mps3_01_pl/tty_00"
LANE = "/dev/mps3_01_pl/tty_01"
BY_ID_00 = "/dev/serial/by-id/usb-FTDI_Quad_RS232-HS-if00-port0"
BY_ID_01 = "/dev/serial/by-id/usb-FTDI_Quad_RS232-HS-if01-port0"
SPELLINGS = [MCC + "/", "/dev/mps3_01_pl//tty_00", "/dev/mps3_01_pl/./tty_00", MCC + "/."]


# --- 10. one tty_00 rule -----------------------------------------------------------------------


@pytest.mark.parametrize("tty", [MCC, *SPELLINGS, "tty_00", BY_ID_00])
def test_every_spelling_of_the_mcc_console_is_the_mcc(tty: str) -> None:
    from harness_manager_mps3 import hub, hub_mcc

    assert TS.is_mcc_tty(tty) and hub_mcc.is_mcc_tty(tty)
    assert hub.is_mcc_share("fpga_uart1", tty)


@pytest.mark.parametrize("tty", [LANE, LANE + "/", "/dev/ttyUSB10", BY_ID_01,
                                 "/dev/mps3_01_pl/tty_001", "/dev/xtty_00x"])
def test_twin_a_lane_or_a_path_that_cannot_be_judged_is_not(tty: str) -> None:
    from harness_manager_mps3 import hub, hub_mcc

    assert not TS.is_mcc_tty(tty) and not hub_mcc.is_mcc_tty(tty)
    assert not hub.is_mcc_share("fpga_uart1", tty)


def test_the_by_id_alias_is_refused_with_a_message() -> None:
    why = TS.mcc_tty_reason(BY_ID_00)
    assert "FT4232H interface 00" in why and "fpgahub device paths" in why
    assert TS.tty_interface(LANE + "/") == 1 and TS.tty_interface("/dev/ttyUSB3") is None


def test_share_links_make_no_link_for_a_trailing_slash_tty_00() -> None:
    from harness_manager_mps3 import hub

    cfg = hub.parse_hub_table({"host": "hub.invalid", "target": "mps3_01_pl",
                               "shares": {"fpga_uart0": MCC + "/", "fpga_uart1": LANE + "/",
                                          "debug": BY_ID_00}})
    assert cfg.shares == {"fpga_uart0": MCC, "fpga_uart1": LANE, "debug": BY_ID_00}
    links = hub.share_links(cfg)
    assert [lk.address.rsplit("/", 1)[-1] for lk in links] == ["tty_01"]


def test_twin_the_hub_client_refuses_every_spelling_before_the_hub_is_asked() -> None:
    from harness_manager_mps3 import hub

    asked: list[list[str]] = []

    def runner(argv: Any, timeout: float | None = None) -> Any:
        asked.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=f"share {LANE} -> 0.0.0.0:12001\n",
                               stderr="")

    client = hub.HubClient("hub.invalid", "mps3_01_pl", runner=runner)
    for tty in (*SPELLINGS, BY_ID_00):
        with pytest.raises(RefusedError, match="never starts or uses an fpgahub share"):
            client.share_start(tty)
    assert asked == []
    with pytest.raises(RefusedError, match="FT4232H interface 00"):
        client.share_start(BY_ID_00)


def test_the_rest_client_refuses_the_alias_before_any_request() -> None:
    from harness_manager.transports import hub_rest

    cfg = hub_rest.RestHubConfig(url="http://127.0.0.1:9", target="mps3_01_pl", timeout_s=1)
    c = hub_rest.RestHubClient(cfg, credential=hub_rest.Credential("t", "test"),
                               retry_backoff_s=())
    calls: list[Any] = []
    c._call = lambda *a, **k: calls.append(a) or SimpleNamespace(body=[])  # type: ignore[method-assign]
    for tty in (MCC + "/", BY_ID_00):
        with pytest.raises(RefusedError, match="MCC console"):
            c.share_start(tty)
    assert calls == []
    # twin: a lane goes to the hub
    with pytest.raises(Exception):  # noqa: B017 - the fake answers no share: any error is fine
        c.share_start(LANE)
    assert calls and calls[0][0] == "share start"


def test_the_settings_check_refuses_the_alias_and_every_spelling() -> None:
    from harness_manager_mps3.settings import _tty

    key = "boards.lab.hub.shares.fpga_uart1"
    assert "must not be on tty_00" in _tty(BY_ID_00, key)
    assert "FT4232H interface 00" in _tty(BY_ID_00, key)
    for tty in SPELLINGS:
        assert "must not be on tty_00" in _tty(tty, key), tty
    # twins: shares.mcc may name it; a lane and a /dev/ttyUSBn are not refused
    assert _tty(MCC, "boards.lab.hub.shares.mcc") == ""
    assert _tty(LANE, key) == "" and _tty("/dev/ttyUSB10", key) == ""


# --- 11. hubs add-board validates the target before the job ------------------------------------


@pytest.mark.parametrize("target", ["-h", "--help", "a b", "x;rm -rf /", "t$(id)", "a" * 65])
def test_add_board_refuses_a_target_that_is_not_a_target_name_before_the_job(target: str) -> None:
    from harness_manager.settings import hubs

    with pytest.raises(UsageError, match="is not an fpgahub target name"):
        hubs.check_target(target)


def test_twin_a_target_name_passes() -> None:
    from harness_manager.settings import hubs

    assert hubs.check_target("mps3_01_pl") == "mps3_01_pl"
    assert hubs.check_target("kr260-01.a") == "kr260-01.a"


def test_target_details_never_runs_an_option_like_target() -> None:
    from harness_manager.settings import hubs, hubtest

    lab = hubs.Hub(name="lab", host="hub.invalid")
    ran: list[Any] = []

    def run(argv: Any) -> Any:
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, '{"network": {}, "description": ""}', "")

    with pytest.raises(UsageError, match="is not an fpgahub target name"):
        hubtest.target_details(lab, "-h", resolver=SimpleNamespace(), run=run)
    assert ran == []
    # twin: a target name is asked for, with fpgahub target show
    try:
        hubtest.target_details(lab, "mps3_01_pl", resolver=SimpleNamespace(), run=run)
    except Exception:  # noqa: BLE001 - how the fake's reply parses is not the point here
        pass
    assert ran and ran[-1][-4:] == ["fpgahub", "target", "show", "mps3_01_pl"] or \
        "fpgahub target show mps3_01_pl" in " ".join(ran[-1])


# --- 12. Detect runs its probes the openocd_probe way ------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="a /bin/sh stand-in and its background child")
def test_a_probe_whose_child_keeps_the_output_open_returns_at_its_timeout(tmp_path: Path) -> None:
    """``subprocess.run(capture_output=True, timeout=…)`` kills the tool but then waits for
    the pipe, which a child the tool left behind still holds: Detect hung. The probe runner
    writes to a file and kills the whole group."""
    from harness_manager.core.proc import probe_run
    from harness_manager.settings import tooltest

    tool = tmp_path / "uv"
    tool.write_text("#!/bin/sh\n(sleep 30 &)\nsleep 30\n")
    tool.chmod(0o755)
    t0 = time.monotonic()
    ver, err = tooltest._probe([str(tool), "--version"], tooltest._UV, 1.0, probe_run)
    assert time.monotonic() - t0 < 8 and ver == "" and "did not finish in 1 s" in err
    # the default runner of every Detect entry point is that runner
    import inspect

    for fn in (tooltest.detect_one, tooltest.detect_tools):
        assert inspect.signature(fn).parameters["runner"].default is probe_run


def test_twin_a_probe_that_answers_is_read() -> None:
    from harness_manager.core.proc import probe_run

    got = probe_run([sys.executable, "-c", "print('uv 0.4.30')"], capture_output=True,
                    text=True, timeout=30)
    assert got.returncode == 0 and "uv 0.4.30" in got.stdout


def test_the_demo_refuses_a_detect_table_that_names_a_path() -> None:
    from harness_manager.daemon.settings_api import _names_a_path

    for table in ({"openocd": "/tmp/evil"}, {"uv": "C:\\tools\\uv.exe"}, {"uv": "~/bin/uv"}):
        assert _names_a_path(table), table
    # twins: no table, a bare command name
    assert not _names_a_path(None) and not _names_a_path({"openocd": "openocd", "uv": ""})


def test_exe_tries_pathext_for_a_windows_path_without_an_extension(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness_manager.settings import tooltest

    (tmp_path / "hw_server.bat").write_text("@echo off\n")
    monkeypatch.setattr(tooltest.sys, "platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    got = tooltest._exe(str(tmp_path / "hw_server"), {"PATH": ""})
    assert got == str(tmp_path / "hw_server.bat")
    assert tooltest._hw_server_names()[0] == "hw_server.bat"
    assert tooltest._beside(tmp_path) == str(tmp_path / "hw_server.bat")
    # twin: elsewhere the name is hw_server, and a missing path is not found
    monkeypatch.setattr(tooltest.sys, "platform", "linux")
    assert tooltest._hw_server_names() == ("hw_server",)
    assert tooltest._exe(str(tmp_path / "hw_server"), {"PATH": ""}) is None


# --- the lead's text fixes: no reboot "over the MCC share" since MCC-FIX ---------------------------


def test_no_text_says_the_board_is_rebooted_over_an_mcc_share() -> None:
    """MCC-FIX: there is no MCC share; the reboot runs on the hub. The words the UI, the
    catalog and the guide show say so."""
    import re

    root = Path(__file__).resolve().parents[2]
    stale = re.compile(r"rebooted over its MCC\s+share|over the hub's MCC share", re.I)
    files = [root / "docs" / "USER_GUIDE.md",
             root / "src/harness_manager/web/static/js/sections/harness.js",
             root / "src/harness_manager/services/harness_catalog.py"]
    for f in files:
        assert not stale.search(f.read_text(encoding="utf-8")), f
    assert "rebooting by the MCC on the hub (paced)" in files[1].read_text(encoding="utf-8")
    # twin: the pattern does find the old words
    assert stale.search("the board is rebooted over its MCC\n  share. A release")
