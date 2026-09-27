"""L1: the lab board through the hub, end to end against the virtual board. Each check has a twin.

The rig (tests/fakes/l1_rig.py) is Thursday's wiring with the network faked:
boards.toml says ``via = "ssh:mapstone-dev…"``, the fake ssh forwards the board's real
ports to a VirtualMps3, and the virtual MCC sits on /dev/mps3_01_pl/tty_00 ON the fake hub,
read there by Harness Manager's hub-side reader (MCC-FIX: never through a share). The pack
runs with its real port defaults.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from harness_manager.core import capabilities as C
from harness_manager.core.errors import UsageError
from harness_manager.core.model import LinkKind
from harness_manager.core.pack import ProbeHints
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.l1_rig import BOARD_IP, HUB, MCC_TTY, lab, write_boards_toml
from tests.fakes.virtual_board import VirtualMps3


def state_dir() -> Path:
    import os

    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


@pytest.fixture
def engine():
    eng = Engine(EngineConfig(state_dir=state_dir()))
    yield eng
    eng.close_all()


def test_boards_toml_routes_the_lab_board_through_the_hub(tmp_path, monkeypatch, engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        cand = engine.candidate_for(BOARD_IP)
        eth = next(lk for lk in cand.links if lk.kind == LinkKind.ETHERNET)
        assert eth.via == "ssh" and eth.address == f"{BOARD_IP}:6900" and f"ssh:{HUB}" in eth.detail
        assert not any(lk.kind == LinkKind.USB_SERIAL for lk in cand.links)   # no MCC share
        mcc = next(lk for lk in cand.links if lk.kind == LinkKind.HUB)
        assert mcc.via == "hub" and mcc.address == f"hub-mcc://{HUB}/mps3_01_pl{MCC_TTY}"

        session = engine.open(cand, note="l1 test")
        info = engine.info(cand.board_id)
        assert info.identity.shell_id == "0x3f1a560f"                     # through the tunnel
        for cap in (C.IDENTIFY, C.DEPLOY_PARTIAL, C.CONSOLE_DUT, C.CONSOLE_CONTROLLER,
                    C.TELEMETRY_TEMP, C.REBOOT_BOARD):
            assert cap in info.capabilities, cap
        status = session.reach.status()
        assert status["state"] == "up" and status["via"] == f"ssh:{HUB}"
        assert set(status["ports"]) == {"6900", "6910", "6921", "6930", "6931", "6932", "2542"}
        assert 2542 not in status["ports"].values()                       # never bound locally
        # The session's endpoints are the tunnel's local ends.
        assert session.shell.host == "127.0.0.1"
        uart0 = session.consoles.console_endpoints()["uart0"]
        assert uart0 == f"tcp://127.0.0.1:{status['ports']['6930']}"
        assert session.deploy.push_port == status["ports"]["6910"] and session.deploy.tunnelled
        assert session.debug.rbb_port == status["ports"]["6921"]
        assert rig.ssh.launches[0][-1] == HUB

        engine.close(cand.board_id)
        assert session.reach.tunnel.state == "down"
        assert not rig.ssh.live()                                         # no ssh left behind


def test_negative_twin_without_a_via_the_candidate_stays_direct(tmp_path, monkeypatch, engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml='[boards.lab]\nmatch = ["192.168.10.101"]\n') as rig:
        cand = engine.candidate_for(BOARD_IP)
        assert all(lk.via == "" for lk in cand.links)
        assert [lk.kind for lk in cand.links] == [LinkKind.ETHERNET]
        assert rig.ssh.launches == []


def test_a_console_reads_the_dut_through_the_tunnel(tmp_path, monkeypatch, engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()):
        cand = engine.candidate_for(BOARD_IP)
        session = engine.open(cand)
        port = int(session.consoles.console_endpoints()["uart0"].rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.settimeout(3)
            assert s.recv(64).startswith(b"nanosoc boot")                  # FakeShell's banner


def test_the_mcc_temperature_is_read_on_the_hub(tmp_path, monkeypatch, engine):
    # MCC-FIX: Harness Manager's reader runs ON the hub against tty_00 (here a real pty in
    # front of the virtual MCC), paced 100 ms a character; no share, no forward to one.
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        session = engine.open(engine.candidate_for(BOARD_IP))
        (temp,) = session.controller.temperatures()
        assert temp.available and temp.value == 35.5 and temp.source == "mcc-console (hub)"
        run = rig.tool.reader_runs[-1]
        assert (run["tty"], run["lines"], run["menu"], run["pace"]) == (
            MCC_TTY, ["CFG R TEMP 0"], "debug", 0.1)
        assert vb.mcc.accepted_lines[-3:] == ["DEBUG", "CFG R TEMP 0", "EXIT"]
        assert vb.mcc.menu == "main"                       # left at Cmd> for the next REBOOT
        assert rig.tool.share_starts() == [] and rig.hub.shares == {}
        assert not any(c[:2] == ["fpgahub", "share"] for c in rig.tool.calls)
        assert rig.hub.share_stops == []


def test_negative_twin_a_share_on_tty_00_is_a_second_reader_and_nothing_is_typed(
        tmp_path, monkeypatch, engine):
    # Someone else's fpgahub share holds tty_00: the hub-side scan refuses, and Harness
    # Manager neither uses that share nor types into the MCC.
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          share_mcc=True) as rig:
        session = engine.open(engine.candidate_for(BOARD_IP))
        (temp,) = session.controller.temperatures()
        # the broker runs as another user: the scan sees its command line, not its fds
        assert not temp.available and "names the MCC console" in temp.reason
        assert "tty_share" in temp.reason and vb.mcc.accepted_lines == []
        assert rig.hub.shares[MCC_TTY].readers == 0 and rig.hub.shares[MCC_TTY].written == b""


def test_negative_twin_start_shares_never_starts_a_tty_00_share(tmp_path, monkeypatch, engine):
    toml = (f'[boards.lab]\nmatch = ["{BOARD_IP}"]\nvia = "ssh:{HUB}"\n'
            f'hub = {{ host = "{HUB}", target = "mps3_01_pl", shares = {{ mcc = "{MCC_TTY}" }}, '
            'start_shares = true }\n')
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml=toml) as rig:
        session = engine.open(engine.candidate_for(BOARD_IP))
        (temp,) = session.controller.temperatures()
        assert temp.available and temp.value == 35.5         # read on the hub, as ever
        assert MCC_TTY not in rig.hub.shares and rig.tool.share_starts() == []
        assert not any(lk.address.startswith("hub://") for lk in session.candidate.links)


def test_another_reader_on_tty_00_refuses_the_read_instead_of_typing(tmp_path, monkeypatch, engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        session = engine.open(engine.candidate_for(BOARD_IP))
        rig.tool.others = [[777, f"cat {MCC_TTY}"]]
        (temp,) = session.controller.temperatures()
        assert not temp.available and "pid 777" in temp.reason
        assert vb.mcc.accepted_lines == []                 # nothing was typed
        rig.tool.others = []                                # twin: alone again, it reads
        (temp,) = session.controller.temperatures()
        assert temp.available and temp.value == 35.5


def test_probe_finds_the_board_through_the_tunnel(tmp_path, monkeypatch, engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        found = engine.probe(ProbeHints(hosts=(BOARD_IP,), scan_usb=False, scan_network=False))
        assert len(found) == 1 and found[0].identity is not None
        assert found[0].identity.shell_id == "0x3f1a560f"
        assert any(lk.via == "ssh" for lk in found[0].links)
        assert not rig.ssh.live()                                         # the probe's tunnel closed


def test_negative_twin_probe_finds_nothing_when_the_hub_cannot_reach_the_board(tmp_path, monkeypatch,
                                                                                 engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        rig.ssh.routes.clear()
        found = engine.probe(ProbeHints(hosts=(BOARD_IP,), scan_usb=False, scan_network=False,
                                        timeout_s=1.0))
        assert found == []


def test_a_dropped_tunnel_comes_back_and_the_session_keeps_working(tmp_path, monkeypatch, engine):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        cand = engine.candidate_for(BOARD_IP)
        session = engine.open(cand)
        tunnel = session.reach.tunnel
        monkeypatch.setattr(tunnel, "_backoff", (1.0,))
        states: list[str] = []
        tunnel.watch(lambda st: states.append(st["state"]))
        rig.ssh.procs[0].drop()
        deadline = time.monotonic() + 10
        while "down" not in states and time.monotonic() < deadline:     # noticed ...
            time.sleep(0.02)
        notes = session.health().notes                                   # within the 1 s back-off
        assert any(n.startswith(f"the SSH tunnel to {HUB} is down") for n in notes)
        while (tunnel.restarts < 1 or tunnel.state != "up") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert states[-1] == "up" and tunnel.restarts == 1
        assert engine.info(cand.board_id).identity.shell_id == "0x3f1a560f"   # ... and back
        assert not any("SSH tunnel" in n for n in session.health().notes)      # the twin


def test_an_explicit_via_works_without_boards_toml(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml="") as rig:
        pack = Mps3Pack()
        cand = pack.candidate_for_host(BOARD_IP, via=f"ssh:{HUB}")
        assert any(lk.via == "ssh" for lk in cand.links)
        with pack.open(cand) as session:
            assert session.identity().shell_id == "0x3f1a560f"
            assert session.hub is None                                    # no hub table: no leases
        assert not rig.ssh.live()


def test_hub_links_carry_lane_numbers_for_the_console_names():
    cfg = hubmod.HubConfig(HUB, shares={"mcc": MCC_TTY, "lane2": "/dev/mps3_01_pl/tty_02"})
    links = hubmod.share_links(cfg)
    assert [lk.address.rsplit("/", 1)[1] for lk in links] == ["tty_02"]   # never the MCC's
    from harness_manager_mps3.usb import is_lane_link

    assert is_lane_link(links[0])
    assert all(lk.via == "hub" and lk.kind == LinkKind.USB_SERIAL for lk in links)
    # twin: a tty_00 under any name is the MCC, and makes no share link either
    assert hubmod.share_links(hubmod.HubConfig(HUB, shares={"console": MCC_TTY})) == []
    mcc = hubmod.mcc_link(cfg)
    assert mcc is not None and mcc.kind == LinkKind.HUB and mcc.address.endswith(MCC_TTY)


def test_boards_toml_hub_table_errors_name_the_key(tmp_path):
    for bad, word in (({"target": "x"}, "host"), ({"host": "h", "shares": {"mcc": "tty_00"}}, "shares"),
                      ({"host": "h", "stop": True}, "unknown"), ({"host": "h", "baud": "fast"}, "baud")):
        with pytest.raises(UsageError) as exc:        # not Exception: KeyError('host') matched too
            hubmod.parse_hub_table(bad)
        assert word in exc.value.message
    assert hubmod.parse_hub_table({"host": "h"}).target == "mps3_01_pl"


def test_share_stop_is_refused_before_it_reaches_the_hub():
    from harness_manager.core.errors import RefusedError
    from tests.fakes.l1_fake_hub import FakeHub

    fake = FakeHub()
    client = hubmod.HubClient(HUB, runner=fake)
    with pytest.raises(RefusedError):
        client._run(["fpgahub", "share", "stop", "mps3_01_pl"])
    assert fake.share_stops == [] and fake.calls == []


def test_the_boards_toml_in_the_hil_doc_routes_the_lab_board(tmp_path, monkeypatch):
    """docs/HIL_B0.md step 0.2 is copied verbatim by david: it must parse and route."""
    import textwrap

    doc = (Path(__file__).resolve().parents[2] / "docs" / "HIL_B0.md").read_text()
    block = doc.split("<<'EOF'\n", 1)[1].split("\n   EOF", 1)[0]
    write_boards_toml(state_dir(), textwrap.dedent("   " + block) + "\n")
    cand = Mps3Pack().candidate_for_host(BOARD_IP)
    eth = next(lk for lk in cand.links if lk.kind == LinkKind.ETHERNET)
    assert eth.via == "ssh" and f"ssh:{HUB}" in eth.detail
    assert not [lk for lk in cand.links if lk.kind == LinkKind.USB_SERIAL]   # no MCC share
    (mcc,) = [lk for lk in cand.links if lk.kind == LinkKind.HUB]
    assert mcc.address == f"hub-mcc://{HUB}/mps3_01_pl{MCC_TTY}"
    cfg = hubmod.hub_config_for(cand)
    assert (cfg.host, cfg.target, cfg.start_shares) == (HUB, "mps3_01_pl", False)


LANE_TOML = (f'[boards.lab]\nmatch = ["{BOARD_IP}"]\nvia = "ssh:{HUB}"\n'
             f'hub = {{ host = "{HUB}", target = "mps3_01_pl", shares = {{ mcc = "{MCC_TTY}", '
             'fpga_uart2 = "/dev/mps3_01_pl/tty_02" } }\n')


def test_a_shared_fpga_lane_is_a_tcp_console_the_broker_and_l2_read_as_a_share(tmp_path, monkeypatch,
                                                                                 engine):
    from harness_manager_mps3 import uart
    from tests.fakes.l1_fake_hub import FakeLane

    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(),
                                          toml=LANE_TOML) as rig:
        lane_tty = FakeLane()
        rig.hub.add_tty("/dev/mps3_01_pl/tty_02", lane_tty, share=True)
        session = engine.open(engine.candidate_for(BOARD_IP))
        eps = session.consoles.console_endpoints()
        lane = eps["fpga_uart2"]
        assert lane.startswith("tcp://127.0.0.1:") and lane.endswith("?baud=115200")
        assert "mcc" not in eps                                # the MCC is never a console
        row = uart.console_baud_info(eps, session.shell)["fpga_uart2"]
        assert row["share"] and row["baud"] == 115200 and not row["settable"]
        port = int(lane.split(":")[2].split("?")[0])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.settimeout(5)
            s.sendall(b"root\n")                            # a share only relays what is new
            got = b""
            while b"root" not in got:
                got += s.recv(100)
        assert lane_tty.received == b"root\n"                # typed through relay, tunnel, share
        engine.close(session.candidate.board_id)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()   # closed with the board


def test_negative_twin_a_lane_whose_share_is_not_running_closes_and_says_why(tmp_path, monkeypatch,
                                                                               engine):
    from harness_manager_mps3 import hub as hubmod

    with VirtualMps3(tmp_path) as vb, lab(vb, monkeypatch, state_dir=state_dir(), toml=LANE_TOML):
        session = engine.open(engine.candidate_for(BOARD_IP))
        lane = session.consoles.console_endpoints()["fpga_uart2"]
        port = int(lane.split(":")[2].split("?")[0])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.settimeout(5)
            assert s.recv(10) == b""
        ref = hubmod.ShareRef(HUB, "mps3_01_pl", "/dev/mps3_01_pl/tty_02")
        assert "fpgahub share start" in hubmod.SHARES.relay(ref).last_error
