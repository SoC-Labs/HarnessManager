"""HM 1.2.1 (preview): "Debug via hostio" over the board's OpenOCD route.

Same lab as ``test_debug_onboard`` (the real MPS3 pack on a claimed Linux board, the board's
``mps3-debug`` launcher behind the claim's ssh), with the fake shell's mint-4 ``hostio`` block
and the launcher's ``--via hostio`` as the contract (``hio_debug_json.md``) has it. Each check
has its negative twin.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from harness_manager.core.errors import (
    ActionFailedError,
    AlreadyError,
    ExitCode,
    HeldError,
    IncompatibleError,
    NothingOnTargetError,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.services import debug_onboard as OB
from harness_manager.services.debug import DebugService, gdb_command
from harness_manager_mps3 import claim as CL
from harness_manager_mps3 import tunnel as T
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.claimed_lock import (
    BOARD_IP,
    HUB,
    TRUSTED,
    HubAndBoardSsh,
    board_key_fp,
    pin_claim,
    route_board,
)
from tests.fakes.do_fake_launcher import (
    HOSTIO_CAPS,
    HOSTIO_INIT,
    JTAG_CAPS,
    FakeLauncher,
)
from tests.fakes.lxslots_board import slot_board
from tests.fakes.t4_debug_rig import use_stub
from tests.fakes.t4_rbb_jtag import FakeJtagServer
from tests.integration.test_debug_onboard import NANOSOC_RM, Lab

P_LINES = ('-ex "set remote p-packet off"', '-ex "set remote P-packet off"')


@pytest.fixture
def hlab(monkeypatch) -> Iterator[Any]:
    made: list[Lab] = []

    def make(*, mode: str = "hostio4", claimed: bool = True, pinned: bool = True,
             hostio_feature: bool = True, boot_rm_id: int = NANOSOC_RM,
             **launcher_kw: Any) -> Lab:
        extra: dict[str, Any] = {}
        if hostio_feature:
            extra = {"features": ("hostio",), "hostio": {"mode": mode}}
        fake = slot_board(profile="linux", ssh_claimed=claimed, boot_rm_id=boot_rm_id,
                          slots={"trusted_peer": TRUSTED}, ssh_host_key_sha256=board_key_fp(),
                          **extra)
        jtag = FakeJtagServer().__enter__()
        jtag.claimed = claimed
        ssh = HubAndBoardSsh()
        route_board(ssh, {6900: fake.control_port, 6910: fake.raw_tcp_port, 6921: jtag.port})
        launcher_kw.setdefault("rm_id", boot_rm_id)
        launcher = FakeLauncher(board_ip=BOARD_IP, hub=HUB, serve=True, **launcher_kw)
        for port, srv in launcher.servers.items():
            ssh.routes[("127.0.0.1", port)] = ("127.0.0.1", srv.port)
        monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
        monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
        monkeypatch.setattr(CL, "DEFAULT_RUN", launcher)
        pack = Mps3Pack()
        session = pack.open(pack.candidate_for_host(f"{BOARD_IP}:6900", via=f"ssh:{HUB}"))
        if pinned:
            pin_claim(session)
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
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def debug(bus) -> Iterator[DebugService]:
    svc = DebugService(bus, start_timeout=15, cooldown=0.0)
    yield svc
    svc.close()


@pytest.fixture(autouse=True)
def _stub(monkeypatch, tmp_path):
    return use_stub(monkeypatch, tmp_path)


def sent(rig: Lab) -> list[list[str]]:
    return rig.launcher.calls


# --- up via hostio ---------------------------------------------------------------------------


def test_up_via_hostio_sends_the_flag_and_reports_via_and_capabilities(hlab, debug):
    rig = hlab()
    st = debug.up(rig.session, via="hostio")
    assert st.state == "up" and st.where == "board" and st.via == "hostio"
    assert st.capabilities == tuple(HOSTIO_CAPS) and st.gdb_init == tuple(HOSTIO_INIT)
    assert st.cores == ("cpu0",) and st.gdb_port == rig.claim_port("gdb0")
    assert st.config == ("nanosoc_mps3_hostio4.cfg",)
    assert rig.launcher.calls[-1] == ["mps3-debug", "up", "--via", "hostio", "--rm", "nanosoc",
                                      "--json"]


def test_twin_jtag_is_the_default_and_is_asked_exactly_as_before(hlab, debug):
    rig = hlab()
    st = debug.up(rig.session)
    assert st.via == "jtag" and st.capabilities == tuple(JTAG_CAPS) and st.gdb_init == ()
    assert rig.launcher.calls[-1] == ["mps3-debug", "up", "--rm", "nanosoc", "--json"]
    assert not any("--via" in c for c in rig.launcher.calls)


def test_a_reply_without_via_is_a_jtag_session_with_the_full_set(hlab, debug):
    rig = hlab()
    rig.launcher.legacy = True
    st = debug.up(rig.session)
    assert st.via == "jtag" and st.capabilities == tuple(JTAG_CAPS)
    assert OB.parse_reply('{"schema":"mps3-debug/1","state":"up"}').via == "jtag"


def test_status_of_a_hostio_session_reports_via(hlab, debug):
    rig = hlab()
    debug.up(rig.session, via="hostio")
    st = debug.status(rig.session)
    assert st.state == "up" and st.via == "hostio" and st.capabilities == tuple(HOSTIO_CAPS)


def test_down_stops_either_via_with_the_same_verb(hlab, debug):
    rig = hlab()
    debug.up(rig.session, via="hostio")
    assert debug.down(rig.session).state == "down"
    assert rig.launcher.calls[-1][:2] == ["mps3-debug", "down"] and rig.launcher.state == "down"


def test_an_unknown_via_is_a_usage_error_and_sends_nothing(hlab, debug):
    rig = hlab()
    with pytest.raises(UsageError):
        debug.up(rig.session, via="swd")
    assert sent(rig) == []


# --- refused locally, nothing sent ----------------------------------------------------------------


def test_refused_locally_when_the_pins_are_in_console_mode(hlab, debug):
    rig = hlab(mode="console")
    with pytest.raises(RefusedError) as e:
        debug.up(rig.session, via="hostio")
    assert e.value.message == OB.HOSTIO_MODE_WORDS and "Switch to hostio4" in e.value.hint
    assert e.value.code == ExitCode.REFUSED
    assert sent(rig) == []


def test_twin_hostio4_pins_go_on(hlab, debug):
    rig = hlab(mode="hostio4")
    assert debug.up(rig.session, via="hostio").state == "up" and sent(rig)


def test_refused_locally_when_hostio4_is_only_pending(hlab, debug):
    rig = hlab(mode="console")
    rig.fake.hostio.pending = "hostio4"          # chosen, latches at the next RP release
    with pytest.raises(RefusedError) as e:
        debug.up(rig.session, via="hostio")
    assert e.value.message == OB.HOSTIO_PENDING_WORDS and sent(rig) == []


def test_refused_locally_when_the_board_has_no_hostio_block(hlab, debug):
    rig = hlab(hostio_feature=False)
    with pytest.raises(UnavailableError) as e:
        debug.up(rig.session, via="hostio")
    assert OB.HOSTIO_NO_SUPPORT_WORDS in e.value.message and sent(rig) == []


def test_refused_locally_when_the_board_is_not_claimed_here(hlab, debug):
    rig = hlab(claimed=False, pinned=False)
    with pytest.raises(RefusedError) as e:
        debug.up(rig.session, via="hostio")
    assert e.value.message.startswith(OB.HOSTIO_NEEDS_BOARD) and sent(rig) == []


def test_refused_when_the_claim_is_not_pinned_here(hlab, debug):
    rig = hlab(claimed=True, pinned=False)
    with pytest.raises(RefusedError) as e:
        debug.up(rig.session, via="hostio")
    assert "holds no claim" in e.value.message and sent(rig) == []


def test_hostio_has_no_path_on_this_pc(hlab, debug, monkeypatch):
    monkeypatch.setenv(OB.ON_BOARD_ENV, "false")
    rig = hlab()
    with pytest.raises(RefusedError) as e:
        debug.up(rig.session, via="hostio")
    assert "debug.on_board is false" in e.value.message and sent(rig) == []


# --- the board's refusals, in plain words --------------------------------------------------------


REFUSALS = [
    # (rc, code, board message, hint, busy) -> (error class, words that must show, never shown)
    ((15, "hostio_mode", "the DUT's pins are in console mode, not hostio4", "switch ...", None),
     RefusedError, OB.HOSTIO_MODE_WORDS),
    ((15, "hostio_mode", "the DUT's pins are in console mode; hostio4 is pending and latches at "
                         "the next RP release", "load the design again", None),
     RefusedError, OB.HOSTIO_PENDING_WORDS),
    ((15, "rp_held", "the RP is held in reset or decoupled: the DUT's ADP is not running",
      "load the design", None), RefusedError, OB.HOSTIO_RP_HELD_WORDS),
    ((13, "no_hostio", "the design jtag_only has no hostio4 debug path (no ADP on the hostio4 "
                       "link)", "use the JTAG path", None),
     NothingOnTargetError, "has no hostio debug link"),
    ((13, "no_dap", "no design is loaded (the greybox): nothing to debug over hostio",
      "load a nanoSoC design", None), NothingOnTargetError, "no design is loaded"),
    ((15, "no_adp", "no ADP on 127.0.0.1:6933: nothing listens there", "is the design loaded?",
      None), RefusedError, OB.HOSTIO_NO_ADP_WORDS),
    ((6, "no_adp", "no ADP on 127.0.0.1:6933: OpenOCD's hostio4 driver got no answer (exit 1)",
      "is the design loaded?", None), ActionFailedError, OB.HOSTIO_NO_ADP_WORDS),
    ((15, "hostio_unavailable", "harnessd's `hostio` verb refused: hostio not supported",
      "mint 4 needed", None), UnavailableError, OB.HOSTIO_UNAVAILABLE_WORDS),
    ((14, "no_cfg", "no nanosoc_mps3_hostio4.cfg on the board", "predates", None),
     IncompatibleError, OB.HOSTIO_NO_CFG_WORDS),
    ((4, "busy", "harnessd's control port 127.0.0.1:6900 gave no answer to `hostio`", "close",
      {"by": "6900 control", "peer": None}), HeldError, OB.BUSY_CONTROL_WORDS),
    ((4, "busy", "127.0.0.1:6933 (hostio ADP) is held by another client (127.0.0.1:51060)",
      "close", {"by": "6933 hostio ADP", "peer": "127.0.0.1:51060"}), HeldError,
     "another client holds the DUT's hostio debug link (127.0.0.1:51060)"),
]


@pytest.mark.parametrize("refusal, cls, words", REFUSALS, ids=[r[0][1] + str(r[0][0])
                                                                  for r in REFUSALS])
def test_each_board_refusal_is_plain_words_and_the_right_class(hlab, debug, refusal, cls, words):
    rig = hlab()
    rig.launcher.hostio_refusal = refusal
    with pytest.raises(cls) as e:
        debug.up(rig.session, via="hostio")
    assert type(e.value) is cls and words in e.value.message
    shown = f"{e.value.message} {e.value.hint}"
    assert f"({refusal[1]})" not in shown and f"code {refusal[1]}" not in shown
    assert not debug.status(rig.session).state == "up"


def test_a_design_without_a_hostio_link_is_no_hostio_and_jtag_still_works(hlab, debug):
    rig = hlab(boot_rm_id=0x0100_0042)
    with pytest.raises(NothingOnTargetError) as e:
        debug.up(rig.session, via="hostio", )
    assert "has no hostio debug link" in e.value.message and "jtag_only" in e.value.message
    assert debug.up(rig.session).via == "jtag"             # the twin: JTAG on the same design


def test_busy_when_jtag_is_up_on_the_board(hlab, debug):
    rig = hlab()
    debug.up(rig.session)                                   # ours, JTAG
    with pytest.raises(AlreadyError) as e:                  # HM itself says it before asking
        debug.up(rig.session, via="hostio")
    assert "via JTAG" in e.value.message and "change the via" in e.value.hint


def test_busy_when_a_jtag_session_the_board_holds_is_not_ours(hlab, debug):
    rig = hlab()
    rig.launcher.asked_via = "jtag"
    rig.launcher.state, rig.launcher.via = "up", "jtag"
    rig.launcher.design, rig.launcher.cores, rig.launcher.pid = "nanosoc", ("cpu0",), 900
    with pytest.raises(HeldError) as e:
        debug.up(rig.session, via="hostio")
    assert "a JTAG debug session is already up on the board" in e.value.message
    assert "mps3-debug via" not in e.value.message


def test_twin_busy_when_hostio_is_up_and_jtag_is_asked(hlab, debug):
    rig = hlab()
    rig.launcher.state, rig.launcher.via = "up", "hostio"
    rig.launcher.design, rig.launcher.cores, rig.launcher.pid = "nanosoc", ("cpu0",), 901
    with pytest.raises(HeldError) as e:
        debug.up(rig.session)
    assert "a hostio debug session is already up on the board" in e.value.message


# --- 6900 is single-client (the board's `hostio` verb) ----------------------------------------------


def test_hostio_up_runs_with_this_processs_control_port_gate_held(hlab, debug):
    """HM holds no 6900 connection of its own (every request opens and closes); during the
    hostio ``up`` its control gate is held too, so none of its own requests is in flight."""
    rig = hlab()
    gate = rig.session.shell.gate()
    seen: list[tuple[str, bool]] = []
    rig.launcher.during = lambda w: seen.append((w[1] + ("-hostio" if "--via" in w else ""),
                                                 gate.held))
    debug.up(rig.session, via="hostio")
    assert ("up-hostio", True) in seen and not gate.held         # held during, let go after
    rig.session.shell.call_raw(lambda client, _tap: client.version())   # 6900 usable again


def test_twin_jtag_up_does_not_take_the_control_gate(hlab, debug):
    rig = hlab()
    gate = rig.session.shell.gate()
    seen: list[tuple[str, bool]] = []
    rig.launcher.during = lambda w: seen.append((w[1], gate.held))
    debug.up(rig.session)
    assert ("up", False) in seen and not any(held for _w, held in seen)


def test_the_mode_check_leaves_no_6900_connection_open(hlab, debug):
    rig = hlab()
    gate = rig.session.shell.gate()
    debug.up(rig.session, via="hostio")
    assert not gate.held


# --- the design's own hostio flag (`version --json` designs[]) ---------------------------------


def test_a_design_the_board_says_has_no_hostio_is_refused_without_an_up(hlab, debug):
    rig = hlab(boot_rm_id=0x0100_0005)
    rig.launcher.hostio_designs.discard("nanosoc_upy")
    with pytest.raises(NothingOnTargetError) as e:
        debug.up(rig.session, via="hostio")
    assert e.value.message == "nanosoc_upy has no hostio"
    assert not any(c[1] == "up" for c in rig.launcher.calls)


def test_an_older_launcher_without_the_flag_is_allowed_and_the_board_decides(hlab, debug):
    rig = hlab()
    rig.launcher.legacy = True
    assert OB.parse_design_hostio('{"schema":"mps3-debug/1","state":"down","designs":'
                                  '[{"name":"nanosoc"}]}', "nanosoc") is None
    assert debug.up(rig.session, via="hostio").state == "up"


def test_hostio_design_for_the_api(hlab, debug):
    rig = hlab(boot_rm_id=0x0100_0005)
    rig.launcher.hostio_designs.discard("nanosoc_upy")
    assert debug.hostio_design(rig.session) == {"design": "nanosoc_upy", "hostio": False}
    rig2 = hlab()
    assert debug.hostio_design(rig2.session) == {"design": "nanosoc", "hostio": True}


# --- swaps -----------------------------------------------------------------------------------------


def test_a_swap_reopens_a_hostio_session_as_hostio_and_a_jtag_one_as_jtag(hlab, debug, bus):
    rig = hlab()
    debug.up(rig.session, via="hostio")
    bus.publish(Event("deploy.started", rig.board_id, {"overlay": "nanosoc"}))
    bus.publish(Event("deploy.done", rig.board_id, {"verified": True}))
    for t in debug.threads:
        t.join(10)
    assert debug.status(rig.session).via == "hostio"
    ups = [c for c in rig.launcher.calls if c[1] == "up"]
    assert ups[-1][2:4] == ["--via", "hostio"]              # reopened as hostio
    debug.down(rig.session)
    debug.up(rig.session)                                   # twin: JTAG
    bus.publish(Event("deploy.started", rig.board_id, {"overlay": "nanosoc"}))
    bus.publish(Event("deploy.done", rig.board_id, {"verified": True}))
    for t in debug.threads:
        t.join(10)
    assert debug.status(rig.session).via == "jtag"


# --- gdb ------------------------------------------------------------------------------------------


def test_gdb_commands_carry_both_p_packet_lines_on_hostio_only():
    on = gdb_command(40001, gdb_init=HOSTIO_INIT)
    off = gdb_command(40001)
    for line in P_LINES:
        assert line in on and line not in off
    assert on.index(P_LINES[0]) < on.index("target extended-remote")     # before the connect
    assert off == 'arm-none-eabi-gdb -ex "set remotetimeout 60" -ex "target extended-remote ' \
                  '127.0.0.1:40001"'


def test_hostio_help_says_it_cannot_verify_in_place():
    assert "verify_image" in OB.HOSTIO_VERIFY_NOTE and "dump_image" in OB.HOSTIO_VERIFY_NOTE
    assert "need JTAG" in OB.HOSTIO_LIMITS


# --- the CLI ------------------------------------------------------------------------------------


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


def hostio_status(**kw):
    from harness_manager.core.services import DebugStatus

    return DebugStatus("up", pid=812, gdb_ports=(40001,), cores=("cpu0",), where="board",
                       config=("nanosoc_mps3_hostio4.cfg",), via="hostio",
                       capabilities=tuple(HOSTIO_CAPS), gdb_init=tuple(HOSTIO_INIT), **kw)


def test_cli_status_shows_via_capabilities_and_the_p_packet_attach_line(cli, capsys):
    import json

    cli.st.debug_state = "up"
    cli.debug._status = hostio_status
    rc, out = run_cli(capsys, "debug", "status", "127.0.0.1")
    lines = out.splitlines()
    assert rc == 0
    assert "via        hostio (memory and load only)" in lines
    assert "can        mem_read mem_write load" in lines
    assert f"limits     {OB.HOSTIO_LIMITS}" in lines
    attach = next(ln for ln in lines if ln.startswith("attach"))
    assert all(p in attach for p in P_LINES)
    body = json.loads(run_cli(capsys, "--json", "debug", "status", "127.0.0.1")[1])
    assert body["status"]["via"] == "hostio"
    assert body["status"]["capabilities"] == HOSTIO_CAPS
    assert all(p in body["gdb_command"] for p in P_LINES)
    assert all(p in body["gdb_commands"][0]["command"] for p in P_LINES)
    row = run_cli(capsys, "--tsv", "debug", "status", "127.0.0.1")[1].strip().splitlines()[-1]
    assert row.split("\t")[-2:] == ["hostio", "mem_read;mem_write;load"]


def test_twin_cli_status_of_a_jtag_session_has_the_full_set_and_no_p_packet(cli, capsys):
    import json

    cli.st.debug_state = "up"
    body = json.loads(run_cli(capsys, "--json", "debug", "status", "127.0.0.1")[1])
    assert body["status"]["via"] == "jtag" and body["status"]["capabilities"] == JTAG_CAPS
    assert "p-packet" not in body["gdb_command"]
    out = run_cli(capsys, "debug", "status", "127.0.0.1")[1]
    assert "via        jtag" in out.splitlines() and "limits" not in out


def test_cli_up_via_hostio_passes_via_and_plain_up_does_not(cli, capsys):
    got: list[dict] = []

    def up(session, **kw):
        got.append(kw)
        return hostio_status()

    cli.debug.up = up
    assert run_cli(capsys, "debug", "up", "127.0.0.1", "--via", "hostio", "--for", "0.01")[0] == 0
    assert got == [{"via": "hostio"}]
    got.clear()
    cli.debug.up = lambda session: got.append({"plain": True}) or hostio_status()
    assert run_cli(capsys, "debug", "up", "127.0.0.1", "--for", "0.01")[0] == 0
    assert got == [{"plain": True}]                       # JTAG: called exactly as before


def test_cli_via_still_names_the_route_for_ssh_and_hub(cli, capsys):
    from harness_manager.cli.main import make_parser, split_debug_via

    args = make_parser().parse_args(["debug", "up", "127.0.0.1", "--via", "ssh:lab"])
    split_debug_via(args)
    assert args.via == "ssh:lab" and args.debug_via == "jtag"
    args = make_parser().parse_args(["debug", "up", "127.0.0.1", "--via", "hostio"])
    split_debug_via(args)
    assert not hasattr(args, "via") and args.debug_via == "hostio"


# --- the API ------------------------------------------------------------------------------------


def _api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from harness_manager.daemon.app import create_app
    from harness_manager.demo import DemoEngine
    from tests.integration.test_demo_showcase import TOKEN, Demo

    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path / "not-the-demo"))
    sdir = tmp_path / "demo"
    eng = DemoEngine(speed=0, showcase=True, state_dir=sdir)
    return eng, TestClient(create_app(eng, token=TOKEN, state_dir=sdir)), Demo


def test_api_debug_up_takes_via_and_status_carries_via_and_capabilities(tmp_path, monkeypatch):
    from harness_manager.demo_showcase import BOARD_LINUX, BOARD_V011
    from tests.integration.test_demo_showcase import H

    eng, client, Demo = _api(tmp_path, monkeypatch)
    try:
        with client:
            demo = Demo(client, eng)
            demo.open(BOARD_LINUX)
            path = f"{demo.b(BOARD_LINUX)}/debug"
            demo.job(f"{path}/up", {"via": "hostio"})
            st = demo.get(path)
            assert st["via"] == "hostio" and st["capabilities"] == HOSTIO_CAPS
            assert st["gdb_init"] == HOSTIO_INIT and st["cores"] == ["cpu0"]
            demo.get(f"{path}/down", method="POST")
            demo.job(f"{path}/up")                          # the twin: no body is JTAG
            st = demo.get(path)
            assert st["via"] == "jtag" and st["capabilities"] == JTAG_CAPS and st["gdb_init"] == []
            demo.get(f"{path}/down", method="POST")
            r = client.post(f"/api/v1{path}/up", headers=H, json={"via": "swd"})
            assert r.status_code == 400 and "via must be jtag or hostio" in r.text
            demo.open(BOARD_V011)                           # bare metal: no board OpenOCD
            demo.job(f"{demo.b(BOARD_V011)}/debug/up", {"via": "hostio"}, ok=False)
    finally:
        eng.close_all()
