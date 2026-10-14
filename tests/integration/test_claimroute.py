"""CLAIMROUTE: the state-changing verbs board image v2.2 adds to the claim lock go through the
claim forward on a board this Harness Manager claimed.

The fake board enforces v2.2 (``claim_gate_all``: swap, reset, set_clk, link, macgen, dutrx,
display (not query), reboot, the design push on 6910 and TFTP refuse a peer that is not the
board itself), so a call site that does not ride the forward fails here. Every verb has its
three cases: claimed by us (forward, success), claimed by someone else (refused before
anything is sent), unclaimed (direct), and the retry-once path when HM's decision was stale.
"""

from __future__ import annotations

import pytest

from harness_manager.core.errors import ClaimLockedError
from harness_manager_mps3 import claim_route as CR
from harness_manager_mps3.claim import LOST_CLAIM_WORDS
from tests.fakes.claimed_lock import TRUSTED
from tests.fakes.lxslots_board import LINUX_SID
from tests.fakes.t2_overlays import SYNTH_RM_ID, make_overlay, use_overlay_dirs
from tests.integration.test_claimed_lock import lab  # noqa: F401  (the fixture)

HUB_SIDE = "127.0.0.1"          # the hub tunnel / this PC: not the board itself


@pytest.fixture
def gate(lab):                                                    # noqa: F811
    """A lab whose fake enforces the whole v2.2 lock."""
    def make(**kw):
        rig = lab(**kw)
        rig.fake.claim_gate_all = True
        return rig
    return make


@pytest.fixture
def overlays(tmp_path, monkeypatch) -> None:
    root = tmp_path / "overlays"
    make_overlay(root, "synth", rm_id=SYNTH_RM_ID, static_id=LINUX_SID)
    use_overlay_dirs(monkeypatch, root)


def sent(rig, op):
    return [peer for o, _a, peer in rig.control if o == op]


# one entry per verb: (op on the wire, how a call site makes it)
def v_reset(rig):
    rig.session.resets.reset("dut")


def v_set_clk(rig):
    CR.routed_call(rig.session, "setting the DUT clock", rig.session.shell,
                   lambda c: c.set_clk("50mhz"))


def v_link(rig):
    CR.routed_call(rig.session, "the link event", rig.session.shell, lambda c: c.link("pulse"))


def v_macgen(rig):
    CR.routed_call(rig.session, "the MAC generator request", rig.session.shell,
                   lambda c: c.macgen(gen=False, chk=False, inject="none"))


def v_dutrx(rig):
    CR.routed_call(rig.session, "reading the DUT's frames", rig.session.shell,
                   lambda c: c.read_dut_frame())


def v_display(rig):
    CR.routed_call(rig.session, "giving the panel away", rig.session.shell,
                   lambda c: c.display("dut"))


def v_reboot(rig):
    rig.session.os_slots.reboot()


VERBS = {"reset": v_reset, "set_clk": v_set_clk, "link": v_link, "macgen": v_macgen,
         "dutrx": v_dutrx, "display": v_display, "reboot": v_reboot}


def attempt(fn, rig):
    """The call may fail on the fake for reasons of its own (no such block); what matters is
    who the board saw. A claim refusal is never swallowed."""
    try:
        fn(rig)
    except ClaimLockedError:
        raise
    except Exception:  # noqa: BLE001
        pass


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_a_verb_on_a_board_we_claimed_rides_the_claim_forward(gate, verb):
    rig = gate(pinned=True, observed=True)
    attempt(VERBS[verb], rig)
    assert sent(rig, verb)[0] == TRUSTED, rig.control
    assert HUB_SIDE not in sent(rig, verb) and rig.fake.gated == []
    assert len(rig.ssh.board_launches()) == 1
    assert rig.session.claim.forward_status() is None            # let go after the verb


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_twin_a_verb_on_an_unclaimed_board_goes_direct(gate, verb):
    rig = gate(claimed=False, pinned=True, observed=False)
    attempt(VERBS[verb], rig)
    assert sent(rig, verb)[0] == HUB_SIDE and rig.ssh.board_launches() == []


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_a_verb_on_a_board_claimed_by_another_key_is_refused_before_sending(gate, verb):
    rig = gate(observed=True)                               # claimed; no pin here
    with pytest.raises(ClaimLockedError) as exc:
        VERBS[verb](rig)
    assert "--adopt" in exc.value.hint
    assert sent(rig, verb) == [] and rig.ssh.board_launches() == []


@pytest.mark.parametrize("verb", ["reset", "link", "reboot"])
def test_a_stale_decision_is_retried_once_through_the_forward_when_the_claim_is_ours(gate, verb):
    rig = gate(pinned=True, observed=False)              # HM thinks unclaimed; the board is not
    attempt(VERBS[verb], rig)
    assert sent(rig, verb)[:2] == [HUB_SIDE, TRUSTED], rig.control    # once direct, once routed
    assert rig.fake.gated == [(verb, HUB_SIDE)]
    assert len(rig.ssh.board_launches()) == 1


@pytest.mark.parametrize("verb", ["reset", "link", "reboot"])
def test_twin_a_stale_decision_with_someone_elses_claim_is_the_claims_plain_refusal(gate, verb):
    rig = gate(observed=False)                           # no pin: not our claim
    with pytest.raises(ClaimLockedError) as exc:
        VERBS[verb](rig)
    assert sent(rig, verb) == [HUB_SIDE] and rig.ssh.board_launches() == []
    assert "claiming key" in exc.value.message or "claimed" in exc.value.message


