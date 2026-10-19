"""DEBUG-ONBOARD: OpenOCD runs ON the Linux harness and gdb reaches it over the board's SSH.

The lab is the real MPS3 pack reached ``via ssh:HUB`` on a board this Harness Manager claimed
(``claimed_lock.pin_claim``), with pyverify's FakeShell (the Linux profile), ``FakeJtagServer``
on 6921, ``stub_openocd`` for this PC's OpenOCD, and the board's ``mps3-debug`` launcher
(``do_fake_launcher.FakeLauncher``) behind the claim's one-shot ssh (``claim.DEFAULT_RUN``). One
fake ssh plays the hub tunnel and the claim forward, whose far end on the board's loopback
reaches the launcher's stand-in gdb servers (3333, 3334). Nothing leaves 127.0.0.1.

Each check has its negative twin.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from harness_manager.core.errors import (
    AlreadyError,
    ClaimLockedError,
    ExitCode,
    HeldError,
    IncompatibleError,
    NothingOnTargetError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.core.services import DebugStatus
from harness_manager.services import debug_onboard as OB
from harness_manager.services.debug import DebugService
from harness_manager_mps3 import claim as CL
from harness_manager_mps3 import tunnel as T
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.claimed_lock import (
    BOARD_IP,
    HUB,
    TRUSTED,
    HubAndBoardSsh,
    board_key_fp,
    forward_specs,
    observed_claimed,
    pin_claim,
    route_board,
)
from tests.fakes.do_fake_launcher import GDB_PORTS, IDCODE, FakeLauncher
from tests.fakes.lxslots_board import slot_board
from tests.fakes.t4_debug_rig import StubRig, use_stub
from tests.fakes.t4_rbb_jtag import FakeJtagServer

NANOSOC_RM = 0x0001_0001          # design 0x0001: nanosoc
MULTICORE_RM = 0x0001_0003        # nanosoc_multicore: two cores, two gdb ports
GREYBOX_RM = 0x0001_0000          # no debug port
UNKNOWN_RM = 0x0001_7A57          # a design Harness Manager has no name for


# --- the lab -----------------------------------------------------------------------------------


@dataclass
class Lab:
    fake: Any
    jtag: FakeJtagServer
    ssh: HubAndBoardSsh
    launcher: FakeLauncher
    session: Any

    @property
    def board_id(self) -> str:
        return self.session.candidate.board_id

    def claim_port(self, name: str) -> int:
        st = self.session.claim.forward_status()
        assert st is not None, "no claim forward is open"
        return int(st["forwards"][name]["local"])


@pytest.fixture
def lab(monkeypatch) -> Iterator[Any]:
    made: list[Lab] = []

    def make(*, claimed: bool = True, pinned: bool = True, observed: bool | None = None,
             boot_rm_id: int = NANOSOC_RM, **launcher_kw: Any) -> Lab:
        fake = slot_board(profile="linux", ssh_claimed=claimed, boot_rm_id=boot_rm_id,
                          slots={"trusted_peer": TRUSTED}, ssh_host_key_sha256=board_key_fp())
        jtag = FakeJtagServer().__enter__()
        jtag.claimed = claimed
        ssh = HubAndBoardSsh()
        route_board(ssh, {6900: fake.control_port, 6910: fake.raw_tcp_port, 6921: jtag.port})
        launcher_kw.setdefault("rm_id", boot_rm_id)
        launcher = FakeLauncher(board_ip=BOARD_IP, hub=HUB, serve=True, **launcher_kw)
        for port, srv in launcher.servers.items():      # the board's own 127.0.0.1:3333/3334
            ssh.routes[("127.0.0.1", port)] = ("127.0.0.1", srv.port)
        monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
        monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
        monkeypatch.setattr(CL, "DEFAULT_RUN", launcher)
        pack = Mps3Pack()
        session = pack.open(pack.candidate_for_host(f"{BOARD_IP}:6900", via=f"ssh:{HUB}"))
        if pinned:
            pin_claim(session)
        if observed is not None:
            observed_claimed(session.candidate.board_id, board_key_fp(), claimed=observed)
        rig = Lab(fake, jtag, ssh, launcher, session)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.session.close()
        rig.ssh.close()
        rig.jtag.close()
        rig.launcher.close()
        rig.fake.stop()


@pytest.fixture
def stub(monkeypatch, tmp_path) -> StubRig:
    return use_stub(monkeypatch, tmp_path)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def debug(bus) -> Iterator[DebugService]:
    svc = DebugService(bus, start_timeout=15, cooldown=0.0)
    yield svc
    svc.close()


def on_board(monkeypatch, value: str) -> None:
    monkeypatch.setenv(OB.ON_BOARD_ENV, value)


def greeting(port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=3.0) as s:
        s.settimeout(3.0)
        return s.recv(64)


def events(bus: EventBus) -> list[dict]:
    got: list[dict] = []
    bus.subscribe("debug.state", lambda e: got.append(dict(e.data)))
    return got


# --- up: the board's OpenOCD, gdb over the claim forward -----------------------------------------


def test_up_runs_openocd_on_the_board_and_forwards_its_gdb_port(lab, stub, debug):
    rig = lab()
    st = debug.up(rig.session)
    assert st.state == "up" and st.where == "board" and st.cores == ("cpu0",)
    assert st.gdb_port == rig.claim_port("gdb0") and st.gdb_ports == (st.gdb_port,)
    assert st.telnet_port == st.tcl_port == 0                    # never forwarded
    assert "on the board" in st.detail and st.config == ("interface/mps3_jtagbb.cfg",
                                                         "target/nanosoc.cfg")
    assert rig.launcher.words() == ["status", "up"]               # detected once, then up
    assert rig.launcher.calls[-1] == ["mps3-debug", "up", "--rm", "nanosoc", "--json"]
    assert stub.runs() == [] and rig.jtag.accepted == 0           # this PC's OpenOCD: never
    assert greeting(st.gdb_port) == b"board-gdb cpu0\n"            # the board's gdb server


def test_twin_on_board_false_keeps_this_pcs_openocd(lab, stub, debug, monkeypatch):
    on_board(monkeypatch, "false")
    rig = lab()
    st = debug.up(rig.session)
    assert st.state == "up" and st.where == "host" and st.telnet_port and st.tcl_port
    assert rig.launcher.calls == [] and len(stub.runs()) == 1 and rig.jtag.accepted == 1


def test_the_claim_forward_carries_gdb0_and_gdb1_on_loopback(lab, stub, debug):
    assert CL.LOCKED_FORWARDS["gdb0"] == 3333 and CL.LOCKED_FORWARDS["gdb1"] == 3334
    rig = lab()
    debug.up(rig.session)
    argv = rig.ssh.board_launches()[-1]
    specs = forward_specs(argv)
    for port in GDB_PORTS:
        spec = next(s for s in specs if s.endswith(f":127.0.0.1:{port}"))
        assert spec.startswith("127.0.0.1:")                      # the local end: loopback only
    assert rig.session.claim.forward_status()["users"] == ["debug-onboard"]


def test_twin_telnet_and_tcl_never_leave_the_board(lab, stub, debug):
    rig = lab()
    debug.up(rig.session)
    remote = {int(s.rsplit(":", 1)[1]) for s in forward_specs(rig.ssh.board_launches()[-1])}
    assert not remote & {4444, 6666}
    assert all(s.startswith("127.0.0.1:") for s in forward_specs(rig.ssh.board_launches()[-1]))


# --- status and down ---------------------------------------------------------------------------


def test_status_asks_the_board_and_shows_the_forwarded_ports(lab, debug):
    rig = lab()
    up = debug.up(rig.session)
    st = debug.status(rig.session)
    assert (st.state, st.where, st.gdb_ports) == ("up", "board", up.gdb_ports)
    assert rig.launcher.words() == ["status", "up", "status"]


def test_twin_status_before_up_says_down_on_the_board_in_one_call(lab, debug):
    rig = lab()
    st = debug.status(rig.session)
    assert (st.state, st.where, st.gdb_port) == ("down", "board", 0)
    assert "not running" in st.detail
    assert rig.launcher.words() == ["status"]            # the detection answered the status


def test_down_stops_the_boards_openocd_and_lets_the_forward_go(lab, debug, bus):
    rig = lab()
    seen = events(bus)
    debug.up(rig.session)
    st = debug.down(rig.session)
    assert (st.state, st.where) == ("down", "board") and rig.launcher.state == "down"
    assert rig.launcher.words()[-1] == "down"
    assert rig.session.claim.forward_status() is None               # never lingers
    assert [e["state"] for e in seen] == ["starting", "up", "down"]
    assert all(e["where"] == "board" for e in seen)


def test_twin_down_with_nothing_running_asks_nothing_down(lab, debug):
    rig = lab()
    st = debug.down(rig.session)
    assert (st.state, st.where) == ("down", "board")
    assert rig.launcher.words() == ["status"]                        # it said down: no "down"


def test_debug_down_stops_a_boards_openocd_it_did_not_start(lab, debug):
    rig = lab()
    rig.launcher._up("nanosoc")                  # someone started it on the board (by hand)
    st = debug.status(rig.session)
    assert st.state == "up" and st.gdb_port == 0 and "not opened here" in st.detail
    debug.down(rig.session)
    assert rig.launcher.state == "down"


def test_twin_closing_the_board_never_stops_one_it_did_not_start(lab, debug):
    rig = lab()
    rig.launcher._up("nanosoc")
    debug.status(rig.session)
    debug.release(rig.session)                   # the engine closing the board
    assert rig.launcher.state == "up" and "down" not in rig.launcher.words()
    # ... and one it did start, it stops
    rig2 = lab()
    debug.up(rig2.session)
    debug.release(rig2.session)
    assert rig2.launcher.state == "down"


# --- already up, multicore ---------------------------------------------------------------------


def test_up_twice_is_already_up_exit_8(lab, debug):
    rig = lab()
    first = debug.up(rig.session)
    with pytest.raises(AlreadyError) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.ALREADY and str(first.gdb_port) in exc.value.hint
    assert rig.launcher.words().count("up") == 1


def test_twin_a_session_already_on_the_board_is_attached_not_refused(lab, debug):
    rig = lab()
    rig.launcher._up("nanosoc")                  # the launcher's up is idempotent: already
    st = debug.up(rig.session)
    assert st.state == "up" and "already running there" in st.detail
    assert greeting(st.gdb_port) == b"board-gdb cpu0\n"


def test_two_cores_two_gdb_ports(lab, debug):
    rig = lab(boot_rm_id=MULTICORE_RM)
    st = debug.up(rig.session)
    assert st.cores == ("cpu0", "cpu1")
    assert st.gdb_ports == (rig.claim_port("gdb0"), rig.claim_port("gdb1"))
    assert [greeting(p) for p in st.gdb_ports] == [b"board-gdb cpu0\n", b"board-gdb cpu1\n"]
    assert rig.launcher.calls[-1][:4] == ["mps3-debug", "up", "--rm", "nanosoc_multicore"]


def test_twin_one_core_one_port(lab, debug):
    rig = lab()
    st = debug.up(rig.session)
    assert len(st.gdb_ports) == 1 and st.cores == ("cpu0",)


# --- no launcher, malformed answers --------------------------------------------------------------


def test_no_launcher_in_auto_falls_back_to_this_pcs_openocd(lab, stub, debug):
    rig = lab(installed=False)
    st = debug.up(rig.session)
    assert st.where == "host" and len(stub.runs()) == 1 and rig.jtag.accepted == 1
    debug.down(rig.session)
    debug.status(rig.session)
    assert rig.launcher.words() == ["status"]                   # asked once per session


def test_twin_no_launcher_with_on_board_true_is_unavailable_12(lab, stub, debug, monkeypatch):
    on_board(monkeypatch, "true")
    rig = lab(installed=False)
    with pytest.raises(UnavailableError) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.UNAVAILABLE and "not installed" in exc.value.reason
    assert "mps3-debug" in exc.value.hint and stub.runs() == []


def test_malformed_detection_falls_back_to_this_pc_in_auto(lab, stub, debug):
    rig = lab(malformed=True)
    assert debug.up(rig.session).where == "host" and len(stub.runs()) == 1


def test_twin_malformed_detection_with_on_board_true_is_12(lab, stub, debug, monkeypatch):
    on_board(monkeypatch, "true")
    rig = lab(malformed=True)
    with pytest.raises(UnavailableError, match="mps3-debug/1") as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.UNAVAILABLE and stub.runs() == []


def test_a_malformed_answer_to_up_is_action_failed_6(lab, stub, debug):
    rig = lab(malformed=("up",))
    with pytest.raises(Exception) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.ACTION_FAILED and "mps3-debug/1" in exc.value.message
    assert stub.runs() == []                     # the launcher was there: no silent fallback


def test_an_ssh_that_fails_in_auto_is_not_remembered_and_keeps_this_pcs_path(lab, stub, debug):
    rig = lab()
    rig.launcher.ssh_fails = True
    assert debug.up(rig.session).where == "host" and len(stub.runs()) == 1
    debug.down(rig.session)
    rig.launcher.ssh_fails = False               # the board answers again: asked again
    assert debug.up(rig.session).where == "board"


def test_twin_an_ssh_that_fails_with_on_board_true_is_unreachable_7(lab, stub, debug,
                                                                    monkeypatch):
    on_board(monkeypatch, "true")
    rig = lab()
    rig.launcher.ssh_fails = True
    with pytest.raises(UnreachableError, match="mps3-debug status") as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.UNREACHABLE and stub.runs() == []


def test_parse_reply_is_tolerant_and_its_twin_refuses_what_is_not_the_launcher():
    ok = OB.parse_reply('motd\n{"schema":"mps3-debug/1","state":"up","cores":[{"name":"cpu0",'
                        '"gdb_port":3333}],"new_key":{"x":1},"pid":"7"}\n')
    assert ok is not None and ok.cores == (("cpu0", 3333),) and ok.pid == 7
    pretty = OB.parse_reply(json.dumps({"schema": "mps3-debug/1.2", "state": "down"}, indent=2))
    assert pretty is not None and pretty.state == "down"
    for bad in ("", "{not json", '{"schema":"mps3-debug/2","state":"up"}',
                '{"state":"up"}', '{"schema":"mps3-debug/1"}', "[1,2]"):
        assert OB.parse_reply(bad) is None, bad


# --- refusals: not claimed, the lease, no_dap, no_cfg, busy ----------------------------------------


def test_on_board_true_on_an_unclaimed_board_is_refused_15(lab, debug, monkeypatch):
    on_board(monkeypatch, "true")
    rig = lab(claimed=False, pinned=False, observed=False)
    with pytest.raises(RefusedError) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.REFUSED and "board claim TARGET" in exc.value.hint
    assert "not claimed" in exc.value.message and rig.launcher.calls == []


def test_on_board_true_on_a_board_claimed_by_another_key_is_refused_with_the_claim_hint(
        lab, debug, monkeypatch):
    on_board(monkeypatch, "true")
    rig = lab(pinned=False, observed=True)
    with pytest.raises(ClaimLockedError) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.REFUSED and exc.value.hint == CL.LOCKED_HINT
    assert rig.launcher.calls == []


def test_twin_auto_on_an_unclaimed_board_keeps_this_pcs_path(lab, stub, debug):
    rig = lab(claimed=False, pinned=False, observed=False)
    st = debug.up(rig.session)
    assert st.where == "host" and rig.launcher.calls == [] and len(stub.runs()) == 1


class _Hub:
    host = "hub.claimed-lock.test"
    target = "mps3_01_pl"

    def close(self) -> None:
        pass


class _Leases:
    def __init__(self, lease: dict | None) -> None:
        self.lease = lease
        self.asked = 0

    def view(self, hub: Any) -> dict:
        self.asked += 1
        return {"lease": self.lease}


def test_on_board_up_needs_the_lease_held_here_4(lab, debug):
    rig = lab()
    rig.session.hub = _Hub()
    debug.leases = _Leases({"holder": "alice@lab", "mine": False, "here": False})
    with pytest.raises(HeldError) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.HELD and exc.value.holder == "alice@lab"
    assert "up" not in rig.launcher.words()


def test_twin_the_lease_held_here_goes_ahead(lab, debug):
    rig = lab()
    rig.session.hub = _Hub()
    debug.leases = _Leases({"holder": "you@here", "mine": True, "here": True})
    assert debug.up(rig.session).state == "up" and debug.leases.asked == 1


def test_a_design_without_a_debug_port_is_13(lab, debug):
    rig = lab(boot_rm_id=GREYBOX_RM)
    with pytest.raises(NothingOnTargetError) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.NOTHING_ON_TARGET and "greybox" in exc.value.message
    assert rig.launcher.calls[-1][:4] == ["mps3-debug", "up", "--rm", "greybox"]
    assert rig.session.claim.forward_status() is None


def test_an_unknown_design_passes_rm_auto_and_no_cfg_is_14(lab, debug):
    rig = lab(boot_rm_id=UNKNOWN_RM, identify_answers=False)
    with pytest.raises(IncompatibleError) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.INCOMPATIBLE and "pass --rm NAME" in exc.value.hint
    assert rig.launcher.calls[-1][:4] == ["mps3-debug", "up", "--rm", "auto"]


def test_twin_a_known_design_passes_its_name_never_auto(lab, debug):
    rig = lab(identify_answers=False)            # auto would fail: the name is what works
    assert debug.up(rig.session).state == "up"
    assert all("auto" not in c for c in rig.launcher.calls)


def test_the_launchers_busy_is_held_4_naming_the_peer(lab, debug):
    rig = lab()
    rig.launcher.busy = {"by": "openocd (pid 77)", "peer": "192.168.10.1"}
    with pytest.raises(HeldError) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.HELD
    assert exc.value.holder == "openocd (pid 77) from 192.168.10.1"
    assert rig.session.claim.forward_status() is None


def test_twin_not_busy_goes_ahead(lab, debug):
    rig = lab()
    rig.launcher.busy = None
    assert debug.up(rig.session).state == "up"


def test_a_start_that_fails_is_6_with_the_log_tail(lab, debug, bus):
    rig = lab()
    seen = events(bus)
    rig.launcher.fail_start = ["Info : mps3_jtagbb", "Error: JTAG scan chain interrogation failed"]
    with pytest.raises(Exception) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.ACTION_FAILED
    assert "scan chain interrogation failed" in exc.value.message
    assert seen[-1]["state"] == "failed" and seen[-1]["where"] == "board"


# --- this PC's OpenOCD turned away while the board runs its own -----------------------------------


def test_this_pcs_openocd_refused_while_the_board_runs_its_own_is_held_with_the_hint(
        lab, stub, debug, monkeypatch):
    on_board(monkeypatch, "false")
    rig = lab()
    rig.launcher._up("nanosoc")                  # the board's OpenOCD holds JTAG
    rig.jtag.refuse_next = 10                    # 6921 refuses generically (harnessd < 2.1)
    with pytest.raises(HeldError) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.HELD and exc.value.hint == OB.BUSY_HINT
    assert "on-board OpenOCD" in exc.value.message and rig.launcher.words() == ["status"]


def test_twin_refused_with_no_session_on_the_board_keeps_the_plain_held(
        lab, stub, debug, monkeypatch):
    on_board(monkeypatch, "false")
    rig = lab()
    rig.jtag.refuse_next = 10
    with pytest.raises(HeldError) as exc:
        debug.up(rig.session)
    assert exc.value.hint != OB.BUSY_HINT and "another debugger" in exc.value.hint


# --- swaps ---------------------------------------------------------------------------------------


def test_a_swap_closes_the_boards_openocd_first_and_reopens_it_after(lab, debug, bus):
    rig = lab()
    debug.up(rig.session)
    bus.publish(Event("deploy.started", rig.board_id, {"overlay": "nanosoc"}))
    assert rig.launcher.state == "down" and rig.launcher.words()[-1] == "down"
    st = debug.status(rig.session)
    assert st.state == "down" and OB.SWAP_DETAIL in st.detail
    bus.publish(Event("deploy.done", rig.board_id, {"verified": True}))
    for t in debug.threads:
        t.join(10)
    assert debug.status(rig.session).state == "up" and rig.launcher.words().count("up") == 2


def test_twin_a_failed_swap_does_not_reopen_it(lab, debug, bus):
    rig = lab()
    debug.up(rig.session)
    bus.publish(Event("deploy.started", rig.board_id, {"overlay": "nanosoc"}))
    bus.publish(Event("deploy.failed", rig.board_id, {"stage": "push", "reason": "x"}))
    assert debug.threads == [] and rig.launcher.words().count("up") == 1
    assert debug.status(rig.session).state == "down"


def test_a_program_asks_a_known_launcher_down_even_with_no_session_of_ours(lab, debug, bus):
    rig = lab()
    rig.launcher._up("nanosoc")                  # someone's session on the board
    debug.status(rig.session)                    # this session has seen the launcher
    bus.publish(Event("deploy.started", rig.board_id, {"overlay": "nanosoc_multicore"}))
    assert rig.launcher.state == "down" and rig.launcher.words()[-1] == "down"
    assert debug.threads == []                   # not ours: nothing to reopen


def test_twin_a_program_on_a_board_never_asked_sends_no_ssh(lab, debug, bus):
    rig = lab()
    bus.publish(Event("deploy.started", rig.board_id, {"overlay": "nanosoc"}))
    assert rig.launcher.calls == []


def test_the_launchers_swap_stop_reads_closed_for_the_swap_not_failed(lab, debug):
    rig = lab()
    debug.up(rig.session)
    rig.launcher.swapped(MULTICORE_RM)           # a swap HM did not see: the watchdog stopped it
    st = debug.status(rig.session)
    assert (st.state, st.where) == ("down", "board") and OB.SWAP_DETAIL in st.detail
    assert rig.session.claim.forward_status() is None


def test_twin_another_openocd_exit_is_failed_with_its_words(lab, debug):
    rig = lab()
    debug.up(rig.session)
    rig.launcher.died = "openocd exited: target lost"
    st = debug.status(rig.session)
    assert st.state == "failed" and "target lost" in st.detail and "swap" not in st.detail


# --- detect: the IDCODE ---------------------------------------------------------------------------


def test_detect_reads_the_idcode_through_a_one_shot_on_the_board(lab, stub, debug):
    rig = lab()
    assert debug.detect(rig.session) == IDCODE
    assert rig.launcher.words() == ["status", "up", "down"] and rig.launcher.state == "down"
    assert stub.runs() == [] and rig.jtag.accepted == 0


def test_twin_detect_while_up_reads_it_from_the_running_session(lab, debug):
    rig = lab()
    debug.up(rig.session)
    assert debug.detect(rig.session) == IDCODE
    assert rig.launcher.words() == ["status", "up", "status"] and rig.launcher.state == "up"


# --- the setting, the pre-check -------------------------------------------------------------------


def test_the_on_board_setting_is_declared_auto_by_default(monkeypatch):
    from harness_manager.settings.resolve import core_schema

    spec = core_schema().spec("debug.on_board")
    assert (spec.default, spec.choices, spec.env) == ("auto", ("auto", "true", "false"),
                                                      OB.ON_BOARD_ENV)
    assert OB.mode() == "auto"
    on_board(monkeypatch, "false")
    assert OB.mode() == "false"


def test_twin_a_bad_on_board_value_fails_naming_the_variable(monkeypatch):
    on_board(monkeypatch, "maybe")
    with pytest.raises(UsageError, match=OB.ON_BOARD_ENV):
        OB.mode()


def test_auto_never_refuses_before_the_board_opens_for_want_of_a_local_openocd(
        stub, monkeypatch):
    monkeypatch.setenv("STUB_OPENOCD_ADAPTERS", "jlink,hostio4")
    svc = DebugService(None)
    assert svc.openocd() == ""                   # the board may run its own


def test_twin_on_board_false_still_refuses_up_front(stub, monkeypatch):
    monkeypatch.setenv("STUB_OPENOCD_ADAPTERS", "jlink,hostio4")
    on_board(monkeypatch, "false")
    with pytest.raises(UnavailableError, match="no remote_bitbang adapter"):
        DebugService(None).openocd()


# --- DebugStatus stays additive ---------------------------------------------------------------------


def test_debug_status_keeps_its_positional_fields_and_fills_the_new_ones():
    st = DebugStatus("up", 29555, 31155, 31955, ("nanosoc_mps3_jtag.cfg",), 4242)
    assert (st.gdb_ports, st.cores, st.where) == ((29555,), ("cpu0",), "host")
    two = DebugStatus("up", gdb_ports=(40001, 40002), where="board")
    assert two.gdb_port == 40001 and two.cores == ("cpu0", "cpu1")


def test_twin_an_older_services_json_decodes_with_the_defaults():
    from harness_manager.client.codec import from_json

    old = {"state": "up", "gdb_port": 23344, "telnet_port": 23346, "tcl_port": 23347,
           "config": ["a.cfg"], "pid": 9, "detail": ""}
    st = from_json(DebugStatus, old)
    assert (st.gdb_ports, st.cores, st.where) == ((23344,), ("cpu0",), "host")
    new = from_json(DebugStatus, {**old, "gdb_ports": [1, 2], "cores": ["a", "b"],
                                  "where": "board"})
    assert (new.gdb_ports, new.cores, new.where) == ((1, 2), ("a", "b"), "board")


# --- the demo and the daemon's rows ---------------------------------------------------------------


def test_the_demos_linux_board_runs_openocd_on_the_board_through_the_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from harness_manager.daemon.app import create_app
    from harness_manager.demo import DemoEngine
    from harness_manager.demo_showcase import BOARD_LINUX, BOARD_V011
    from tests.integration.test_demo_showcase import TOKEN, Demo

    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path / "not-the-demo"))
    sdir = tmp_path / "demo"
    eng = DemoEngine(speed=0, showcase=True, state_dir=sdir)
    try:
        with TestClient(create_app(eng, token=TOKEN, state_dir=sdir)) as client:
            demo = Demo(client, eng)
            for bid, where in ((BOARD_LINUX, "board"), (BOARD_V011, "host")):   # + its twin
                demo.open(bid)
                demo.job(f"{demo.b(bid)}/debug/up")
                st = demo.get(f"{demo.b(bid)}/debug")
                assert st["state"] == "up" and st["where"] == where
                assert st["gdb_ports"] == [st["gdb_port"]] and st["cores"] == ["cpu0"]
                assert (st["telnet_port"] == 0) == (where == "board")
                down = demo.get(f"{demo.b(bid)}/debug/down", method="POST")
                assert down["state"] == "down" and down["where"] == where
    finally:
        eng.close_all()


DEBUG_JS = (Path(__file__).resolve().parents[2]
            / "src/harness_manager/web/static/js/sections/debug.js")


def test_the_apps_debug_card_says_where_and_builds_each_cores_line():
    from harness_manager.services.debug import gdb_command

    js = DEBUG_JS.read_text(encoding="utf-8")
    assert "OpenOCD runs on the board" in js and "OpenOCD runs on this PC" in js
    line = js.split("export function gdbCmdFor(port) {", 1)[1].split("`", 2)[1]
    assert line.replace("${port}", "40002") == gdb_command(40002)
    # UI v2 (WORKBENCH's Debug card): one row per core, "gdb <core>", each its own attach id
    assert "data-testid=${`debug-attach-${core}`}" in js and 'data-testid="debug-where"' in js


def test_twin_the_card_check_catches_a_drift():
    from harness_manager.services.debug import gdb_command

    drifted = 'arm-none-eabi-gdb -ex "target extended-remote 127.0.0.1:${port}"'
    assert drifted.replace("${port}", "40002") != gdb_command(40002)


# --- the CLI: one attach line per core ----------------------------------------------------------


@pytest.fixture
def cli():
    from harness_manager.cli.engine import set_engine_factory
    from tests.fakes.t5_fake_engine import FakeEngine

    eng = FakeEngine()
    previous = set_engine_factory(lambda _args: eng)
    yield eng
    set_engine_factory(previous)


def run_cli(capsys, *argv: str) -> tuple[int, str]:
    from harness_manager.cli.main import main

    rc = main(list(argv))
    return rc, capsys.readouterr()[0]


def test_cli_prints_one_attach_line_per_core_and_where(cli, capsys):
    from harness_manager.services.debug import gdb_command

    cli.st.debug_state = "up"
    cli.debug._status = lambda: DebugStatus("up", config=("target/nanosoc_multicore.cfg",),
                                            pid=812, gdb_ports=(40001, 40002),
                                            cores=("cpu0", "cpu1"), where="board")
    rc, out = run_cli(capsys, "debug", "status", "127.0.0.1")
    lines = out.splitlines()
    assert rc == 0
    assert "where      the board: OpenOCD runs there; gdb reaches it through the board's SSH" \
        in lines
    assert "gdb        127.0.0.1:40001  cpu0" in lines and "gdb        127.0.0.1:40002  cpu1" in lines
    assert [ln for ln in lines if ln.startswith("attach")] == [
        f"attach     {gdb_command(40001)}", f"attach     {gdb_command(40002)}"]
    assert not any(ln.startswith(("telnet", "tcl")) for ln in lines)       # on the board only
    rc, out = run_cli(capsys, "--json", "debug", "status", "127.0.0.1")
    body = json.loads(out)
    assert body["gdb_command"] == gdb_command(40001)
    assert body["gdb_commands"] == [
        {"core": "cpu0", "gdb_port": 40001, "command": gdb_command(40001)},
        {"core": "cpu1", "gdb_port": 40002, "command": gdb_command(40002)}]
    assert body["status"]["where"] == "board" and body["status"]["gdb_ports"] == [40001, 40002]
    rc, out = run_cli(capsys, "--tsv", "debug", "status", "127.0.0.1")
    row = out.strip().splitlines()[-1].split("\t")
    assert row[-3:] == ["board", "40001;40002", "cpu0;cpu1"]


def test_twin_one_core_on_this_pc_keeps_the_single_lines(cli, capsys):
    from harness_manager.services.debug import gdb_command

    cli.st.debug_state = "up"
    rc, out = run_cli(capsys, "debug", "status", "127.0.0.1")
    lines = out.splitlines()
    assert "gdb        127.0.0.1:29555" in lines                         # no core name
    assert [ln for ln in lines if ln.startswith("attach")] == [f"attach     {gdb_command(29555)}"]
    assert "where      this PC: OpenOCD runs here, on the board's JTAG port (6921)" in lines
    body = json.loads(run_cli(capsys, "--json", "debug", "status", "127.0.0.1")[1])
    assert len(body["gdb_commands"]) == 1 and body["status"]["where"] == "host"
