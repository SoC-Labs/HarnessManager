from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.virtual_board import VirtualMps3


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test touch the user's real ~/.config/harness-manager, nor the PTY links
    of a daemon the user runs on the same machine (/tmp/harness-manager-$USER): a PTY is
    only made on request, and the L2 tests pick their own directory (l2_rig.pty_dir), but
    a test that forgets must land here, not beside a live daemon's links."""
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HARNESS_MANAGER_PTY_DIR", str(tmp_path / "ptys"))
    # Never run the machine's real Vivado (`vivado -version` from kit discovery): a test that
    # wants one points HARNESS_MANAGER_VIVADO at a fake (tests/fakes/kit_fakes.py).
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", "off")


@pytest.fixture(autouse=True)
def _no_real_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test enumerate the test machine's real serial ports or volumes."""
    from harness_manager_mps3 import usb

    monkeypatch.setattr(usb, "DEFAULT_ENV", usb.UsbEnv(lambda: [], lambda: []))


@pytest.fixture
def vboard(tmp_path: Path) -> Iterator[VirtualMps3]:
    with VirtualMps3(tmp_path) as vb:
        yield vb


@pytest.fixture
def mps3_pack(vboard: VirtualMps3) -> Mps3Pack:
    """An MPS3 pack pointed at the virtual board's ephemeral ports."""
    return Mps3Pack(console_ports=vboard.console_ports,
                    push_port=vboard.shell.raw_tcp_port, tftp_port=vboard.shell.tftp_port)


def hil_enabled() -> bool:
    return os.environ.get("HARNESS_MANAGER_HIL") == "1"
