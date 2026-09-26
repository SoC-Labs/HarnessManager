"""LINUX-ANSWERS: Harness Manager matches what the Linux harness really does.

The Linux lead's answers to HM's slot (S1-S9) and claim (C1-C5) questions, read from
harnessd's code (``HM_ANSWERS_2026-09-26.md`` on feat/linux-harness), corrected several HM
assumptions. Board-free here: the refusal words (``harness_manager_mps3.slot_words``), what a
status says about the boot (``services.slot_health``), the planner, the update executor
against ``FakeOsSlots``, and the CRC and schema_version comparisons. Every behaviour sits
next to its negative twin.
"""

from __future__ import annotations

import dataclasses
import json
import zlib

import pytest

from harness_manager.core.errors import (
    ActionFailedError,
    ExitCode,
    HeldError,
    IncompatibleError,
    RefusedError,
)
from harness_manager.core.pack import SlotInfo, SlotJob, SlotStatus
from harness_manager.services import slot_health as H
from harness_manager.services.update import RESULT_INSTALLED, RESULT_WRITTEN
from harness_manager.services.update.planner import BoardView, make_plan, os_image_running
from harness_manager.services.update.schema import Asset, Component, os_header_crc
from harness_manager_mps3 import slot_words as W
from harness_manager_mps3.deploy import _refusal_error
from tests.fakes.s0lb_image import header_crc, linux_bundle_s0lb, make_s0lb
from tests.fakes.t7_board import FakeOsSlots
from tests.unit.test_t7_os_slots import IMAGE, plan_for, world  # noqa: F401 - fixture

VALID = "valid"


def status(running="A", default="A", staged="", target="", raw=None, **slots) -> SlotStatus:
    infos = {n: SlotInfo(n, state=VALID, hdr_crc=f"0x{n.lower() * 8}", verified=v)
             for n, v in (slots or {"A": "boot", "B": "no"}).items()}
    return SlotStatus(running=running, default=default, staged=staged, target=target,
                      slots=infos, raw=dict(raw or {}))


# --- 1. two locks, not one ----------------------------------------------------------------------


def test_the_claim_lock_is_its_own_error_with_its_own_hint():
    exc = W.refusal("commit", W.SLOT_LOCKED_ERR)
    assert isinstance(exc, W.ClaimLockedError) and not isinstance(exc, W.IdentityLockError)
    assert "claim lock, not the identity lock" in exc.hint and "board claim-status" in exc.hint


def test_twin_the_identity_lock_is_not_the_claim_lock():
    exc = W.refusal("rollback", "slot A runs, but identity lock: image 0x0badcafe != fabric 0x7")
    assert isinstance(exc, W.IdentityLockError) and not isinstance(exc, W.ClaimLockedError)
    assert not isinstance(exc, IncompatibleError) and exc.code == ExitCode.REFUSED
    assert "not the SSH claim" in exc.hint


@pytest.mark.parametrize("reason", ["image 0x0badcafe != fabric 0x72bb0a36",
                                    "usr_access 0x01000002 != image 0x01000001"])
def test_an_identity_lock_mismatch_says_push_commit_reboot(reason):
    exc = W.identity_lock_error(f"identity lock: {reason}", "the swap")
    assert exc.kind == W.LOCK_MISMATCH and not exc.fabric_unknown and reason in exc.message
    for step in ("slot push", "slot commit", "reboot"):
        assert step in exc.hint
    assert "give up" not in exc.hint.lower()


@pytest.mark.parametrize("reason,fabric", [
    ("no stage0 status block mapped", True), ("no valid stage0 status block", True),
    ("fabric static_id unknown", True), ("image static_id not provisioned", False)])
def test_twin_an_identity_lock_unknown_is_split_from_a_mismatch(reason, fabric):
    exc = W.identity_lock_error(f"identity lock: {reason}")
    assert exc.kind == W.LOCK_UNKNOWN and exc.fabric_unknown is fabric
    if fabric:        # the board refuses slot writes too (slot_linux.c fabric_err): say so
        assert "refuses slot writes" in exc.hint and "slot push" not in exc.hint
    else:             # the image's claim is missing: a push of the right image fixes it
        assert "slot push" in exc.hint and "slot commit" in exc.hint


