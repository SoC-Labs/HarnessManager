"""OTA-C: the stage side of app self-update (david U3: notify, auto-stage, apply on click).

- ``dep`` artifacts: pyverify is downloaded and sha256-checked like the wheel, and the
  lock's line for it is pinned to the verified local file; twins: a tampered dep (nothing
  is built), a lock that pins another version of the dep;
- a version that failed its health check (``mark_bad``) is never offered, auto-staged or
  installed again; twins: a newer version IS offered, and ``clear_bad`` offers it again;
- staging runs beside the running version while a board is open; twins: the switch is
  still refused while busy, and a second stage is refused while one runs;
- the background stage follows the administrator's policy (OTA-L): ``stage`` stages,
  ``notify`` and ``off`` do not; a user's click (``auto=False``) stages under ``notify``;
- M6: a release carries one lock per extras set; the stage takes the one that covers the
  install's recorded extras (twin: no extras -> the base lock).

No real uv (``FakeUv``), no network beyond 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket

import pytest

from harness_manager.core.errors import HeldError, RefusedError
from harness_manager.core.events import EventBus
from harness_manager.core.session import SessionLock
from harness_manager.services.update.app import AppLayout, AppUpdater, LocalBusyProbe
from harness_manager.services.update.appstage import (
    StageLock,
    clear_bad,
    mark_bad,
    pin_deps,
    stage_app,
)
from harness_manager.services.update.policy import Policy
from harness_manager.services.update.schema import ChannelFormatError, parse_channel
from harness_manager.services.update.service import UpdateService
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t7_board import FakeUv

KEYS = TestKeys()
DEP_NAME = "mps3_pyverify-0.1.0-py3-none-any.whl"
DEP = b"PK-fake-pyverify-0.1.0"
LOCK = ("fastapi==0.110 \\\n    --hash=sha256:" + "ab" * 32 + "\n"
        "mps3-pyverify==0.1.0 \\\n    --hash=sha256:" + "cc" * 32 + "\n"
        "    # via harness-manager\n")


def add_release(b: ChannelBuilder, version: str, *, dep: bytes = DEP, lock: str = LOCK) -> None:
    rel = b.add_app(version, AssetFile(f"harness_manager-{version}-py3-none-any.whl",
                                       f"PK-wheel-{version}".encode()),
                    lock=AssetFile(f"harness_manager-{version}.lock.txt", lock.encode()),
                    notes=f"notes for {version}")
    rel["artifacts"].append({"kind": "dep", **b.put(AssetFile(DEP_NAME, dep))})


@pytest.fixture
def channel(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        b = ChannelBuilder(srv.root, KEYS)
        add_release(b, "0.1.2")
        b.publish(serial=1)
        yield srv, b


def service(tmp_path, uv: FakeUv, *, bus: EventBus | None = None, policy: Policy | None = None,
            extras: tuple[str, ...] = ()) -> UpdateService:
    state_dir = tmp_path / "state"
    app = AppUpdater(AppLayout(state_dir / "update" / "app"), LocalBusyProbe(state_dir),
                     uv="/opt/uv", runner=uv, python_version="3.11", running_version="0.1.1",
                     windows=os.name == "nt", extras=extras)
    return UpdateService(state_dir=state_dir, trust=KEYS.trust(), app_version="0.1.1",
                         app_updater=app, token="", bus=bus, policy=policy or Policy())


# --- deps ------------------------------------------------------------------------------------


def test_a_dep_is_verified_and_pinned_to_the_local_file(channel, tmp_path):
    srv, _ = channel
    uv = FakeUv()
    out = service(tmp_path, uv).update_app(source=srv.source(), switch=False)
    assert out["staged"]["state"] == "staged" and out["locked"]
    reqs = uv.reqs_seen[0]
    line = next(ln for ln in reqs.splitlines() if ln.startswith("mps3-pyverify"))
    assert line.startswith("mps3-pyverify @ file://") and DEP_NAME in line
    assert line.endswith("--hash=sha256:" + hashlib.sha256(DEP).hexdigest())
    assert "cc" * 32 not in reqs and "fastapi==0.110" in reqs          # others kept as is
    named = tmp_path / "state" / "update" / "app" / "wheels" / DEP_NAME
    assert named.read_bytes() == DEP                                     # the PEP 427 name


def test_twin_a_tampered_dep_refuses_before_anything_is_built(channel, tmp_path):
    srv, _ = channel
    (srv.root / "assets" / DEP_NAME).write_bytes(DEP[:-1] + b"X")        # same size
    uv = FakeUv()
    with pytest.raises(RefusedError, match="fails its sha256 check"):
        service(tmp_path, uv).update_app(source=srv.source(), switch=False)
    assert uv.calls == []


def test_twin_a_lock_pinning_another_dep_version_is_refused(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        b = ChannelBuilder(srv.root, KEYS)
        add_release(b, "0.1.2", lock="mps3-pyverify==0.2.0 --hash=sha256:" + "cc" * 32 + "\n")
        b.publish(serial=1)
        uv = FakeUv()
        with pytest.raises(RefusedError, match=r"pins mps3-pyverify==0\.2\.0"):
            service(tmp_path, uv).update_app(source=srv.source(), switch=False)
        assert uv.calls == []


def test_pin_deps_forms(tmp_path):
    wheel = tmp_path / DEP_NAME
    deps = {"mps3-pyverify": ("0.1.0", wheel, "ee" * 32)}
    pin = f"mps3-pyverify @ {wheel.resolve().as_uri()} --hash=sha256:{'ee' * 32}"
    url_form = "mps3-pyverify @ http://h/x.whl --hash=sha256:" + "cc" * 32 + "\n"
    assert pin_deps(url_form, deps) == pin + "\n"                        # the spike's form
    assert pin_deps("pyserial==3.5\n", deps) == f"pyserial==3.5\n{pin}\n"   # absent: added
    assert pin_deps("mps3_pyverify==0.1.0 --hash=sha256:" + "cc" * 32, deps) == pin + "\n"


def test_the_schema_checks_deps():
    base = {"schema": "harness-manager-channel", "schema_version": 1, "channel": "stable",
            "serial": 1, "issued_at": "2026-09-24T00:00:00Z", "signing_key_id": "0" * 16,
            "app": {"releases": [{"version": "0.2.0", "status": "current", "artifacts": [
                {"kind": "wheel", "name": "harness_manager-0.2.0-py3-none-any.whl", "url": "w.whl",
                 "sha256": "0" * 64, "size": 1, "access": "github-token", "repo": "o/r"},
                {"kind": "dep", "name": DEP_NAME, "url": "d.whl", "sha256": "1" * 64, "size": 1}]}]}}
    rel = parse_channel(base).app_release("0.2.0")
    assert rel.wheel.access == "github-token"                     # U1: a private wheel is fine
    assert [d.name for d in rel.deps] == [DEP_NAME]
    for bad, why in ((dict(name="pyverify.tar.gz"), "not a wheel file name"),
                     (dict(name="harness_manager-0.1.0-py3-none-any.whl"), "harness-manager itself")):
        doc = json.loads(json.dumps(base))
        doc["app"]["releases"][0]["artifacts"][1].update(bad)
        with pytest.raises(ChannelFormatError, match=why):
            parse_channel(doc)
    doc = json.loads(json.dumps(base))
    doc["app"]["releases"][0]["artifacts"].append(
        {"kind": "dep", "name": "mps3_pyverify-0.1.1-py3-none-any.whl", "url": "e.whl",
         "sha256": "2" * 64, "size": 1})
    with pytest.raises(ChannelFormatError, match="two deps"):
        parse_channel(doc)


# --- bad versions ----------------------------------------------------------------------------


def test_a_bad_version_is_never_offered_again(channel, tmp_path):
    srv, _ = channel
    uv = FakeUv()
    svc = service(tmp_path, uv)
    assert svc.check(source=srv.source())["app_update"] == "0.1.2"
    mark_bad(svc.state, "0.1.2", "the daemon did not answer /health in 30 s")
    report = svc.check(source=srv.source())
    assert report["app_update"] == "" and not report["available"]      # the OTA spike's break
    assert report["app_skipped"]["version"] == "0.1.2"
    res = stage_app(svc, source=srv.source())                           # auto-stage skips it
    assert res == {"staged": False, "version": "", "channel": "stable", "skipped_bad": "0.1.2",
                   "why": res["why"]} and "health" in res["why"]
    with pytest.raises(RefusedError, match="marked bad"):
        svc.update_app(source=srv.source())
    with pytest.raises(RefusedError, match="marked bad"):
        stage_app(svc, source=srv.source(), version="0.1.2")
    assert uv.calls == []


def test_twin_a_newer_release_is_offered_and_a_cleared_mark_offers_again(channel, tmp_path):
    srv, b = channel
    svc = service(tmp_path, FakeUv())
    mark_bad(svc.state, "0.1.2", "health")
    add_release(b, "0.1.3")
    b.publish(serial=2)
    assert svc.check(source=srv.source())["app_update"] == "0.1.3"
    svc2 = service(tmp_path / "other", FakeUv())
    mark_bad(svc2.state, "0.1.3", "health")
    assert clear_bad(svc2.state, "0.1.3")
    assert svc2.check(source=srv.source())["app_update"] == "0.1.3"


def test_the_pointers_own_bad_state_is_honoured(channel, tmp_path):
    srv, _ = channel
    svc = service(tmp_path, FakeUv())
    pointer = svc.app().layout.pointer
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"current": "", "previous": "", "versions": {
        "0.1.2": {"state": "bad", "reason": "rolled back by the apply helper"}}}))
    assert svc.check(source=srv.source())["app_update"] == ""


# --- stage beside the running version --------------------------------------------------------


def test_staging_runs_beside_the_running_version_while_a_board_is_open(channel, tmp_path):
    srv, _ = channel
    uv, bus = FakeUv(), EventBus()
    events: list = []
    bus.subscribe("*", events.append)
    svc = service(tmp_path, uv, bus=bus)
    lock = SessionLock("mps3@192.168.10.101:6900", lock_dir=tmp_path / "state" / "locks")
    lock.acquire()
    try:
        res = stage_app(svc, source=srv.source())
        assert res["staged"] and res["version"] == "0.1.2" and res["notes"] == "notes for 0.1.2"
        st = svc.app().state()
        assert st["current"] == "" and st["versions"]["0.1.2"]["state"] == "staged"
        with pytest.raises(HeldError, match="cannot switch"):          # twin: switch waits
            svc.app().switch("0.1.2")
    finally:
        lock.release()
    assert [e.topic for e in events if e.topic.startswith("update.app")] == ["update.app.staged"]
    assert not StageLock(svc.state).path.exists()                      # released


def test_twin_a_second_stage_is_refused_while_one_runs(channel, tmp_path):
    srv, _ = channel
    uv = FakeUv()
    svc = service(tmp_path, uv)
    held = StageLock(svc.state)
    held.acquire("stage harness-manager 0.1.2")                       # a live stager (us)
    try:
        with pytest.raises(HeldError, match="another stage is running"):
            stage_app(svc, source=srv.source())
        assert uv.calls == []
    finally:
        held.release()


def test_a_dead_stagers_lock_is_taken_over(channel, tmp_path):
    srv, _ = channel
    svc = service(tmp_path, FakeUv())
    path = StageLock(svc.state).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 2 ** 22 + 12345, "host": socket.gethostname(),
                                "what": "stage", "at": 0}))
    assert stage_app(svc, source=srv.source())["staged"]


# --- the administrator's policy (OTA-L) --------------------------------------------------------


@pytest.mark.parametrize("mode", ["notify", "off"])
def test_the_background_stage_follows_the_policy(channel, tmp_path, mode):
    srv, _ = channel
    uv = FakeUv()
    svc = service(tmp_path, uv, policy=Policy(path="/etc/p.toml", self_update=mode))
    res = stage_app(svc, source=srv.source())
    assert not res["staged"] and ("notify" in res["why"] or "off" in res["why"])
    assert uv.calls == [] and svc.check(source=srv.source())["app_update"] == (
        "0.1.2" if mode == "notify" else "")                        # notify still notifies


def test_twin_a_users_click_stages_under_notify(channel, tmp_path):
    srv, _ = channel
    svc = service(tmp_path, FakeUv(), policy=Policy(path="/etc/p.toml", self_update="notify"))
    assert stage_app(svc, source=srv.source(), auto=False)["staged"]


# --- M6: one lock per extras set ---------------------------------------------------------------

APP_LOCK = "pywebview==5.1 --hash=sha256:" + "dd" * 32 + "\n" + LOCK


def extras_channel(root):
    b = ChannelBuilder(root, KEYS)
    add_release(b, "0.1.2")
    rel = b.app[0]
    rel["lock"]["extras"] = ["serial", "ina260"]
    rel["artifacts"].append({"kind": "lock", "extras": ["app"],
                             **b.put(AssetFile("harness_manager-0.1.2.app.lock.txt",
                                               APP_LOCK.encode()))})
    b.publish(serial=1)
    return b


def test_an_install_with_the_app_extra_stages_from_the_app_lock(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        extras_channel(srv.root)
        uv = FakeUv()
        res = stage_app(service(tmp_path, uv, extras=("app",)), source=srv.source())
        assert res["prepared"]["lock"] == "harness_manager-0.1.2.app.lock.txt"
        assert "pywebview==5.1" in uv.reqs_seen[0]
        assert "mps3-pyverify @ file://" in uv.reqs_seen[0]           # deps pinned in it too


def test_twin_without_extras_the_base_lock_is_used(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        extras_channel(srv.root)
        uv = FakeUv()
        res = stage_app(service(tmp_path, uv), source=srv.source())
        assert res["prepared"]["lock"] == "harness_manager-0.1.2.lock.txt"
        assert "pywebview" not in uv.reqs_seen[0]


def test_lock_for_picks_the_smallest_covering_lock():
    rel = parse_channel(json.loads(json.dumps(EXTRAS_DOC))).app_release("0.2.0")
    assert rel.lock_extras == ("ina260", "serial")
    assert rel.lock_for(()).name == "base.lock"
    assert rel.lock_for(["serial"]).name == "base.lock"                # covered by the base
    assert rel.lock_for(["app"]).name == "app.lock"
    assert rel.lock_for(["app", "dev"]).name == "all.lock"
    assert rel.lock_for(["nope"]).name == "base.lock"                  # twin: none covers it


def _lock(name, extras=None):
    d = {"name": name, "url": name, "sha256": "0" * 64, "size": 1}
    if extras is not None:
        d.update(kind="lock", extras=extras)
    return d


EXTRAS_DOC = {"schema": "harness-manager-channel", "schema_version": 1, "channel": "stable",
              "serial": 1, "issued_at": "2026-09-24T00:00:00Z", "signing_key_id": "0" * 16,
              "app": {"releases": [{"version": "0.2.0", "status": "current",
                                    "lock": {**_lock("base.lock"), "extras": ["serial", "ina260"]},
                                    "artifacts": [
                                        {"kind": "wheel", **_lock("harness_manager-0.2.0-py3-none-any.whl")},
                                        _lock("app.lock", ["app"]),
                                        _lock("all.lock", ["app", "dev"])]}]}}


@pytest.mark.parametrize("mutate,why", [
    (lambda r: r["artifacts"].append(_lock("x.lock", [])), "names the extras it covers"),
    (lambda r: r["artifacts"].append(_lock("y.lock", ["APP"])), "same extras set"),
    (lambda r: r.pop("lock"), "need the base lock"),
])
def test_twin_malformed_extras_locks_are_refused(mutate, why):
    doc = json.loads(json.dumps(EXTRAS_DOC))
    mutate(doc["app"]["releases"][0])
    with pytest.raises(ChannelFormatError, match=why):
        parse_channel(doc)
