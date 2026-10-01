"""FIX-PACK-7 DEBUG-DOWN-FIRST, unit: the deploy service asks the board's OpenOCD down first.

Board-agnostic: a scripted deploy adapter (``test_t2_deploy_service``) and a scripted on-board
route (``OnBoardRoute``: the launcher's answers per verb), no sockets, no ssh. Each check has
its negative twin.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from harness_manager.core.errors import ExitCode, RefusedError, UnavailableError, UnreachableError
from harness_manager.core.events import EventBus
from harness_manager.services import debug_onboard as OB
from harness_manager.services.deploy import DeployService
from tests.unit.test_t2_deploy_service import (
    NANOSOC,
    FakeSession,
    ScriptedAdapter,
    with_item,
)

DOWN = {"state": "down", "already": False}
ALREADY = {"state": "down", "already": True}


def answer(rc: int, body: Any) -> SimpleNamespace:
    """The launcher's reply: ``body`` a dict is its JSON (behind a banner line), a str is raw."""
    text = body if isinstance(body, str) else \
        "Linux mps3 6.6.0\n" + json.dumps({"schema": OB.SCHEMA, **body}) + "\n"
    return SimpleNamespace(returncode=rc, stdout=text, stderr="")


class Route:
    """An ``OnBoardRoute`` whose launcher answers are scripted per verb."""

    launcher = "mps3-debug"

    def __init__(self, *, plan: str = OB.READY, down: Any = (0, DOWN),
                 version: Any = (0, {"launcher": "1.0.0"})) -> None:
        self.state = plan
        self.replies = {"down": down, "version": version}
        self.calls: list[str] = []

    def plan(self):
        return self.state, (None if self.state == OB.READY
                            else UnavailableError("debug_dut", "bare metal"))

    def run(self, verb: str, *, rm: str = "", timeout: float = 30.0) -> Any:
        self.calls.append(verb)
        got = self.replies[verb]
        if isinstance(got, BaseException):
            raise got
        return answer(*got)

    def design_name(self) -> str:
        return ""

    def hold(self) -> dict[int, int]:
        return {}

    def release(self) -> None:
        pass

    def describe(self) -> str:
        return "mps3-debug (scripted)"


class Board(FakeSession):
    """The deploy test's session, with a debug adapter whose ``onboard()`` is ``route``."""

    def __init__(self, route: Route | None, adapter: ScriptedAdapter | None = None) -> None:
        super().__init__(adapter or ScriptedAdapter())
        self.debug = SimpleNamespace(onboard=lambda: route) if route is not None else None


@pytest.fixture
def seen():
    bus = EventBus()
    got: list[Any] = []
    bus.subscribe("deploy.*", got.append)
    return SimpleNamespace(engine=SimpleNamespace(bus=bus), events=got)


def topics(events) -> list[str]:
    return [e.topic for e in events]


def deploy(seen, route: Route | None, **kw: Any) -> tuple[Board, Any]:
    board = Board(route)
    return board, DeployService(seen.engine).deploy(board, NANOSOC, **kw)


# --- down ok -----------------------------------------------------------------------------------


def test_a_deploy_asks_the_boards_openocd_down_before_deploy_started(seen):
    route = Route()
    order: list[str] = []
    seen.engine.bus.subscribe("deploy.started", lambda e: order.append(f"started:{route.calls}"))
    board, result = deploy(seen, route)
    assert result.verified and board.deploy.deployed == ["nanosoc"]
    assert route.calls == ["down"]                          # once, and no version asked
    assert order == ["started:['down']"]                    # the down came BEFORE the start
    assert topics(seen.events)[0] == "deploy.started" and "deploy.warning" not in topics(seen.events)


def test_twin_a_route_that_is_not_ready_asks_nothing_and_deploys_unchanged(seen):
    for plan in (OB.NONE, OB.REFUSED):                      # bare metal; not claimed here
        route = Route(plan=plan)
        board, result = deploy(seen, route)
        assert result.verified and route.calls == []
    board, result = deploy(seen, None)                      # no route at all
    assert result.verified and board.deploy.deployed == ["nanosoc"]