def test_twin_a_push_for_another_static_is_not_an_identity_lock():
    # "image FOR 0x.. != fabric" is the push's own static check (a job failure), not the lock
    assert W.identity_lock_error("image for 0x3f1a560f != fabric 0x72bb0a36") is None
    job = SlotJob(act="push", slot="B", state="failed",
                  err="image for 0x3f1a560f != fabric 0x72bb0a36")
    assert isinstance(W.job_failure(job), IncompatibleError)


def test_a_swap_refused_by_the_identity_lock_gets_the_same_words():
    ov = dataclasses.make_dataclass("Ov", [("name", str)])("nanosoc")
    exc = _refusal_error({"ok": False, "err": "identity lock: image 0x1 != fabric 0x2"}, ov)
    assert isinstance(exc, W.IdentityLockError) and exc.kind == W.LOCK_MISMATCH
    # twin: the bare-metal fabric-mismatch markers still mean incompatible
    assert isinstance(_refusal_error({"ok": False, "err": "fabric mismatch"}, ov),
                      IncompatibleError)


# --- 2. fallback detection ----------------------------------------------------------------------


def test_a_fallback_is_running_not_default_with_nothing_staged():
    st = status(running="A", default="B", staged="")
    assert H.fell_back(st) == "B" and H.committed_unbooted(st) == ""
    line = H.fallback_line(st)
    assert line.startswith("slot B failed to boot; A is running; roll back to make A the "
                           "default")
    assert "failed to boot" in H.fallback_hint(st) and "slot rollback" in H.fallback_hint(st)


def test_twin_a_commit_waiting_for_its_reboot_is_not_a_fallback():
    st = status(running="A", default="B", staged="B")
    assert H.fell_back(st) == "" and H.committed_unbooted(st) == "B"
    assert "rule 1" in H.fallback_hint(st) and "failed to boot" not in H.fallback_hint(st)
    assert H.fell_back(status()) == ""                              # running == default


def test_the_push_refusal_no_free_slot_maps_to_the_fallback_hint():
    err = "no free slot: A runs, B is the default -- rollback first"
    job = SlotJob(act="push", slot="", state="failed", err=err)
    fell = W.job_failure(job, st=status(running="A", default="B"))
    assert isinstance(fell, RefusedError) and "failed to boot" in fell.hint
    # twin: rule 1 when the status shows the commit; both when it is not known
    assert "rule 1" in W.job_failure(job, st=status(running="A", default="B",
                                                    staged="B")).hint
    both = W.job_failure(job).hint
    assert "rule 1" in both and "failed to boot" in both
    assert "failed to boot" in W.refusal("push", err, st=status(running="A",
                                                                  default="B")).hint


def view(**kw) -> BoardView:
    from harness_manager.core.model import BoardIdentity
    from tests.fakes.t7_bundles import FIELDED_STATIC

    ident = BoardIdentity(board_type="mps3", shell_id=FIELDED_STATIC, harness_version="1.1.0",
                          harness_impl="linux")
    return BoardView(board_id="mps3@test", pack="mps3", identity=ident, has_os_slots=True,
                     **kw)


def test_the_planner_offers_rollback_after_a_fallback(world):  # noqa: F811
    ch = world["verified"].channel
    plan = make_plan(ch, view(os_fell_back="B", os_running="A", os_pending="B"),
                     app_version="0.1.0")
    assert any(w.startswith("slot B failed to boot; A is running; roll back to make A the "
                            "default") for w in plan.warnings)
    step = next(s for s in plan.steps if s.action == "rollback-os-slot")
    assert "failed to boot" in step.detail and "rule 1" not in step.detail
    assert plan.summary()["os_fell_back"] == "B"


def test_twin_a_pending_commit_is_planned_as_rule_1_not_as_a_fallback(world):  # noqa: F811
    plan = make_plan(world["verified"].channel, view(os_pending="B"), app_version="0.1.0")
    assert not any("failed to boot" in w for w in plan.warnings)
    step = next(s for s in plan.steps if s.action == "rollback-os-slot")
    assert "rule 1" in step.detail and plan.summary()["os_fell_back"] is None


