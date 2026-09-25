"""FLAKE-2: a virtual board comes back on its own ports after a REBOOT, on a busy host.

A REBOOT closes every shell listener (and the Linux harness's UDP identify responder) and
the boot binds the same ports again. On ``bind(0)`` ports the kernel sometimes gave one
to another socket meanwhile, and the boot died with EADDRINUSE (the t7 rollback and the
Q2 reboot-while-busy test, under load). ``BoardPorts`` keeps them from the kernel (below
its ephemeral range) and from every other virtual board (a claim held for the board's
life). The helpers that swap in their own shell (T9, L2, the panel) use the same ports.
Each check has a negative twin.
"""

from __future__ import annotations

import json
import socket

import pytest
from pyverify.client import ShellClient

from tests.fakes.clcd_panel_shell import PanelVirtualMps3
from tests.fakes.l2_rig import l2_virtual_board
from tests.fakes.t9_shell import t9_virtual_board
from tests.fakes.virtual_board import (
    BOARD_PORT_RANGE,
    LINUX_HARNESSD,
    SHELL_PORT_KEYS,
    BoardPorts,
    VirtualMps3,
)

BOARDS = {
    "fielded": lambda p: VirtualMps3(p),
    "linux": lambda p: VirtualMps3(p, LINUX_HARNESSD),       # + the UDP identify responder
    "t9": t9_virtual_board,
    "l2": l2_virtual_board,
    "panel": PanelVirtualMps3,
}


def _ephemeral_low() -> int:
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as fh:
            return int(fh.read().split()[0])
    except OSError:
        return 49152                                # Windows and macOS


def _all_ports(vb: VirtualMps3) -> tuple[int, ...]:
    return tuple(vb.board_ports.shell_ports.values()) + (
        (vb.identify_port,) if vb.identify_port else ())


def _claimable(port: int) -> bool:
    """Could another virtual board claim ``port`` now?"""
    try:
        BoardPorts(candidates=(port,), keys=("control_port",)).release()
    except AssertionError:
        return False
    return True


def _identify(port: int) -> dict:
    with socket.socket(type=socket.SOCK_DGRAM) as s:
        s.settimeout(5)
        s.sendto(json.dumps({"op": "identify", "v": 1, "nonce": "0123abcd"}).encode(),
                 ("127.0.0.1", port))
        return json.loads(s.recv(2048))


@pytest.mark.parametrize("make", BOARDS.values(), ids=BOARDS.keys())
def test_a_rebooted_board_comes_back_on_its_ports_and_no_other_board_takes_one_meanwhile(
        tmp_path, make):
    with make(tmp_path) as vb:
        assert {k: getattr(vb.shell, k) for k in SHELL_PORT_KEYS} == vb.board_ports.shell_ports
        if vb.identify_port:                        # the Linux harness's identify responder
            assert vb.identify_port == vb.board_ports.identify_port
        ports = _all_ports(vb)
        assert all(BOARD_PORT_RANGE[0] <= p < BOARD_PORT_RANGE[1] for p in ports)
        assert max(ports) < _ephemeral_low()        # the kernel never hands one out
        endpoint = vb.shell_endpoint
        vb._on_reboot()                             # the MCC's REBOOT: every listener closes
        assert not [p for p in ports if _claimable(p)]   # a board made meanwhile gets none
        vb._on_boot()                               # "FPGA configuration complete."
        assert vb.shell_endpoint == endpoint and _all_ports(vb) == ports
        with ShellClient("127.0.0.1", vb.shell.control_port) as shell:
            shell.ping()
        if vb.identify_port:
            assert _identify(vb.identify_port)["nonce"] == "0123abcd"


@pytest.mark.parametrize("make", BOARDS.values(), ids=BOARDS.keys())
def test_negative_twin_a_closed_boards_ports_are_free_for_the_next_one(tmp_path, make):
    with make(tmp_path) as vb:
        ports = _all_ports(vb)
    assert [p for p in ports if _claimable(p)] == list(ports)   # the claim, not luck, held them


def test_the_linux_boards_identify_port_is_held_through_rescue_too(tmp_path):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        port = vb.identify_port
        vb.enter_rescue()
        assert vb.identify_port == port and _identify(port)["mode"] == "rescue"
        vb.leave_rescue()
        assert vb.identify_port == port and not _claimable(port)
