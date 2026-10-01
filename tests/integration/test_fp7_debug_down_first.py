"""FIX-PACK-7 DEBUG-DOWN-FIRST, integration: before every swap the board's OpenOCD is asked
down over the claim's SSH, whoever started it; a failed down refuses the deploy (15).

Three layers, each check with its negative twin:

- the real MPS3 pack's on-board route (``Mps3OnBoard``) on a board this Harness Manager
  claimed, the board's ``mps3-debug`` behind the claim's one-shot ssh (``FakeLauncher``), the
  real ``DebugService`` and ``DeployService``. The swap itself is a scripted adapter on that
  session (``Swapping``), which records what the board's OpenOCD was doing when the swap began
  and tells the launcher (its watchdog) the rm_id changed. Nothing leaves 127.0.0.1;
- the CLI (``program|restore --force``, the refusal's line, the warning) over the T5 fake engine;
- the daemon's API (``force`` in the deploy and restore bodies) over the demo's Linux board.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from harness_manager.core.errors import ExitCode, RefusedError
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import Check
from harness_manager.core.pack import DeployResult, OverlayRef, PreflightItem
from harness_manager.services import debug_onboard as OB
from harness_manager.services.debug import DebugService
from harness_manager.services.deploy import ITEM_FILES, DeployService
from tests.integration import test_debug_onboard as _onboard
from tests.integration import test_demo_showcase as _showcase

# the fixtures: DEBUG-ONBOARD's lab (the real pack, claimed, the launcher behind the claim's
# ssh), its DebugService and bus; the showcase demo over the real daemon app (``api``)
lab, debug, bus, stub = _onboard.lab, _onboard.debug, _onboard.bus, _onboard.stub
api = _showcase.showcase
on_board = _onboard.on_board

UPY = OverlayRef(name="nanosoc_upy", rm_id="0x01000005", static_id="0x44ee76d5", source="t")


# --- the swap on the lab's real session ------------------------------------------------------------


class SwapAdapter:
    """A deploy adapter on the real session: the preflight passes, the swap records the board's
    OpenOCD state as it began and tells the launcher's watchdog."""

    def __init__(self, board: Swapping) -> None:
        self.board = board
        self.met: list[str] = []          # the launcher's state when each swap began
        self.verified = True

    def overlays(self):
        return [UPY]

    def preflight(self, overlay):
        return [PreflightItem(ITEM_FILES, Check.OK, "crc ok")]

    def baseline(self):
        return None

    def deploy(self, overlay, progress=None):
        launcher = self.board.rig.launcher
        self.met.append(launcher.state)
        launcher.swapped(int(overlay.rm_id, 16))
        self.board.rm_id = overlay.rm_id
        return DeployResult(rm_id=overlay.rm_id, verified=self.verified, seconds=0.1,
                            transport="tcp")


class Swapping:
    """The lab's MPS3 session (its debug adapter, claim and on-board route) with ``SwapAdapter``
    as its deploy adapter, and the rm_id the swap landed."""

    def __init__(self, rig: Any) -> None:
        self.rig = rig
        self.rm_id = ""
        self.deploy = SwapAdapter(self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.rig.session, name)

    def identity(self):
        ident = self.rig.session.identity()
        return replace(ident, rm_id=self.rm_id) if self.rm_id else ident


def deploys(bus: EventBus, debug: DebugService | None = None) -> DeployService:
    return DeployService(SimpleNamespace(bus=bus, debug=debug) if debug is not None
                         else SimpleNamespace(bus=bus))


def heard(bus: EventBus, pattern: str = "*") -> list[Event]:
    got: list[Event] = []
    bus.subscribe(pattern, got.append)
    return got


def settle(debug: DebugService) -> None:
    for t in debug.threads:
        t.join(15)


# --- gap (c): an OpenOCD this Harness Manager never saw ----------------------------------------------


