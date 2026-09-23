"""Engine (Team T1) on a scriptable fake pack. Each check has a negative twin.

The virtual-MPS3 end-to-end tests are in tests/integration/test_t1_engine_virtual.py.
"""

from __future__ import annotations

import gc
import json
import os
import socket
from pathlib import Path

import pytest

from harness_manager import engine as engine_mod
from harness_manager.core.errors import (
    AbsentError,
    AlreadyError,
    ExitCode,
    HeldError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.events import EventBus
from harness_manager.core.model import BoardIdentity, LinkKind
from harness_manager.core.pack import ProbeHints
from harness_manager.core.services import Engine as EngineProtocol
from harness_manager.core.services import EngineConfig
from harness_manager.core.session import SessionLock
from harness_manager.engine import Engine, resolve_state_dir
from harness_manager.services._unavailable import UnavailableService, is_unavailable
from harness_manager.services.store import ContentStore
from harness_manager.services.telemetry import TelemetryService
from tests.fakes.t1_fakes import FakePack, candidate

DEAD_PID = 2**22 + 12345          # above Linux pid_max: never a live process


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def make(state: Path, *packs: FakePack, bus: EventBus | None = None) -> Engine:
    packs = packs or (FakePack(),)
    return Engine(EngineConfig(state_dir=state), packs={p.name: p for p in packs}, bus=bus)


def recorder(bus: EventBus) -> list:
    seen: list = []
    bus.subscribe("*", seen.append)
    return seen


def write_lock(state: Path, board_id: str, pid: int, note: str = "") -> Path:
    path = SessionLock(board_id, lock_dir=state / "locks").path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"user": "someone", "host": socket.gethostname(), "pid": pid,
                                "since": 0.0, "note": note}))
    return path


# -- state dir ----------------------------------------------------------------------------

def test_state_dir_config_beats_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path / "env"))
    assert resolve_state_dir(EngineConfig(state_dir=tmp_path / "cfg")) == tmp_path / "cfg"
    assert resolve_state_dir(EngineConfig()) == tmp_path / "env"
    assert Engine().state_dir == tmp_path / "env"


