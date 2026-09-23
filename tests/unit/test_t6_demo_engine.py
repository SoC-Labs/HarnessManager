"""The demo engine implements the frozen Engine protocol and T1/T2's observable semantics.

No Qt needed: the demo engine is plain Python.
"""

from __future__ import annotations

import pytest

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    AlreadyError,
    HeldError,
    IncompatibleError,
    NothingOnTargetError,
)
from harness_manager.core.model import Check, LinkKind
from harness_manager.core.services import (
    ConsoleBroker,
    ContentStore,
    DebugService,
    DeployService,
    Engine,
    TelemetryService,
)
from harness_manager.demo import BOARD_FIELDED, BOARD_HELD, BOARD_USB, DemoEngine


@pytest.fixture
def engine():
    eng = DemoEngine(speed=0.02)      # 2 % of the demo's pacing
    yield eng
    eng.close_all()


def topics(engine: DemoEngine) -> list:
    seen: list = []
    engine.bus.subscribe("*", lambda ev: seen.append(ev))
    return seen


def test_demo_engine_satisfies_every_service_protocol(engine):
    assert isinstance(engine, Engine)
    assert isinstance(engine.deploy, DeployService)
    assert isinstance(engine.consoles, ConsoleBroker)
    assert isinstance(engine.debug, DebugService)
    assert isinstance(engine.telemetry, TelemetryService)
    assert isinstance(engine.store, ContentStore)


def test_negative_twin_an_object_without_the_methods_is_not_an_engine():
    class Half:
        bus = store = deploy = consoles = debug = telemetry = None

        def packs(self):
            return {}

    assert not isinstance(Half(), Engine)


def test_three_scripted_boards_one_held(engine):
    ids = [c.board_id for c in engine.probe()]
    assert ids == [BOARD_FIELDED, BOARD_USB, BOARD_HELD]
    owner = engine.lock_owner(BOARD_HELD)
    assert owner is not None and owner.user == "alice"
    assert engine.lock_owner(BOARD_FIELDED) is None           # negative twin: free
    with pytest.raises(HeldError) as exc:
        engine.open(engine.candidate_for("192.168.10.103"))
    assert "alice" in exc.value.holder


def test_info_needs_an_open_board_like_the_real_engine(engine):
    with pytest.raises(AbsentError):
        engine.info(BOARD_FIELDED)
    engine.open(engine.candidate_for("192.168.10.101"))
    info = engine.info(BOARD_FIELDED)
    assert info.identity.build_check is Check.UNCHECKED
    assert info.unavailable["reboot_board"].startswith("needs the Debug USB cable")
    assert BOARD_FIELDED in engine.open_boards()
    with pytest.raises(AlreadyError):
        engine.open(engine.candidate_for("192.168.10.101"))


def test_add_usb_changes_the_capability_view(engine):
    engine.open(engine.candidate_for("192.168.10.101"))
    assert "reboot_board" not in engine.info(BOARD_FIELDED).capabilities
    engine.add_usb(BOARD_FIELDED)
    info = engine.info(BOARD_FIELDED)
    assert "reboot_board" in info.capabilities
    assert LinkKind.USB_SERIAL in {lk.kind for lk in info.candidate.links}


def test_deploy_events_have_t2_shapes_in_order(engine):
    seen = topics(engine)
    s = engine.open(engine.candidate_for("192.168.10.101"))
    nanosoc = next(o for o in engine.deploy.overlays(s) if o.name == "nanosoc")
    result = engine.deploy.deploy(s, nanosoc)
    assert result.verified and result.rm_id == "0x01000001"
    deploy = [e for e in seen if e.topic.startswith("deploy.")]
    assert deploy[0].topic == "deploy.started" and deploy[-1].topic == "deploy.done"
    assert {"overlay", "rm_id", "preflight"} <= set(deploy[0].data)
    phases = [e.data["phase"] for e in deploy if e.topic == "deploy.progress"]
    assert phases[0] == "guard" and phases[-1] == "verify"
    assert engine.info(BOARD_FIELDED).identity.rm_name == "nanosoc"


def test_negative_twin_incompatible_deploy_publishes_only_failed(engine):
    seen = topics(engine)
    s = engine.open(engine.candidate_for("192.168.10.101"))
    multicore = next(o for o in engine.deploy.overlays(s) if o.name == "nanosoc_multicore")
    with pytest.raises(IncompatibleError):
        engine.deploy.deploy(s, multicore)
    deploy = [e.topic for e in seen if e.topic.startswith("deploy.")]
    assert deploy == ["deploy.failed"]
    assert engine.info(BOARD_FIELDED).identity.rm_name == "greybox"      # untouched


def test_failure_injection_mid_push(engine):
    s = engine.open(engine.candidate_for("192.168.10.101"))
    engine.failures["deploy.push"] = ActionFailedError("push window timed out")
    nanosoc = next(o for o in engine.deploy.overlays(s) if o.name == "nanosoc")
    with pytest.raises(ActionFailedError):
        engine.deploy.deploy(s, nanosoc)


def test_debug_detect_needs_a_design_with_a_dap(engine):
    fielded = engine.open(engine.candidate_for("192.168.10.101"))
    usb = engine.open(engine.candidate_for("192.168.10.102"))
    with pytest.raises(NothingOnTargetError):
        engine.debug.detect(fielded)                    # greybox: nothing to detect
    assert engine.debug.detect(usb) == "0x6ba00477"


def test_console_stream_fans_out_and_records_writes(engine):
    s = engine.open(engine.candidate_for("192.168.10.101"))
    a = engine.consoles.subscribe(s, "uart0")
    b = engine.consoles.subscribe(s, "uart0")
    assert engine.inject_console(BOARD_FIELDED, "uart0", "hi\n") == 2
    assert a.read(0.5) == b"hi\n" and b.read(0.5) == b"hi\n"
    assert a.read(0.05) == b""                          # negative twin: nothing more
    a.write(b"help\n")
    assert (BOARD_FIELDED, "uart0", b"help\n") in engine.consoles.writes
