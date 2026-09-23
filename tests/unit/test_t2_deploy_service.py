"""T2 unit tests: the board-agnostic DeployService, driven through a scripted adapter.

No sockets. The adapter is a fake that records calls, so every refusal can be
shown to happen BEFORE the adapter's deploy is reached. Each check has a
negative twin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from socharness.core.errors import (
    AbsentError,
    ActionFailedError,
    ExitCode,
    IncompatibleError,
    RefusedError,
    UnavailableError,
    UnreachableError,
)
from socharness.core.events import EventBus
from socharness.core.model import BoardIdentity, Candidate, Check
from socharness.core.pack import DeployAdapter, DeployResult, OverlayRef, PreflightItem
from socharness.core.services import DeployService as DeployServiceProtocol
from socharness.services.deploy import (
    ITEM_CLEARING_FITS,
    ITEM_FILES,
    ITEM_SHELL_ID,
    ITEM_TRANSPORT,
    ITEM_USERCODE,
    DeployService,
    refusal,
)

GREYBOX = OverlayRef(name="greybox", rm_id="0x00000000", static_id="0x3f1a560f", source="g")
NANOSOC = OverlayRef(name="nanosoc", rm_id="0x01000001", static_id="0x3f1a560f", source="n")
STALE = OverlayRef(name="stale", rm_id="0x01000002", static_id="0xdeadbeef", source="s")
BIG = OverlayRef(name="big", rm_id="0x01000003", static_id="0x3f1a560f", source="b")


def ok_items() -> list[PreflightItem]:
    return [
        PreflightItem(ITEM_SHELL_ID, Check.OK, "0x3f1a560f"),
        PreflightItem(ITEM_FILES, Check.OK, "crc ok"),
        PreflightItem(ITEM_TRANSPORT, Check.OK, "tcp+windowed"),
        PreflightItem(ITEM_USERCODE, Check.UNCHECKED, "needs JTAG"),
    ]


def with_item(name: str, check: Check, detail: str) -> list[PreflightItem]:
    return [i for i in ok_items() if i.name != name] + [PreflightItem(name, check, detail)]


@dataclass
class ScriptedAdapter:
    """A DeployAdapter whose answers are scripted per overlay name."""

    board: FakeSession | None = None
    refs: tuple[OverlayRef, ...] = (GREYBOX, NANOSOC, STALE, BIG)
    items: dict[str, list[PreflightItem]] = field(default_factory=dict)
    base: OverlayRef | None = GREYBOX
    verified: bool = True
    lands_as: str | None = None        # rm_id the board reports afterwards (default: the overlay's)
    fail_with: Exception | None = None
    deployed: list[str] = field(default_factory=list)
    stores: list[object] = field(default_factory=list)

    def overlays(self):
        return self.refs

    def preflight(self, overlay):
        return self.items.get(overlay.name, ok_items())

    def baseline(self):
        return self.base

    def use_store(self, store):
        self.stores.append(store)

    def deploy(self, overlay, progress=None):
        self.deployed.append(overlay.name)
        if progress:
            progress("guard", 1, 1)
            progress("push", 0, 512)
            progress("push", 128, 512)
            progress("push", 512, 512)
            progress("verify", 1, 1)
        if self.fail_with is not None:
            raise self.fail_with
        if self.board is not None:
            self.board.rm_id = self.lands_as or overlay.rm_id
        return DeployResult(rm_id=overlay.rm_id, verified=self.verified, seconds=0.5,
                            transport="tcp+windowed")


class FakeSession:
    def __init__(self, adapter: ScriptedAdapter | None, rm_id: str = "0x00000000") -> None:
        self.candidate = Candidate(pack="fake", board_id="fake@1", links=())
        self.rm_id = rm_id
        self.deploy = adapter
        if adapter is not None:
            adapter.board = self

    def identity(self) -> BoardIdentity:
        return BoardIdentity(board_type="fake", shell_id="0x3f1a560f", rm_id=self.rm_id)


@pytest.fixture
def bus_events():
    bus = EventBus()
    seen = []
    bus.subscribe("deploy.*", seen.append)
    return SimpleNamespace(bus=bus), seen


def topics(events) -> list[str]:
    return [e.topic for e in events]


# -- protocol conformance -----------------------------------------------------------


def test_service_and_adapter_satisfy_the_frozen_protocols():
    assert isinstance(DeployService(), DeployServiceProtocol)
    assert isinstance(ScriptedAdapter(), DeployAdapter)


# -- refusal mapping ----------------------------------------------------------------


def test_refusal_is_none_when_nothing_mismatches():
    assert refusal(ok_items(), "x") is None                       # UNCHECKED is not blocking


def test_identity_mismatch_is_incompatible_other_mismatch_is_refused():
    shell = refusal(with_item(ITEM_SHELL_ID, Check.MISMATCH, "0xdeadbeef"), "x")
    usercode = refusal(with_item(ITEM_USERCODE, Check.MISMATCH, "impl run"), "x")
    crc = refusal(with_item(ITEM_FILES, Check.MISMATCH, "crc32 mismatch"), "x")
    assert isinstance(shell, IncompatibleError) and shell.code == ExitCode.INCOMPATIBLE
    assert isinstance(usercode, IncompatibleError)
    assert isinstance(crc, RefusedError) and crc.code == ExitCode.REFUSED
    assert "crc32 mismatch" in str(crc)


def test_mixed_mismatches_report_every_reason_and_identity_wins():
    items = with_item(ITEM_SHELL_ID, Check.MISMATCH, "wrong shell")
    items.append(PreflightItem(ITEM_CLEARING_FITS, Check.MISMATCH, "too big"))
    err = refusal(items, "x")
    assert isinstance(err, IncompatibleError)
    assert "wrong shell" in str(err) and "too big" in str(err)


# -- deploy: success ----------------------------------------------------------------


def test_deploy_success_orders_events_and_confirms_identity(bus_events):
    engine, seen = bus_events
    adapter = ScriptedAdapter()
    session = FakeSession(adapter)
    result = DeployService(engine).deploy(session, NANOSOC)
    assert result.verified and adapter.deployed == ["nanosoc"]
    assert topics(seen) == ["deploy.started"] + ["deploy.progress"] * 5 + ["deploy.done"]
    started, done = seen[0], seen[-1]
    assert started.board_id == "fake@1" and started.data["rm_id"] == "0x01000001"
    # UNCHECKED items are reported, not hidden and not blocking.
    assert {"name": ITEM_USERCODE, "check": "unchecked", "detail": "needs JTAG"} \
        in started.data["preflight"]
    assert [e.data["phase"] for e in seen[1:-1]] == ["guard", "push", "push", "push", "verify"]
    assert [e.data["bytes"] for e in seen[2:5]] == [0, 128, 512]
    assert all(e.data["total"] == 512 for e in seen[2:5])
    assert done.data["rm_id"] == "0x01000001" and done.data["verified"] is True


def test_deploy_without_an_engine_publishes_nothing_and_still_works():
    adapter = ScriptedAdapter()
    assert DeployService(None).deploy(FakeSession(adapter), NANOSOC).verified


def test_engine_store_is_handed_to_the_adapter():
    store = object()
    adapter = ScriptedAdapter()
    DeployService(SimpleNamespace(bus=EventBus(), store=store)).overlays(FakeSession(adapter))
    assert adapter.stores == [store]


def test_no_store_on_the_engine_means_no_handoff():
    adapter = ScriptedAdapter()
    DeployService(SimpleNamespace(bus=EventBus())).overlays(FakeSession(adapter))
    assert adapter.stores == []


# -- deploy: refusals happen before the adapter is asked to push ---------------------


def test_static_id_mismatch_is_incompatible_and_nothing_deploys(bus_events):
    engine, seen = bus_events
    adapter = ScriptedAdapter(items={"stale": with_item(ITEM_SHELL_ID, Check.MISMATCH, "0xdeadbeef")})
    with pytest.raises(IncompatibleError):
        DeployService(engine).deploy(FakeSession(adapter), STALE)
    assert adapter.deployed == []
    assert topics(seen) == ["deploy.failed"]
    assert seen[0].data["stage"] == "preflight" and "0xdeadbeef" in seen[0].data["reason"]


def test_integrity_mismatch_is_refused_and_nothing_deploys(bus_events):
    engine, seen = bus_events
    adapter = ScriptedAdapter(items={"big": with_item(ITEM_CLEARING_FITS, Check.MISMATCH, "300000 B")})
    with pytest.raises(RefusedError):
        DeployService(engine).deploy(FakeSession(adapter), BIG)
    assert adapter.deployed == [] and topics(seen) == ["deploy.failed"]


def test_preflight_error_is_published_and_propagates(bus_events):
    engine, seen = bus_events

    class Unreachable(ScriptedAdapter):
        def preflight(self, overlay):
            raise UnreachableError("no route")

    adapter = Unreachable()
    with pytest.raises(UnreachableError):
        DeployService(engine).deploy(FakeSession(adapter), NANOSOC)
    assert adapter.deployed == [] and topics(seen) == ["deploy.failed"]


# -- deploy: failures after the push --------------------------------------------------


def test_unverified_result_is_action_failed(bus_events):
    engine, seen = bus_events
    with pytest.raises(ActionFailedError, match="verified: false"):
        DeployService(engine).deploy(FakeSession(ScriptedAdapter(verified=False)), NANOSOC)
    assert topics(seen)[0] == "deploy.started" and topics(seen)[-1] == "deploy.failed"
    assert "deploy.done" not in topics(seen)


def test_board_reporting_another_rm_id_afterwards_is_action_failed(bus_events):
    engine, seen = bus_events
    session = FakeSession(ScriptedAdapter(lands_as="0x00000000"))
    with pytest.raises(ActionFailedError, match="expected 0x01000001"):
        DeployService(engine).deploy(session, NANOSOC)
    assert seen[-1].topic == "deploy.failed" and seen[-1].data["stage"] == "confirm"


def test_rm_id_compare_is_by_value_not_text():
    session = FakeSession(ScriptedAdapter(lands_as="0x01000001"))
    ref = OverlayRef(name="nanosoc", rm_id="0x1000001", static_id="0x3f1a560f")  # unpadded
    assert DeployService().deploy(session, ref).verified


def test_adapter_error_is_published_and_propagates(bus_events):
    engine, seen = bus_events
    adapter = ScriptedAdapter(fail_with=ActionFailedError("swap rejected"))
    with pytest.raises(ActionFailedError, match="swap rejected"):
        DeployService(engine).deploy(FakeSession(adapter), NANOSOC)
    assert seen[-1].topic == "deploy.failed" and seen[-1].data["stage"] == "deploy"


# -- compatible ---------------------------------------------------------------------


def test_compatible_gives_a_reason_for_each_incompatible_overlay():
    adapter = ScriptedAdapter(items={
        "stale": with_item(ITEM_SHELL_ID, Check.MISMATCH, "built for 0xdeadbeef"),
        "big": with_item(ITEM_CLEARING_FITS, Check.MISMATCH, "clearing is 300000 B"),
    })
    loadable, reasons = DeployService().compatible(FakeSession(adapter))
    assert [o.name for o in loadable] == ["greybox", "nanosoc"]      # UNCHECKED stays loadable
    assert reasons == {
        "stale": f"{ITEM_SHELL_ID}: built for 0xdeadbeef",
        "big": f"{ITEM_CLEARING_FITS}: clearing is 300000 B",
    }


def test_compatible_keeps_two_reasons_for_two_overlays_with_one_name():
    twin = OverlayRef(name="stale", rm_id="0x01000002", static_id="0x11111111", source="s2")
    adapter = ScriptedAdapter(refs=(STALE, twin),
                              items={"stale": with_item(ITEM_SHELL_ID, Check.MISMATCH, "x")})
    _, reasons = DeployService().compatible(FakeSession(adapter))
    assert len(reasons) == 2


# -- restore_baseline ---------------------------------------------------------------


def test_restore_baseline_deploys_the_greybox():
    adapter = ScriptedAdapter()
    session = FakeSession(adapter, rm_id="0x01000001")
    result = DeployService().restore_baseline(session)
    assert result.rm_id == "0x00000000" and adapter.deployed == ["greybox"]


def test_restore_baseline_without_a_known_baseline_is_absent():
    adapter = ScriptedAdapter(base=None)
    with pytest.raises(AbsentError):
        DeployService().restore_baseline(FakeSession(adapter))
    assert adapter.deployed == []


# -- no adapter ---------------------------------------------------------------------


def test_session_without_deploy_adapter_is_unavailable_with_a_reason():
    with pytest.raises(UnavailableError) as info:
        DeployService().overlays(FakeSession(None))
    assert info.value.capability == "deploy_partial" and "Ethernet" in info.value.reason
