"""Lane L2 on the virtual MPS3: uart0's rate from the design or the harness, and a PTY on uart0.

The board is ``VirtualMps3`` (fielded profile) with nanosoc loaded; ``L2BaudShell``
adds lane L6's ``uart_baud`` verb where a test asks for the feature. The pack is
wired with the L2 CCR (``apply_pack_ccr``). FakeShell echoes uart0, so a PTY
client's typing comes back to every reader. Every check has a negative twin.
"""

from __future__ import annotations

import sys

import pytest

if not sys.platform.startswith("linux"):
    # Client counting (inotify) and TIOCINQ are Linux-only; the PTYs are untested elsewhere.
    pytest.skip("the PTY tests need Linux", allow_module_level=True)

import os
import shutil
import subprocess
import termios
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_manager.core.errors import ActionFailedError, UnavailableError
from harness_manager.core.events import Event, EventBus
from harness_manager.services import pty as ptymod
from harness_manager.services.console import ConsoleBroker
from harness_manager_mps3 import uart
from harness_manager_mps3 import usb as usbmod
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.l2_rig import (
    ETH_SS_RM_ID,
    PtyClient,
    apply_pack_ccr,
    fast_pty_options,
    install_uart_baud_codec,
    l2_virtual_board,
    wait_for,
)
from tests.fakes.t4_console_rig import EventLog, read_until

pytestmark = pytest.mark.skipif(not ptymod.supported(), reason="PTYs need a POSIX system")

BANNER = b"nanosoc boot\n"


@pytest.fixture(autouse=True)
def _wiring(tmp_path: Path, monkeypatch) -> None:
    apply_pack_ccr(monkeypatch)
    install_uart_baud_codec(monkeypatch)
    monkeypatch.setenv(ptymod.PTY_DIR_ENV, str(tmp_path / "ptys"))


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def log(bus: EventBus) -> EventLog:
    return EventLog(bus)


@pytest.fixture
def broker(bus: EventBus) -> Iterator[ConsoleBroker]:
    b = ConsoleBroker(bus, backoff=(0.05, 0.2), pty_options=fast_pty_options())
    yield b
    b.shutdown()


def open_session(vb, **pack_kw):
    pack = Mps3Pack(console_ports=vb.console_ports, **pack_kw)
    return pack.open(pack.candidate_for_host(vb.shell_endpoint))


def ops(vb, op: str) -> list[dict]:
    return [o for o in vb.shell.ops if o.get("op") == op]


# -- the fielded harness: no uart_baud ------------------------------------------------------------


def test_fielded_harness_reports_nanosocs_fixed_rate_and_refuses_a_change(tmp_path, broker):
    with l2_virtual_board(tmp_path, features=()) as vb:
        s = open_session(vb)
        row = broker.baud(s, "uart0")
        assert (row["baud"], row["source"], row["settable"], row["kind"]) == (76800, "design",
                                                                              False, "ethernet")
        assert "76800" in row["reason"] and "nanosoc" in row["reason"]
        with pytest.raises(UnavailableError) as err:
            broker.set_baud(s, "uart0", 115200)
        assert "76800" in err.value.reason and "'uart_baud'" in err.value.reason
        assert ops(vb, "uart_baud") == []                  # never asked a harness without it
        rows = {r["name"]: r for r in broker.consoles(s)}
        assert rows["uart1"]["baud"] is None and rows["swo"]["baud"] == 2_000_000


def test_negative_twin_another_design_changes_the_answer(tmp_path, broker):
    with l2_virtual_board(tmp_path, features=(), rm_id=ETH_SS_RM_ID) as vb:
        row = broker.baud(open_session(vb), "uart0")
    assert row["baud"] is None and "eth_ss ties its console off" in row["reason"]


def test_greybox_has_no_console_rate(tmp_path, broker):
    with l2_virtual_board(tmp_path, features=(), rm_id=0) as vb:
        row = broker.baud(open_session(vb), "uart0")
    assert row["baud"] is None and "greybox" in row["reason"]


# -- a harness with uart_baud -----------------------------------------------------------------------


