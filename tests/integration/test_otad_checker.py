"""Lane OTA-D: the daemon's periodic app-update checker (``daemon/update_checker.py``).

The channel is T7's ``FakeChannelServer`` on 127.0.0.1 (an app-only document: the ``hm-app``
catalogue), signed with the test keys; the updater builds with a ``FakeUv``. ``tick()`` is
driven directly, so no test waits for a timer; the timer itself is checked with a first
delay of zero. Every behaviour has a negative twin.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from harness_manager import __version__, _launch
from harness_manager.core.events import Event, EventBus
from harness_manager.daemon.update_checker import (
    MAX_BACKOFF_S,
    MIN_BACKOFF_S,
    UpdateChecker,
    error_kind,
)
from harness_manager.services.update import UpdateService
from harness_manager.services.update import selfupdate as su
from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe
from harness_manager.services.update.policy import Policy
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t7_board import FakeUv
from tests.fakes.t13_daemon import state_dir

KEYS = TestKeys()


class _Engine:
    """What UpdateService needs of an engine here: a state dir and a bus."""

    def __init__(self, sd: Path) -> None:
        self.state_dir = sd
        self.bus = EventBus()
        self.store = None


@pytest.fixture
def rig(tmp_path: Path, monkeypatch):
    sd = state_dir()
    root = tmp_path / "root"
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").write_text("installer python")
    _launch.register(root, root / "venv", __version__, extras=[], windows=False)
    uv = FakeUv()
    eng = _Engine(sd)
    app = AppUpdater(AppLayout(root), LocalBusyProbe(sd), uv="/opt/uv", runner=uv,
                     python_version="3.11", running_version=__version__, windows=False,
                     state_dir=sd)
    svc = UpdateService(eng, state_dir=sd, trust=KEYS.trust(), token="", bus=eng.bus,
                        app_version=__version__, app_updater=app, policy=Policy())
    events: list[Event] = []
    eng.bus.subscribe("update.*", events.append)
    with FakeChannelServer(tmp_path / "www") as srv:
        monkeypatch.setenv("HARNESS_MANAGER_UPDATE_SOURCE", srv.source())
        builder = ChannelBuilder(srv.root, KEYS)
        checker = UpdateChecker(lambda: svc, eng.bus, sd, first_delay_s=0, rng=lambda: 0.5)
        yield {"svc": svc, "srv": srv, "builder": builder, "uv": uv, "events": events,
               "checker": checker, "sd": sd, "app": app, "root": root}
        checker.stop()


def publish_app(r: dict, version: str, serial: int, **kw) -> None:
    r["builder"].add_app(version, AssetFile(f"harness_manager-{version}-py3-none-any.whl",
                                            b"PK-wheel-" + version.encode()), **kw)
    r["builder"].publish(serial=serial)


def topics(r: dict, topic: str) -> list[dict]:
    return [e.data for e in r["events"] if e.topic == topic]


def test_the_checker_stages_the_offer_and_publishes_it_once(rig):
    r = rig
    publish_app(r, "0.2.0", serial=1)
    rec = r["checker"].tick()
    assert rec["available"] == "0.2.0" and rec["staged"] is True and rec["catalog"] == "hm-app"
    assert r["app"].state()["versions"]["0.2.0"]["state"] == "staged"
    assert r["app"].state()["current"] == ""                         # it never switches
    avail = topics(r, "update.available")
    assert [(a["app"], a["staged"], a["source"]) for a in avail] == [("0.2.0", True, "checker")]
    assert [s["version"] for s in topics(r, "update.app.staged")] == ["0.2.0"]   # OTA-C's
    last = json.loads(su.last_check_path(r["sd"]).read_text())
    assert last["available"] == "0.2.0" and last["announced"] == ["0.2.0", True]
    # negative twin: the next check sees the same offer: no new event, nothing rebuilt
    calls = len(r["uv"].calls)
    r["checker"].tick()
    assert len(topics(r, "update.available")) == 1 and len(r["uv"].calls) == calls
    # a newer release is announced again
    publish_app(r, "0.3.0", serial=2)
    r["checker"].tick()
    assert [a["app"] for a in topics(r, "update.available")] == ["0.2.0", "0.3.0"]


def test_notify_publishes_without_staging(rig):
    r = rig
    su.save_settings(r["sd"], auto="notify")
    publish_app(r, "0.2.0", serial=1)
    rec = r["checker"].tick()
    assert rec["available"] == "0.2.0" and rec["staged"] is False and rec["mode"] == "notify"
    assert [(a["app"], a["staged"]) for a in topics(r, "update.available")] == [("0.2.0", False)]
    assert r["uv"].calls == [] and "0.2.0" not in r["app"].state()["versions"]


@pytest.mark.parametrize("how", ["policy", "settings", "developer install"])
def test_off_means_silence_no_fetch_no_event_no_record(rig, how):
    r = rig
    publish_app(r, "0.2.0", serial=1)
    if how == "policy":
        r["svc"].policy = Policy(path="/etc/harness-manager/policy.toml", self_update="off")
    elif how == "settings":
        su.save_settings(r["sd"], auto="off")
    else:
        r["app"].dev_install = "this is a developer install (pip install -e)"
    before = len(r["srv"].requests)
    rec = r["checker"].tick()
    assert rec.get("skipped") and rec.get("error") is None
    assert len(r["srv"].requests) == before                         # nothing fetched
    assert r["events"] == [] and not su.last_check_path(r["sd"]).exists()
    assert r["uv"].calls == []


def test_negative_twin_with_self_update_on_the_same_channel_is_fetched(rig):
    r = rig
    publish_app(r, "0.2.0", serial=1)
    before = len(r["srv"].requests)
    r["checker"].tick()
    assert len(r["srv"].requests) > before and topics(r, "update.available")


def test_a_bad_version_is_never_offered_by_the_checker(rig):
    r = rig
    publish_app(r, "0.2.0", serial=1)
    r["app"].mark_bad("0.2.0", "the daemon exited while starting", phase="start")
    rec = r["checker"].tick()
    assert rec["available"] == "" and rec["skipped_bad"]["version"] == "0.2.0"
    assert topics(r, "update.available") == [] and r["uv"].calls == []
    # the twin: a newer release is offered and staged as usual
    publish_app(r, "0.2.1", serial=2)
    assert r["checker"].tick()["available"] == "0.2.1"


def test_offline_is_quiet_and_backs_off_then_recovers(rig, tmp_path, monkeypatch):
    r = rig
    monkeypatch.setenv("HARNESS_MANAGER_UPDATE_SOURCE", str(tmp_path / "nothing-here"))
    ck = r["checker"]
    rec = ck.tick()
    assert rec["error"] and rec["error_kind"] == "offline", rec
    assert r["events"] == []
    delays = [ck.delay_after(rec) for _ in range(12)]
    assert delays[0] == MIN_BACKOFF_S and delays[1] == 2 * MIN_BACKOFF_S
    assert delays[-1] == MAX_BACKOFF_S                                # capped at 24 h
    assert json.loads(su.last_check_path(r["sd"]).read_text())["error_kind"] == "offline"
    # the twin: the source is back, the offer is published, the interval is the policy's
    monkeypatch.setenv("HARNESS_MANAGER_UPDATE_SOURCE", r["srv"].source())
    publish_app(r, "0.2.0", serial=1)
    rec = ck.tick()
    assert rec["error"] == "" and topics(r, "update.available")
    assert ck.delay_after(rec) == pytest.approx(6 * 3600) and ck.backoff_s == 0


def test_a_refused_channel_is_loud_and_recorded_as_refused(rig, caplog):
    r = rig
    publish_app(r, "0.2.0", serial=2)
    r["checker"].tick()
    r["builder"].publish(serial=1)                                     # a rollback of the serial
    rec = r["checker"].tick()
    assert rec["error_kind"] == "refused" and "serial" in rec["error"]
    assert any("refused the channel" in m for m in caplog.messages)


def test_error_kinds():
    from harness_manager.core.errors import (
        ActionFailedError,
        RefusedError,
        UnreachableError,
    )

    assert error_kind(UnreachableError("no route")) == "offline"
    assert error_kind(ActionFailedError("the download timed out")) == "offline"
    assert error_kind(RefusedError("bad signature")) == "refused"
    assert error_kind(ActionFailedError("disk full")) == "failed"


def test_staging_as_a_job_is_deferred_while_the_service_is_busy(rig):
    from harness_manager.core.errors import HeldError

    r = rig
    submitted: list = []

    def busy(kind, fn):
        raise HeldError("update_app job 1 is running in the service")

    r["checker"].submit = busy
    publish_app(r, "0.2.0", serial=1)
    rec = r["checker"].tick()
    assert rec["staged"] is False and "running" in rec["stage_deferred"]
    assert r["checker"].delay_after(rec) == 600.0                     # retried in 10 minutes
    # the twin: a free service runs it as a job, which stages and announces it
    import threading

    done = threading.Event()

    def as_a_job(kind, fn):                       # a worker thread, as JobManager runs it
        submitted.append(kind)
        threading.Thread(target=lambda: (fn(lambda *a: None), done.set())).start()
        return type("Job", (), {"id": "j1"})()

    r["checker"].submit = as_a_job
    rec = r["checker"].tick()
    assert rec["stage_job"] == "j1" and done.wait(20)
    assert submitted == ["update_stage"]
    assert [(a["app"], a["staged"]) for a in topics(r, "update.available")] == \
        [("0.2.0", False), ("0.2.0", True)]


def test_the_timer_runs_the_first_check_and_stops(rig):
    r = rig
    publish_app(r, "0.2.0", serial=1)
    r["checker"].start()
    deadline = time.monotonic() + 10
    # The event is published inside the tick; next_in_s is set after it returns: wait for both.
    while (not topics(r, "update.available") or r["checker"].next_in_s is None) \
            and time.monotonic() < deadline:
        time.sleep(0.05)
    assert topics(r, "update.available")
    assert r["checker"].next_in_s == pytest.approx(6 * 3600)
    r["checker"].stop()


# --- SMALL-4: last_check.notes and the timer's next_check ------------------------------------------


def test_a_found_update_records_its_notes_or_a_one_line_summary(rig):
    r = rig
    notes = "Faster consoles.\nThe touch bus no longer wedges."
    publish_app(r, "0.2.0", serial=1, notes=notes)
    rec = r["checker"].tick()
    assert rec["available"] == "0.2.0" and rec["notes"] == notes
    assert json.loads(su.last_check_path(r["sd"]).read_text())["notes"] == notes
    assert topics(r, "update.available")[0]["notes"] == notes          # the event is unchanged
    # a release without notes: one line that says what was found
    publish_app(r, "0.3.0", serial=2)
    rec = r["checker"].tick()
    assert rec["notes"] == "harness-manager 0.3.0 is available on the stable channel (serial 2)"
    assert "\n" not in rec["notes"]


def test_negative_twin_no_update_found_records_no_notes(rig):
    r = rig
    publish_app(r, "0.2.0", serial=1, notes="not for you")
    r["app"].mark_bad("0.2.0", "the daemon exited while starting", phase="start")
    rec = r["checker"].tick()
    assert rec["available"] == "" and "notes" not in rec
    assert "notes" not in json.loads(su.last_check_path(r["sd"]).read_text())


def test_next_check_is_the_timers_schedule_and_none_without_one(rig):
    r = rig
    clock = [1_790_000_000.0]
    c = UpdateChecker(lambda: r["svc"], None, r["sd"], first_delay_s=3600, rng=lambda: 0.5,
                      now=lambda: clock[0])
    assert c.next_check() is None                                     # no timer yet
    c.start()
    try:
        assert c.next_check() == "2026-09-21T15:13:20Z"               # now + the first delay
        clock[0] += 60
        c._on_settings(Event("settings.changed", "", {"keys": ["updates.channel"]}))
        assert c.next_check() == "2026-09-21T14:14:20Z"               # a change checks at once
        c._on_settings(Event("settings.changed", "", {"keys": ["theme"]}))
        assert c.next_check() == "2026-09-21T14:14:20Z"               # the twin: not its key
    finally:
        c.stop()
    assert c.next_check() is None                                     # stopped: none


def test_after_a_check_next_check_is_one_interval_on(rig):
    r = rig
    publish_app(r, "0.2.0", serial=1)
    r["checker"].start()
    deadline = time.monotonic() + 10
    while r["checker"].next_in_s is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert r["checker"].next_in_s == pytest.approx(6 * 3600)
    assert r["checker"].next_at == pytest.approx(time.time() + 6 * 3600, abs=30)
    iso = r["checker"].next_check()
    assert iso is not None and iso.endswith("Z") and "T" in iso
    r["checker"].stop()
    assert r["checker"].next_check() is None
