"""SLOT-TIMING: OS-slot card jobs on a slow card, their progress, and the reset guard.

Silicon (B2, 2026-09-26): the user microSD writes ~70 KB/s and reads back 14-135 KB/s (29 MB:
~12 min + ~35 min), and a reset mid-job wedged the card. The board is pyverify's FakeShell
through the real MPS3 pack (``tests.fakes.lxslots_board``) with the slow-job knobs:
``write_bps``/``verify_s`` (a job that lasts), ``hold_job`` (another host's job), and a
restart mid-job wedges the card. Every behaviour has its negative twin beside it.
"""

from __future__ import annotations

import dataclasses
import io
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from harness_manager.cli.main import main
from harness_manager.cli.output import StderrProgress
from harness_manager.core.errors import ActionFailedError, RefusedError, UsageError
from harness_manager.core.model import BoardIdentity
from harness_manager.core.pack import CardStatus, DeployResult, OverlayRef, SlotJob, SlotStatus
from harness_manager.services import reset_guard
from harness_manager.services.deploy import DeployService
from harness_manager.services.slots import (
    SlotService,
    card_line,
    job_text,
    push_source,
    slot_status_json,
)
from harness_manager_mps3 import os_slots as O
from harness_manager_mps3.deploy import PUSH_PORT_ENV
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from tests.fakes.lxslots_board import LINUX_SID, board_session, slot_board
from tests.fakes.s0lb_image import make_s0lb

SID = f"0x{LINUX_SID:08x}"
A_RECORDED = {"state": "valid", "hdr_crc": 0x3E5E9C2C, "len": 24354312, "sid": LINUX_SID}
MB29 = 29_000_000


@pytest.fixture(autouse=True)
def _no_setting_env(monkeypatch):
    for env in (O.JOB_TIMEOUT_ENV, O.STALL_ENV, O.CARD_WRITE_BPS_ENV, O.CARD_READ_BPS_ENV):
        monkeypatch.delenv(env, raising=False)


def _board(monkeypatch, **kw):
    kw.setdefault("slots", {"a": A_RECORDED})
    fake = slot_board(**kw)
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    return fake


@pytest.fixture
def linux(monkeypatch):
    fake = _board(monkeypatch)
    session = board_session(fake)
    yield fake, session
    session.close()
    fake.stop()


def image(tmp_path: Path, n: int = 8192) -> Path:
    p = tmp_path / "linux_slot.img"
    p.write_bytes(make_s0lb(b"\x5a" * n))
    return p


class Recorder:
    """A Progress that takes detail (``core.pack.TAKES_DETAIL``)."""

    takes_detail = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int, dict]] = []

    def __call__(self, phase: str, done: int, total: int, detail: dict | None = None) -> None:
        self.calls.append((phase, done, total, dict(detail or {})))


# --- 1. the budget is the card's rates x the size (one place), with a floor ------------------------


def test_a_29mb_push_is_budgeted_from_the_cards_rates_not_180s(monkeypatch):
    t = O.slot_timeouts(MB29)
    # B2: ~12 min written + ~35 min read back = ~47 min; x1.5 at 70 KB/s and 14 KB/s
    assert t.write_bps == 70_000 and t.read_bps == 14_000
    assert t.size_s == pytest.approx(1.5 * (MB29 / 70_000 + MB29 / 14_000))
    assert t.job_s == t.size_s > 47 * 60 > 1800 > 180
    assert t.push_stall_s == 900.0                      # the no-progress limit, not pyverify's 30
    assert O.slot_timeouts(MB29, write=False).job_s == pytest.approx(1.5 * MB29 / 14_000)


def test_twin_a_small_image_gets_the_1800s_floor_and_the_rows_move_it(monkeypatch):
    assert O.slot_timeouts(1_000_000).job_s == 1800.0          # 1 MB asks for ~2 min: the floor
    monkeypatch.setenv(O.CARD_READ_BPS_ENV, "7000")             # a slower card (the SPI fix)
    assert O.slot_timeouts(MB29).job_s == pytest.approx(1.5 * (MB29 / 70_000 + MB29 / 7_000))
    monkeypatch.setenv(O.JOB_TIMEOUT_ENV, "9000")
    assert O.slot_timeouts(1_000_000).job_s == 9000.0
    monkeypatch.setenv(O.STALL_ENV, "not-a-number")
    with pytest.raises(UsageError, match=O.STALL_ENV):
        O.slot_timeouts(1)