def test_state_dir_falls_back_to_home(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HARNESS_MANAGER_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    assert resolve_state_dir() == tmp_path / "home" / ".config" / "harness-manager"


def test_engine_satisfies_the_frozen_protocol(state: Path):
    eng = make(state)
    assert isinstance(eng, EngineProtocol)
    assert isinstance(eng.store, ContentStore) and eng.store.root == state / "store"
    assert isinstance(eng.telemetry, TelemetryService)


# -- packs --------------------------------------------------------------------------------

def test_packs_come_from_the_registry_with_overrides(state: Path):
    eng = Engine(EngineConfig(state_dir=state,
                              pack_overrides={"mps3": {"console_ports": {"uart0": 1234}}}))
    packs = eng.packs()
    assert "mps3" in packs and packs["mps3"]._console_ports == {"uart0": 1234}
    # Negative twin: without the override the pack keeps its real ports.
    assert Engine(EngineConfig(state_dir=state)).packs()["mps3"]._console_ports["uart0"] == 6930


def test_override_for_a_missing_pack_is_a_usage_error(state: Path):
    eng = Engine(EngineConfig(state_dir=state, pack_overrides={"haps": {}}))
    with pytest.raises(UsageError, match="haps"):
        eng.packs()


def test_override_with_a_bad_setting_is_a_usage_error(state: Path):
    eng = Engine(EngineConfig(state_dir=state, pack_overrides={"mps3": {"colour": "red"}}))
    with pytest.raises(UsageError, match="mps3"):
        eng.packs()


def test_candidate_for_uses_the_named_pack(state: Path):
    eng = make(state, FakePack(name="fake"))
    assert eng.candidate_for("10.0.0.1", pack="fake").board_id == "fake@10.0.0.1"
    with pytest.raises(AbsentError) as exc:
        eng.candidate_for("10.0.0.1", pack="haps")
    assert exc.value.code == ExitCode.ABSENT and "fake" in exc.value.hint


# -- probe --------------------------------------------------------------------------------

def test_probe_asks_every_pack_and_publishes_board_found(state: Path):
    bus = EventBus()
    seen = recorder(bus)
    a = FakePack(name="a", found=[candidate("a@1", pack="a")])
    b = FakePack(name="b", found=[candidate("b@1", pack="b"), candidate("b@2", pack="b")])
    found = make(state, a, b, bus=bus).probe(ProbeHints(scan_usb=False))
    assert [c.board_id for c in found] == ["a@1", "b@1", "b@2"]
    assert [(e.topic, e.board_id) for e in seen] == [
        ("board.found", "a@1"), ("board.found", "b@1"), ("board.found", "b@2")]
    assert seen[0].data["links"] == [{"kind": "ethernet", "address": "ethernet:a@1",
                                      "detail": ""}]


def test_probe_deduplicates_by_board_id_and_merges_links(state: Path):
    eth = candidate("x@1", LinkKind.ETHERNET)
    usb = candidate("x@1", LinkKind.USB_SERIAL, LinkKind.ETHERNET)
    found = make(state, FakePack(found=[eth, usb, eth])).probe(ProbeHints())
    assert len(found) == 1
    assert [lk.kind for lk in found[0].links] == [LinkKind.ETHERNET, LinkKind.USB_SERIAL]


def test_probe_nothing_found_publishes_nothing(state: Path):
    bus = EventBus()
    seen = recorder(bus)
    assert make(state, FakePack(found=[]), bus=bus).probe() == [] and seen == []


def test_a_pack_that_fails_to_probe_does_not_hide_the_others(state: Path):
    broken = FakePack(name="broken", probe_error=UnreachableError("cable fell out"))
    good = FakePack(name="good", found=[candidate("g@1", pack="good")])
    assert [c.board_id for c in make(state, broken, good).probe()] == ["g@1"]
    assert broken.probes == 1


def test_bad_probe_hints_are_reported_not_swallowed(state: Path):
    pack = FakePack(probe_error=UsageError("bad host spelling"))
    with pytest.raises(UsageError):
        make(state, pack).probe()


# -- open / session / close ---------------------------------------------------------------

def test_open_takes_the_lock_and_close_releases_it(state: Path):
    bus = EventBus()
    seen = recorder(bus)
    pack = FakePack()
    eng = make(state, pack, bus=bus)
    cand = candidate("fake@1")
    session = eng.open(cand, note="flashing")
    assert eng.session("fake@1") is session and eng.open_boards() == ["fake@1"]
    owner = eng.lock_owner("fake@1")
    assert owner is not None and owner.pid == os.getpid() and owner.note == "flashing"
    eng.close("fake@1")
    assert session.closed == 1 and eng.lock_owner("fake@1") is None
    assert [e.topic for e in seen] == ["session.opened", "session.closed"]
    assert seen[0].data["note"] == "flashing" and seen[0].data["pack"] == "fake"


def test_session_of_a_board_that_is_not_open_is_absent(state: Path):
    eng = make(state)
    with pytest.raises(AbsentError):
        eng.session("fake@1")
    with pytest.raises(AbsentError):
        eng.info("fake@1")
    eng.open(candidate("fake@1"))
    eng.close("fake@1")
    with pytest.raises(AbsentError):                      # ...and again after close
        eng.session("fake@1")


def test_close_of_a_board_that_is_not_open_is_a_quiet_no_op(state: Path):
    bus = EventBus()
    seen = recorder(bus)
    make(state, bus=bus).close("fake@nowhere")
    assert seen == []


def test_opening_twice_in_one_engine_is_already(state: Path):
    eng = make(state)
    eng.open(candidate("fake@1"))
    with pytest.raises(AlreadyError) as exc:
        eng.open(candidate("fake@1"))
    assert exc.value.code == ExitCode.ALREADY


def test_open_with_an_unknown_pack_is_absent_and_takes_no_lock(state: Path):
    eng = make(state)
    with pytest.raises(AbsentError):
        eng.open(candidate("zzz@1", pack="zzz"))
    assert eng.lock_owner("zzz@1") is None


def test_pack_open_failure_releases_the_lock(state: Path):
    pack = FakePack(open_error=UnreachableError("no route"))
    eng = make(state, pack)
    with pytest.raises(UnreachableError):
        eng.open(candidate("fake@1"))
    assert eng.lock_owner("fake@1") is None and eng.open_boards() == []
    pack.open_error = None
    other = make(state, pack)                             # nobody is left holding it
    other.open(candidate("fake@1"))
    other.close_all()


def test_context_manager_closes_everything(state: Path):
    pack = FakePack()
    with make(state, pack) as eng:
        eng.open(candidate("fake@1"))
        eng.open(candidate("fake@2"))
    assert eng.open_boards() == [] and [s.closed for s in pack.opened] == [1, 1]
    assert not any((state / "locks").glob("*.lock"))


# -- lock contention ------------------------------------------------------------------

def test_second_engine_in_the_same_process_is_held(state: Path):
    first, second = make(state), make(state)
    first.open(candidate("fake@1"), note="first engine")
    with pytest.raises(HeldError) as exc:
        second.open(candidate("fake@1"))
    assert exc.value.code == ExitCode.HELD and "first engine" in exc.value.holder
    # Negative twin: a different board is free, and so is this one once released.
    second.open(candidate("fake@2"))
    first.close("fake@1")
    second.open(candidate("fake@1"))
    second.close_all()


def test_live_other_process_holding_the_lock_is_held(state: Path):
    # Simulate another live process: the lock names our parent's pid (alive).
    write_lock(state, "fake@1", os.getppid(), note="other process deploying")
    eng = make(state)
    with pytest.raises(HeldError) as exc:
        eng.open(candidate("fake@1"))
    assert f"pid {os.getppid()}" in exc.value.holder
    assert "other process deploying" in exc.value.holder
    assert eng.open_boards() == []


def test_stale_lock_from_a_dead_process_is_taken_over(state: Path):
    path = write_lock(state, "fake@1", DEAD_PID, note="crashed")
    eng = make(state)
    eng.open(candidate("fake@1"), note="took over")
    owner = eng.lock_owner("fake@1")
    assert owner.pid == os.getpid() and owner.note == "took over"
    eng.close("fake@1")
    assert not path.exists()


def test_an_engine_dropped_without_closing_does_not_hold_the_board_forever(state: Path):
    dropped = make(state)
    dropped.open(candidate("fake@1"))
    del dropped
    gc.collect()
    eng = make(state)
    eng.open(candidate("fake@1"))            # same pid, dead engine: the claim is stale
    eng.close_all()


# -- info ---------------------------------------------------------------------------------

def test_info_negotiates_capabilities_from_links_and_features(state: Path):
    pack = FakePack(identity=BoardIdentity(board_type="fake", shell_id="0x1", features=()))
    eng = make(state, pack)
    eng.open(candidate("fake@1", LinkKind.ETHERNET))
    info = eng.info("fake@1")
    assert info.capabilities == {"identify"}
    assert info.unavailable == {"reboot_board": "needs the Debug USB cable",
                                "reset_shell": "needs harness firmware with 'reboot'"}
    # Twin: a board reporting the feature over a USB link gains both.
    pack2 = FakePack(name="f2", identity=BoardIdentity(board_type="fake", features=("reboot",)))
    eng2 = make(state, pack2)
    eng2.open(candidate("f2@1", LinkKind.ETHERNET, LinkKind.USB_SERIAL, pack="f2"))
    info2 = eng2.info("f2@1")
    assert info2.capabilities == {"identify", "reboot_board", "reset_shell"}
    assert info2.unavailable == {}


def test_info_publishes_board_identity_only_when_it_changes(state: Path):
    bus = EventBus()
    seen = recorder(bus)
    pack = FakePack(identity=BoardIdentity(board_type="fake", rm_id="0x00000000"))
    eng = make(state, pack, bus=bus)
    eng.open(candidate("fake@1"))
    eng.info("fake@1")
    eng.info("fake@1")
    pack.opened[0].set_identity(BoardIdentity(board_type="fake", rm_id="0x01000001"))
    eng.info("fake@1")
    ident = [e.data["rm_id"] for e in seen if e.topic == "board.identity"]
    assert ident == ["0x00000000", "0x01000001"]


# -- lazy services ------------------------------------------------------------------------

@pytest.fixture
def lazy(monkeypatch):
    def point(attr: str, module: str, cls: str) -> None:
        monkeypatch.setitem(engine_mod.LAZY_SERVICES, attr,
                            (module, cls, engine_mod.LAZY_SERVICES[attr][2]))
    return point


@pytest.mark.parametrize("attr", ["deploy", "consoles", "debug"])
def test_missing_service_module_is_an_unavailable_stub(state: Path, lazy, attr: str):
    lazy(attr, "harness_manager.services.t1_not_installed", "Nope")
    svc = getattr(make(state), attr)
    assert is_unavailable(svc)
    with pytest.raises(UnavailableError) as exc:
        svc.up(object())
    assert exc.value.code == ExitCode.UNAVAILABLE == 12
    assert exc.value.capability == attr and exc.value.reason == "not installed in this build"


def test_present_service_is_built_with_the_engine(state: Path, lazy):
    lazy("deploy", "tests.fakes.t1_services", "RecordingService")
    eng = make(state)
    svc = eng.deploy
    assert not is_unavailable(svc) and svc.engine is eng
    assert eng.deploy is svc                               # resolved once


def test_service_whose_dependency_is_missing_is_unavailable_with_the_reason(
        state: Path, lazy, tmp_path: Path, monkeypatch):
    # A broken install: the service module exists, but its own dependency does not.
    # Written at test time, so pytest never tries to collect it.
    (tmp_path / "t1_broken_service_mod.py").write_text(
        "import harness_manager_t1_dependency_that_is_not_installed\n"
        "class DeployService:\n"
        "    def __init__(self, engine): pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    lazy("deploy", "t1_broken_service_mod", "DeployService")
    svc = make(state).deploy
    with pytest.raises(UnavailableError) as exc:
        svc.deploy(None, None)
    assert "failed to load" in exc.value.reason
    assert "harness_manager_t1_dependency_that_is_not_installed" in exc.value.reason


def test_service_module_without_the_class_is_unavailable(state: Path, lazy):
    lazy("debug", "tests.fakes.t1_services", "DebugService")
    with pytest.raises(UnavailableError, match="has no DebugService"):
        make(state).debug.status(None)


def test_service_whose_constructor_raises_is_unavailable(state: Path, lazy):
    lazy("consoles", "tests.fakes.t1_services", "ExplodingService")
    with pytest.raises(UnavailableError, match="constructor exploded"):
        make(state).consoles.names(None)


def test_stub_private_attributes_behave_normally():
    stub = UnavailableService("deploy")
    with pytest.raises(AttributeError):
        stub._private                                    # noqa: B018
    assert "deploy" in repr(stub)


def test_close_releases_consoles_and_debug_that_are_in_use(state: Path, lazy):
    lazy("consoles", "tests.fakes.t1_services", "RecordingService")
    lazy("debug", "tests.fakes.t1_services", "RecordingService")
    eng = make(state)
    session = eng.open(candidate("fake@1"))
    consoles, debug = eng.consoles, eng.debug
    eng.close("fake@1")
    assert consoles.closed_boards == ["fake@1"] and debug.downed == [session]


def test_close_does_not_build_services_nobody_used(state: Path, lazy):
    lazy("consoles", "tests.fakes.t1_services", "ExplodingService")
    eng = make(state)
    eng.open(candidate("fake@1"))
    eng.close("fake@1")
    assert "consoles" not in eng._services


def test_a_service_failing_to_let_go_does_not_stop_the_close(state: Path, lazy):
    lazy("consoles", "tests.fakes.t1_services", "FailingReleaseService")
    lazy("debug", "tests.fakes.t1_services", "FailingReleaseService")
    bus = EventBus()
    seen = recorder(bus)
    pack = FakePack()
    eng = make(state, pack, bus=bus)
    eng.open(candidate("fake@1"))
    eng.consoles, eng.debug                               # noqa: B018 - put them in use
    eng.close("fake@1")
    assert pack.opened[0].closed == 1 and eng.lock_owner("fake@1") is None
    assert seen[-1].topic == "session.closed"