def test_a_program_stops_an_openocd_started_by_hand_before_the_swap(lab, debug, bus):
    rig = lab()
    rig.launcher._up("nanosoc")                   # someone ran mps3-debug up over ssh
    board = Swapping(rig)
    result = deploys(bus, debug).deploy(board, UPY)
    assert result.verified
    assert board.deploy.met == ["down"]           # the swap began with OpenOCD stopped
    assert rig.launcher.words() == ["down"]       # one call: no status, no detect first
    assert rig.launcher.calls[0] == ["mps3-debug", "down", "--json"]
    assert debug.threads == []                    # not ours: nothing to reopen


def test_twin_without_a_debug_service_the_route_is_asked_all_the_same(lab, bus):
    rig = lab()
    rig.launcher._up("nanosoc")
    board = Swapping(rig)
    assert deploys(bus).deploy(board, UPY).verified       # the CLI's in-process engine shape
    assert board.deploy.met == ["down"] and rig.launcher.words() == ["down"]


def test_twin_an_unclaimed_board_is_never_asked(lab, debug, bus):
    rig = lab(claimed=False, pinned=False)
    rig.launcher._up("nanosoc")
    board = Swapping(rig)
    assert deploys(bus, debug).deploy(board, UPY).verified
    assert rig.launcher.calls == [] and board.deploy.met == ["up"]     # today's path


def test_already_down_goes_on(lab, debug, bus):
    rig = lab()
    board = Swapping(rig)
    assert deploys(bus, debug).deploy(board, UPY).verified
    assert rig.launcher.words() == ["down"] and board.deploy.met == ["down"]


def test_no_launcher_127_goes_on(lab, debug, bus):
    rig = lab(installed=False)
    board = Swapping(rig)
    events = heard(bus, "deploy.*")
    assert deploys(bus, debug).deploy(board, UPY).verified
    assert rig.launcher.words() == ["down"]
    assert "deploy.warning" not in [e.topic for e in events]


def test_twin_a_malformed_down_refuses_15(lab, debug, bus):
    rig = lab(malformed=("down",))
    board = Swapping(rig)
    with pytest.raises(RefusedError, match="no mps3-debug/1 answer"):
        deploys(bus, debug).deploy(board, UPY)
    assert board.deploy.met == []


# --- a failed down refuses before anything touches the board ------------------------------------------


def test_the_boards_ssh_failing_refuses_15_before_deploy_started(lab, debug, bus):
    rig = lab()
    rig.launcher._up("nanosoc")
    rig.launcher.ssh_fails = True
    board = Swapping(rig)
    events = heard(bus, "deploy.*")
    with pytest.raises(RefusedError) as exc:
        deploys(bus, debug).deploy(board, UPY)
    assert exc.value.code == ExitCode.REFUSED
    assert exc.value.message.startswith("`mps3-debug down` failed before the swap (the board's "
                                        "SSH failed: ssh to the board for `mps3-debug down "
                                        "--json` failed")
    assert exc.value.hint == OB.DOWN_FIRST_HINT
    assert [e.topic for e in events] == ["deploy.failed"]
    assert events[0].data["stage"] == "preflight"
    assert board.deploy.met == []                 # never swapped


def test_twin_force_swaps_with_a_warning(lab, debug, bus):
    rig = lab()
    rig.launcher._up("nanosoc")
    rig.launcher.fail_down = (6, "openocd did not stop (pid 812)")
    board = Swapping(rig)
    events = heard(bus, "deploy.*")
    assert deploys(bus, debug).deploy(board, UPY, force=True).verified
    assert board.deploy.met == ["up"]             # forced: it went on with OpenOCD still up
    assert [e.topic for e in events][:2] == ["deploy.warning", "deploy.started"]
    assert "exit 6, openocd_exit: openocd did not stop (pid 812)" in events[0].data["message"]
    assert "version" not in rig.launcher.words()  # forced: the lock is not asked


def test_the_v21_lock_capability_goes_on_with_a_warning(lab, debug, bus):
    rig = lab()
    rig.launcher.fail_down = (6, "openocd did not stop")
    rig.launcher.capabilities = ["xvc", OB.LOCK_CAPABILITY]
    board = Swapping(rig)
    events = heard(bus, "deploy.warning")
    assert deploys(bus, debug).deploy(board, UPY).verified
    assert rig.launcher.words() == ["down", "version"]
    assert "harnessd lock (harnessd-lock)" in events[0].data["message"]