def test_a_read_stays_open_to_any_peer(gate):
    rig = gate(observed=True)                               # claimed by someone else
    assert rig.session.shell.live().shell_id
    assert rig.session.shell.call(lambda c: c.display_owner()) is not None
    assert rig.fake.gated == []


# --- the swap and the design push ----------------------------------------------------------------


def test_a_swap_and_its_push_on_a_board_we_claimed_ride_the_claim_forward(gate, overlays):
    rig = gate(pinned=True, observed=True, boot_rm_id=0)
    ref = next(r for r in rig.session.deploy.overlays() if r.name == "synth")
    res = rig.session.deploy.deploy(ref)
    assert res.verified
    assert sent(rig, "swap") == [TRUSTED] and rig.fake.gated == []
    assert len(rig.ssh.board_launches()) == 1


def test_twin_a_swap_on_an_unclaimed_board_goes_direct(gate, overlays):
    rig = gate(claimed=False, pinned=True, observed=False, boot_rm_id=0)
    ref = next(r for r in rig.session.deploy.overlays() if r.name == "synth")
    assert rig.session.deploy.deploy(ref).verified
    assert sent(rig, "swap") == [HUB_SIDE] and rig.ssh.board_launches() == []


def test_a_swap_on_a_board_claimed_by_another_key_is_refused_before_anything_is_sent(
        gate, overlays):
    rig = gate(observed=True, boot_rm_id=0)
    ref = next(r for r in rig.session.deploy.overlays() if r.name == "synth")
    with pytest.raises(ClaimLockedError, match="swap to synth"):
        rig.session.deploy.deploy(ref)
    assert sent(rig, "swap") == [] and rig.fake.gated == []


def test_a_swap_refused_by_a_board_thought_unclaimed_is_retried_once_through_the_forward(
        gate, overlays):
    rig = gate(pinned=True, observed=False, boot_rm_id=0)
    ref = next(r for r in rig.session.deploy.overlays() if r.name == "synth")
    assert rig.session.deploy.deploy(ref).verified
    assert sent(rig, "swap") == [HUB_SIDE, TRUSTED]
    assert rig.fake.gated[0] == ("swap", HUB_SIDE)           # (a push the PC began is refused too)
    assert {peer for _v, peer in rig.fake.gated} == {HUB_SIDE}


def test_twin_a_swap_refused_for_someone_elses_claim_is_the_claims_refusal(gate, overlays):
    rig = gate(observed=False, boot_rm_id=0)
    ref = next(r for r in rig.session.deploy.overlays() if r.name == "synth")
    with pytest.raises(ClaimLockedError):
        rig.session.deploy.deploy(ref)
    assert sent(rig, "swap") == [HUB_SIDE] and rig.ssh.board_launches() == []


@pytest.mark.parametrize("kind", [0, 1, 2])
def test_the_design_push_is_closed_unread_from_the_pc(gate, kind):
    """The REAL gate (rc2): 6910 header of any kind from a non-local peer: closed, no line."""
    import socket

    rig = gate(observed=True)
    header = b"MPS3" + bytes([0, 0, kind]) + b"\0" * 17
    with socket.create_connection(("127.0.0.1", rig.fake.raw_tcp_port), timeout=2) as s:
        s.sendall(header)
        s.shutdown(socket.SHUT_WR)               # the board drains until the peer is done
        s.settimeout(5)
        assert s.recv(200) == b""                # closed unread: nothing to read


def test_a_tftp_write_is_refused_from_the_pc_before_ack_0(gate):
    import socket

    rig = gate(observed=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        u.settimeout(5)
        u.sendto(b"\x00\x02design.bin\x00octet\x00", ("127.0.0.1", rig.fake.tftp_port))
        pkt, _ = u.recvfrom(600)
    assert pkt[:4] == b"\x00\x05\x00\x02" and b"access violation" in pkt   # ERROR 2, not ACK


# --- card-less boards ----------------------------------------------------------------------------


def status_of(rig):
    claim = rig.session.claim
    return claim.compose(claim.observe())


def test_a_board_that_lost_its_claim_on_restart_says_so_and_is_not_claimed_again(lab):  # noqa: F811
    from harness_manager_mps3 import claim as CL

    rig = lab(claimed=False, pinned=True, observed=False)
    before = CL.ClaimRecords().get(rig.board_id)
    st = status_of(rig)
    assert st["state"] == "unclaimed"
    assert st["lost"]["message"] == LOST_CLAIM_WORDS
    assert "mps3-reclaim --reopen" in LOST_CLAIM_WORDS and "Claim this board" in LOST_CLAIM_WORDS
    assert "from this PC" not in LOST_CLAIM_WORDS and LOST_CLAIM_WORDS in st["notes"]
    assert CL.ClaimRecords().get(rig.board_id).get("key_fp") == before.get("key_fp")
    assert rig.ssh.board_launches() == [] and rig.fake.ssh_claimed is False   # nothing re-claimed


def test_twin_no_lost_warning_for_a_claimed_board(lab):  # noqa: F811
    assert status_of(lab(pinned=True, observed=True))["lost"] is None


def test_twin_no_lost_warning_for_a_board_never_claimed_here(lab):  # noqa: F811
    assert status_of(lab(claimed=False, observed=False))["lost"] is None