def test_already_down_goes_on(seen):
    route = Route(down=(0, ALREADY))
    _board, result = deploy(seen, route)
    assert result.verified and route.calls == ["down"]
    assert OB.ask_down(route).already and OB.ask_down(route).word == "already down"


def test_no_launcher_127_goes_on_unchanged(seen):
    route = Route(down=(127, "sh: mps3-debug: not found\n"))
    _board, result = deploy(seen, route)
    assert result.verified and route.calls == ["down"]
    assert OB.ask_down(route).no_launcher and "deploy.warning" not in topics(seen.events)


def test_twin_another_exit_refuses_15(seen):
    route = Route(down=(6, {"state": "failed", "error": {"code": "openocd_exit",
                                                         "message": "kill failed"}}))
    board = Board(route)
    with pytest.raises(RefusedError) as exc:
        DeployService(seen.engine).deploy(board, NANOSOC)
    assert exc.value.code == ExitCode.REFUSED
    assert "`mps3-debug down` failed before the swap (exit 6, openocd_exit: kill failed)" \
        in exc.value.message
    assert board.deploy.deployed == []


# --- a failed down refuses before anything touches the board -----------------------------------


def test_an_ssh_failure_refuses_15_before_deploy_started(seen):
    route = Route(down=UnreachableError("ssh to the board for `mps3-debug down --json` failed: "
                                        "Connection timed out"))
    board = Board(route)
    with pytest.raises(RefusedError) as exc:
        DeployService(seen.engine).deploy(board, NANOSOC)
    err = exc.value
    assert err.code == ExitCode.REFUSED == 15
    assert err.message.startswith("`mps3-debug down` failed before the swap (the board's SSH "
                                  "failed: ssh to the board")
    assert err.message.endswith("so nothing was programmed")
    assert err.hint == OB.DOWN_FIRST_HINT and "--force" in err.hint and "v2.0.0" in err.hint
    assert err.data["debug_down"]["launcher"] == "mps3-debug"
    assert board.deploy.deployed == []                       # the board was never touched
    assert topics(seen.events) == ["deploy.failed"]          # no deploy.started
    assert seen.events[0].data["stage"] == "preflight"
    assert "mps3-debug down" in seen.events[0].data["reason"]
    assert route.calls == ["down", "version"]                # the lock was looked for


def test_twin_force_warns_and_goes_on(seen, caplog):
    route = Route(down=UnreachableError("ssh: Connection timed out"))
    with caplog.at_level(logging.WARNING, logger="harness_manager.services.deploy"):
        board, result = deploy(seen, route, force=True)
    assert result.verified and board.deploy.deployed == ["nanosoc"]
    assert route.calls == ["down"]                           # forced: no version asked
    assert topics(seen.events)[:2] == ["deploy.warning", "deploy.started"]
    warning = seen.events[0].data["message"]
    assert warning.startswith("swapping anyway (forced) although `mps3-debug down` failed")
    assert "may still drive JTAG during the reconfiguration" in warning
    assert seen.events[0].data["overlay"] == "nanosoc"
    assert warning in caplog.text


def test_an_unparsable_reply_refuses(seen):
    route = Route(down=(0, "mps3-debug: usage\n{not json"))
    with pytest.raises(RefusedError, match="no mps3-debug/1 answer, exit 0"):
        deploy(seen, route)


def test_twin_exit_0_with_a_state_that_is_not_down_refuses(seen):
    route = Route(down=(0, {"state": "up"}))
    with pytest.raises(RefusedError, match="it answered state 'up'"):
        deploy(seen, route)


def test_a_refused_preflight_never_asks_the_board_down(seen):
    from harness_manager.core.model import Check
    from harness_manager.services.deploy import ITEM_FILES

    route = Route()
    board = Board(route, ScriptedAdapter(items={"nanosoc": with_item(ITEM_FILES, Check.MISMATCH,
                                                                    "crc bad")}))
    with pytest.raises(RefusedError):
        DeployService(seen.engine).deploy(board, NANOSOC)
    assert route.calls == []                                  # nobody's OpenOCD was stopped