def test_the_harness_verb_reads_and_sets_uart0(tmp_path, broker, log):
    with l2_virtual_board(tmp_path) as vb:
        s = open_session(vb)
        row = broker.baud(s, "uart0")
        assert (row["baud"], row["source"], row["settable"], row["mode"]) == (76800, "harness",
                                                                              True, "auto")
        assert 115200 in row["choices"]
        mark = log.mark()
        assert broker.set_baud(s, "uart0", 115200) == {"name": "uart0", "baud": 115200,
                                                        "source": "harness", "mode": "set"}
        assert vb.shell.uart["uart0"]["baud"] == 115200
        ev = log.wait_for(lambda e: e.topic == "console.state" and e.data.get("baud") == 115200,
                          after=mark)
        assert ev.data["name"] == "uart0"
        assert broker.baud(s, "uart0")["mode"] == "set"           # the cache was dropped
        broker.set_baud(s, "uart0", 0)                            # back to auto
        assert vb.shell.uart["uart0"] == {"baud": 76800, "default": 76800, "mode": "auto",
                                          "settable": True}
        sets = [o for o in ops(vb, "uart_baud") if "baud" in o]
        assert [o["baud"] for o in sets] == [115200, 0]


def test_negative_twin_a_harness_that_says_fixed_is_refused_with_its_reason(tmp_path, broker):
    with l2_virtual_board(tmp_path, settable=False) as vb:
        s = open_session(vb)
        row = broker.baud(s, "uart0")
        assert not row["settable"] and row["mode"] == "fixed" and "fixed" in row["reason"]
        with pytest.raises(UnavailableError):
            broker.set_baud(s, "uart0", 115200)
        assert not [o for o in ops(vb, "uart_baud") if "baud" in o]    # no set was sent


def test_a_rate_the_harness_refuses_is_an_action_failure(tmp_path, broker):
    with l2_virtual_board(tmp_path) as vb:
        s = open_session(vb)
        with pytest.raises(ActionFailedError) as err:
            broker.set_baud(s, "uart0", 5_000_000)
        assert "ERANGE" in err.value.message
        assert vb.shell.uart["uart0"]["baud"] == 76800


def test_a_pyverify_without_the_codec_is_a_reason_not_a_hand_rolled_request(tmp_path, broker,
                                                                           monkeypatch):
    from pyverify.client import ShellClient

    monkeypatch.delattr(ShellClient, "uart_baud", raising=False)
    with l2_virtual_board(tmp_path) as vb:
        s = open_session(vb)
        row = broker.baud(s, "uart0")
        assert row["reason"] == uart.NO_CODEC and not row["settable"] and row["baud"] == 76800
        with pytest.raises(UnavailableError):
            broker.set_baud(s, "uart0", 115200)
        assert ops(vb, "uart_baud") == []


def test_the_rate_report_is_cached_and_a_swap_drops_it(tmp_path, bus, broker):
    with l2_virtual_board(tmp_path) as vb:
        s = open_session(vb)
        broker.baud(s, "uart0")
        broker.consoles(s)
        broker.baud(s, "uart1")
        assert len(ops(vb, "ping")) == 1                          # one control connection
        bus.publish(Event("deploy.done", s.candidate.board_id, {"verified": True}))
        broker.baud(s, "uart0")
        assert len(ops(vb, "ping")) == 2                          # negative twin: asked again


def test_offline_reads_never_touch_the_board(tmp_path, broker):
    with l2_virtual_board(tmp_path) as vb:
        s = open_session(vb)
        row = broker.baud(s, "uart0", live=False, offline_reason="deploy job 1 is running")
        assert row["baud"] is None and row["reason"] == "deploy job 1 is running"
        assert vb.shell.ops == []
        broker.baud(s, "uart0")
        cached = broker.baud(s, "uart0", live=False)
        assert cached["baud"] == 76800 and cached["source"] == "harness"


def test_a_hub_share_is_serial_and_not_settable(tmp_path, broker, monkeypatch):
    monkeypatch.setattr(usbmod, "serial_console_endpoints",
                        lambda cand: {"fpga_uart0": "tcp://127.0.0.1:9?baud=115200"})
    with l2_virtual_board(tmp_path, features=()) as vb:
        s = open_session(vb)
        row = broker.baud(s, "fpga_uart0")
        assert (row["kind"], row["baud"], row["settable"]) == ("serial", 115200, False)
        assert "share sets the rate" in row["reason"]
        with pytest.raises(UnavailableError):
            broker.set_baud(s, "fpga_uart0", 57600)


# -- a PTY on uart0 ------------------------------------------------------------------------------------


