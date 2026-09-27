"""HUB-SD: a harness base installed through the hub, end to end on fakes. Each check has a twin.

The world: a board behind the lab hub (no Debug USB here), a signed channel on 127.0.0.1
(T7's ``FakeChannelServer``) with the fielded 1.0.0 and a firmware-only 1.1.0, and
``FakeSdHub`` as fpgahub over ssh (``HubClient`` with the fake as its runner): the SD write
takes 68 s of the fake clock, the CLI's 30 s client window closes first. The controller is
``hub_mcc.HubMccController`` over the same fake (MCC-FIX: pyverify's tools run ON the hub,
never a share on tty_00): a REBOOT loads whatever the hub's SD now holds. Nothing here reaches a
network beyond 127.0.0.1, a real hub, or an SD.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_manager.core.errors import HarnessError, HeldError, RefusedError
from harness_manager.core.events import EventBus
from harness_manager.services.update import UpdateService
from harness_manager.services.update.executor import (
    RESULT_DARK,
    RESULT_INSTALLED,
    RESULT_RESTORED,
    RESULT_REVERTED,
)
from harness_manager_mps3.hub_sd import (
    NANOSOC_BIT,
    HubSdDoor,
    InFlight,
    RestSdBackend,
    SshSdBackend,
)
from tests.fakes.fake_channel import ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.hubsd_fakes import (
    FakeBoard,
    FakeHubHandle,
    FakeLeases,
    FakeSdHub,
    HubSession,
    sha,
    ssh_client,
)
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t7_bundles import Release

KEYS = TestKeys()
OLD = Release.fielded(with_overlays=False)             # 1.0.0, the board runs it
NEW = Release("1.1.0", with_overlays=False)            # same static, firmware c0ffee00


@pytest.fixture
def server(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        yield srv


class World:
    def __init__(self, tmp_path: Path, server: FakeChannelServer, *, rest: bool = False,
                 releases: tuple[Release, ...] = (OLD, NEW)) -> None:
        self.clock = FakeClock(1_758_000_000.0)
        self.hub = FakeSdHub(self.clock, sd_bit=OLD.bit())
        self.board = FakeBoard(loaded=OLD.bit())
        client = ssh_client(self.hub)
        self.handle = FakeHubHandle(client)
        if rest:
            self.api = self.hub.rest_api()
            backend = RestSdBackend(_RestClient(), api=self.api, connect_wait_s=2.0)
        else:
            backend = SshSdBackend(client, uploader=self.hub.upload, clock=self.clock)
        self.door = HubSdDoor(self.handle, backend=backend, clock=self.clock,
                              sleep=self.clock.sleep)
        self.session = HubSession(self.hub, self.board, self.door, self.handle)
        self.bus = EventBus()
        self.events: list = []
        self.bus.subscribe("update.*", self.events.append)
        self.svc = UpdateService(state_dir=tmp_path / "state", trust=KEYS.trust(), token="",
                                 app_version="0.1.0", leases=FakeLeases(self.hub), bus=self.bus)
        self.server = server
        builder = ChannelBuilder(server.root, KEYS)
        for i, r in enumerate(releases):
            r.add_to(builder, tmp_path / "art", current=(i == len(releases) - 1))
        builder.publish(serial=1)

    def plan(self, **kw):
        return self.svc.plan_harness(self.session, source=self.server.source(), **kw)

    def installer(self):
        inst = self.svc.installer("mps3")
        inst.now, inst.sleep = self.clock, self.clock.sleep
        return inst

    def install(self, *, auto_revert: bool | None = None, phrase: str | None = None):
        plan, verified = self.plan()
        approval = plan.approve(board_phrase=plan.board_phrase if phrase is None else phrase,
                                auto_revert=auto_revert)
        return self.installer().run(self.session, plan, approval, verified), plan

    def program_calls(self) -> list[list[str]]:
        return [c for c in self.hub.calls if c[:3] == ["fpgahub", "target", "program"]
                and "--list" not in c]

    def topics(self) -> list[str]:
        return [e.topic for e in self.events]

    def phases(self) -> list[str]:
        return [e.data["phase"] for e in self.events if e.topic == "update.progress"]


class _RestClient:
    target = "mps3_01_pl"
    host = "mapstone-dev.ecs.soton.ac.uk"
    transport = "rest"


# --- the plan -------------------------------------------------------------------------------------


def test_a_board_behind_the_hub_is_offered_the_hub_door(tmp_path, server):
    w = World(tmp_path, server)
    plan, _ = w.plan()
    assert plan.via == "hub" and plan.base and not plan.blockers, plan.blockers
    assert plan.board_phrase == "INSTALL mps3_01_pl HELD BY dam1n19@mapstone-dev 0 QUEUED"
    assert plan.auto_revert and plan.hub["backup"]["version"] == "1.0.0"
    actions = [s.action for s in plan.steps]
    assert actions[:2] == ["download", "verify"]
    assert ["backup-sd", "hub-upload", "hub-write", "hub-verify", "reboot",
            "confirm-identity", "auto-revert"] == actions[2:]
    assert "a client timeout here is expected" in plan.steps[4].detail


def test_twin_a_board_without_a_hub_door_is_not_offered_it(tmp_path, server):
    w = World(tmp_path, server)
    w.session.hub_sd = None                        # no hub: its SD needs the Debug USB here
    plan, _ = w.plan()
    assert plan.via == "" and plan.board_phrase == ""
    assert any("Debug USB" in b for b in plan.blockers)
    assert w.plan(via="hub")[0].blockers[0].startswith("this board has no hub door")


def test_the_consent_names_the_holder_and_the_queue(tmp_path, server):
    w = World(tmp_path, server)
    w.hub.queue = ["alice@lab-pc", "bob@lab-pc"]
    plan, _ = w.plan()
    assert plan.board_phrase == "INSTALL mps3_01_pl HELD BY dam1n19@mapstone-dev 2 QUEUED"
    assert "alice@lab-pc" in plan.hub["consent_text"]
    with pytest.raises(RefusedError) as err:
        plan.approve()                              # --yes never implies the typed phrase
    assert "INSTALL mps3_01_pl" in err.value.hint


def test_twin_a_queue_that_moves_changes_the_plan(tmp_path, server):
    w = World(tmp_path, server)
    before = w.plan()[0].fingerprint()
    w.hub.queue = ["alice@lab-pc"]
    assert w.plan()[0].fingerprint() != before       # the approval no longer fits


def test_an_unrecorded_running_release_has_no_backup_so_no_hub_door(tmp_path, server):
    w = World(tmp_path, server, releases=(NEW,))     # the channel does not list 1.0.0
    plan, _ = w.plan()
    assert plan.via == "hub" and any("backup" in b for b in plan.blockers)
    assert not plan.auto_revert


# --- the install ------------------------------------------------------------------------------


def test_happy_path_one_write_proven_by_the_journal_then_the_paced_reboot(tmp_path, server):
    w = World(tmp_path, server)
    out, plan = w.install()
    assert out.result == RESULT_INSTALLED, out.detail
    assert w.board.identity().firmware_sha == "c0ffee00"
    assert w.hub.sd_bit == NEW.bit() and w.hub.mcc_reboots == 1
    assert len(w.program_calls()) == 1 and len(w.hub.writes) == 1
    assert w.door.last["reply"] == "timeout" and w.door.last["completion"] == "journal"
    assert w.door.last["hub_sha"] == sha(NEW.bit())[:12]
    phases = w.phases()
    for p in ("sd:uploading", "sd:writing", "sd:verifying", "sd:verified", "reboot:sent",
              "confirm"):
        assert p in phases, (p, phases)
    rec = w.svc.installer("mps3").records.history(w.session.candidate.board_id)[0]
    assert rec["via"] == "hub" and rec["result"] == RESULT_INSTALLED
    assert Path(rec["backup"]["path"]).is_file()      # the previous .bit, kept


def test_the_client_timeout_is_not_a_failure_and_is_never_retried(tmp_path, server):
    w = World(tmp_path, server)
    w.hub.write_s = 300.0                            # a slow card: five client windows
    out, _ = w.install()
    assert out.result == RESULT_INSTALLED, out.detail
    assert len(w.program_calls()) == 1, w.program_calls()        # ONE write
    assert w.door.last["reply"] == "timeout"


def test_twin_an_answered_request_is_proven_by_the_same_record(tmp_path, server):
    w = World(tmp_path, server)
    w.hub.write_s = 5.0                              # inside the client window
    out, _ = w.install()
    assert out.result == RESULT_INSTALLED and w.door.last["reply"] == "ok"
    assert len(w.program_calls()) == 1


def test_a_sha_mismatch_is_refused_and_nothing_reboots(tmp_path, server):
    w = World(tmp_path, server)
    w.hub.write_other = b"someone else's bitstream"
    with pytest.raises(HarnessError) as err:
        w.install()
    assert "refusing to REBOOT" in err.value.message
    assert w.hub.mcc_reboots == 0
    assert w.board.identity().firmware_sha == OLD.sha      # still the old image


def test_twin_a_failed_write_reported_by_the_hub_is_not_rebooted_either(tmp_path, server):
    from harness_manager.core.errors import ActionFailedError

    w = World(tmp_path, server)
    w.hub.fail_write = True
    with pytest.raises(ActionFailedError) as err:
        w.install()
    assert "the hub reports the SD write failed" in err.value.message
    assert w.hub.mcc_reboots == 0 and len(w.hub.writes) == 1


def test_a_hub_without_the_journal_is_proven_by_its_last_fingerprint(tmp_path, server):
    w = World(tmp_path, server)
    w.hub.no_journal = True
    out, _ = w.install()
    assert out.result == RESULT_INSTALLED and w.door.last["completion"] == "hub-state"


def test_twin_no_record_at_all_within_the_budget_refuses_and_keeps_the_marker(tmp_path, server):
    from harness_manager.core.errors import ActionFailedError

    w = World(tmp_path, server)
    w.hub.no_journal = True
    w.hub.write_s = 10_000.0                         # the hub never finishes in the budget
    with pytest.raises(ActionFailedError) as err:
        w.install()
    assert "may still be writing" in err.value.message and "do NOT reset" in err.value.hint
    assert w.hub.mcc_reboots == 0 and len(w.program_calls()) == 1
    assert w.door.pending()["state"] == "verifying"      # nothing writes again until proven


def test_no_lease_is_refused_before_any_upload(tmp_path, server):
    w = World(tmp_path, server)
    plan, verified = w.plan()
    approval = plan.approve(board_phrase=plan.board_phrase)
    w.hub.holder = "alice@lab-pc"                    # the lease moved after the plan
    with pytest.raises(HeldError) as err:
        w.installer().run(w.session, plan, approval, verified)
    assert "alice@lab-pc" in err.value.message
    assert w.hub.uploads == [] and w.program_calls() == []


def test_twin_the_door_itself_checks_the_lease_before_the_upload(tmp_path, server):
    w = World(tmp_path, server)
    calls = []
    w.door.bind(lease_check=lambda what: calls.append(what) or (_ for _ in ()).throw(
        HeldError("bob holds it", holder="bob")))
    w.door.set_previous_base("1.0.0", {NANOSOC_BIT: _file(tmp_path, "old.bit", OLD.bit())})
    rec = w.door.backup(tmp_path / "b")
    with pytest.raises(HeldError):
        w.door.install({NANOSOC_BIT: _file(tmp_path, "new.bit", NEW.bit())}, backup=rec)
    assert calls and w.hub.uploads == []


def test_someone_else_on_tty_00_is_refused_before_any_upload(tmp_path, server):
    w = World(tmp_path, server)
    w.hub.tty_others = [[4242, "cat /dev/mps3_01_pl/tty_00"]]    # pyverify's scan sees it
    with pytest.raises(HeldError) as err:
        w.install()
    assert "tty_00" in err.value.message and "pid 4242" in err.value.message
    assert "nothing was written" in err.value.message
    assert w.hub.uploads == [] and w.program_calls() == [] and w.hub.mcc_reboots == 0
    assert w.hub.mcc_runs and all(r["mode"] == "scan" for r in w.hub.mcc_runs)


def test_twin_a_share_count_is_not_read_any_more_the_hub_scan_decides(tmp_path, server):
    # MCC-FIX: Harness Manager holds no share on tty_00 and does not read share counts; a
    # clean hub-side scan lets the install through.
    w = World(tmp_path, server)
    w.hub.share_readers = 1
    out, _ = w.install()
    assert out.result == RESULT_INSTALLED, out.detail
    assert not any(c[:3] == ["fpgahub", "share", "list"] for c in w.hub.calls)


def test_a_second_write_while_one_is_in_flight_is_held(tmp_path, server):
    w = World(tmp_path, server)
    flight = w.door._flight()
    w.door.bind(state_dir=w.svc.state.root)
    flight = w.door._flight()
    import os

    flight.write(state="verifying", sha256=sha(NEW.bit()))       # this process: alive
    flight.write(pid=os.getppid())                                # a live other process
    w.door.set_previous_base("1.0.0", {NANOSOC_BIT: _file(tmp_path, "old.bit", OLD.bit())})
    rec = w.door.backup(tmp_path / "b")
    with pytest.raises(HeldError):
        w.door.install({NANOSOC_BIT: _file(tmp_path, "new.bit", NEW.bit())}, backup=rec)
    assert w.hub.uploads == []


def test_twin_a_dead_owner_whose_write_the_hub_recorded_is_settled(tmp_path, server):
    w = World(tmp_path, server)
    w.door.bind(state_dir=w.svc.state.root)
    flight: InFlight = w.door._flight()
    flight.write(state="verifying", sha256=sha(OLD.bit()), pid=999_999_999)
    w.hub.journal.append((w.clock() - 1, "program dispatched: board=mps3_01_pl method=sd "
                          f"plugin=sd_install ok=True part=x sha256={sha(OLD.bit())[:12]} "
                          "dur=68.00s"))
    w.door.backend.begin()                           # its window opens before that record
    w.door.backend._since = int(w.clock()) - 60
    assert w.door.pending() is None and flight.read() is None


# --- a board that stays dark (U10) ------------------------------------------------------------


def test_dark_after_the_reboot_with_auto_revert_armed_is_written_back(tmp_path, server):
    w = World(tmp_path, server)
    w.board.dark_shas.add(sha(NEW.bit()))            # the new image never answers
    out, _ = w.install()
    assert out.result == RESULT_REVERTED, out.detail
    assert "AUTO-REVERTED" in out.detail and "DARK" in out.detail
    assert len(w.hub.writes) == 2 and w.hub.sd_bit == OLD.bit()     # install, then revert
    assert w.hub.mcc_reboots == 2
    assert w.board.identity().firmware_sha == OLD.sha
    assert "update.dark" in w.topics() and "update.auto_revert" in w.topics()
    rec = w.svc.installer("mps3").records.history(w.session.candidate.board_id)[0]
    assert rec["dark"] is True and rec["auto_revert"]["written"] is True
    assert rec["result"] == RESULT_REVERTED and not out.ok


def test_twin_dark_without_auto_revert_is_reported_loudly_and_nothing_is_done(tmp_path, server):
    w = World(tmp_path, server)
    w.board.dark_shas.add(sha(NEW.bit()))
    out, _ = w.install(auto_revert=False)
    assert out.result == RESULT_DARK and "NOTHING WAS DONE" in out.detail
    assert "auto-revert was NOT armed" in out.detail and "rollback" in out.restore_hint
    assert len(w.hub.writes) == 1 and w.hub.mcc_reboots == 1
    dark = [e for e in w.events if e.topic == "update.dark"]
    assert dark and dark[0].data["armed"] is False
    assert "update.auto_revert" not in w.topics()
    rec = w.svc.installer("mps3").records.history(w.session.candidate.board_id)[0]
    assert rec["result"] == RESULT_DARK and rec["dark"] is True


def test_a_manual_rollback_writes_the_backup_back_through_the_hub(tmp_path, server):
    w = World(tmp_path, server)
    w.board.dark_shas.add(sha(NEW.bit()))
    out, _ = w.install(auto_revert=False)
    assert out.result == RESULT_DARK
    back = w.installer().rollback(w.session)         # via the last install's door: the hub
    assert back.result == RESULT_RESTORED, back.detail
    assert w.hub.sd_bit == OLD.bit() and w.board.identity().firmware_sha == OLD.sha


# --- REST ----------------------------------------------------------------------------------------


def test_over_rest_the_upload_goes_to_the_repository_and_one_post_programs_it(tmp_path, server):
    w = World(tmp_path, server, rest=True)
    try:
        out, _ = w.install()
    finally:
        w.api.stopped = True
    assert out.result == RESULT_INSTALLED, out.detail
    assert len(w.api.posts) == 1 and w.api.posts[0][1]["bitstream_id"].startswith("bs-")
    assert w.door.last["reply"] == "timeout"
    assert w.door.last["completion"] in ("event", "hub-state")


def test_twin_over_rest_a_corrupt_upload_is_refused_before_the_write(tmp_path, server):
    w = World(tmp_path, server, rest=True)
    w.hub.corrupt_upload = True
    try:
        with pytest.raises(HarnessError) as err:
            w.install()
        assert "nothing was written" in err.value.message and w.api.posts == []
        assert w.door.pending() is None
        w.hub.corrupt_upload = False                 # the journal was left droppable
        out, _ = w.install()
    finally:
        w.api.stopped = True
    assert out.result == RESULT_INSTALLED and len(w.api.posts) == 1


def _file(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


# --- what the door needs from the hub ---------------------------------------------------------


def test_a_hub_with_no_sd_method_is_not_a_door(tmp_path, server):
    w = World(tmp_path, server)
    w.hub.no_sd_method = True
    plan, _ = w.plan()
    assert plan.via == "hub" and any("no 'sd' program method" in b for b in plan.blockers)


def test_twin_no_mcc_on_the_hub_means_nothing_could_reboot_it(tmp_path, server):
    w = World(tmp_path, server)
    w.session.controller = None
    plan, _ = w.plan()
    assert any("the MCC reached on the hub" in b for b in plan.blockers)
    assert not any("Debug USB" in b for b in plan.blockers)
