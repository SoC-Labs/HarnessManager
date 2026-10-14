"""SEC-ALIGN: Harness Manager against the REAL claim enforcement (platform rc2, 16a9aad6).

pyverify's ``FakeShell`` enforces ``pyverify.claim_table`` (generated from the firmware's
``mps3_mg_table[]``) on 6900, the 6910 push header and TFTP WRQs. These tests use that, not a
copy of it: the verbs Harness Manager sends through the claim tunnel are MUTATE rows, the
ones it sends direct are READ rows, every newly locked verb is refused for a peer that is not
the board itself, and Harness Manager's routed call gets through. The card-less board's
``claim lost on reboot`` reply is not retried through the tunnel and is shown in the new words.

``lease show/status`` is deliberately not pinned here (SEC is splitting it into a READ row).
"""

from __future__ import annotations

import json

import pytest
from pyverify import claim_table as T

from harness_manager.core.errors import ClaimLockedError
from harness_manager_mps3 import claim_route as CR
from harness_manager_mps3 import slot_words as SW
from harness_manager_mps3.claim import LOST_CLAIM_WORDS
from tests.fakes.claimed_lock import TRUSTED, V22_LOCKED_VERBS
from tests.integration.test_claimed_lock import lab  # noqa: F401  (the fixture)
from tests.integration.test_claimroute import HUB_SIDE, VERBS, sent, status_of

LAN = "192.0.2.77"            # a LAN peer: not the board itself

# Every state-changing form HM sends through the claim tunnel (6900), as the table writes it.
LOCKED_FORMS = {
    "swap": {"op": "swap", "rm": "nanosoc", "src": "tcp"},
    "set_clk": {"op": "set_clk", "preset": "25mhz"},
    "reset dut": {"op": "reset", "target": "dut"},
    "reset rp": {"op": "reset", "target": "rp"},
    "reset mcc_warm": {"op": "reset", "target": "mcc_warm"},
    "reset ddr": {"op": "reset", "target": "ddr"},
    "link": {"op": "link", "event": "pulse"},
    "macgen": {"op": "macgen", "gen": False, "chk": False, "inject": "none"},
    "dutrx": {"op": "dutrx"},
    "reboot": {"op": "reboot"},
    "display dut": {"op": "display", "owner": "dut"},
    "display harness": {"op": "display", "owner": "harness"},
    "display toggle": {"op": "display", "owner": "toggle"},
    "touch_cal set": {"op": "touch_cal", "act": "set", "ax": 1, "bx": 0, "cx": 0, "ay": 0,
                      "by": 1, "cy": 0, "shift": 0},
    "touch_cal default": {"op": "touch_cal", "act": "default"},
    "identity_set": {"op": "identity_set", "label": "SEC-T"},
    "usd rescan": {"op": "usd", "action": "rescan"},
    "commit": {"op": "commit", "rm": "nanosoc", "src": "tcp"},
    "slot commit": {"op": "slot", "act": "commit"},
    "slot rollback": {"op": "slot", "act": "rollback"},
    "hostio set": {"op": "hostio", "act": "set", "mode": "console"},
    "sensors set": {"op": "sensors", "act": "set"},
    "cosim step": {"op": "cosim", "act": "step", "n": 1},
}
# What HM sends straight to the board, from any peer.
OPEN_FORMS = {
    "ping": {"op": "ping"}, "diag": {"op": "diag"}, "identity": {"op": "identity"},
    "locate": {"op": "locate", "s": 1},
    "slot verify": {"op": "slot", "act": "verify", "slot": "A"},
    "slot status": {"op": "slot", "act": "status"},
    "usd status": {"op": "usd"},
    "display query": {"op": "display", "owner": "query"},
    "touch_cal get": {"op": "touch_cal", "act": "get"},
    "hostio get": {"op": "hostio", "act": "get"},
    "cosim cycles": {"op": "cosim", "act": "cycles"},
}


@pytest.mark.parametrize("name", sorted(LOCKED_FORMS))
def test_the_real_table_says_hm_routes_this_form_through_the_tunnel(name):
    assert T.mutates(LOCKED_FORMS[name]), name


@pytest.mark.parametrize("name", sorted(OPEN_FORMS))
def test_the_real_table_says_hm_may_send_this_form_direct(name):
    assert not T.mutates(OPEN_FORMS[name]), name


def test_the_fake_lease_gates_name_only_mutate_verbs():
    for verb in V22_LOCKED_VERBS:
        row = T.lookup_ctrl({"op": verb, "target": "dut", "owner": "dut"})
        assert row is not None and row["cls"] == "mutate", verb


@pytest.mark.parametrize("name", sorted(LOCKED_FORMS))
def test_a_lan_peer_is_refused_with_the_locked_code_and_the_board_itself_is_not(lab, name):  # noqa: F811
    rig = lab(pinned=True, observed=True)
    req = LOCKED_FORMS[name]
    got = rig.fake.handle_control(dict(req), peer=LAN)
    row = T.lookup_ctrl(req)
    if "handler" in row["gate"] and got.get("code") == "not_supported":
        # v0.20 order: not_supported is asked BEFORE the lock for handler-gated rows, and this
        # lab's board has no such engine: the lock is not reached, which the table allows
        assert got["err"].endswith("not supported")
        return
    assert got.get("ok") is False and got.get("code") == "locked", got
    assert "locked: board claimed (use ssh)" in got["err"]
    # byte for byte the table's refusal verb (identity_set refuses as "identity")
    assert got == T.refusal(row["verb"])
    inside = rig.fake.handle_control(dict(req), peer=TRUSTED)
    assert inside.get("code") != "locked", inside


