"""LINUX-SLOTS: the MPS3 OS-slot client and the user-microSD verbs against pyverify's FakeShell.

The board is ``FakeShell(profile="linux", slots=..., usd_card=...)`` (pyverify's own model
of harnessd's ``slot`` verb, the kind-2 push, the claim lock, ``usd`` and the re-push
``commit``), plus reboots that boot stage0's pick (``tests.fakes.lxslots_board``). The
session is the real MPS3 pack's. Every behaviour has its negative twin next to it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_manager.core.errors import (
    HeldError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
    UnreachableError,
)
from harness_manager.services.slots import SlotService, push_source
from harness_manager_mps3 import tunnel as T
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from tests.fakes.claimed_lock import board_key_fp, pin_claim
from tests.fakes.lxslots_board import LINUX_SID, TRUSTED, BoardSsh, board_session, slot_board
from tests.fakes.s0lb_image import header_crc, linux_bundle_s0lb, make_s0lb
from tests.fakes.t2_overlays import SYNTH_RM_ID, make_overlay, use_overlay_dirs

SID = f"0x{LINUX_SID:08x}"
OTHER = "0x3f1a560f"


@pytest.fixture
def image(tmp_path) -> Path:
    p = tmp_path / "linux_slot.img"
    p.write_bytes(make_s0lb(b"\x5a" * 8192))
    return p


#: Slot A as harnessd leaves it once it has stamped the record of the slot it booted
#: (SLOT_VERB_DRAFT §5 item 6, HM's answer "yes"): bound to the fabric's static.
A_RECORDED = {"state": "valid", "hdr_crc": 0x3E5E9C2C, "len": 24354312, "sid": LINUX_SID}


@pytest.fixture
def linux(monkeypatch):
    fake = slot_board(slots={"a": A_RECORDED})
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    fake.images[0x3E5E9C2C] = {"harness_version": "1.0.0"}
    session = board_session(fake)
    yield fake, session
    session.close()
    fake.stop()


def slot_ops(fake) -> dict:
    """What a mutation would change on the board: default, staged, the slots, the job."""
    m = fake.slots
    return {"default": m.deflt, "running": m.running, "staged": m.staged, "seq": m.seq,
            "slots": json.dumps(m.slot, sort_keys=True), "job": dict(m.job)}


def push(session, image: Path, static: str = SID, version: str = "1.1.0"):
    return session.os_slots.push(image, static_id=static, sha256="", version=version)


# --- 1. status A/B, and bare metal has none ----------------------------------------------------------


def test_slot_status_reports_both_slots_and_where_a_push_goes(linux):
    fake, session = linux
    assert session.os_slots.slots_reason() == ""
    st = SlotService().status(session)
    assert (st.running, st.default, st.target, st.staged) == ("A", "A", "B", "")
    assert st.card and st.fabric_sid == SID and not st.pending_commit
    a, b = st.slots["A"], st.slots["B"]
    assert (a.state, a.verified, a.hdr_crc) == ("valid", "boot", "0x3e5e9c2c")
    assert (b.state, b.verified) == ("empty", "no")


def test_twin_a_bare_metal_harness_has_no_os_slots_and_is_never_asked(monkeypatch):
    fake = slot_board(profile="bare-metal", slots=None)     # no slot model: `slot` is unknown
    try:
        session = board_session(fake)
        reason = session.os_slots.slots_reason()
        # "bare-metal ... no OS slots" comes from `version` alone: had a slot op been sent,
        # the reason would quote the harness's "unknown op".
        assert "bare-metal harness has no OS slots" in reason and "unknown op" not in reason
        with pytest.raises(UnavailableError) as exc:
            SlotService().status(session)
        assert exc.value.code == 12
        session.close()
    finally:
        fake.stop()


def test_twin_a_linux_harness_with_no_card_has_no_os_slots(monkeypatch):
    fake = slot_board(slots={"card": False})
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    try:
        session = board_session(fake)
        assert "no user microSD card" in session.os_slots.slots_reason()
        session.close()
    finally:
        fake.stop()


# --- 2. push, commit, reboot, then roll back ----------------------------------------------------------


def test_push_commit_reboot_and_rollback(linux, image):
    fake, session = linux
    fake.images[int(header_crc(image.read_bytes()), 16)] = {"harness_version": "1.1.0"}
    svc = SlotService()
    out = svc.push(session, push_source(image, static_id=SID, version="1.1.0"))
    st = out["status"]
    assert out["slot"] == "B" and st.staged == "B" and st.default == "A"     # not committed
    b = st.slots["B"]
    assert b.hdr_crc == header_crc(image.read_bytes()) and b.verified == "readback"
    assert b.version == "1.1.0" and b.image_sha256                           # what HM pushed
    st = svc.commit(session, "B")["status"]
    assert st.default == "B" and st.running == "A" and st.pending_commit == "B"
    session.os_slots.reboot(wait_s=10)
    st = svc.status(session)
    assert (st.running, st.default) == ("B", "B") and st.slots["B"].verified == "boot"
    assert session.identity().harness_version == "1.1.0"
    assert st.slots["A"].verified == "no"                  # what the last boot read is gone
    # healthy but wrong: back to A (verify A, make it the default, reboot, check)
    back = svc.rollback(session, wait_s=10)
    assert back["rebooted"] and back["status"].running == "A"
    assert session.identity().harness_version == "1.0.0"
    assert fake.boots == ["B", "A"]


def test_twin_a_rollback_to_a_slot_nobody_verified_is_refused_by_the_board(linux, image):
    fake, session = linux
    push(session, image)
    session.os_slots.commit("B")
    session.os_slots.reboot(wait_s=10)
    before = slot_ops(fake)
    with pytest.raises(RefusedError, match="slot A not verified"):
        session.os_slots.rollback("A")                    # the adapter alone: no verify first
    assert slot_ops(fake) == before


def test_twin_a_slot_with_no_record_cannot_be_rolled_back_to_after_a_reboot(monkeypatch, image):
    # Slot A as stage0_mkcard.py wrote it (no slot record): once B runs, A cannot be read
    # back as "for this fabric", so the board refuses to make it the default (§5 item 6).
    fake = slot_board()
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    try:
        session = board_session(fake)
        push(session, image)
        session.os_slots.commit("B")
        session.os_slots.reboot(wait_s=10)
        before = slot_ops(fake)
        with pytest.raises(RefusedError, match="no slot record") as exc:
            SlotService().rollback(session, wait_s=10)
        # S1: harnessd stamps a record only on its own push; say so, and how to fix it
        assert "written outside harnessd" in exc.value.hint
        assert "Push it again from Harness Manager" in exc.value.hint
        after = slot_ops(fake)
        assert (after["default"], after["running"]) == (before["default"], before["running"])
        assert fake.boots == ["B"]
        session.close()
    finally:
        fake.stop()


def test_twin_an_image_that_never_comes_up_healthy_leaves_the_old_slot_running(linux, image):
    fake, session = linux
    fake.unhealthy.add(int(header_crc(image.read_bytes()), 16))
    push(session, image)
    session.os_slots.commit("B")
    session.os_slots.reboot(wait_s=10)
    st = session.os_slots.status()
    assert st.running == "A" and fake.boots == ["A"]      # stage0 went back by itself


def test_a_pending_commit_is_undone_without_a_reboot(linux, image):
    fake, session = linux
    push(session, image)
    session.os_slots.commit("B")
    out = SlotService().rollback(session)
    assert not out["rebooted"] and out["status"].default == "A" and fake.reboots == []


# --- 3. rule 1: roll back before a second push -----------------------------------------------------


def test_rule1_a_second_push_after_a_commit_is_refused_before_sending(linux, image):
    fake, session = linux
    push(session, image)
    session.os_slots.commit("B")
    before = slot_ops(fake)
    pushes = len(fake.push_events)
    with pytest.raises(RefusedError) as exc:
        push(session, image)
    assert "committed and not booted" in exc.value.message and "rollback" in exc.value.hint
    assert slot_ops(fake) == before and len(fake.push_events) == pushes


def test_twin_rule1_rollback_first_frees_the_slot_and_the_push_goes(linux, image):
    fake, session = linux
    push(session, image)
    session.os_slots.commit("B")
    out = SlotService().push(session, push_source(image, static_id=SID), rollback_first=True)
    assert out["rolled_back_first"] == "B" and out["slot"] == "B"
    assert out["status"].default == "A" and out["status"].staged == "B"


# --- 4. the image's static must be the fabric's ----------------------------------------------------


def test_an_image_for_another_static_is_refused_before_a_byte_is_sent(linux, image):
    fake, session = linux
    before = slot_ops(fake)
    with pytest.raises(IncompatibleError, match="provisioned for static 0x3f1a560f"):
        push(session, image, static=OTHER)
    assert slot_ops(fake) == before


def test_twin_the_board_refuses_it_too_when_the_host_check_is_bypassed(linux, image):
    from pyverify import slot as pv

    fake, session = linux
    pv.push_slot_image(image.read_bytes(), fake.host, static_id=int(OTHER, 16),
                       port=fake.raw_tcp_port)
    st = session.os_slots.status()
    assert st.job.state == "failed" and "!= fabric" in st.job.err and not st.staged


# --- the claim lock: mutations through the board's own SSH ---------------------------------------------


@pytest.fixture
def claimed(monkeypatch):
    """A board THIS Harness Manager claimed (CLAIMED-LOCK: its pin and its record)."""
    fake = slot_board(ssh_claimed=True, slots={"trusted_peer": TRUSTED},
                      ssh_host_key_sha256=board_key_fp())
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    ssh = BoardSsh(fake)
    monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
    monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
    session = board_session(fake)
    pin_claim(session)
    yield fake, session, ssh
    session.close()
    ssh.close()
    fake.stop()


def test_a_claimed_board_takes_the_push_and_commit_through_its_own_ssh(claimed, image):
    fake, session, ssh = claimed
    st = push(session, image)
    assert st.staged == "B" and session.os_slots.last_route == "board-ssh"
    st = session.os_slots.commit("B")
    assert st.default == "B" and session.os_slots.last_route == "board-ssh"
    argv = ssh.launches[-1]
    specs = [argv[i + 1] for i, a in enumerate(argv) if a == "-L"]
    assert any(s.endswith(":127.0.0.1:6900") for s in specs)
    assert any(s.endswith(":127.0.0.1:6910") for s in specs)
    assert all(p.returncode is not None for p in ssh.procs)     # each forward closed after use


def test_twin_a_claimed_board_refuses_a_direct_commit_and_nothing_changes(claimed, image):
    fake, session, ssh = claimed
    push(session, image)
    before = slot_ops(fake)
    from pyverify import slot as pv

    with pytest.raises(pv.SlotError, match="slot locked"):
        pv.slot_request(fake.host, "commit", port=fake.control_port)
    assert slot_ops(fake) == before


def test_twin_when_the_boards_ssh_will_not_come_up_nothing_changes(claimed, image):
    fake, session, ssh = claimed
    push(session, image)
    ssh.fail = "auth"
    before = slot_ops(fake)
    with pytest.raises(UnreachableError, match="go through its SSH"):
        session.os_slots.commit("B")
    assert slot_ops(fake) == before


# --- 6. the user microSD: no card, then a card ------------------------------------------------------


def usd_state(fake) -> dict:
    return {"card": fake.usd_card, "commits": list(fake.commits),
            "slots": repr(fake.usd_slots), "default": fake.usd_default_valid}


def test_card_commands_with_no_card_refuse_cleanly_and_touch_nothing(linux):
    fake, session = linux
    svc = SlotService()
    st = svc.card_status(session)
    assert not st.present and st.state == "none" and st.default is None
    assert any("boots exactly as it always has" in n for n in st.notes)
    before = usd_state(fake)
    for act in (lambda: svc.card_commit(session), lambda: svc.card_clear(session)):
        with pytest.raises(RefusedError) as exc:
            act()
        assert "no card" in exc.value.message and exc.value.code == 15
        assert "boots exactly as it always has" in exc.value.hint
    assert usd_state(fake) == before


@pytest.fixture
def with_card(tmp_path, monkeypatch):
    root = tmp_path / "overlays"
    make_overlay(root, "synth", rm_id=SYNTH_RM_ID, static_id=LINUX_SID)
    use_overlay_dirs(monkeypatch, root)
    fake = slot_board(usd_card="da", boot_rm_id=SYNTH_RM_ID)
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    session = board_session(fake)
    yield fake, session
    session.close()
    fake.stop()


def test_twin_with_a_card_status_commit_and_clear_work(with_card):
    fake, session = with_card
    svc = SlotService()
    st = svc.card_status(session)
    assert st.present and st.state == "empty" and st.committable and st.default is None
    assert st.os_slots is not None and st.os_slots.running == "A"   # the same card's OS slots
    out = svc.card_commit(session)
    assert out["rm_name"] == "synth" and out["slot"] in ("A", "B")
    assert fake.commits == [("synth", out["slot"])]
    st = svc.card_status(session)
    assert st.state == "valid" and st.default["rm_name"] == "synth"
    assert st.default["static_id"] == SID
    st = svc.card_clear(session)
    assert st.default is None and st.state != "valid"


def test_twin_a_card_commit_of_the_greybox_or_an_unknown_rm_is_refused(with_card, monkeypatch,
                                                                        tmp_path):
    from harness_manager.core.errors import AbsentError

    fake, session = with_card
    fake.current_rm_id = 0
    with pytest.raises(RefusedError, match="greybox"):
        SlotService().card_commit(session)
    fake.current_rm_id = 0x0100_7A58                       # a design this host has no copy of
    with pytest.raises(AbsentError, match="not in this host's overlay store"):
        SlotService().card_commit(session)
    assert fake.commits == []


# --- 7. bare metal: no slot or card commands, nothing changes --------------------------------------------


def test_bare_metal_has_no_card_store_either_and_nothing_is_sent(monkeypatch):
    fake = slot_board(profile="bare-metal", slots=None, usd_card="da",
                      features=("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed"))
    try:
        session = board_session(fake)
        svc = SlotService()
        before = usd_state(fake)
        for act in (lambda: svc.card_status(session), lambda: svc.card_commit(session),
                    lambda: svc.card_clear(session), lambda: svc.status(session)):
            with pytest.raises(UnavailableError):
                act()
        assert usd_state(fake) == before and fake.slots is None
        session.close()
    finally:
        fake.stop()


# --- the lease ----------------------------------------------------------------------------------


def test_every_change_asks_the_lease_first_and_reads_do_not(linux, image):
    fake, session = linux
    asked: list[str] = []

    def refuse(_session, what):
        asked.append(what)
        raise HeldError(f"cannot {what}: it is for the lease holder only")

    svc = SlotService(lease_check=refuse)
    svc.status(session)
    svc.card_status(session)
    before = slot_ops(fake)
    for act in (lambda: svc.push(session, push_source(image, static_id=SID)),
                lambda: svc.commit(session), lambda: svc.rollback(session),
                lambda: svc.card_clear(session)):
        with pytest.raises(HeldError):
            act()
    assert len(asked) == 4 and slot_ops(fake) == before


# --- push sources: the bundle names the static, the sha and the frames -----------------------------


def bundle_for(tmp_path: Path, image: Path, **over) -> Path:
    import hashlib

    data = image.read_bytes()
    doc = {"schema": "mps3-linux-bundle", "schema_version": "1", "fieldable": True,
           "harness": "1.1.0", "static_id": SID,
           "targets": {"ethernet": {
               "slot_image": {"name": "linux_slot.img", "sha256": hashlib.sha256(data).hexdigest(),
                              "bytes": len(data), "s0lb": linux_bundle_s0lb(data)},
               "provisioned": {"static_id": SID.upper().replace("0X", "0x")}}}}
    for k, v in over.items():
        doc[k] = v
    p = tmp_path / "linux_bundle.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_a_bundle_names_the_static_and_version_of_its_image(tmp_path, image):
    src = push_source(None, bundle=bundle_for(tmp_path, image))
    assert src.image == image and src.static_id == SID and src.version == "1.1.0"


def test_twin_a_bundle_that_does_not_describe_the_image_is_refused(tmp_path, image):
    b = bundle_for(tmp_path, image)
    image.write_bytes(make_s0lb(b"\x11" * 64))
    with pytest.raises(RefusedError, match="is not the image"):
        push_source(None, bundle=b)
    from harness_manager.core.errors import UsageError

    with pytest.raises(UsageError, match="never assumed"):
        push_source(image)                                 # no bundle, no static: never guess