def test_the_planner_warns_when_the_failed_slot_holds_this_very_image(tmp_path):
    from tests.unit.test_lxslots_update import IMAGE as DECLARED
    from tests.unit.test_lxslots_update import parsed

    ch, rel = parsed(tmp_path)                      # a release that declares its S0LB frames
    assert os_header_crc(rel.component("os-slot")) == header_crc(DECLARED)
    plan = make_plan(ch, view(os_fell_back="B", os_running="A",
                              os_fell_back_crc=header_crc(DECLARED).upper().replace("0X", "0x")),
                     app_version="0.1.0")
    assert any("THIS release's OS image" in w for w in plan.warnings)
    twin = make_plan(ch, view(os_fell_back="B", os_running="A", os_fell_back_crc="0x12345678"),
                     app_version="0.1.0")
    assert not any("THIS release's OS image" in w for w in twin.warnings)


def test_the_update_service_reads_a_fallback_off_the_board(world):  # noqa: F811
    from harness_manager.services.update.service import UpdateService

    slots = world["slots"]
    slots.unhealthy.add("B")
    slots.slots["B"] = SlotInfo("B", state=VALID, hdr_crc="0xbbbbbbbb", verified="readback")
    slots.staged, slots.default = "B", "B"
    slots.reboot()
    assert (slots.running, slots.default) == ("A", "B")
    svc = UpdateService.__new__(UpdateService)
    svc.os_slots_for = lambda session: slots
    svc.hub_door_view = lambda session: {}
    v = svc.board_view(world["session"])
    assert (v.os_fell_back, v.os_running, v.os_fell_back_crc) == ("B", "A", "0xbbbbbbbb")
    assert "verify" not in " ".join(slots.calls)                   # status only, never verify
    # twin: a healthy reboot is no fallback
    slots.unhealthy.clear()
    slots.default = "A"
    slots.reboot()
    assert svc.board_view(world["session"]).os_fell_back == ""


# --- 3. verified: boot is not "confirmed" --------------------------------------------------------


def test_a_booted_slot_is_never_called_confirmed_without_the_confirmed_field():
    st = status()
    assert H.boot_words(st, "A") == "booted (not yet confirmed)"
    assert H.confirmed(st) is None and "confirm" in " ".join(H.notes(st))
    assert H.boot_words(status(raw={"confirmed": False}), "A") == "booted (not yet confirmed)"
    # twin: only the field says confirmed
    assert H.boot_words(status(raw={"confirmed": True}), "A") == "booted, confirmed healthy"
    assert H.boot_words(status(A="readback", B="no"), "A") == "read back this boot"


def run_update(w, **fake):
    for k, v in fake.items():
        setattr(w["slots"], k, v)
    plan = plan_for(w)
    return w["installer"].run(w["session"], plan, plan.approve(), w["verified"])


def os_check(out):
    return next(c for c in out.checks if c.name == "os slot")


def test_an_update_waits_for_harnessds_confirm_and_says_confirmed(world):  # noqa: F811
    world["installer"].sleep = lambda s: None
    out = run_update(world, reports_confirmed=True, confirm_after=3)
    assert out.result == RESULT_INSTALLED, out.detail
    assert "confirmed healthy" in os_check(out).detail and out.os_slot["confirmed"] is True


def test_twin_a_boot_harnessd_never_confirms_is_not_installed(world):  # noqa: F811
    world["installer"].sleep = lambda s: None
    out = run_update(world, reports_confirmed=True, confirm_after=-1)
    assert out.result == RESULT_WRITTEN
    assert "has not confirmed a healthy boot" in os_check(out).detail
    assert out.os_slot["confirmed"] is False


def test_twin_without_the_field_the_boot_is_accepted_but_never_called_confirmed(world):  # noqa: F811
    out = run_update(world)
    assert out.result == RESULT_INSTALLED, out.detail
    detail = os_check(out).detail
    assert "booted (not yet confirmed)" in detail and "confirmed healthy" not in detail
    assert out.os_slot["confirmed"] is None