def test_the_open_forms_are_served_to_a_lan_peer(lab):  # noqa: F811
    rig = lab(pinned=True, observed=True)
    for name, req in OPEN_FORMS.items():
        got = rig.fake.handle_control(dict(req), peer=LAN)
        assert got.get("code") != "locked", (name, got)
    assert rig.fake.gated == []


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_hms_routed_call_gets_through_the_real_gate_on_our_claim(lab, verb):  # noqa: F811
    rig = lab(pinned=True, observed=True)
    try:
        VERBS[verb](rig)
    except ClaimLockedError:
        raise
    except Exception:  # noqa: BLE001  (the fake may refuse for its own reasons)
        pass
    assert sent(rig, verb)[0] == TRUSTED and rig.fake.gated == []


# --- the claim lost on a reboot (card-less board, warm restart) --------------------------------

LOST_ERR = "{verb} locked: claim lost on reboot (re-claim on the serial console)"


def lose_the_claim(rig):
    """The board answers every mutation from a non-local peer with the lost-claim line, and
    does not accept the claim forward's peer either (nothing is claimed: no tunnel works)."""
    inner = rig.fake.handle_control

    def lost(request, peer=None):
        verb = str(request.get("op"))
        if T.mutates(request):
            rig.control.append((verb, str(request.get("act") or ""), str(peer)))
            return {"ok": False, "err": LOST_ERR.format(verb=verb), "code": "locked"}
        return inner(request, peer=peer)

    rig.fake.handle_control = lost


def test_the_lost_claim_line_is_a_lock_by_code_but_never_retried():
    err = LOST_ERR.format(verb="reset")
    assert SW.is_claim_lost(err, "locked") and SW.is_claim_lost(err)
    assert not SW.is_claim_lock(err, "locked") and not SW.is_claim_lock(err)
    assert CR.is_locked(err, "locked") is False
    plain = "reset locked: board claimed (use ssh)"
    assert SW.is_claim_lock(plain, "locked") and not SW.is_claim_lost(plain, "locked")
    # a code other than locked is never the lock, whatever the text says (match on code)
    assert not SW.is_claim_lock("board claimed", "busy") and not SW.is_claim_lost(err, "busy")
    # the error shown
    exc = SW.lost_claim_error(err)
    assert isinstance(exc, ClaimLockedError) and exc.message.startswith(LOST_CLAIM_WORDS)
    assert SW.refusal("commit", err, "locked").message.startswith(LOST_CLAIM_WORDS)
    from harness_manager.services.claim import lock_error
    from harness_manager_mps3.claim import refusal_error
    assert lock_error(err, "XVC").message.startswith(LOST_CLAIM_WORDS)
    assert refusal_error(err, "the swap").message.startswith(LOST_CLAIM_WORDS)


@pytest.mark.parametrize("verb", ["reset", "link", "reboot", "set_clk", "macgen", "display"])
def test_a_lost_claim_reply_is_not_retried_through_the_tunnel(lab, verb):  # noqa: F811
    rig = lab(pinned=True, observed=True)        # HM still believes it holds the claim
    lose_the_claim(rig)
    with pytest.raises(ClaimLockedError) as exc:
        VERBS[verb](rig)
    assert exc.value.message.startswith(LOST_CLAIM_WORDS), exc.value.message
    assert "mps3-reclaim --reopen" in exc.value.message and "Claim this board" in exc.value.message
    # HM thought the claim was ours, so the ONE attempt rode the forward; the lost-claim reply is
    # final: no second attempt, no second forward
    assert sent(rig, verb) == [TRUSTED] and len(rig.ssh.board_launches()) == 1, rig.control


def test_a_lost_claim_on_a_stale_decision_is_one_direct_attempt_and_no_forward(lab):  # noqa: F811
    rig = lab(pinned=True, observed=False)       # HM thinks unclaimed -> direct
    lose_the_claim(rig)
    with pytest.raises(ClaimLockedError) as exc:
        VERBS["reset"](rig)
    assert exc.value.message.startswith(LOST_CLAIM_WORDS)
    assert sent(rig, "reset") == [HUB_SIDE] and rig.ssh.board_launches() == []


def test_the_lost_claim_state_is_shown_after_the_reply_even_if_identify_says_nothing(lab):  # noqa: F811
    rig = lab(claimed=False, observed=False)     # never claimed here: no record -> no lost
    assert status_of(rig)["lost"] is None
    lose_the_claim(rig)
    with pytest.raises(ClaimLockedError):
        VERBS["link"](rig)
    st = status_of(rig)
    assert st["lost"] and st["lost"]["message"] == LOST_CLAIM_WORDS


def test_identify_claim_pending_shows_the_lost_claim_and_its_absence_is_tolerated(lab):  # noqa: F811
    from harness_manager_mps3.claim import Mps3Claim

    obs = Mps3Claim._from_raw({"ssh": {"claimed": False, "host_key_sha256": "k"}}, "lan", 1.0)
    assert obs.claim_pending is False
    obs = Mps3Claim._from_raw({"ssh": {"claimed": False, "host_key_sha256": "k",
                                       "claim_pending": True}}, "lan", 1.0)
    assert obs.claim_pending is True
    rig = lab(claimed=False, observed=False)
    claim = rig.session.claim
    import dataclasses
    base = claim.observe()
    st = claim.compose(dataclasses.replace(base, claimed=False, claim_pending=True))
    assert st["lost"]["message"] == LOST_CLAIM_WORDS
    assert claim.compose(dataclasses.replace(base, claimed=False))["lost"] is None


def test_the_lost_claim_json_line_on_the_debug_ports_reads_as_a_lock():
    from harness_manager.services.claim import lock_refusal

    line = json.dumps({"ok": False, "err": LOST_ERR.format(verb="xvc"), "code": "locked"})
    assert "claim lost on reboot" in lock_refusal(line)
