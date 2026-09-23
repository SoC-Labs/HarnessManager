"""TelemetryService (Team T1). Each check has a negative twin."""

from __future__ import annotations

from harness_manager.core.errors import ActionFailedError
from harness_manager.core.services import TelemetryService as TelemetryServiceProtocol
from harness_manager.services.telemetry import (
    EMPTY_SOURCES_REASON,
    NO_SOURCE_REASON,
    TelemetryService,
)
from tests.fakes.t1_fakes import (
    FakeController,
    FakeSession,
    FakeTelemetry,
    TempOnlyController,
    candidate,
    reading,
)

NOW = 1_800_000_000.0


def service(max_age_s: float = 60.0) -> TelemetryService:
    return TelemetryService(max_age_s, clock=lambda: NOW)


def session(**adapters) -> FakeSession:
    return FakeSession(candidate(), **adapters)


def test_satisfies_the_frozen_protocol():
    assert isinstance(TelemetryService(), TelemetryServiceProtocol)


# -- provenance -----------------------------------------------------------------------

def test_every_source_is_gathered_and_keeps_its_provenance():
    s = session(
        telemetry=FakeTelemetry([reading("fpga_die_temp", 48.5, "sysmon-jtag", now=NOW)]),
        controller=FakeController(
            [reading("mcc_temp", 35.0, "mcc-console", now=NOW)],
            [reading("osc0", 50.0, "mcc-console", unit="MHz", now=NOW)]),
    )
    got = service().readings(s)
    assert [(r.name, r.value, r.source) for r in got] == [
        ("fpga_die_temp", 48.5, "sysmon-jtag"),
        ("mcc_temp", 35.0, "mcc-console"),
        ("osc0", 50.0, "mcc-console"),
    ]
    assert all(r.reason == "" for r in got)


def test_unlabelled_reading_is_labelled_with_its_adapter():
    # Negative twin of the above: an adapter that forgot ``source`` still gets provenance.
    s = session(telemetry=FakeTelemetry([reading("x", 1.0, "", now=NOW)]),
                controller=TempOnlyController([reading("t", 30.0, "", now=NOW)]))
    got = service().readings(s)
    assert [r.source for r in got] == ["telemetry", "controller"]


def test_controller_without_oscillators_is_fine():
    got = service().readings(session(controller=TempOnlyController(
        [reading("mcc_temp", 35.0, "mcc-console", now=NOW)])))
    assert [r.name for r in got] == ["mcc_temp"]


# -- staleness ----------------------------------------------------------------------

def test_stale_reading_is_flagged_and_keeps_its_value():
    s = session(telemetry=FakeTelemetry([
        reading("old", 41.0, "stmpe811", age_s=125.4, now=NOW),
        reading("fresh", 42.0, "stmpe811", age_s=10, now=NOW),
        reading("edge", 43.0, "stmpe811", age_s=60, now=NOW),
    ]))
    old, fresh, edge = service(max_age_s=60).readings(s)
    assert old.value == 41.0 and old.reason == "stale (125 s old)"
    # Negative twins: younger than, and exactly at, the maximum age.
    assert fresh.reason == "" and edge.reason == ""


def test_stale_note_is_added_to_an_existing_caveat():
    s = session(telemetry=FakeTelemetry([
        reading("vccint", 0.85, "sysmon-jtag", unit="V", age_s=300, now=NOW,
                reason="VREFP caveat"),
    ]))
    (r,) = service().readings(s)
    assert r.reason == "VREFP caveat; stale (300 s old)"


def test_max_age_is_configurable():
    s = session(telemetry=FakeTelemetry([reading("t", 1.0, "x", age_s=20, now=NOW)]))
    assert service(max_age_s=10).readings(s)[0].reason == "stale (20 s old)"
    assert service(max_age_s=30).readings(s)[0].reason == ""


# -- failing sources ------------------------------------------------------------------

def test_raising_source_becomes_an_unavailable_reading_not_an_exception():
    s = session(
        telemetry=FakeTelemetry([reading("fpga_die_temp", 48.5, "sysmon-jtag", now=NOW)]),
        controller=FakeController(
            temp_error=ActionFailedError("MCC did not answer CFG R TEMP", hint="check tty_00"),
            oscs=[reading("osc0", 50.0, "mcc-console", unit="MHz", now=NOW)]),
    )
    got = service().readings(s)
    assert [r.name for r in got] == ["fpga_die_temp", "temperature", "osc0"]
    bad = got[1]
    assert bad.value is None and not bad.available          # never a 0
    assert bad.unit == "degC" and bad.source == "controller"
    assert bad.reason == "MCC did not answer CFG R TEMP — check tty_00"
    # Negative twin: the working sources are untouched.
    assert got[0].available and got[2].available


def test_error_without_a_message_still_gives_a_reason():
    s = session(telemetry=FakeTelemetry(error=RuntimeError()))
    (r,) = service().readings(s)
    assert r.value is None and r.reason == "RuntimeError" and r.source == "telemetry"


def test_oscillator_failure_is_reported_in_mhz():
    s = session(controller=FakeController(osc_error=TimeoutError("no reply")))
    (r,) = service().readings(s)
    assert (r.name, r.unit, r.value, r.reason) == ("oscillators", "MHz", None, "no reply")


def test_a_non_reading_from_a_source_is_reported_not_passed_through():
    s = session(telemetry=FakeTelemetry([0.0]))            # a bare 0 must not look like data
    (r,) = service().readings(s)
    assert r.value is None and "float" in r.reason


# -- no sources ---------------------------------------------------------------------

def test_no_sources_gives_one_explicit_unavailable_reading():
    got = service().readings(session())
    assert len(got) == 1
    (r,) = got
    assert (r.name, r.unit, r.value) == ("temperature", "degC", None)
    assert r.reason == NO_SOURCE_REASON


def test_sources_that_return_nothing_are_not_an_empty_list():
    # Negative twin: sources exist but report nothing; say so, differently.
    got = service().readings(session(telemetry=FakeTelemetry([]), controller=FakeController()))
    assert len(got) == 1 and got[0].value is None
    assert got[0].reason == EMPTY_SOURCES_REASON and got[0].source == "telemetry, controller"