def test_the_plan_step_does_not_call_a_boot_a_confirm(world):  # noqa: F811
    step = next(s for s in plan_for(world).steps if s.action == "confirm-os-slot")
    assert "a boot alone is not a confirm" in step.detail
    assert "verified by its boot" not in step.detail


def test_wait_confirmed_reads_only_until_the_field_is_not_false():
    reads = iter([status(raw={"confirmed": False})] * 2 + [status(raw={"confirmed": True})])
    got = H.wait_confirmed(lambda: next(reads), sleep=lambda s: None)
    assert H.confirmed(got) is True
    # twin: a harness that never reports it is read once, never waited on
    calls = []
    H.wait_confirmed(lambda: calls.append(1) or status(), sleep=lambda s: None)
    assert calls == [1]


# --- 4. a slot written outside harnessd has no record --------------------------------------------


def test_a_verify_that_finds_no_record_says_how_the_slot_got_there():
    job = SlotJob(act="verify", slot="A", state="failed", err=W.NO_RECORD_ERR)
    exc = W.job_failure(job)
    assert isinstance(exc, RefusedError) and W.NO_RECORD_ERR in exc.message
    assert "written outside harnessd" in exc.hint and "Push it again from Harness Manager" in \
        exc.hint
    # twin: a read-back that failed for another reason is not blamed on a missing record
    bad = W.job_failure(dataclasses.replace(job, err="read-back: region 0 CRC"))
    assert isinstance(bad, ActionFailedError) and "outside harnessd" not in bad.hint


# --- 5. additive fields: code, claimed ----------------------------------------------------------


def test_a_code_decides_the_class_before_the_text():
    assert isinstance(W.refusal("commit", "the card is busy right now", "busy"), HeldError)
    assert isinstance(W.refusal("commit", "claimed; come in over ssh", "locked"),
                      W.ClaimLockedError)
    job = SlotJob(act="verify", slot="A", state="failed", err="S0SR missing")
    assert "written outside harnessd" in W.job_failure(job, "no_record").hint
    # twin: the same new wording without a code is not guessed at
    assert isinstance(W.refusal("commit", "the card is busy right now"), ActionFailedError)
    assert isinstance(W.job_failure(job), ActionFailedError)
    # and a code overrides a text that would have matched something else
    assert not isinstance(W.refusal("commit", "EBUSY", "nothing_staged"), HeldError)


def test_the_os_slot_adapter_reads_code_and_job_code_from_the_reply():
    from harness_manager_mps3 import os_slots as OS

    assert isinstance(OS._reply_refusal("commit", {"ok": False, "err": "x", "code": "locked"}),
                      W.ClaimLockedError)
    st = SlotStatus(running="A", raw={"job": {"code": "no_record"}})
    job = SlotJob(act="verify", slot="B", state="failed", err="new words")
    assert "outside harnessd" in OS._job_failure(job, st).hint
    assert "outside harnessd" not in OS._job_failure(job).hint       # twin: no reply, no code
    assert W.reply_code({"code": 3}) == "" and W.reply_code(None) == ""


def test_claimed_is_read_from_slot_status_when_present():
    assert H.claimed(status(raw={"claimed": True})) is True
    assert H.claimed(status(raw={"claimed": False})) is False
    assert H.claimed(status()) is None                               # twin: absent = unknown
    assert H.claimed(status(raw={"claimed": "yes"})) is None         # not a bool: not trusted


# --- 6. linux_bundle.json's schema_version is the STRING "1" -------------------------------------


def test_the_bundle_reader_takes_schema_version_as_the_string_it_is(tmp_path):
    from tests.spikes.harness_dist_publish import from_linux_bundle

    doc = {"schema": "mps3-linux-bundle", "schema_version": "1", "fieldable": True,
           "static_id": "0x72BB0A36", "static_usercode": "0x1", "targets": {"ethernet": {}}}
    assert from_linux_bundle(dict(doc), sd_files={}, os_image=b"",
                             features=[], vivado="", proto="").impl == "linux"
    # twin: another version is refused, an int 1 still reads as v1
    with pytest.raises(ValueError, match="v1"):
        from_linux_bundle({**doc, "schema_version": "2"})
    assert from_linux_bundle({**doc, "schema_version": 1}, sd_files={}, os_image=b"",
                             features=[], vivado="", proto="").impl == "linux"


