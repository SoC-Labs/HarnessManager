from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from socharness_board_mps3.pack import Mps3Pack
from tests.fakes.virtual_board import VirtualMps3


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test touch the user's real ~/.config/socharness."""
    monkeypatch.setenv("SOCHARNESS_STATE_DIR", str(tmp_path / "state"))


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
    return os.environ.get("SOCHARNESS_HIL") == "1"