def test_the_push_hands_pyverify_the_stall_limit(linux, tmp_path, monkeypatch):
    fake, session = linux
    seen: dict[str, Any] = {}
    real = O.pv_slot.push_slot_image

    def spy(*a: Any, **kw: Any) -> int:
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(O.pv_slot, "push_slot_image", spy)
    session.os_slots.push(image(tmp_path), static_id=SID)
    assert seen["timeout_s"] == 900.0
    assert session.os_slots.last_timeouts.job_s == 1800.0


def test_a_slow_job_that_outlasts_the_old_fixed_budget_completes(monkeypatch, tmp_path):
    # Scaled: a ~1 s write and a 1 s read-back against a 0.5 s floor (the old fixed 180 s's
    # stand-in); the card's rates say 64 KB each way at 40 KB/s x1.5 = ~4.9 s.
    monkeypatch.setenv(O.JOB_TIMEOUT_ENV, "0.5")
    monkeypatch.setenv(O.CARD_WRITE_BPS_ENV, "40000")
    monkeypatch.setenv(O.CARD_READ_BPS_ENV, "40000")
    fake = _board(monkeypatch, write_bps=64_000, verify_s=1.0)
    session = board_session(fake)
    try:
        st = session.os_slots.push(image(tmp_path, 64_000), static_id=SID)
        assert st.staged == "B" and st.slots["B"].verified == "readback"
        assert session.os_slots.last_timeouts.job_s > 3.0
    finally:
        session.close()
        fake.stop()


def test_twin_the_same_job_under_a_fixed_cap_is_stopped_and_says_the_cards_state(monkeypatch,
                                                                                  tmp_path):
    fake = _board(monkeypatch, write_bps=64_000, verify_s=5.0)
    session = board_session(fake)
    try:
        session.os_slots.job_timeout_s = 0.5                   # what a fixed budget does
        with pytest.raises(ActionFailedError) as exc:
            session.os_slots.push(image(tmp_path, 64_000), static_id=SID)
        assert "after its 0 s cap" in exc.value.message or "s cap" in exc.value.message
        assert "card: running A, default A, A valid, B " in exc.value.message
        assert "job push B verifying" in exc.value.message
        assert "do not push again or reset" in exc.value.hint      # nothing is re-sent
    finally:
        session.close()
        fake.stop()


# --- 2. THE guard is the stall; the cap is the backstop --------------------------------------------


def test_a_job_whose_bytes_stop_moving_is_stuck_after_the_stall_limit(linux):
    fake, session = linux
    fake.hold_job("writing", got=12_300_000, length=MB29)
    t0 = time.monotonic()
    with pytest.raises(ActionFailedError) as exc:
        session.os_slots._wait_job(None, phase="readback", before=None,
                                   deadline=t0 + 30.0, stall_s=0.3)
    assert time.monotonic() - t0 < 5.0
    assert "made no progress for" in exc.value.message and "stuck writing" in exc.value.message
    assert "job push B writing 12.3 MB / 29 MB" in exc.value.message
    assert O.STALL_KEY in exc.value.hint


def test_twin_bytes_that_keep_moving_are_never_stuck_and_an_uncounted_readback_has_the_cap_only(
        linux):
    fake, session = linux
    fake.hold_job("writing", got=0, length=1_000_000)
    stop = threading.Event()

    def card() -> None:                                    # the card keeps taking bytes
        got = 0
        while not stop.wait(0.05) and got < 1_000_000:
            got += 50_000
            with fake._lock:
                fake.slots.job["got"] = got
        with fake._lock:
            fake.slots.job["state"] = "verifying"          # a read-back that counts nothing
        time.sleep(0.8)
        fake.end_job("ok")

    t = threading.Thread(target=card, daemon=True)
    t.start()
    try:
        st = session.os_slots._wait_job(None, phase="readback", before=None,
                                        deadline=time.monotonic() + 30.0, stall_s=0.3)
    finally:
        stop.set()
        t.join()
    assert st.job.state == "ok"                            # 0.8 s verifying > 0.3 s stall


