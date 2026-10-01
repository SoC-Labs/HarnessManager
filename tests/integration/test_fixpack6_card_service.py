"""FIX-PACK-6 item 1: ``card``/``slot`` changes go through the running service.

H1 Z1 (board 1, 1 Oct): with the app holding the board, ``harness-manager card clear`` got
"mps3@…:6900 is in use — held by … harness-manager-daemon: harness-manager-ui". The CLI ran
the card and slot changes in-process, while ``program``, ``restore`` and ``mcc reboot`` went
through the service. Now every ``card``/``slot`` verb does, as the service's jobs
(``POST /boards/{bid}/card/{commit,clear}``, ``/slots/{push,commit,verify,rollback}``), and
without a service they still run in-process.

The board is pyverify's FakeShell as a Linux ``SlotBoard`` with a card; the service is the
real daemon (``LiveDaemon``) holding the board as the page does; the CLI is ``main([...])``
with no engine factory, so it finds the service the way a user's shell does. Each check has
its negative twin.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from harness_manager.core.errors import ExitCode
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from tests.fakes.lxslots_board import LINUX_SID, slot_board
from tests.fakes.s0lb_image import linux_bundle_s0lb, make_s0lb
from tests.fakes.t2_overlays import SYNTH_RM_ID, make_overlay, use_overlay_dirs
from tests.fakes.t13_daemon import LiveDaemon, daemon_verbs

SID = f"0x{LINUX_SID:08x}"
A_RECORDED = {"state": "valid", "hdr_crc": 0x3E5E9C2C, "len": 24354312, "sid": LINUX_SID}


def run(capsys, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    from harness_manager.cli.main import main

    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with daemon_verbs():
            rc = main(list(argv))
    finally:
        sys.stdin = old
    out, err = capsys.readouterr()
    return rc, out, err


@contextmanager
def held_by_the_app(**kw: Any) -> Iterator[tuple[Any, LiveDaemon, str, Engine]]:
    """The service runs and holds the board (the page opened it), as in H1."""
    fake = slot_board(**kw)
    eng = Engine(EngineConfig(state_dir=Path(os.environ["HARNESS_MANAGER_STATE_DIR"]),
                              pack_overrides={"mps3": {
                                  "console_ports": fake.console_ports,
                                  "push_port": fake.raw_tcp_port,
                                  "tftp_port": fake.tftp_port}}))
    target = f"{fake.host}:{fake.control_port}"
    try:
        with LiveDaemon(eng, write_json=True) as live:
            live.app.state.daemon.presence._stop.set()
            with live.client() as c:
                r = c.post("/api/v1/boards", json={"target": target,
                                                   "note": "harness-manager-ui"})
                assert r.status_code == 200, r.text
                bid = r.json()["board_id"]
            slots = eng.session(bid).os_slots
            if slots is not None:                      # a fast fake: poll it fast
                slots.poll_s = slots.poll_max_s = slots.reboot_poll_s = 0.02
            yield fake, live, target, eng
            eng.close_all()
    finally:
        fake.stop()


def job_kinds(live: LiveDaemon) -> list[str]:
    with live.client() as c:
        return [j["kind"] for j in c.get("/api/v1/jobs").json()["jobs"]]


@pytest.fixture
def service(monkeypatch):
    monkeypatch.delenv("HARNESS_MANAGER_NO_DAEMON", raising=False)


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    root = tmp_path / "ovl"
    make_overlay(root, "synth", rm_id=SYNTH_RM_ID, static_id=LINUX_SID)
    use_overlay_dirs(monkeypatch, root)
    return root


@pytest.fixture
def bundle(tmp_path):
    data = make_s0lb(b"\x5a" * 8192)
    (tmp_path / "linux_slot.img").write_bytes(data)
    doc = {"schema": "mps3-linux-bundle", "schema_version": "1", "fieldable": True,
           "harness": "1.1.0", "static_id": SID,
           "targets": {"ethernet": {
               "slot_image": {"name": "linux_slot.img", "sha256": hashlib.sha256(data).hexdigest(),
                              "bytes": len(data), "s0lb": linux_bundle_s0lb(data)},
               "provisioned": {"static_id": SID}}}}
    (tmp_path / "linux_bundle.json").write_text(json.dumps(doc), encoding="utf-8")
    return tmp_path


# --- the card -----------------------------------------------------------------------------------


def test_card_commit_and_clear_run_as_the_services_jobs_while_the_app_holds_the_board(
        service, overlay, capsys):
    with held_by_the_app(usd_card="da", boot_rm_id=SYNTH_RM_ID,
                         slots={"a": A_RECORDED}) as (fake, live, target, _eng):
        rc, out, err = run(capsys, "card", "commit", target, "--yes")
        assert rc == ExitCode.OK, err
        assert "synth (0x01007a57) is the power-on default" in out
        assert "default    synth" in out                 # the card read after it is fresh
        assert fake.commits and fake.commits[0][0] == "synth"
        rc, out, err = run(capsys, "--json", "card", "clear", target, "--yes")
        assert rc == ExitCode.OK, err
        doc = json.loads(out)
        assert doc["default"] is None and "greybox loads at the next power-on" in doc["result"]
        assert job_kinds(live) == ["card_commit", "card_clear"]   # the service ran them
        assert "in use" not in err


def test_twin_in_process_card_clear_is_refused_by_the_services_lock_as_in_h1(overlay, capsys,
                                                                          monkeypatch):
    with held_by_the_app(usd_card="da", boot_rm_id=SYNTH_RM_ID,
                         slots={"a": A_RECORDED}) as (fake, live, target, _eng):
        monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")        # the old routing
        rc, _out, err = run(capsys, "card", "clear", target, "--yes")
        assert rc == ExitCode.HELD and "in use" in err, err
        assert job_kinds(live) == [] and fake.commits == []


def test_twin_the_confirm_is_still_asked_here_and_a_no_sends_no_job(service, overlay, capsys):
    with held_by_the_app(usd_card="da", boot_rm_id=SYNTH_RM_ID,
                         slots={"a": A_RECORDED}) as (fake, live, target, _eng):
        rc, _out, err = run(capsys, "card", "clear", target, stdin="n\n")
        assert rc == ExitCode.REFUSED and "not confirmed" in err
        rc, _out, err = run(capsys, "card", "commit", target, stdin="\n")
        assert rc == ExitCode.REFUSED
        assert job_kinds(live) == [] and fake.commits == []


def test_twin_no_card_is_refused_through_the_service_as_in_process(service, capsys):
    with held_by_the_app(slots={"a": A_RECORDED}) as (fake, live, target, _eng):
        for act in ("commit", "clear"):
            rc, _out, err = run(capsys, "card", act, target, "--yes")
            assert rc == ExitCode.REFUSED and "no card" in err, err
        assert job_kinds(live) == [] and fake.usd_card is None


def test_without_a_service_the_changes_still_run_in_process(service, overlay, capsys,
                                                           monkeypatch):
    from harness_manager_mps3.deploy import PUSH_PORT_ENV

    fake = slot_board(usd_card="da", boot_rm_id=SYNTH_RM_ID, slots={"a": A_RECORDED})
    try:
        monkeypatch.setenv(PUSH_PORT_ENV, str(fake.raw_tcp_port))
        rc, out, err = run(capsys, "card", "commit", f"{fake.host}:{fake.control_port}", "--yes")
        assert rc == ExitCode.OK, err
        assert "synth (0x01007a57) is the power-on default" in out
        rc, out, err = run(capsys, "card", "clear", f"{fake.host}:{fake.control_port}", "--yes")
        assert rc == ExitCode.OK, err
        assert "greybox loads at the next power-on" in out and fake.commits
    finally:
        fake.stop()


# --- the OS slots: the same gap, fixed the same way -------------------------------------------------


def test_slot_push_commit_verify_rollback_go_through_the_service(service, bundle, capsys):
    with held_by_the_app(slots={"a": A_RECORDED}) as (fake, live, target, _eng):
        rc, out, err = run(capsys, "slot", "push", target, "--bundle", str(bundle), "--yes")
        assert rc == ExitCode.OK, err
        assert "slot B holds linux_slot.img, read back" in out and "release 1.1.0" in out
        rc, out, err = run(capsys, "--json", "slot", "commit", target, "--slot", "B", "--yes")
        assert rc == ExitCode.OK, err
        data = json.loads(out)
        assert data["default"] == "B" and data["pending_commit"] == "B"
        rc, out, err = run(capsys, "slot", "rollback", target, "--yes", "--no-reboot")
        assert rc == ExitCode.OK, err
        assert "the commit of slot B is undone" in out and fake.slots.deflt == "A"
        rc, out, err = run(capsys, "slot", "verify", target, "--slot", "B")
        assert rc == ExitCode.OK, err
        assert "slot B read back" in out
        assert job_kinds(live) == ["slot_push", "slot_commit", "slot_rollback", "slot_verify"]


def test_twin_a_push_the_bundle_does_not_describe_is_refused_before_any_job(service, bundle,
                                                                           capsys):
    with held_by_the_app(slots={"a": A_RECORDED}) as (fake, live, target, _eng):
        before = (fake.slots.staged, len(fake.push_events))
        other = bundle / "other.img"
        other.write_bytes(make_s0lb(b"\x33" * 8192))
        rc, _out, err = run(capsys, "slot", "push", target, str(other), "--bundle", str(bundle),
                            "--yes")
        assert rc == ExitCode.REFUSED and "is not the image" in err, err
        assert job_kinds(live) == [] and (fake.slots.staged, len(fake.push_events)) == before


def test_twin_the_service_checks_the_bundle_again_and_wants_absolute_paths(service, bundle):
    """The route itself, as another client would call it: a relative path is refused 400, an
    image the bundle does not describe 409, before any job."""
    with held_by_the_app(slots={"a": A_RECORDED}) as (fake, live, target, eng):
        from tests.fakes.t13_daemon import bid_path

        bid = eng.open_boards()[0]
        other = bundle / "other.img"
        other.write_bytes(make_s0lb(b"\x33" * 8192))
        with live.client() as c:
            r = c.post(bid_path(bid) + "/slots/push",
                       json={"confirm": True, "image": "linux_slot.img",
                             "bundle": str(bundle / "linux_bundle.json")})
            assert r.status_code == 400 and "absolute" in r.json()["error"]["message"], r.text
            r = c.post(bid_path(bid) + "/slots/push",
                       json={"confirm": True, "image": str(other),
                             "bundle": str(bundle / "linux_bundle.json")})
            assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED", r.text
            r = c.post(bid_path(bid) + "/slots/push", json={"image": str(other)})
            assert r.status_code == 400 and "confirm" in r.json()["error"]["message"]
            r = c.post(bid_path(bid) + "/slots/commit", json={"confirm": True, "slot": "C"})
            assert r.status_code == 400
        assert job_kinds(live) == [] and fake.slots.staged is None
