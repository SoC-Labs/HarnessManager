"""LINUX-SLOTS: ``harness-manager slot|card`` end to end, in-process, against pyverify's FakeShell.

The CLI runs exactly as a user runs it (``main([...])``, the in-process engine, the real
MPS3 pack). The fake board's ports come in through the pack's env seams. Each case has
its negative twin: a refused change leaves the board exactly as it was.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from harness_manager.cli.main import main
from harness_manager_mps3.deploy import PUSH_PORT_ENV
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from tests.fakes.lxslots_board import LINUX_SID, slot_board
from tests.fakes.s0lb_image import linux_bundle_s0lb, make_s0lb
from tests.fakes.t2_overlays import SYNTH_RM_ID, make_overlay, use_overlay_dirs

SID = f"0x{LINUX_SID:08x}"
A_RECORDED = {"state": "valid", "hdr_crc": 0x3E5E9C2C, "len": 24354312, "sid": LINUX_SID}


def run(capsys, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        rc = main(list(argv))
    finally:
        sys.stdin = old
    out, err = capsys.readouterr()
    return rc, out, err


def board(monkeypatch, **kw):
    fake = slot_board(**kw)
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    monkeypatch.setenv(PUSH_PORT_ENV, str(fake.raw_tcp_port))
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    return fake, f"{fake.host}:{fake.control_port}"


@pytest.fixture
def linux(monkeypatch):
    fake, target = board(monkeypatch, slots={"a": A_RECORDED})
    yield fake, target
    fake.stop()


@pytest.fixture
def bundle(tmp_path):
    import hashlib

    data = make_s0lb(b"\x5a" * 8192)
    (tmp_path / "linux_slot.img").write_bytes(data)
    doc = {"schema": "mps3-linux-bundle", "schema_version": "1", "fieldable": True,
           "harness": "1.1.0", "static_id": SID.upper().replace("0X", "0x"),
           "targets": {"ethernet": {
               "slot_image": {"name": "linux_slot.img", "sha256": hashlib.sha256(data).hexdigest(),
                              "bytes": len(data), "s0lb": linux_bundle_s0lb(data)},
               "provisioned": {"static_id": SID}}}}
    (tmp_path / "linux_bundle.json").write_text(json.dumps(doc), encoding="utf-8")
    return tmp_path


def test_slot_status_push_commit_status(capsys, linux, bundle):
    fake, target = linux
    rc, out, _ = run(capsys, "slot", "status", target)
    assert rc == 0 and "running A, default A, a push goes to B" in out
    rc, out, err = run(capsys, "slot", "push", target, "--bundle", str(bundle), "--yes")
    assert rc == 0, err
    assert "slot B holds linux_slot.img, read back" in out and "release 1.1.0" in out
    rc, out, _ = run(capsys, "--json", "slot", "commit", target, "--slot", "B", "--yes")
    data = json.loads(out)
    assert rc == 0 and data["default"] == "B" and data["pending_commit"] == "B"
    assert data["slots"]["B"]["verified"] == "readback"
    rc, out, _ = run(capsys, "--tsv", "slot", "status", target)
    rows = [ln.split("\t") for ln in out.splitlines()]
    assert rc == 0 and [r[1] for r in rows] == ["A", "B"]
    assert all(len(r) == 12 for r in rows)                         # the `slot` layout
    assert rows[1][4] == "yes" and rows[1][9] == "1.1.0"          # B: default, release 1.1.0


def test_twin_an_unconfirmed_push_sends_nothing(capsys, linux, bundle):
    fake, target = linux
    before = (fake.slots.staged, dict(fake.slots.job), len(fake.push_events))
    rc, _, err = run(capsys, "slot", "push", target, "--bundle", str(bundle), stdin="n\n")
    assert rc == 15 and "not confirmed" in err
    assert (fake.slots.staged, dict(fake.slots.job), len(fake.push_events)) == before


def test_twin_rule1_a_second_push_is_refused_until_rolled_back(capsys, linux, bundle):
    fake, target = linux
    assert run(capsys, "slot", "push", target, "--bundle", str(bundle), "--yes")[0] == 0
    assert run(capsys, "slot", "commit", target, "--yes")[0] == 0
    rc, _, err = run(capsys, "slot", "push", target, "--bundle", str(bundle), "--yes")
    assert rc == 15 and "rule 1" in err and fake.slots.deflt == "B"
    rc, out, err = run(capsys, "slot", "push", target, "--bundle", str(bundle), "--yes",
                       "--rollback-first")
    assert rc == 0 and "pending commit of slot B was rolled back first" in out
    assert fake.slots.deflt == "A" and fake.slots.staged == "B"


def test_slot_rollback_after_booting_the_new_image(capsys, linux, bundle):
    fake, target = linux
    run(capsys, "slot", "push", target, "--bundle", str(bundle), "--yes")
    run(capsys, "slot", "commit", target, "--yes")
    fake.slots.running = fake.slots.deflt = "B"          # booted into it (a reboot, elsewhere)
    fake.slots.staged = None
    fake.slots.boot_crc = fake.slots.slot["B"]["hdr_crc"]
    fake.slots.vcrc = {"A": 0, "B": 0}
    rc, out, err = run(capsys, "slot", "rollback", target, "--yes", "--wait", "10")
    assert rc == 0, err
    assert "slot A runs again (rebooted)" in out and fake.boots == ["A"]


def test_card_status_without_a_card_and_every_change_refused(capsys, linux):
    fake, target = linux
    rc, out, _ = run(capsys, "card", "status", target)
    assert rc == 0 and "no card (the board boots exactly as it always has)" in out
    for act in ("commit", "clear"):
        rc, _, err = run(capsys, "card", act, target, "--yes")
        assert rc == 15 and "no card" in err
    assert fake.commits == [] and fake.usd_card is None


def test_twin_card_status_commit_clear_with_a_card(capsys, monkeypatch, tmp_path):
    root = tmp_path / "ovl"
    make_overlay(root, "synth", rm_id=SYNTH_RM_ID, static_id=LINUX_SID)
    use_overlay_dirs(monkeypatch, root)
    fake, target = board(monkeypatch, usd_card="da", boot_rm_id=SYNTH_RM_ID,
                         slots={"a": A_RECORDED})
    try:
        rc, out, _ = run(capsys, "card", "status", target)
        assert rc == 0 and "card" in out and "empty" in out and "default    none" in out
        assert "os slots" in out
        rc, out, err = run(capsys, "card", "commit", target, "--yes")
        assert rc == 0, err
        assert "synth (0x01007a57) is the power-on default" in out
        rc, out, _ = run(capsys, "--json", "card", "status", target)
        data = json.loads(out)
        assert data["default"]["rm_name"] == "synth" and data["present"] is True
        assert data["os_slots"]["running"] == "A" and "default synth" in data["line"]
        rc, out, _ = run(capsys, "--tsv", "card", "status", target)
        row = out.rstrip("\n").split("\t")
        from harness_manager.cli.output import TSV_COLUMNS

        assert rc == 0 and len(row) == len(TSV_COLUMNS["card"]) == 11
        assert row[1:6] == ["yes", "valid", "15193", "synth", row[5]] and row[8] == "A"
        rc, out, _ = run(capsys, "card", "clear", target, "--yes")
        assert rc == 0 and "greybox loads at the next power-on" in out
        assert fake.commits and fake.commits[0][0] == "synth"
    finally:
        fake.stop()


def test_bare_metal_slot_and_card_stop_at_exit_12_and_change_nothing(capsys, monkeypatch, bundle):
    fake, target = board(monkeypatch, profile="bare-metal", slots=None, usd_card="da",
                         features=("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed"))
    try:
        for argv in (("slot", "status", target), ("card", "status", target),
                     ("slot", "push", target, "--bundle", str(bundle), "--yes"),
                     ("card", "commit", target, "--yes"), ("card", "clear", target, "--yes")):
            rc, out, err = run(capsys, *argv)
            assert rc == 12 and out == "", (argv, err)
        assert "bare-metal harness has no OS slots" in run(capsys, "slot", "status", target)[2]
        assert "this harness has no microSD store" in run(capsys, "card", "status", target)[2]
        assert fake.commits == [] and len(fake.push_events) == 0 and fake.slots is None
    finally:
        fake.stop()