def test_a_card_that_fails_mid_job_says_so_with_its_last_state(linux):
    fake, session = linux
    fake.hold_job("writing", got=5_000_000, length=MB29)

    def fail() -> None:
        time.sleep(0.2)
        with fake._lock:
            fake.slots.card = "io"

    threading.Thread(target=fail, daemon=True).start()
    with pytest.raises(ActionFailedError) as exc:
        session.os_slots._wait_job(None, phase="readback", before=None,
                                   deadline=time.monotonic() + 30.0, stall_s=30.0)
    assert "slot status answers 'card io'" in exc.value.message
    assert "last seen: card: running A" in exc.value.message and "writing 5 MB / 29 MB" in \
        exc.value.message


def test_twin_a_failed_readback_is_the_boards_verdict_with_the_cards_state(linux):
    fake, session = linux
    fake.hold_job("verifying", got=MB29, length=MB29)
    fake.end_job("failed", "read-back: region 0 CRC")
    with pytest.raises(ActionFailedError) as exc:
        session.os_slots._wait_job(None, phase="readback", before=None,
                                   deadline=time.monotonic() + 5.0, stall_s=1.0)
    assert "failed: read-back: region 0 CRC (card: running A" in exc.value.message


# --- 3. progress: phase, bytes, rate, ETA ---------------------------------------------------------


def test_a_push_reports_writing_then_verifying_with_rate_and_eta(monkeypatch, tmp_path):
    fake = _board(monkeypatch, write_bps=150_000, verify_s=0.6)
    session = board_session(fake)
    rec = Recorder()
    try:
        session.os_slots.push(image(tmp_path, 300_000), static_id=SID, progress=rec)
    finally:
        session.close()
        fake.stop()
    writing = [c for c in rec.calls if c[0] == "writing"]
    assert writing and any(0 < d < t for _, d, t, _ in writing)
    texts = [x["text"] for *_, x in writing]
    assert any(t.startswith("writing slot B: 0.") and "MB / 0.3 MB, ~" in t for t in texts)
    measured = [x["rate_bps"] for *_, x in writing if x["rate_bps"] != 70_000]
    assert measured and all(50_000 < r < 400_000 for r in measured)   # the observed rate
    assert all(x["eta_s"] >= 60 for *_, x in writing)
    readback = [x for p, *_, x in rec.calls if p == "readback" and x]
    assert readback and readback[0]["text"].startswith("verifying slot B (0.3 MB), ~")


def test_twin_a_plain_progress_gets_three_arguments_and_nothing_else(monkeypatch, tmp_path):
    fake = _board(monkeypatch, write_bps=150_000, verify_s=0.3)
    session = board_session(fake)
    calls: list[tuple] = []
    try:
        session.os_slots.push(image(tmp_path, 200_000), static_id=SID,
                              progress=lambda *a: calls.append(a))
    finally:
        session.close()
        fake.stop()
    assert calls and all(len(c) == 3 for c in calls)
    assert {"push", "writing", "readback"} <= {c[0] for c in calls}


def test_status_json_and_the_card_line_name_the_running_job(linux):
    fake, session = linux
    fake.hold_job("writing", got=12_300_000, length=MB29)
    st = SlotService().status(session)
    j = slot_status_json(st)["job"]
    assert j["busy"] and j["rate_bps"] == 70_000
    # 16.7 MB left at 70 KB/s + 29 MB read back at 14 KB/s = ~38 min
    assert j["text"] == "writing slot B: 12.3 MB / 29 MB, ~38 min left"
    card = CardStatus(store=True, present=True, state="valid", os_slots=st)
    assert card_line(card) == j["text"]


def test_twin_an_idle_card_has_no_job_line(linux):
    fake, session = linux
    st = SlotService().status(session)
    j = slot_status_json(st)["job"]
    assert not j["busy"] and j["text"] == "" and j["eta_s"] is None
    assert card_line(CardStatus(store=True, present=True, state="valid", os_slots=st)) == \
        "valid · OS A:valid* B:empty"


