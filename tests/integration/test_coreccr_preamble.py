"""CORE-CCR: the shell's explicit preamble hook for a riding hello (CCR PANEL-3).

``Mps3Shell.preamble`` runs on each connection ``call_raw`` opens, before the caller's own
requests, inside the same error mapping. The MPS3 panel adapter sets it to ``Mps3Panel.ride``
(it no longer wraps ``shell.call_raw`` per instance): a hello goes out only when one is
waiting, and only a board that reports ``presence`` is ever armed. Real sockets: the Engine
and MPS3 pack over ``PanelVirtualMps3``. Every check has a negative twin.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from harness_manager.core import capabilities as C
from harness_manager.core.errors import UnavailableError
from harness_manager.core.panel import Hello, HelloLease, PanelState
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager_mps3.pack import Mps3Pack
from harness_manager_mps3.shell import Mps3Shell, ShellWedgedError
from tests.fakes.clcd_panel_shell import LINUX_PANEL, V011_BARE_METAL, PanelVirtualMps3

HELLO = Hello(sid="c0ffee01", who="core@ccr", app="hm/0.1.0", name="mps3-01", role="holder",
              lease=HelloLease(by="core@ccr", left=600, q=0))


def ops(vb: PanelVirtualMps3, since: int = 0) -> list[str]:
    return [r.get("op") for r in vb.shell.requests[since:]]


class Local:
    def __init__(self, vb: PanelVirtualMps3, tmp_path: Path) -> None:
        self.vb = vb
        self.engine = Engine(EngineConfig(state_dir=tmp_path / "state"),
                             packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        self.session = self.engine.open(vb.candidate(), note="core-ccr")


@contextmanager
def local(tmp_path: Path, profile) -> Iterator[Local]:
    with PanelVirtualMps3(tmp_path, profile) as vb:
        rig = Local(vb, tmp_path)
        try:
            yield rig
        finally:
            rig.engine.close_all()


def test_the_ride_is_the_shells_preamble_hook_not_a_wrapper(tmp_path):
    with local(tmp_path, LINUX_PANEL) as rig:
        shell = rig.session.shell
        assert shell.preamble == rig.session.panel.ride
        assert "call_raw" not in vars(shell), "call_raw is no longer wrapped per instance"


def test_the_preamble_sends_a_waiting_hello_once(tmp_path):
    with local(tmp_path, LINUX_PANEL) as rig:
        got: list[PanelState] = []
        rig.session.panel.offer(HELLO, got.append)
        before = len(rig.vb.shell.requests)
        rig.session.identity()
        assert ops(rig.vb, before) == ["hello", "ping", "version"]
        before = len(rig.vb.shell.requests)
        rig.session.identity()
        rig.session.health()
        assert "hello" not in ops(rig.vb, before), "sent once: nothing waits any more"
        assert len(got) == 1 and len(rig.vb.shell.hellos) == 1 and rig.session.panel.rides == 1


@pytest.mark.parametrize("profile", [
    V011_BARE_METAL,
    replace(LINUX_PANEL, name="linux-panel-no-presence",
            features=tuple(f for f in LINUX_PANEL.features if f != "presence")),
], ids=["bare-metal", "linux-without-presence"])
def test_twin_never_a_hello_to_a_board_without_presence(tmp_path, profile):
    with local(tmp_path, profile) as rig:
        assert rig.session.shell.preamble is not None
        with pytest.raises(UnavailableError) as exc:
            rig.session.panel.offer(HELLO, lambda _s: None)
        assert exc.value.capability == C.PRESENCE
        rig.session.identity()
        rig.session.health()
        rig.session.panel.state()
        assert "hello" not in ops(rig.vb) and rig.vb.shell.hellos == []


def test_mps3shell_runs_the_preamble_first_inside_its_error_mapping(tmp_path):
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        shell = Mps3Shell("127.0.0.1", vb.shell.control_port, timeout=2.0)
        order: list[str] = []

        def preamble(client: Any, tap: Any) -> None:
            order.append("preamble")
            client.ping()

        shell.preamble = preamble
        shell.call(lambda c: order.append("fn") or c.version())
        assert order == ["preamble", "fn"] and ops(vb) == ["ping", "version"]

        def wedged(_client: Any, _tap: Any) -> None:
            raise TimeoutError("no reply")

        shell.preamble = wedged
        with pytest.raises(ShellWedgedError):
            shell.call(lambda c: order.append("never"))
        assert "never" not in order


def test_twin_without_a_preamble_a_connection_carries_only_its_own_requests(tmp_path):
    with PanelVirtualMps3(tmp_path, LINUX_PANEL) as vb:
        shell = Mps3Shell("127.0.0.1", vb.shell.control_port, timeout=2.0)
        assert shell.preamble is None
        shell.call(lambda c: c.version())
        assert ops(vb) == ["version"]
