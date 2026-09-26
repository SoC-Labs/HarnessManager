"""LINUX-ANSWERS against a board: pyverify's FakeShell as ``AnswersBoard`` (tests/fakes/).

The fake reboots as the Linux lead says stage0 does (S9, ``SlotBoard``): the default slot if
it comes up healthy, else the other slot, else rescue, and the default STAYS on a slot that
failed. It can also send the fields the Linux lead proposed (``confirmed``, ``claimed``,
``code``: ``AnswersBoard``). The
session is the real MPS3 pack's; the CLI and the daemon are the real ones. Every behaviour
has its negative twin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager.services import slot_health as H
from harness_manager.services.slots import SlotService
from harness_manager_mps3 import shell as shellmod
from harness_manager_mps3 import slot_words as W
from harness_manager_mps3.deploy import PUSH_PORT_ENV
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from tests.fakes.lxanswers_board import answers_board
from tests.fakes.lxslots_board import LINUX_SID, TRUSTED, board_session
from tests.fakes.s0lb_image import header_crc, make_s0lb
from tests.integration.test_lxslots_cli import run

SID = f"0x{LINUX_SID:08x}"
A_RECORDED = {"state": "valid", "hdr_crc": 0x3E5E9C2C, "len": 24354312, "sid": LINUX_SID}


@pytest.fixture
def image(tmp_path) -> Path:
    p = tmp_path / "linux_slot.img"
    p.write_bytes(make_s0lb(b"\x5a" * 8192))
    return p


def cli_board(monkeypatch, **kw):
    """An ``AnswersBoard`` the CLI reaches (test_lxslots_cli's ``board``, this fake)."""
    fake = answers_board(**kw)
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    monkeypatch.setenv(PUSH_PORT_ENV, str(fake.raw_tcp_port))
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    return fake, f"{fake.host}:{fake.control_port}"


@pytest.fixture
def make(monkeypatch):
    made = []

    def _make(**kw):
        kw.setdefault("slots", {"a": A_RECORDED})
        fake = answers_board(**kw)
        monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
        session = board_session(fake)
        made.append((fake, session))
        return fake, session

    yield _make
    for fake, session in made:
        session.close()
        fake.stop()


def push_commit_reboot(session, image: Path) -> None:
    session.os_slots.push(image, static_id=SID, sha256="", version="1.1.0")
    session.os_slots.commit("B")
    session.os_slots.reboot(wait_s=10)


# --- 10 + 11. the fake boots what stage0 would; after a reboot, target is the OTHER slot -------


def test_a_healthy_new_slot_runs_as_the_default_and_the_target_is_the_other_slot(make, image):
    fake, session = make()
    push_commit_reboot(session, image)
    st = session.os_slots.status()
    assert (st.running, st.default, st.target, st.staged) == ("B", "B", "A", "")
    assert st.job.state == "idle" and fake.boots == ["B"] and H.fell_back(st) == ""


def test_twin_an_unhealthy_default_falls_back_and_the_default_stays(make, image):
    fake, session = make(unhealthy={"B"})
    push_commit_reboot(session, image)
    st = session.os_slots.status()
    assert (st.running, st.default, st.target, st.staged) == ("A", "B", "", "")
    assert fake.boots == ["A"] and H.fell_back(st) == "B"
    assert st.slots["B"].verified == "no" and st.job.state == "idle"   # per-boot state gone


def test_twin_no_healthy_slot_boots_rescue(make, image):
    fake, session = make(unhealthy={"A", "B"})
    push_commit_reboot(session, image)
    assert fake.slots.running == "rescue" and fake.boots == ["rescue"]


# --- 2. a fallback is said plainly, blocks a push with the right fix, and rollback fixes it ------


def test_after_a_fallback_the_push_says_so_and_rollback_frees_the_slot(make, image):
    fake, session = make(unhealthy={"B"})
    push_commit_reboot(session, image)
    pushes = len(fake.push_events)
    with pytest.raises(RefusedError) as exc:
        session.os_slots.push(image, static_id=SID)
    assert "slot B failed to boot" in exc.value.message and "slot rollback" in exc.value.hint
    assert "committed and not booted" not in exc.value.message and len(fake.push_events) == pushes
    out = SlotService().rollback(session)
    st = out["status"]
    assert not out["rebooted"] and (st.running, st.default, st.target) == ("A", "A", "B")
    fake.unhealthy.clear()
    assert session.os_slots.push(image, static_id=SID).staged == "B"    # the slot is free again


def test_slot_status_says_a_fallback_plainly(capsys, monkeypatch, image):
    fake, target = cli_board(monkeypatch, slots={"a": A_RECORDED}, unhealthy={"B"})
    try:
        session = board_session(fake)
        push_commit_reboot(session, image)
        session.close()
        rc, out, _ = run(capsys, "slot", "status", target)
        assert rc == 0 and "slot B failed to boot; A is running; roll back to make A the " \
            "default" in out and "FAILED TO BOOT" in out
        rc, out, _ = run(capsys, "--json", "slot", "status", target)
        data = json.loads(out)
        assert data["fell_back"] == "B" and data["committed_unbooted"] is None
        assert data["slots"]["A"]["boot"] == "booted (not yet confirmed)"
        rc, out, _ = run(capsys, "slot", "rollback", target, "--yes")
        assert rc == 0 and "slot A is the default again (slot B failed to boot)" in out
        rc, out, _ = run(capsys, "slot", "status", target)
        assert "failed to boot" not in out                  # twin: fixed, nothing to say
    finally:
        fake.stop()


# --- 3. booted is not confirmed; the field, when sent, says it ----------------------------------


def test_status_never_calls_a_boot_confirmed_until_the_board_says_so(capsys, monkeypatch):
    fake, target = cli_board(monkeypatch, slots={"a": A_RECORDED})
    try:
        rc, out, _ = run(capsys, "slot", "status", target)
        assert "booted (not yet confirmed)" in out and "confirmed healthy" not in out
        fake.reports_confirmed = True
        rc, out, _ = run(capsys, "slot", "status", target)
        assert "booted, confirmed healthy" in out
        fake.confirmed = False
        rc, out, _ = run(capsys, "--json", "slot", "status", target)
        data = json.loads(out)
        assert data["confirmed"] is False and data["slots"]["A"]["boot"] == \
            "booted (not yet confirmed)"
    finally:
        fake.stop()


# --- 1. the identity lock blocks a flip to a booted slot, never the fix -------------------------


def test_the_identity_lock_refuses_a_flip_to_the_booted_slot_with_the_fix(make, image):
    fake, session = make(slots={"a": A_RECORDED,
                                "identity_lock": "image 0x0badcafe != fabric 0x72bb0a36"})
    session.os_slots.push(image, static_id=SID)
    st = session.os_slots.commit("B")          # the fix: a read-back slot ignores the lock
    assert st.default == "B"
    with pytest.raises(W.IdentityLockError) as exc:
        SlotService().rollback(session)        # back to A, known only by its boot: locked
    assert exc.value.kind == W.LOCK_MISMATCH and "slot commit" in exc.value.hint
    assert not isinstance(exc.value, W.ClaimLockedError) and fake.slots.deflt == "B"


# --- 5. additive fields: claimed, the slot feature by name, codes --------------------------------


def test_claimed_in_slot_status_answers_without_identify_or_the_lock_probe(make, monkeypatch):
    fake, session = make(reports_claimed=True, ssh_claimed=True)
    asked = []
    monkeypatch.setattr(shellmod, "identify_quietly", lambda *a, **k: asked.append(a))
    st = session.os_slots.status()
    fake.slot_acts.clear()
    assert session.os_slots.claimed(st) is True
    assert asked == [] and fake.slot_acts == []
    fake.ssh_claimed = False
    assert session.os_slots.claimed(session.os_slots.status()) is False


def test_twin_without_claimed_the_adapter_asks_identify_then_probes(make, monkeypatch):
    fake, session = make(ssh_claimed=True, slots={"a": A_RECORDED, "trusted_peer": TRUSTED})
    asked = []
    monkeypatch.setattr(shellmod, "identify_quietly", lambda *a, **k: asked.append(a))
    fake.slot_acts.clear()
    assert session.os_slots.claimed() is True
    assert asked and "rollback" in fake.slot_acts           # the no-op guarded lock probe


def test_the_slot_feature_is_matched_by_name_on_any_harness(make):
    fake, session = make()
    fake.impl = None                                         # version: a bare-metal harness
    assert "has no OS slots" in session.os_slots.slots_reason()
    fake.features = (*fake.features, "slot")                 # a harness that reports `slot`
    fresh = board_session(fake)
    try:
        assert fresh.os_slots.slots_reason() == ""
    finally:
        fresh.close()


def test_codes_from_the_board_are_read(make, image):
    fake, session = make(codes=True)
    reply = session.os_slots._ask("commit")
    assert reply.get("code") == "nothing_staged"
    with pytest.raises(RefusedError, match="nothing staged") as exc:
        session.os_slots.commit()
    assert "push an image first" in exc.value.hint


# --- 9. nothing reads a slot back unless asked ---------------------------------------------------


def test_status_and_the_api_never_start_a_verify(capsys, monkeypatch):
    from tests.fakes.t13_daemon import bid_path, headers
    from tests.integration.test_lxslots_api import client_for

    fake, target = cli_board(monkeypatch, slots={"a": A_RECORDED}, usd_card="da")
    try:
        assert run(capsys, "slot", "status", target)[0] == 0
        assert run(capsys, "card", "status", target)[0] == 0
        eng, client = client_for(fake)
        with client:
            r = client.post("/api/v1/boards", json={"target": target, "note": "answers"},
                            headers=headers())
            bid = r.json()["board_id"]
            body = client.get(bid_path(bid) + "/slots", headers=headers()).json()
            assert body["slots"]["slots"]["A"]["boot"] == "booted (not yet confirmed)"
            assert body["slots"]["fell_back"] is None
            client.get(bid_path(bid) + "/card", headers=headers())
        eng.close_all()
        assert fake.slot_acts and "verify" not in fake.slot_acts
        # twin: asking for it sends it
        assert run(capsys, "slot", "verify", target, "--slot", "A")[0] == 0
        assert "verify" in fake.slot_acts
    finally:
        fake.stop()


def test_the_board_s_pushed_image_is_known_by_its_table_crc(make, image):
    fake, session = make()
    st = session.os_slots.push(image, static_id=SID)
    assert st.slots["B"].hdr_crc == header_crc(image.read_bytes())
