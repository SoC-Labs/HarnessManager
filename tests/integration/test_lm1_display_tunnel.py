"""The compositor through the REAL ``SshTunnel`` (driven by the tunnel tests' ``FakeSsh``), in
the ``claim.open_forward({"lcd_mirror": 6940})`` shape (docs/design/LCD_MIRROR.md §7.2;
lane LM1). The source here is what lane LM2 wires for the MPS3: a socket to the forward's
local port, ssh's "open failed" lines as the diagnosis, and a release that closes the
forward. Two of the transport spike's checks: an image without the service, and a tunnel
drop with the key refused for a while, recovered on the SAME local port, bit-exact.
"""

from __future__ import annotations

import socket
import time
from typing import Any

import pytest

from harness_manager.services.display import DisplayService, DisplayTimings
from harness_manager_mps3 import tunnel as T
from tests.fakes import lm1_golden as G
from tests.fakes.l1_fake_ssh import FakeSsh
from tests.fakes.lm1_fake_lcd_mirror import FakeLcdMirror, FakePanel, ViewerModel, bind_ephemeral

BOARD, HUB, LCDM = "mps3-01-board.test", "hub.test", 6940
FAST = DisplayTimings(grace_s=1.0, ping_s=0.1, stale_s=2.0, dead_s=5.0, tick_s=0.02,
                      backoff_s=(0.1, 0.2, 0.4), no_service_retry_s=0.5)


def wait_for(pred: Any, timeout: float = 20.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while not (v := pred()):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)
    return v


class TunnelSource:
    """The MPS3 source's shape (lane LM2): a new connection to the forward per connect."""

    def __init__(self, tunnel: T.SshTunnel) -> None:
        self.tunnel = tunnel
        self.released = 0

    def display_reason(self) -> str:
        return ""

    def display_connect(self) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.tunnel.local_port("lcd_mirror")), timeout=5)

    def display_diagnose(self, since: float) -> str:
        fails = self.tunnel.open_failures_since(since, wait_s=0.5)
        return f"no lcd_mirror service on the board ({fails[-1]})" if fails else ""

    def display_release(self) -> None:
        self.released += 1


@pytest.fixture
def rig() -> Any:
    fake_ssh = FakeSsh(hub_loopback=False)
    s = bind_ephemeral()
    local = s.getsockname()[1]
    s.close()
    pinned = ["-o", "StrictHostKeyChecking=yes", "-o", "PreferredAuthentications=publickey"]
    tunnel = T.SshTunnel(BOARD, [T.Forward("lcd_mirror", "127.0.0.1", LCDM, local)],
                         launcher=fake_ssh, ssh_g=fake_ssh.ssh_g, jump=HUB, user="root",
                         options=pinned, backoff_s=(0.2, 0.5), label="mps3-01 lcd_mirror (test)")
    tunnel.start()
    svc = DisplayService(timings=FAST)
    yield fake_ssh, tunnel, svc
    svc.shutdown()
    tunnel.close()
    fake_ssh.close()


def test_the_forward_is_the_claim_open_forward_shape(rig: Any) -> None:
    fake_ssh, tunnel, _svc = rig
    argv = fake_ssh.launches[0]
    assert argv[-1] == BOARD and argv[argv.index("-J") + 1] == HUB and argv[argv.index("-l") + 1] == "root"
    assert argv[argv.index("-L") + 1] == f"127.0.0.1:{tunnel.local_port('lcd_mirror')}:127.0.0.1:{LCDM}"
    for opt in ("ControlPath=none", "ExitOnForwardFailure=yes", "BatchMode=yes", "StrictHostKeyChecking=yes"):
        assert opt in argv


def test_an_image_without_the_service_says_so(rig: Any) -> None:
    fake_ssh, tunnel, svc = rig
    dead = bind_ephemeral()                                   # nothing listens behind it
    fake_ssh.routes[("127.0.0.1", LCDM)] = ("127.0.0.1", dead.getsockname()[1])
    dead.close()
    src = TunnelSource(tunnel)
    v = svc.attach("mps3-01", src)
    wait_for(lambda: svc.status("mps3-01")["state"] == "down", what="down")
    reason = svc.status("mps3-01")["reason"]
    assert reason.startswith("no lcd_mirror service on the board") and "open failed" in reason
    v.close()


def test_tunnel_drop_and_refused_key_recover_on_the_same_port(rig: Any) -> None:
    fake_ssh, tunnel, svc = rig
    model, grid, regs = G.harness_pictures()["boot"]
    with FakeLcdMirror(FakePanel(model, regs=regs), mode="sw") as board:
        fake_ssh.routes[("127.0.0.1", LCDM)] = ("127.0.0.1", board.port)
        src = TunnelSource(tunnel)
        vm = ViewerModel()
        v = svc.attach("mps3-01", src)
        wait_for(lambda: vm.pump(v) and bytes(vm.frame) == grid, what="the first picture")
        local = tunnel.local_port("lcd_mirror")
        fake_ssh.fail = "auth"                                # the next ssh start is refused
        fake_ssh.current.drop()                               # the tunnel dies
        wait_for(lambda: "Permission denied" in tunnel.detail, what="the refused key")
        assert svc.status("mps3-01")["state"] in ("reconnecting", "connecting", "syncing")
        model2, grid2, regs2 = G.harness_pictures()["link_down"]
        with board.edit() as p:
            p.set_frame(model2)
            p.regs[:] = regs2
        fake_ssh.fail = ""                                    # the key works again
        wait_for(lambda: svc.status("mps3-01")["state"] == "live" and vm.pump(v) >= 0
                 and bytes(vm.frame) == grid2, what="exact after the reclaim")
        assert tunnel.local_port("lcd_mirror") == local and tunnel.restarts >= 1
        assert svc.status("mps3-01")["counters"]["connects"] >= 2
        v.close()
        svc.close("mps3-01", "test over")
        # close() releases, and the upstream thread again as it ends: no once-only flag, so a
        # forward a late connect opened is released too (REVIEW-W5 6; release is idempotent)
        assert 1 <= src.released <= 2