def test_the_cli_line_is_the_jobs_text_and_rate():
    err = io.StringIO()
    p = StderrProgress("push", err)
    p("writing", 12_300_000, MB29, detail={"text": "writing slot B: 12.3 MB / 29 MB, ~38 min "
                                                   "left", "rate_bps": 70_000})
    assert err.getvalue() == "push: writing slot B: 12.3 MB / 29 MB, ~38 min left (70 KB/s)\n"


def test_twin_without_detail_the_cli_line_is_as_before():
    err = io.StringIO()
    StderrProgress("push", err)("writing", 1, 2)
    assert err.getvalue() == "push: writing 1/2 (50%)\n"


def test_job_text_of_an_uncounted_readback_and_of_no_job():
    job = SlotJob(act="verify", slot="A", state="verifying", got=0, length=MB29, eta_s=1200.0)
    assert job_text(job) == "verifying slot A (29 MB), ~20 min left"
    assert job_text(SlotJob()) == ""


# --- 4. the reset guard: every reset refused while the card works; idle is allowed -------------------


@pytest.mark.parametrize("state, words", [("writing", "slot B is being written (12.3/29 MB)"),
                                          ("verifying", "slot B is being read back (29 MB)")])
def test_the_harness_reboot_is_refused_during_a_card_job(linux, state, words):
    fake, session = linux
    fake.hold_job(state, got=12_300_000 if state == "writing" else MB29, length=MB29)
    with pytest.raises(reset_guard.CardBusyError) as exc:
        session.os_slots.reboot(wait_s=5)
    assert exc.value.code == 4
    assert exc.value.message.startswith(f"harness reboot refused: {words}; a reset now can "
                                        "wedge the card. Wait ~")
    assert fake.reboots == [] and not fake.wedged


def test_twin_idle_the_harness_reboots_and_why_the_guard_exists(linux):
    fake, session = linux
    assert session.os_slots.reboot(wait_s=10)["up_after_s"] > 0       # idle: allowed
    assert len(fake.reboots) == 1 and not fake.wedged
    # What the guard prevents (B2: "uSD init error"): a reboot sent past it, mid-write.
    fake.hold_job("writing")
    session.shell.call(lambda c: c.reboot())
    deadline = time.monotonic() + 5
    while not fake.wedged and time.monotonic() < deadline:
        time.sleep(0.05)
    assert fake.wedged


class FakeController:
    def __init__(self) -> None:
        self.reboots = 0
        self.commands: list[str] = []

    def command(self, line: str, *, arm: bool = False) -> str:
        self.commands.append(line)
        if reset_guard.is_reboot_line(line):
            self.reboots += 1
        return "ok"

    def reboot(self, progress: Any = None, wait_s: float | None = None) -> dict:
        self.reboots += 1
        return {"summary": "rebooted"}

    def temperatures(self) -> list:
        return []

    def oscillators(self) -> list:
        return []


class FakePower:
    label = "fake-outlet"
    cycle_reason = ""

    def __init__(self) -> None:
        self.cycles = 0

    def read(self) -> list:
        return []

    def power_cycle(self, off_s: float = 5.0, *, wait: bool = True, progress: Any = None) -> dict:
        self.cycles += 1
        return {"off_s": off_s, "confirmed_off": True, "confirmed_on": True, "seconds": 0.1}


