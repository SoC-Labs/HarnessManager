"""CLAIMED-LOCK: every claim-locked path on a claimed Linux board goes over the board's own SSH.

A claimed Linux harness serves XVC 2542, JTAG 6921, the slot push (6910 kind 2), ``slot
commit``/``rollback`` and the D13 store's ``usd`` actions and re-push ``commit`` to the board
itself only (a loopback peer: the far end of ``ssh -L ...:127.0.0.1:PORT``). Through the hub
the board sees the HUB as the peer and refuses. So, on a board THIS Harness Manager claimed,
each of those paths rides the session's ONE claim forward (``claim.hold_forward``); a board
claimed by another key (or with no pin here) is refused before anything is sent; the one-line
refusal (``{"ok":false,...,"code":"locked"}``) is a typed ``ClaimLockedError`` with the claim
hint; bare metal and unclaimed boards keep today's paths.

The lab is the real MPS3 pack, reached ``via ssh:HUB``, against pyverify's FakeShell (the slot
lock, plus ``claimed_lock.StoreLock``: the store lock), ``FakeXvcServer`` and
``FakeJtagServer`` with harnessd's accept-time lock line, and ``stub_openocd``. One fake ssh
plays both the hub tunnel (relays from 127.0.0.1: not the board) and the claim forward
(relays from 127.0.0.3: the board itself). Nothing leaves 127.0.0.1. Every check has its
negative twin.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from harness_manager.core.errors import ClaimLockedError, ExitCode, RefusedError
from harness_manager.core.events import EventBus
from harness_manager.services import xvc as xvc_service
from harness_manager.services.claim import LOCK_HINT, lock_error, lock_refusal
from harness_manager.services.debug import DebugService, classify_failure
from harness_manager.services.slots import SlotService
from harness_manager.services.xvc import XvcService
from harness_manager_mps3 import claim as CL
from harness_manager_mps3 import slot_words as W
from harness_manager_mps3 import tunnel as T
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.claimed_lock import (
    BOARD_IP,
    HUB,
    JTAG_LOCKED_LINE,
    TRUSTED,
    XVC_LOCKED_LINE,
    HubAndBoardSsh,
    board_key_fp,
    forward_specs,
    observed_claimed,
    pin_claim,
    route_board,
)
from tests.fakes.lxslots_board import LINUX_SID, slot_board
from tests.fakes.s0lb_image import make_s0lb
from tests.fakes.t2_overlays import SYNTH_RM_ID, make_overlay, use_overlay_dirs
from tests.fakes.t4_debug_rig import StubRig, use_stub
from tests.fakes.t4_rbb_jtag import FakeJtagServer
from tests.fakes.xvc_server import FakeXvcServer

NANOSOC_RM = 0x0001_0001            # design 0x0001 (nanosoc): the MPS3 debug adapter has its cfgs
LOCKED_PORTS = {6900, 6910, 6921, 2542}


# --- the lab ---------------------------------------------------------------------------------------


@dataclass
class Lab:
    fake: Any
    xvc: FakeXvcServer
    jtag: FakeJtagServer
    ssh: HubAndBoardSsh
    session: Any
    #: every 6900 request the board served: (op, act/action, peer)
    control: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def board_id(self) -> str:
        return self.session.candidate.board_id

    def mutations(self, op: str, *acts: str) -> list[tuple[str, str, str]]:
        return [c for c in self.control if c[0] == op and (not acts or c[1] in acts)]


@pytest.fixture
def lab(monkeypatch, tmp_path) -> Iterator[Any]:
    made: list[Lab] = []

    def make(*, claimed: bool = True, pinned: bool = False, observed: bool | None = None,
             profile: str = "linux", boot_rm_id: int = NANOSOC_RM,
             usd_card: str | None = None) -> Lab:
        linux = profile == "linux"
        fake = slot_board(profile=profile, ssh_claimed=claimed, boot_rm_id=boot_rm_id,
                          slots={"trusted_peer": TRUSTED} if linux else None,
                          ssh_host_key_sha256=board_key_fp(), usd_card=usd_card,
                          **({} if linux else {"features": ("xvc_dbgbr", "jtag_server",
                                                            "windowed")}))
        xvc = FakeXvcServer().start()
        jtag = FakeJtagServer().__enter__()
        xvc.claimed = jtag.claimed = claimed
        ssh = HubAndBoardSsh()
        route_board(ssh, {6900: fake.control_port, 6910: fake.raw_tcp_port,
                          6921: jtag.port, 2542: xvc.port})
        monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
        monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
        pack = Mps3Pack()                                   # the lab's real ports
        session = pack.open(pack.candidate_for_host(f"{BOARD_IP}:6900", via=f"ssh:{HUB}"))
        if session.os_slots is not None:
            session.os_slots.poll_s = session.os_slots.poll_max_s = 0.02
        rig = Lab(fake, xvc, jtag, ssh, session)
        served = fake.handle_control

        def record(request: dict, peer: str | None = None) -> dict:
            rig.control.append((str(request.get("op")),
                                str(request.get("act") or request.get("action") or ""),
                                str(peer)))
            return served(request, peer=peer)

        fake.handle_control = record
        if pinned:
            pin_claim(session)
        if observed is not None:
            observed_claimed(session.candidate.board_id, board_key_fp(), claimed=observed)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.session.close()
        rig.ssh.close()
        rig.xvc.close()
        rig.jtag.close()
        rig.fake.stop()


def assert_claim_forward(lab: Lab, port: int, launches: int = 1) -> list[str]:
    """``launches`` claim forwards were launched, each ``-J HUB -l root BOARD_IP`` with ``-L
    127.0.0.1:p:127.0.0.1:PORT`` for the locked ports and no forward to the hub's view of the
    board. Returns the last argv."""
    launched = lab.ssh.board_launches()
    assert len(launched) == launches, launched
    for argv in launched:
        check_forward_argv(argv, port)
    return launched[-1]


def check_forward_argv(argv: list[str], port: int) -> None:
    specs = forward_specs(argv)
    assert argv[argv.index("-J") + 1] == HUB and argv[argv.index("-l") + 1] == "root"
    assert any(s.startswith("127.0.0.1:") and s.endswith(f":127.0.0.1:{port}") for s in specs)
    assert {int(s.rsplit(":", 1)[1]) for s in specs} == LOCKED_PORTS
    assert not any(BOARD_IP in s for s in specs)
    assert "StrictHostKeyChecking=yes" in argv                  # the pinned host key


def claim_port(lab: Lab, name: str) -> int:
    st = lab.session.claim.forward_status()
    assert st is not None, "no claim forward is open"
    return int(st["forwards"][name]["local"])


# --- the refusal line ------------------------------------------------------------------------------


def test_the_lock_line_is_read_from_every_port_that_sends_it():
    assert lock_refusal(XVC_LOCKED_LINE) == "xvc locked: board claimed (use ssh)"
    assert lock_refusal(JTAG_LOCKED_LINE.decode()) == "jtag locked: board claimed (use ssh)"
    old = b'{"ok":false,"err":"usd locked: board claimed (use ssh)"}\n'     # before the code
    assert lock_refusal(old) == "usd locked: board claimed (use ssh)"
    exc = lock_error("xvc locked: board claimed (use ssh)", "XVC")
    assert isinstance(exc, ClaimLockedError) and exc.code == ExitCode.REFUSED
    assert exc.hint == LOCK_HINT and "board claim" in exc.hint
    assert W.ClaimLockedError is ClaimLockedError is CL.ClaimLockedError     # one lock, one class


def test_twin_anything_else_is_not_the_lock():
    for data in (b"xvcServer_v1.0:2048\n", b"", None, b"{not json\n",
                 b'{"ok":true,"err":"","code":"locked"}\n',
                 b'{"ok":false,"err":"EBUSY","code":"busy"}\n', b"0", b"1"):
        assert lock_refusal(data) == ""


def test_openocd_reading_the_jtag_line_is_the_claim_lock_not_held():
    text = ("Info : remote_bitbang interface quit\n"
            "Error: remote_bitbang: invalid read response: {(123)\n")
    exc = classify_failure(text, 1, target="remote_bitbang 127.0.0.1:6921")
    assert isinstance(exc, ClaimLockedError) and "claim" in exc.hint
    twin = classify_failure("Error: Error on socket 'remote_bitbang_fill_buf': errno==104, "
                            "message: Connection reset by peer.", 1)
    assert not isinstance(twin, ClaimLockedError) and twin.code == ExitCode.HELD


def test_xvc_probe_says_locked_and_its_twin_says_free():
    with FakeXvcServer() as srv:
        srv.claimed = True
        assert xvc_service.probe("127.0.0.1", srv.port)["state"] == "locked"
        srv.claimed = False
        assert xvc_service.probe("127.0.0.1", srv.port)["state"] == "free"


# --- the plan: whose claim, which route ------------------------------------------------------------


def test_the_plan_for_a_board_we_claimed_is_the_claim_forward(lab):
    rig = lab(pinned=True)
    assert rig.session.claim.lock_plan(impl="linux") == ("board-ssh", "")
    assert rig.session.claim.lock_plan(impl="linux", claimed=True) == ("board-ssh", "")


def test_twin_the_plan_for_bare_metal_or_an_unclaimed_board_is_todays_path(lab):
    rig = lab(claimed=False, pinned=True, observed=False)
    assert rig.session.claim.lock_plan(impl="linux") == ("", "")          # unclaimed now
    assert rig.session.claim.lock_plan(impl="bare-metal", claimed=True) == ("", "")


def test_the_plan_for_a_board_claimed_by_another_key_is_refused_with_the_hint(lab):
    rig = lab(observed=True)                                  # claimed; no pin here
    route, why = rig.session.claim.lock_plan(impl="linux")
    assert route == "locked" and "did not claim or adopt" in why
    with pytest.raises(ClaimLockedError) as exc:
        rig.session.claim.lock_route("XVC", impl="linux")
    assert "--adopt" in exc.value.hint and "mps3-unclaim" in exc.value.hint


def test_twin_a_board_not_known_to_be_claimed_keeps_todays_path(lab):
    rig = lab()                                               # claimed, but nobody has looked
    assert rig.session.claim.lock_plan(impl="linux") == ("", "")


# --- JTAG 6921: debug up -----------------------------------------------------------------------------


@pytest.fixture
def stub(monkeypatch, tmp_path) -> StubRig:
    return use_stub(monkeypatch, tmp_path)


@pytest.fixture
def debug() -> Iterator[DebugService]:
    svc = DebugService(EventBus(), start_timeout=15, cooldown=0.0)
    yield svc
    svc.close()


def rbb_of(run: list[str]) -> tuple[str, int]:
    sets = dict(a.split()[1:3] for a in run if a.startswith("set RBB_"))
    return sets["RBB_HOST"], int(sets["RBB_PORT"])


def test_debug_on_a_board_we_claimed_rides_the_claim_forward(lab, stub, debug):
    rig = lab(pinned=True)
    st = debug.up(rig.session)
    assert st.state == "up"
    assert_claim_forward(rig, 6921)
    assert rbb_of(stub.runs()[-1]) == ("127.0.0.1", claim_port(rig, "rbb"))
    assert rig.jtag.accepted == 1 and rig.jtag.lock_refusals == 0
    assert "through its SSH" in rig.session.debug.describe()
    debug.down(rig.session)
    assert rig.session.claim.forward_status() is None                    # never lingers
    assert all(p.returncode is not None for p in rig.ssh.procs if p.argv[-1] == BOARD_IP)


def test_twin_debug_on_an_unclaimed_board_uses_the_hub_tunnel(lab, stub, debug):
    rig = lab(claimed=False)
    assert debug.up(rig.session).state == "up"
    assert rbb_of(stub.runs()[-1]) == ("127.0.0.1", rig.session.reach.ports["rbb"])
    assert rig.ssh.board_launches() == [] and rig.jtag.accepted == 1


def test_debug_meeting_the_lock_line_is_a_typed_claim_error(lab, stub, debug):
    rig = lab()                               # claimed, not known here: today's path is tried
    with pytest.raises(ClaimLockedError) as exc:
        debug.up(rig.session)
    assert exc.value.code == ExitCode.REFUSED and "claim" in exc.value.hint
    assert "closed the connection" not in exc.value.message
    assert rig.jtag.lock_refusals == 1 and rig.jtag.accepted == 0
    assert debug.status(rig.session).state == "down"


def test_debug_on_a_board_claimed_by_another_key_is_refused_before_openocd_runs(lab, stub,
                                                                                  debug):
    rig = lab(observed=True)
    with pytest.raises(ClaimLockedError, match="debug session"):
        debug.up(rig.session)
    assert stub.runs() == [] and rig.jtag.accepted == 0 and rig.jtag.lock_refusals == 0
    assert rig.ssh.board_launches() == []


# --- XVC 2542 ---------------------------------------------------------------------------------------


@pytest.fixture
def xvc(tmp_path) -> Iterator[XvcService]:
    svc = XvcService(EventBus(), acquire_timeout=3.0)
    yield svc
    svc.close_all()


def test_xvc_on_a_board_we_claimed_rides_the_claim_forward(lab, xvc):
    rig = lab(pinned=True)
    st = xvc.open(rig.session, byo=True)
    assert st.board_slot == "ours" and st.reach == "board-ssh"
    assert_claim_forward(rig, 2542)
    assert rig.xvc.lock_refusals == 0 and len(rig.xvc.events("connect")) == 1
    assert rig.xvc.events("connect")[0][2].startswith(f"{TRUSTED}:")      # the board itself
    xvc.close(rig.session)
    assert rig.session.claim.forward_status() is None


def test_twin_xvc_on_bare_metal_uses_the_hub_tunnel(lab, xvc):
    rig = lab(claimed=False, profile="bare-metal")
    st = xvc.open(rig.session, byo=True)
    assert st.board_slot == "ours" and st.reach == "hub-tunnel"
    assert rig.ssh.board_launches() == []
    assert rig.xvc.events("connect")[0][2].startswith("127.0.0.1:")


def test_debug_and_xvc_share_one_claim_forward(lab, stub, debug, xvc):
    rig = lab(pinned=True)
    debug.up(rig.session)
    xvc.open(rig.session, byo=True)
    assert len(rig.ssh.board_launches()) == 1 and rig.session.claim.forwards_opened == 1
    assert rig.session.claim.forward_status()["users"] == ["debug", "xvc"]
    debug.down(rig.session)
    assert rig.session.claim.forward_status()["users"] == ["xvc"]      # XVC still has it
    xvc.close(rig.session)
    assert rig.session.claim.forward_status() is None


def test_xvc_meeting_the_lock_line_is_a_typed_claim_error_never_retried(lab, xvc,
                                                                         monkeypatch):
    rig = lab()                             # claimed, not known here, reach = hub: refused
    from harness_manager_mps3 import xvc as mx

    monkeypatch.setattr(mx, "xvc_config", lambda cand: {"reach": "hub"})   # the hub tunnel
    with pytest.raises(ClaimLockedError) as exc:
        xvc.open(rig.session, byo=True)
    assert "xvc locked: board claimed (use ssh)" in exc.value.message
    assert "claim" in exc.value.hint
    assert rig.xvc.lock_refusals == 1                           # once: a lock is not "held"
    assert xvc.status(rig.session).state in ("down", "failed")


def test_xvc_on_a_board_claimed_by_another_key_is_refused_before_connecting(lab, xvc):
    rig = lab(observed=True)
    with pytest.raises(ClaimLockedError, match="XVC"):
        xvc.open(rig.session, byo=True)
    assert rig.xvc.lock_refusals == 0 and rig.xvc.events("connect") == []
    assert rig.ssh.board_launches() == []


# --- the slots: commit/rollback over 6900, the push over 6910 ----------------------------------------


@pytest.fixture
def image(tmp_path) -> Path:
    p = tmp_path / "linux_slot.img"
    p.write_bytes(make_s0lb(b"\x5a" * 8192))
    return p


def test_slot_push_and_commit_on_a_board_we_claimed_go_over_the_claim_forward(lab, image):
    rig = lab(pinned=True)
    st = rig.session.os_slots.push(image, static_id=f"0x{LINUX_SID:08x}", sha256="",
                                   version="1.1.0")
    assert st.staged == "B" and rig.session.os_slots.last_route == "board-ssh"
    st = rig.session.os_slots.commit("B")
    assert st.default == "B" and rig.session.os_slots.last_route == "board-ssh"
    assert rig.mutations("slot", "commit") == [("slot", "commit", TRUSTED)]   # never direct
    assert_claim_forward(rig, 6910, launches=2)             # one per mutation, shared ports
    assert rig.session.claim.forward_status() is None                    # held per mutation


def test_twin_slot_commit_on_an_unclaimed_board_goes_direct(lab, image):
    rig = lab(claimed=False)
    rig.session.os_slots.push(image, static_id=f"0x{LINUX_SID:08x}", sha256="", version="1")
    rig.session.os_slots.commit("B")
    assert rig.session.os_slots.last_route == "direct"
    assert [c[2] for c in rig.mutations("slot", "commit")] == ["127.0.0.1"]
    assert rig.ssh.board_launches() == []


def test_slot_commit_on_a_board_claimed_by_another_key_is_refused_before_sending(lab):
    rig = lab(observed=True)
    before = (rig.fake.slots.deflt, rig.fake.slots.staged)
    with pytest.raises(ClaimLockedError) as exc:
        rig.session.os_slots.commit("A")
    assert "--adopt" in exc.value.hint
    assert rig.mutations("slot", "commit", "rollback") == []
    assert (rig.fake.slots.deflt, rig.fake.slots.staged) == before


def test_twin_a_board_not_known_claimed_refuses_once_then_says_whose_lock(lab):
    rig = lab()                                               # claimed; nothing known here
    with pytest.raises(ClaimLockedError) as exc:
        rig.session.os_slots.commit("A")
    assert "--adopt" in exc.value.hint
    assert rig.mutations("slot", "commit") == [("slot", "commit", "127.0.0.1")]  # refused
    assert rig.ssh.board_launches() == []


# --- the card: usd clear and the D13 commit over 6900/6910 ---------------------------------------------


@pytest.fixture
def overlays(tmp_path, monkeypatch) -> None:
    root = tmp_path / "overlays"
    make_overlay(root, "synth", rm_id=SYNTH_RM_ID, static_id=LINUX_SID)
    use_overlay_dirs(monkeypatch, root)


def test_card_clear_and_commit_on_a_board_we_claimed_go_over_the_claim_forward(lab, overlays):
    rig = lab(pinned=True, usd_card="da", boot_rm_id=SYNTH_RM_ID)
    svc = SlotService()
    out = svc.card_commit(rig.session)
    assert rig.fake.commits == [("synth", out["slot"])]
    assert rig.mutations("commit") == [("commit", "", TRUSTED)]
    svc.card_clear(rig.session)
    assert rig.mutations("usd", "clear") == [("usd", "clear", TRUSTED)]
    assert rig.session.claim.forward_status() is None


def test_twin_card_clear_on_an_unclaimed_board_goes_direct(lab):
    rig = lab(claimed=False, usd_card="da")
    SlotService().card_clear(rig.session)
    assert rig.mutations("usd", "clear") == [("usd", "clear", "127.0.0.1")]
    assert rig.ssh.board_launches() == []


def test_card_clear_meeting_the_usd_lock_is_a_typed_claim_error(lab):
    rig = lab(usd_card="da")                                  # claimed; nothing known here
    with pytest.raises(ClaimLockedError) as exc:
        SlotService().card_clear(rig.session)
    assert isinstance(exc.value, RefusedError) and "--adopt" in exc.value.hint
    assert rig.mutations("usd", "clear") == [("usd", "clear", "127.0.0.1")]


def test_the_store_locks_words_are_the_claim_lock_and_their_twin_is_not():
    from harness_manager_mps3.card import _usd_error

    for err in ("usd locked: board claimed (use ssh)", "commit locked: board claimed (use ssh)"):
        exc = _usd_error("card clear", err)
        assert isinstance(exc, ClaimLockedError) and exc.hint == LOCK_HINT
    assert not isinstance(_usd_error("card clear", "store busy"), ClaimLockedError)
    assert not isinstance(_usd_error("card clear", "identity lock: image 0x1 != fabric 0x2"),
                          ClaimLockedError)


def test_card_commit_on_a_board_claimed_by_another_key_is_refused_before_sending(lab, overlays):
    rig = lab(observed=True, usd_card="da", boot_rm_id=SYNTH_RM_ID)
    with pytest.raises(ClaimLockedError, match="card commit"):
        SlotService().card_commit(rig.session)
    assert rig.mutations("commit") == [] and rig.fake.commits == []


def test_keep_on_the_card_on_a_board_claimed_by_another_key_is_refused_before_the_swap(
        lab, overlays):
    rig = lab(observed=True, usd_card="da", boot_rm_id=0)
    ref = next(r for r in rig.session.deploy.overlays() if r.name == "synth")
    with pytest.raises(ClaimLockedError, match="on the card"):
        rig.session.deploy.deploy(ref, keep_on_card=True)
    assert rig.mutations("swap") == [] and rig.mutations("commit") == []


def test_twin_a_persist_refused_by_the_lock_says_so_in_the_cards_why():
    from pyverify.swap import PERSIST_FAILED, PersistResult

    from harness_manager_mps3.deploy import card_outcome

    locked = card_outcome(PersistResult(status=PERSIST_FAILED,
                                        err="commit locked: board claimed (use ssh)"))
    assert not locked.kept and "claiming key" in locked.why and "--adopt" in locked.why
    plain = card_outcome(PersistResult(status=PERSIST_FAILED, err="card io"))
    assert "claiming key" not in plain.why and "card write failed" in plain.why


# --- the session ------------------------------------------------------------------------------------


def test_closing_the_session_closes_the_claim_forward(lab, xvc):
    rig = lab(pinned=True)
    xvc.open(rig.session, byo=True)
    fwd = [p for p in rig.ssh.procs if p.argv[-1] == BOARD_IP]
    assert len(fwd) == 1 and fwd[0].returncode is None
    rig.session.close()
    assert fwd[0].returncode is not None
    with pytest.raises(Exception, match="closed"):
        rig.session.claim.hold_forward("debug")