@pytest.mark.parametrize("caps", [None, [], ["xvc", "lock"]], ids=["absent", "empty", "other"])
def test_twin_without_the_lock_capability_it_refuses(lab, debug, bus, caps):
    rig = lab()
    rig.launcher.fail_down = (6, "openocd did not stop")
    rig.launcher.capabilities = caps
    board = Swapping(rig)
    with pytest.raises(RefusedError, match="exit 6"):
        deploys(bus, debug).deploy(board, UPY)
    assert rig.launcher.words() == ["down", "version"] and board.deploy.met == []


def test_on_board_false_still_asks_the_board_down(lab, debug, bus, monkeypatch):
    on_board(monkeypatch, "false")
    rig = lab()
    rig.launcher._up("nanosoc")
    board = Swapping(rig)
    assert deploys(bus, debug).deploy(board, UPY).verified
    assert board.deploy.met == ["down"]


# --- Harness Manager's own session: closed once, reopened after a verified swap ------------------------


def test_hms_own_session_is_closed_once_and_reopens_after_a_verified_swap(lab, debug, bus):
    rig = lab()
    first = debug.up(rig.session)
    states = heard(bus, "debug.state")
    board = Swapping(rig)
    assert deploys(bus, debug).deploy(board, UPY).verified
    settle(debug)
    assert board.deploy.met == ["down"]
    assert rig.launcher.words().count("down") == 1              # no second down at deploy.started
    closed = [e.data for e in states if e.data["state"] == "down"]
    assert closed and closed[0]["detail"] == OB.SWAP_REASON and closed[0]["where"] == "board"
    again = debug.status(rig.session)
    assert again.state == "up" and again.where == "board" and again.pid != first.pid
    assert [e.data["state"] for e in states][-1] == "up"


def test_twin_a_refused_deploy_leaves_hms_session_open(lab, debug, bus):
    rig = lab()
    debug.up(rig.session)
    rig.launcher.fail_down = (6, "openocd did not stop")
    board = Swapping(rig)
    with pytest.raises(RefusedError):
        deploys(bus, debug).deploy(board, UPY)
    assert debug.onboard.live(rig.board_id) is not None          # still ours, still forwarded
    assert rig.session.claim.forward_status() is not None
    assert debug.threads == []


def test_twin_a_failed_swap_closes_it_and_says_not_reopened(lab, debug, bus):
    rig = lab()
    debug.up(rig.session)
    states = heard(bus, "debug.state")
    board = Swapping(rig)
    board.deploy.verified = False
    with pytest.raises(Exception, match="did not verify"):
        deploys(bus, debug).deploy(board, UPY)
    assert debug.threads == []
    last = states[-1].data
    assert last["state"] == "down" and last["detail"].startswith("not reopened: the swap failed")
    assert last["where"] == "board"


# --- the CLI ------------------------------------------------------------------------------------------


@pytest.fixture
def cli():
    from harness_manager.cli.engine import set_engine_factory
    from tests.fakes.t5_fake_engine import FakeEngine

    eng = FakeEngine()
    previous = set_engine_factory(lambda _args: eng)
    yield eng
    set_engine_factory(previous)


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    from harness_manager.cli.main import main

    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_cli_program_and_restore_force_pass_force(cli, capsys):
    cli.st.deploy_warning = "swapping anyway (forced) although `mps3-debug down` failed (x)"
    rc, _out, err = run_cli(capsys, "program", "127.0.0.1", "nanosoc", "--yes", "--force")
    assert rc == 0 and cli.st.deploy_forces == [True]
    assert "deploy: warning: swapping anyway (forced) although `mps3-debug down` failed (x)" \
        in err
    rc, _out, _err = run_cli(capsys, "restore", "127.0.0.1", "--force")
    assert rc == 0 and cli.st.restore_forces == [True]