def run(capsys, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        rc = main(list(argv))
    finally:
        sys.stdin = old
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def cli(monkeypatch):
    fake = _board(monkeypatch)
    ctl, power = FakeController(), FakePower()
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    monkeypatch.setenv(PUSH_PORT_ENV, str(fake.raw_tcp_port))
    monkeypatch.setattr("harness_manager_mps3.mcc.make_controller_adapter", lambda s: ctl)
    monkeypatch.setattr("harness_manager_mps3.telemetry.make_power_adapter", lambda s: power)
    yield fake, f"{fake.host}:{fake.control_port}", ctl, power
    fake.stop()


RESETS = [("mcc", "reboot"), ("mcc", "cmd", "REBOOT"), ("power", "cycle")]


def _argv(verb: tuple[str, ...], target: str) -> list[str]:
    return [verb[0], target, *verb[1:]] if verb[0] == "mcc" else [verb[0], verb[1], target]


def _resets(ctl: FakeController, power: FakePower) -> int:
    return ctl.reboots + power.cycles


@pytest.mark.parametrize("verb", RESETS, ids=lambda v: " ".join(v))
@pytest.mark.parametrize("state", ["writing", "verifying"])
def test_every_board_reset_is_refused_by_the_cli_during_a_card_job(capsys, cli, verb, state):
    fake, target, ctl, power = cli
    fake.hold_job(state)
    argv = _argv(verb, target) + (["--yes"] if verb != ("mcc", "cmd", "REBOOT") else [])
    rc, _, err = run(capsys, *argv)
    assert rc == 4, err
    assert "refused: slot B is being" in err and "a reset now can wedge the card" in err
    assert _resets(ctl, power) == 0


@pytest.mark.parametrize("verb", RESETS, ids=lambda v: " ".join(v))
def test_twin_idle_every_board_reset_goes_ahead(capsys, cli, verb):
    fake, target, ctl, power = cli
    argv = _argv(verb, target) + (["--yes"] if verb != ("mcc", "cmd", "REBOOT") else [])
    rc, _, err = run(capsys, *argv)
    assert rc == 0, err
    assert _resets(ctl, power) == 1


def test_twin_a_controller_command_that_is_not_a_reset_runs_during_a_job(capsys, cli):
    fake, target, ctl, _ = cli
    fake.hold_job("writing")
    rc, out, err = run(capsys, "mcc", target, "cmd", "CFG", "R", "OSC", "0")
    assert rc == 0, err
    assert ctl.commands == ["CFG R OSC 0"] and ctl.reboots == 0


def test_force_needs_its_typed_phrase_and_yes_never_implies_it(capsys, cli):
    fake, target, ctl, _ = cli
    fake.hold_job("writing")
    rc, _, err = run(capsys, "mcc", target, "reboot", "--yes", "--force")      # EOF at the prompt
    assert rc == 15 and "not confirmed" in err and ctl.reboots == 0
    rc, _, err = run(capsys, "mcc", target, "reboot", "--yes", "--force", "--consent", "RESET x")
    assert rc == 15 and ctl.reboots == 0
    bid = f"mps3@{target}"
    assert f"RESET {bid}" in err


def test_twin_force_with_the_phrase_resets_during_the_job(capsys, cli):
    fake, target, ctl, power = cli
    fake.hold_job("writing")
    bid = f"mps3@{target}"
    rc, _, err = run(capsys, "mcc", target, "reboot", "--yes", "--force",
                     stdin=f"RESET {bid}\n")
    assert rc == 0, err
    assert "WEDGE THE CARD" in err and ctl.reboots == 1
    rc, _, err = run(capsys, "power", "cycle", target, "--yes", "--force",
                     "--consent", f"RESET {bid}")
    assert rc == 0, err
    assert power.cycles == 1


def test_force_is_not_offered_for_a_harness_reboot_or_a_swap(linux):
    fake, session = linux
    for action in (reset_guard.ACTION_HARNESS_REBOOT, reset_guard.ACTION_DEPLOY):
        with pytest.raises(UsageError, match="--force is not offered"):
            reset_guard.check(session, action, force=True, consent="anything")


class SpyDeploy:
    """The deploy adapter as far as the service goes: records what reached it."""

    def __init__(self, rm_id: str) -> None:
        self.rm_id = rm_id
        self.deployed: list[str] = []

    def preflight(self, overlay: OverlayRef) -> list:
        return []

    def deploy(self, overlay: OverlayRef, progress: Any = None) -> DeployResult:
        self.deployed.append(overlay.name)
        return DeployResult(rm_id=overlay.rm_id, verified=True, seconds=0.0)

    def baseline(self) -> OverlayRef:
        return OverlayRef(name="greybox", rm_id=self.rm_id, static_id=SID)


@pytest.mark.parametrize("state", ["writing", "verifying"])
def test_deploy_and_restore_are_refused_during_a_card_job(linux, state):
    fake, session = linux
    rm = session.identity().rm_id
    session.deploy = spy = SpyDeploy(rm)
    fake.hold_job(state)
    with pytest.raises(reset_guard.CardBusyError, match="^deploy refused: slot B is being"):
        DeployService().deploy(session, OverlayRef(name="nanosoc", rm_id=rm, static_id=SID))
    with pytest.raises(reset_guard.CardBusyError, match="^restore refused: slot B is being"):
        DeployService().restore_baseline(session)
    assert spy.deployed == []


def test_twin_idle_deploy_and_restore_reach_the_board(linux):
    fake, session = linux
    rm = session.identity().rm_id
    session.deploy = spy = SpyDeploy(rm)
    DeployService().deploy(session, OverlayRef(name="nanosoc", rm_id=rm, static_id=SID))
    DeployService().restore_baseline(session)
    assert spy.deployed == ["nanosoc", "greybox"]


def test_a_bare_metal_harness_is_never_asked_for_slots_and_resets_freely(monkeypatch):
    fake = slot_board(profile="bare-metal", slots=None)
    session = board_session(fake)
    try:
        assert reset_guard.check(session, reset_guard.ACTION_MCC_REBOOT) is None
        assert reset_guard.busy_job(session) is None
    finally:
        session.close()
        fake.stop()


# --- 5. the update executor waits for the job before its reboot -------------------------------------


@dataclasses.dataclass
class BusyOsSlots:
    """``FakeOsSlots`` whose card is busy (another host's push) for ``busy_left`` reads
    after the push."""

    base: Any
    busy_after_push: int = 3
    busy_left: int = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def status(self) -> SlotStatus:
        st = self.base.status()
        if self.busy_left > 0:
            self.busy_left -= 1
            return dataclasses.replace(st, job=SlotJob(act="push", slot="A", state="writing",
                                                       got=1_000_000, length=MB29,
                                                       eta_s=600.0))
        return st

    def push(self, *a: Any, **kw: Any) -> SlotStatus:
        out = self.base.push(*a, **kw)
        self.busy_left = self.busy_after_push
        return out


@pytest.fixture
def world(tmp_path):
    from tests.unit.test_lxslots_update import world as lx_world

    verified, session, slots, installer = lx_world.__wrapped__(tmp_path)
    busy = BusyOsSlots(slots)
    session.os_slots = busy
    installer.os_slots_for = lambda s: busy
    installer.sleep = lambda s: None
    installer.card_poll_s = 0.0
    return verified, session, slots, busy, installer


def _events(installer) -> list:
    from harness_manager.core.events import EventBus

    bus = EventBus()
    seen: list = []
    bus.subscribe("update.*", seen.append)
    installer.bus = bus
    return seen


def test_the_executor_waits_for_the_card_job_then_reboots(world):
    from tests.unit.test_lxslots_update import plan_of

    verified, session, slots, busy, installer = world
    seen = _events(installer)
    plan = plan_of(verified, session, slots)
    out = installer.run(session, plan, plan.approve(), verified)
    assert out.result == "installed", out.detail
    assert [c for c in slots.calls if c != "status"] == ["push:B", "commit:B", "reboot"]
    assert busy.busy_left == 0                              # it read the job out before rebooting
    waits = [e.data for e in seen if e.topic == "update.progress"
             and e.data.get("phase") == "reboot:waiting-card:writing"]
    assert waits and waits[0]["text"].startswith("waiting for the card job before the reboot: "
                                                 "writing slot A: 1 MB / 29 MB, ~10 min left")


def test_twin_a_job_that_outlasts_the_wait_is_not_rebooted(world):
    from tests.unit.test_lxslots_update import plan_of

    verified, session, slots, busy, installer = world
    busy.busy_after_push = 10_000
    installer.card_wait_s = 0.0
    plan = plan_of(verified, session, slots)
    out = installer.run(session, plan, plan.approve(), verified)
    assert out.result == "written-not-running"
    assert "NOT rebooted" in out.detail and "slot A is being written" in out.detail
    assert "reboot" not in slots.calls


def test_the_guard_reads_an_adapter_without_busy_job_through_status(world):
    _, session, _, busy, _ = world
    busy.busy_left = 1
    with pytest.raises(reset_guard.CardBusyError):
        reset_guard.check(session, reset_guard.ACTION_MCC_REBOOT)
    assert reset_guard.check(session, reset_guard.ACTION_MCC_REBOOT) is None     # twin: idle


def test_a_nested_check_inside_guarded_passes_without_reading_the_board(linux):
    fake, session = linux
    with reset_guard.guarded(session, reset_guard.ACTION_MCC_REBOOT):
        fake.hold_job("writing")                            # starts after the outer check
        assert reset_guard.check(session, reset_guard.ACTION_MCC_REBOOT) is None
        assert reset_guard.scope_of(session.candidate.board_id).action == "MCC REBOOT"
    with pytest.raises(reset_guard.CardBusyError):         # twin: outside, it is read
        reset_guard.check(session, reset_guard.ACTION_MCC_REBOOT)


def test_push_source_is_unchanged(tmp_path):
    src = push_source(image(tmp_path), static_id=SID)
    assert src.static_id == SID and src.sha256


def test_refusal_without_an_eta_or_a_readable_job():
    busy = reset_guard.refusal("MCC REBOOT", SlotJob(act="push", slot="B", state="writing",
                                                     got=1, length=2))
    assert busy.message.endswith("Wait for it to finish.") and "--force" in busy.hint
    held = reset_guard.refusal("deploy", None, why="another client holds 6900")
    assert "cannot be read (another client holds 6900)" in held.message
    assert "--force" not in held.hint
    assert isinstance(held, reset_guard.CardBusyError) and not isinstance(held, RefusedError)


def test_identity_is_untouched(linux):
    _, session = linux
    assert isinstance(session.identity(), BoardIdentity)


def test_the_service_itself_refuses_force_without_the_phrase(linux):
    fake, session = linux
    fake.hold_job("verifying", got=MB29, length=MB29)
    bid = session.candidate.board_id
    for consent in ("", "RESET", f"RESET {bid}x", "yes"):
        with pytest.raises(RefusedError, match="needs the typed phrase"):
            reset_guard.check(session, reset_guard.ACTION_MCC_REBOOT, force=True,
                              consent=consent)
    job = reset_guard.check(session, reset_guard.ACTION_MCC_REBOOT, force=True,
                            consent=f"RESET {bid}")                  # twin: the phrase
    assert job is not None and job.state == "verifying"


# --- 6. the daemon: the reboot and power-cycle jobs refuse, naming the job -------------------------


@pytest.fixture
def api(monkeypatch):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from harness_manager.core.services import EngineConfig
    from harness_manager.daemon.app import create_app
    from harness_manager.engine import Engine
    from tests.fakes.t13_daemon import TOKEN, bid_path, headers, state_dir

    fake = _board(monkeypatch)
    ctl, power = FakeController(), FakePower()
    monkeypatch.setattr("harness_manager_mps3.mcc.make_controller_adapter", lambda s: ctl)
    monkeypatch.setattr("harness_manager_mps3.telemetry.make_power_adapter", lambda s: power)
    eng = Engine(EngineConfig(state_dir=state_dir(), pack_overrides={"mps3": {
        "console_ports": fake.console_ports, "push_port": fake.raw_tcp_port,
        "tftp_port": fake.tftp_port}}))
    client = TestClient(create_app(eng, token=TOKEN, static_dir=None))
    with client:
        r = client.post("/api/v1/boards", json={"target": f"{fake.host}:{fake.control_port}",
                                                "note": "slot-timing"}, headers=headers())
        assert r.status_code == 200, r.text
        bid = r.json()["board_id"]
        yield fake, client, bid, bid_path(bid), headers(), ctl, power
    eng.close_all()
    fake.stop()


def _job(client, path: str, body: dict, h: dict) -> dict:
    r = client.post(path, json=body, headers=h)
    assert r.status_code == 202, r.text
    job, deadline = r.json()["job"], time.monotonic() + 20
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/jobs/{job}", headers=h).json()
        if state["state"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError(f"job {job} still running")


def test_the_daemon_refuses_a_reboot_and_a_power_cycle_during_a_card_job(api):
    fake, client, bid, path, h, ctl, power = api
    fake.hold_job("writing")
    for suffix, body in (("/controller/reboot", {}), ("/power/cycle", {"off_s": 2})):
        state = _job(client, path + suffix, body, h)
        assert state["state"] == "failed" and state["error"]["name"] == "HELD", state
        assert "slot B is being written (12.3/29 MB)" in state["error"]["message"]
    r = client.post(path + "/controller/command", json={"line": "REBOOT"}, headers=h)
    assert r.status_code == 409 and "slot B is being written" in r.json()["error"]["message"]
    assert _resets(ctl, power) == 0


def test_twin_the_daemon_resets_when_idle_or_forced_with_the_phrase(api):
    fake, client, bid, path, h, ctl, power = api
    assert _job(client, path + "/controller/reboot", {}, h)["state"] == "done"
    fake.hold_job("verifying", got=MB29, length=MB29)
    bad = _job(client, path + "/controller/reboot", {"force": True, "consent": "RESET"}, h)
    assert bad["state"] == "failed" and bad["error"]["name"] == "REFUSED"
    good = _job(client, path + "/power/cycle",
                {"off_s": 2, "force": True, "consent": f"RESET {bid}"}, h)
    assert good["state"] == "done", good
    assert (ctl.reboots, power.cycles) == (1, 1)


class _HeldThenFree:
    """``os_slots`` whose control port is held for ``held`` reads, then idle."""

    def __init__(self, held: int) -> None:
        self.held = held

    def busy_job(self) -> SlotStatus | None:
        from harness_manager.core.errors import HeldError

        if self.held > 0:
            self.held -= 1
            raise HeldError("another client holds the control port")
        return None


class _Session:
    def __init__(self, slots: Any) -> None:
        from harness_manager.core.model import Candidate

        self.candidate = Candidate(pack="mps3", board_id="mps3@held", links=())
        self.os_slots = slots


def test_the_executors_wait_reads_a_held_port_again_within_its_budget():
    s = _Session(_HeldThenFree(3))
    assert reset_guard.wait_idle(s, timeout_s=60, poll_s=0, sleep=lambda _: None) is None
    assert s.os_slots.held == 0


def test_twin_a_port_held_past_the_budget_refuses_the_reboot():
    s = _Session(_HeldThenFree(10**6))
    clock = iter(range(0, 10**6, 10))
    with pytest.raises(reset_guard.CardBusyError, match="cannot be read .another client"):
        reset_guard.wait_idle(s, timeout_s=30, poll_s=0, sleep=lambda _: None,
                              clock=lambda: float(next(clock)))


# --- 7. the daemon's job progress carries the job's line (the web's install progress) ------------


def _job_progress(report) -> tuple[dict, list]:
    from harness_manager.core.events import EventBus
    from harness_manager.daemon.jobs import BoardGates, JobManager

    bus, seen = EventBus(), []
    bus.subscribe("job.progress", lambda ev: seen.append(ev.data))
    jobs = JobManager(bus, BoardGates())
    try:
        job = jobs.submit("harness_install", "mps3@x", lambda progress: report(progress))
        jobs.wait_idle(10)
        return dict(job.progress), seen
    finally:
        jobs.shutdown(wait=True)


def test_job_progress_carries_the_card_jobs_rate_eta_and_line():
    from harness_manager.core.pack import report_progress

    detail = {"slot": "B", "rate_bps": 70000, "eta_s": 360,
              "text": "writing slot B: 12.3 MB / 29 MB, ~6 min left", "junk": 1}
    got, seen = _job_progress(lambda p: report_progress(p, "os:writing", 12_300_000, MB29,
                                                        detail))
    assert got == {"phase": "os:writing", "done": 12_300_000, "total": MB29, "slot": "B",
                   "rate_bps": 70000, "eta_s": 360,
                   "text": "writing slot B: 12.3 MB / 29 MB, ~6 min left"}
    assert seen[-1]["text"] == got["text"] and "junk" not in seen[-1]


def test_twin_plain_job_progress_is_unchanged():
    got, seen = _job_progress(lambda p: p("sd:writing", 1, 2))
    assert got == {"phase": "sd:writing", "done": 1, "total": 2}
    assert set(seen[-1]) == {"job", "phase", "done", "total"}
