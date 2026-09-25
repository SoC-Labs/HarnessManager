"""FLAKE-2: a virtual board comes back on its own ports after a REBOOT, on a busy host.

A REBOOT closes every shell listener and the boot binds the same ports again. On
``bind(0)`` ports the kernel sometimes gave one to another socket meanwhile, and the boot
died with EADDRINUSE (the t7 rollback and the Q2 reboot-while-busy test, under load).
``BoardPorts`` keeps them from the kernel (below its ephemeral range) and from every
other virtual board (a claim held for the board's life). Each check has a negative twin.
"""

from __future__ import annotations

import pytest
from pyverify.client import ShellClient

from tests.fakes.virtual_board import (
    BOARD_PORT_RANGE,
    FIELDED_3F1A560F,
    LINUX_HARNESSD,
    SHELL_PORT_KEYS,
    BoardPorts,
    VirtualMps3,
)


def _ephemeral_low() -> int:
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as fh:
            return int(fh.read().split()[0])
    except OSError:
        return 49152                                # Windows and macOS


@pytest.mark.parametrize("profile", [FIELDED_3F1A560F, LINUX_HARNESSD], ids=lambda p: p.name)
def test_a_rebooted_board_comes_back_on_its_ports_and_no_other_board_takes_one_meanwhile(
        tmp_path, profile):
    with VirtualMps3(tmp_path, profile) as vb:
        claimed = vb.board_ports.ports
        assert {k: getattr(vb.shell, k) for k in SHELL_PORT_KEYS} == claimed   # the shell's own
        ports = tuple(claimed.values())
        assert all(BOARD_PORT_RANGE[0] <= p < BOARD_PORT_RANGE[1] for p in ports)
        assert max(ports) < _ephemeral_low()        # the kernel never hands one out
        endpoint = vb.shell_endpoint
        vb._on_reboot()                             # the MCC's REBOOT: every listener closes
        with pytest.raises(AssertionError, match="no free port"):
            BoardPorts(candidates=ports)            # a board made meanwhile cannot pick one
        vb._on_boot()                               # "FPGA configuration complete."
        assert vb.shell_endpoint == endpoint
        with ShellClient("127.0.0.1", vb.shell.control_port) as shell:
            shell.ping()


def test_negative_twin_a_closed_boards_ports_are_free_for_the_next_one(tmp_path):
    with VirtualMps3(tmp_path) as vb:
        ports = tuple(vb.board_ports.ports.values())
    again = BoardPorts(candidates=ports)            # the claim, not luck, kept them
    try:
        assert set(again.ports.values()) == set(ports)
    finally:
        again.release()