# --- harnessd v2.1's lock: one capability, never a version -------------------------------------


@pytest.mark.parametrize("caps", [["harnessd-lock"], ["xvc", "harnessd-lock", "other"]])
def test_the_lock_capability_turns_a_failed_down_into_a_warning(seen, caps):
    route = Route(down=(6, {"state": "failed"}), version=(0, {"capabilities": caps}))
    board, result = deploy(seen, route)
    assert result.verified and route.calls == ["down", "version"]
    warning = seen.events[0].data["message"]
    assert seen.events[0].topic == "deploy.warning"
    assert "going on: the board's harnessd lock (harnessd-lock) keeps OpenOCD off JTAG" in warning
    assert OB.LOCK_CAPABILITY == "harnessd-lock"


@pytest.mark.parametrize("version", [
    (0, {"launcher": "2.1.0"}),                                   # the field absent: no lock
    (0, {"launcher": "2.1.0", "capabilities": ["xvc", "lock"]}),  # present, not the string
    (0, {"launcher": "9.9.9", "capabilities": "harnessd-lock"}),  # not a list
    (2, {"capabilities": ["harnessd-lock"]}),                     # not exit 0
    (127, "sh: mps3-debug: not found\n"),
], ids=["absent", "without-the-string", "not-a-list", "exit-2", "no-answer"])
def test_twin_no_lock_capability_refuses(seen, version):
    route = Route(down=(6, {"state": "failed"}), version=version)
    with pytest.raises(RefusedError):
        deploy(seen, route)
    assert route.calls == ["down", "version"]


def test_the_lock_is_keyed_on_the_capability_never_on_a_version_string():
    assert OB.parse_capabilities(answer(0, {"launcher": "2.1.0"}).stdout) == ()
    assert OB.parse_capabilities(answer(0, {"capabilities": ["harnessd-lock"]}).stdout) == \
        ("harnessd-lock",)
    assert OB.parse_capabilities("no json here") is None
    # a status object before it (the combined output E-OCD's OCD0 shows) never hides it
    both = answer(0, {"launcher": "2.1", "capabilities": ["harnessd-lock"]}).stdout + \
        json.dumps({"schema": OB.SCHEMA, "state": "down"}) + "\n"
    assert OB.parse_capabilities(both) == ("harnessd-lock",)


# --- the setting and restore ----------------------------------------------------------------------


def test_on_board_false_still_asks_the_board_down(seen, monkeypatch):
    monkeypatch.setenv(OB.ON_BOARD_ENV, "false")             # someone else's OpenOCD may be up
    route = Route()
    deploy(seen, route)
    assert route.calls == ["down"]


def test_restore_baseline_asks_too_and_takes_force(seen):
    route = Route(down=UnreachableError("no route to host"))
    board = Board(route)
    with pytest.raises(RefusedError):
        DeployService(seen.engine).restore_baseline(board)
    assert board.deploy.deployed == []
    result = DeployService(seen.engine).restore_baseline(board, force=True)
    assert result.verified and board.deploy.deployed == ["greybox"]


def test_the_engines_debug_service_is_asked_when_it_has_one(seen):
    asked: list[tuple[str, bool]] = []

    def down_first(session, *, force=False):
        asked.append((session.candidate.board_id, force))
        return OB.DownFirst(asked=True, ok=True)

    engine = SimpleNamespace(bus=seen.engine.bus, debug=SimpleNamespace(down_first=down_first))
    route = Route()
    DeployService(engine).deploy(Board(route), NANOSOC, force=True)
    assert asked == [("fake@1", True)] and route.calls == []  # the service's own path asked


def test_twin_an_unavailable_debug_service_falls_back_to_the_route(seen):
    from harness_manager.services._unavailable import UnavailableService

    engine = SimpleNamespace(bus=seen.engine.bus, debug=UnavailableService("debug"))
    route = Route()
    DeployService(engine).deploy(Board(route), NANOSOC)
    assert route.calls == ["down"]