def test_slot_push_reads_a_bundle_whose_schema_version_is_a_string(tmp_path):
    from harness_manager.core.errors import UsageError
    from harness_manager.services.slots import push_source

    data = make_s0lb(b"\x5a" * 1024)
    (tmp_path / "linux_slot.img").write_bytes(data)
    doc = {"schema": "mps3-linux-bundle", "schema_version": "1", "fieldable": True,
           "targets": {"ethernet": {"provisioned": {"static_id": "0x72BB0A36"}}}}
    (tmp_path / "linux_bundle.json").write_text(json.dumps(doc), encoding="utf-8")
    assert push_source(None, bundle=tmp_path).static_id == "0x72bb0a36"
    (tmp_path / "linux_bundle.json").write_text(json.dumps({**doc, "schema_version": "2"}),
                                                encoding="utf-8")
    with pytest.raises(UsageError, match="v1"):
        push_source(None, bundle=tmp_path)


# --- 7. hdr_crc is the S0LB table CRC, compared as an integer ------------------------------------


def os_comp(**extra) -> Component:
    return Component(name="os", target="ethernet", kind="os-slot",
                     asset=Asset(name="linux_slot.img", url="x", sha256="0" * 64, size=1),
                     extra=extra)


def test_an_upper_case_bundle_crc_matches_the_boards_lower_case_hdr_crc():
    data = make_s0lb(b"\x5a" * 2048)
    frames = linux_bundle_s0lb(data)                       # 0x%08X, as linux_bundle.py writes
    assert frames["header_crc32"] == frames["header_crc32"].upper().replace("0X", "0x")
    board = header_crc(data)                               # 0x%08lx, as net_proto.c writes
    assert board == board.lower() and board != frames["header_crc32"]
    comp = os_comp(s0lb=frames, crc32=f"0x{zlib.crc32(data) & 0xFFFFFFFF:08X}")
    assert os_image_running(comp, view(os_active_crc=board))


def test_twin_the_whole_file_crc32_is_never_taken_for_the_hdr_crc():
    data = make_s0lb(b"\x5a" * 2048)
    board = header_crc(data)
    frames = {**linux_bundle_s0lb(data), "header_crc32": "0x11111111"}
    comp = os_comp(s0lb=frames, crc32=board.upper().replace("0X", "0x"))   # slot_image.crc32
    assert not os_image_running(comp, view(os_active_crc=board))
    assert not os_image_running(os_comp(crc32=board), view(os_active_crc=board))  # no s0lb


# --- 9. no confirm tokens on slot acts; verify is soft-busy --------------------------------------


def test_the_help_says_the_lease_and_confirm_are_harness_managers_not_the_boards():
    from harness_manager.cli import cmd_slots

    doc = " ".join((cmd_slots.__doc__ or "").split())
    assert "the board has no lease and no confirm on slot acts" in doc
    assert "holds the board's card for minutes" in doc
    assert "it runs only when asked, never from a status read" in doc


# --- 10. the fake board boots what stage0 would ---------------------------------------------------


def test_fake_os_slots_reboot_boots_the_default_else_the_other_else_rescue():
    s = FakeOsSlots()
    s.slots["B"] = SlotInfo("B", state=VALID, hdr_crc="0xbbbbbbbb", verified="readback")
    s.default, s.staged = "B", "B"
    s.reboot()
    assert (s.running, s.default, s.staged) == ("B", "B", "")
    assert s.slots["A"].verified == "no" and s.status().target == "A"   # target: the OTHER
    s.unhealthy.add("A")
    s.default = "A"
    s.reboot()
    assert (s.running, s.default) == ("B", "A") and H.fell_back(s.status()) == "A"
    s.unhealthy.add("B")
    s.reboot()
    assert s.running == "rescue" and s.status().target == "B"
