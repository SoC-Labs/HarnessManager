"""The lab, faked (lane L1): srv03335 -> SSH -> hub -> the MPS3, with no network at all.

``lab(vb, monkeypatch)`` wires the pieces the way Thursday's real run is wired:

- boards.toml in the test's state dir, exactly the table docs/HIL_B0.md tells
  david to write (``via = "ssh:mapstone-dev…"``, a ``hub`` table with the MCC share);
- ``FakeSsh`` as the tunnel's launcher: the board's REAL ports as the hub sees
  them (``192.168.10.101:6900`` …) lead to the ``VirtualMps3``'s ephemeral ports,
  so the pack runs with its real defaults (6900/6910/6921/6930-6932/2542);
- ``FakeHub`` as every hub runner, with the MCC (``vb.mcc``) on
  ``/dev/mps3_01_pl/tty_00``, shared or not.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3 import tunnel as tunmod
from tests.fakes.l1_fake_hub import FakeHub
from tests.fakes.l1_fake_ssh import FakeSsh
from tests.fakes.virtual_board import VirtualMps3

BOARD_IP = "192.168.10.101"
HUB = "mapstone-dev.ecs.soton.ac.uk"
TARGET = "mps3_01_pl"
MCC_TTY = "/dev/mps3_01_pl/tty_00"
LANE2_TTY = "/dev/mps3_01_pl/tty_02"

LAB_TOML = f"""\
[boards.lab]
match = ["{BOARD_IP}"]
via = "ssh:{HUB}"
hub = {{ host = "{HUB}", target = "{TARGET}", shares = {{ mcc = "{MCC_TTY}" }} }}
"""


def write_boards_toml(state_dir: Path, text: str = LAB_TOML) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "boards.toml"
    path.write_text(text, encoding="utf-8")
    return path


def board_routes(vb: VirtualMps3) -> dict[int, int]:
    """The lab board's real TCP ports -> the virtual board's ephemeral ones."""
    shell = vb.shell
    return {6900: shell.control_port, 6910: shell.raw_tcp_port,
            6930: vb.console_ports.get("uart0", 0), 6931: vb.console_ports.get("uart1", 0),
            6932: vb.console_ports.get("swo", 0)}


@dataclass
class Lab:
    vb: VirtualMps3
    ssh: FakeSsh
    hub: FakeHub
    boards_toml: Path
    runners: list[tuple[str, Any]] = field(default_factory=list)


@contextmanager
def lab(vb: VirtualMps3, monkeypatch: pytest.MonkeyPatch, *, state_dir: Path,
        share_mcc: bool = True, toml: str = LAB_TOML) -> Iterator[Lab]:
    ssh = FakeSsh()
    ssh.route_board(BOARD_IP, board_routes(vb))
    hub = FakeHub(TARGET)
    hub.add_tty(MCC_TTY, vb.mcc, share=share_mcc)
    rig = Lab(vb, ssh, hub, write_boards_toml(state_dir, toml))

    def runner_factory(host: str, group: str | None) -> FakeHub:
        rig.runners.append((host, group))
        return hub

    monkeypatch.setattr(tunmod, "DEFAULT_LAUNCHER", ssh)
    monkeypatch.setattr(tunmod, "DEFAULT_SSH_G", ssh.ssh_g)
    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", runner_factory)
    try:
        yield rig
    finally:
        hubmod.SHARES.close_all()
        hub.close()
        ssh.close()