def test_twin_without_force_nothing_is_forced(cli, capsys):
    rc, _out, err = run_cli(capsys, "program", "127.0.0.1", "nanosoc", "--yes")
    assert rc == 0 and cli.st.deploy_forces == [False] and "warning" not in err
    rc, _out, _err = run_cli(capsys, "restore", "127.0.0.1")
    assert rc == 0 and cli.st.restore_forces == [False]


def test_cli_the_refusal_is_exit_15_with_the_hint(cli, capsys):
    cli.st.raises["deploy.deploy"] = OB.DownFirst(
        asked=True, ok=False, launcher="mps3-debug",
        why="the board's SSH failed: ssh: connect to host 192.168.11.101 port 22: Connection "
            "timed out").refusal()
    rc, _out, err = run_cli(capsys, "program", "127.0.0.1", "nanosoc", "--yes")
    assert rc == ExitCode.REFUSED == 15
    assert "`mps3-debug down` failed before the swap (the board's SSH failed" in err
    assert "retry, or add --force to swap anyway (on Linux v2.0.0 OpenOCD on the board may " \
           "still drive JTAG during the reconfiguration)" in err
    rc, out, _err = run_cli(capsys, "--json", "program", "127.0.0.1", "nanosoc", "--yes")
    body = json.loads(out)
    assert body["error"]["name"] == "REFUSED" and body["error"]["data"]["debug_down"]["force"]


# --- the API: force in the deploy and restore bodies -----------------------------------------------------


def test_api_a_failed_down_fails_the_job_15_and_force_goes_on(api):
    from harness_manager.demo_showcase import BOARD_LINUX

    api.open(BOARD_LINUX)
    B = api.b(BOARD_LINUX)
    api.engine.set_debug_down(BOARD_LINUX, "the board's SSH failed: Connection timed out")
    events: list[Event] = []
    api.engine.bus.subscribe("deploy.*", events.append)
    job = api.job(f"{B}/deploy", {"overlay": "nanosoc_upy"}, ok=False)
    err = job["error"]
    assert err["code"] == 15 and err["name"] == "REFUSED"
    assert err["message"].startswith("`mps3-debug down` failed before the swap (the board's SSH "
                                     "failed: Connection timed out)")
    assert err["hint"] == OB.DOWN_FIRST_HINT and err["data"]["debug_down"]["launcher"] == "mps3-debug"
    assert [e.topic for e in events] == ["deploy.failed"]
    assert events[0].data["stage"] == "preflight"
    job = api.job(f"{B}/deploy", {"overlay": "nanosoc_upy", "force": True})
    assert job["result"]["verified"] is True
    assert [e.topic for e in events][1:3] == ["deploy.warning", "deploy.started"]
    assert api.engine.called("deploy.debug_down")[-2:] == [(BOARD_LINUX, False),
                                                           (BOARD_LINUX, True)]
    api.job(f"{B}/restore", {}, ok=False)                       # restore: refused too
    assert api.job(f"{B}/restore", {"force": True})["result"]["rm_id"] == "0x00000000"


def test_twin_api_a_board_whose_down_works_needs_no_force_and_a_bad_force_is_400(api):
    from harness_manager.demo_showcase import BOARD_LINUX, BOARD_V011

    api.open(BOARD_LINUX)
    api.open(BOARD_V011)
    job = api.job(f"{api.b(BOARD_LINUX)}/deploy", {"overlay": "nanosoc_upy"})
    assert job["result"]["verified"] is True
    assert api.engine.called("deploy.debug_down") == [(BOARD_LINUX, False)]
    api.job(f"{api.b(BOARD_V011)}/deploy", {"overlay": "nanosoc_upy"})
    assert api.engine.called("deploy.debug_down") == [(BOARD_LINUX, False)]   # bare metal: no
    bad = api.get(f"{api.b(BOARD_LINUX)}/deploy", method="POST", status=400,
                  json={"overlay": "nanosoc_upy", "force": "yes"})
    assert bad["error"]["name"] == "USAGE" and "force must be true or false" in bad["error"]["message"]