def test_a_pty_on_uart0_and_the_gui_see_the_same_console(tmp_path, broker):
    with l2_virtual_board(tmp_path, features=()) as vb:
        s = open_session(vb)
        info = broker.pty(s, "uart0")
        assert info["command"] == f"screen {info['path']}"        # 76800: no rate to pass
        gui = broker.subscribe(s, "uart0")
        with PtyClient(info["path"]) as client:
            client.read_until(BANNER)
            read_until(gui, BANNER)
            client.type("print(1)")                               # FakeShell echoes uart0
            client.read_until(b"print(1)\r")
            read_until(gui, b"print(1)\r")
        up = broker._ups[(s.candidate.board_id, "uart0")]
        assert up.pace_s == 0.02 and up.connects == 1             # paced, ONE board connection


def test_uart0_reattach_after_an_exclusive_client_exits(tmp_path, broker):
    with l2_virtual_board(tmp_path, features=()) as vb:
        s = open_session(vb)
        bid = s.candidate.board_id
        info = broker.pty(s, "uart0")
        with PtyClient(info["path"], exclusive=True) as first:        # as screen opens it
            first.read_until(BANNER)
            first.type("one")
            first.read_until(b"one\r")
        wait_for(lambda: broker.pty_info(bid, "uart0")["clients"] == 0, what="the client left")
        with PtyClient(info["path"], exclusive=True) as second:       # EBUSY without the reset
            second.type("two")
            second.read_until(b"two\r")


def test_negative_twin_a_speed_set_on_an_ethernet_pty_changes_nothing(tmp_path, broker):
    with l2_virtual_board(tmp_path) as vb:
        s = open_session(vb)
        info = broker.pty(s, "uart0")
        with PtyClient(info["path"], speed=termios.B115200) as client:
            client.read_until(BANNER)
            port = broker._ptys.get(s.candidate.board_id, "uart0")
            wait_for(lambda: port.seen_speed == termios.B115200, what="the watcher saw it")
        assert not [o for o in ops(vb, "uart_baud") if "baud" in o]
        assert vb.shell.uart["uart0"]["baud"] == 76800


@pytest.mark.skipif(shutil.which("screen") is None, reason="GNU screen is not installed")
def test_real_screen_shows_the_banner_printed_before_it_attached(tmp_path, broker):
    # What david sees on Thursday: the board booted, THEN he runs `screen <path>`.
    screendir = tmp_path / "screens"
    screendir.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if k != "STY"}
    env["SCREENDIR"] = str(screendir)
    name = f"l2-banner-{os.getpid()}"
    shot = tmp_path / "hardcopy.txt"

    def screen(*args: str) -> None:
        subprocess.run(["screen", *args], env=env, check=False, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def shown() -> str:
        shot.unlink(missing_ok=True)
        screen("-S", name, "-X", "hardcopy", str(shot))
        return wait_for(lambda: shot.exists() and shot.read_text(), timeout=5,
                        what="a hardcopy") or ""

    with l2_virtual_board(tmp_path, features=()) as vb:
        s = open_session(vb)
        info = broker.pty(s, "uart0")
        port = broker._ptys.get(s.candidate.board_id, "uart0")
        wait_for(lambda: BANNER in bytes(port._ring), what="the banner, before screen")
        screen("-dmS", name, info["path"])
        try:
            wait_for(lambda: "nanosoc boot" in shown(), timeout=10, what="the banner in screen")
        finally:
            screen("-S", name, "-X", "quit")


@pytest.mark.skipif(shutil.which("screen") is None, reason="GNU screen is not installed")
def test_real_screen_attaches_detaches_and_reattaches(tmp_path, broker):
    screendir = tmp_path / "screens"
    screendir.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if k != "STY"}
    env["SCREENDIR"] = str(screendir)

    def screen(*args: str) -> None:
        subprocess.run(["screen", *args], env=env, check=False, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with l2_virtual_board(tmp_path, features=()) as vb:
        s = open_session(vb)
        info = broker.pty(s, "uart0")
        bid = s.candidate.board_id
        gui = broker.subscribe(s, "uart0")
        read_until(gui, BANNER)
        for round_ in (1, 2):                                     # 2: the TIOCEXCL trap
            name = f"l2-{os.getpid()}-{round_}"
            screen("-dmS", name, info["path"])
            try:
                wait_for(lambda: broker.pty_info(bid, "uart0")["clients"] == 1,
                         what=f"screen {round_} attached")
                screen("-S", name, "-X", "stuff", f"hi{round_}")
                read_until(gui, f"hi{round_}".encode())           # typed in screen, echoed
            finally:
                screen("-S", name, "-X", "quit")
            wait_for(lambda: broker.pty_info(bid, "uart0")["clients"] == 0,
                     what=f"screen {round_} gone")
            time.sleep(0.2)
