"""BOARD-ID: detect an identity clash and make a board match its hub entry, on the real MPS3
pack against HM's own model of the Linux harness's identity verbs (``tests/fakes/idn_board``,
net-protocol v0.16).

The lab of 2026-09-28: board 2 (``mps3_02_pl``, 192.168.11.101) runs board 1's identity
(``MPS3-01``, 192.168.10.101, 02:00:00:4d:50:53). Every behaviour has its negative twin: a
matching identity gives no warning; the fix is refused during a card job, without the lease,
on a claim that is not ours, on bare metal, on a netbooted board, and with the wrong phrase;
and it NEVER asks the MCC to reboot: the harness's own ``reboot`` verb does it.
"""

from __future__ import annotations

import pytest

from harness_manager.core.errors import (
    ActionFailedError,
    HeldError,
    RefusedError,
    UnavailableError,
)
from harness_manager.services.board_identity import IdentityService, SeenIdentities
from harness_manager_mps3 import tunnel as T
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from harness_manager_mps3.net_identity import HarnessdSetter, SshCommandSetter
from tests.fakes.claimed_lock import board_key_fp, pin_claim
from tests.fakes.idn_board import (
    BOARD1_MAC,
    BOARD2_MAC,
    FakeHub,
    FakeLeases,
    SpyController,
    identity_board,
)
from tests.fakes.lxslots_board import TRUSTED, BoardSsh, board_session

#: Board 2 with board 1's identity: board 1's stage0 bake (label, IP) and the image's MAC.
#: V7-ALIGN: on the shipped image a "default" label is always ``MPS3``, never ``MPS3-01``.
AS_BOARD1 = {"label": "MPS3-01", "ip": "192.168.10.101/24", "mac": BOARD1_MAC,
             "source": {"label": "stage0", "ip": "stage0", "mac": "default"}}
#: Board 2 on rc2_v7 before its identity bake is fielded: the generic label and the old MAC;
#: its IP is already its own, from stage0.
BOARD2_TONIGHT = {"label": "MPS3", "ip": "192.168.11.101/24", "mac": BOARD1_MAC,
                  "source": {"label": "default", "ip": "stage0", "mac": "default"}}
#: Board 2 as its hub entry says.
AS_BOARD2 = {"label": "MPS3-02", "ip": "192.168.11.101/24", "mac": BOARD2_MAC,
             "source": {"label": "stage0", "ip": "stage0", "mac": "stage0"}}


def board1_seen(seen: SeenIdentities) -> None:
    """Board 1, read by this Harness Manager earlier (another session)."""
    import time

    seen.update("mps3@192.168.10.101:6900", label="MPS3-01", ip="192.168.10.101/24",
                mac="02:00:00:4d:50:53", hostname="mps3-01", target="mps3_01_pl",
                address="192.168.10.101:6900", at=time.time(), name="mps3-01")


@pytest.fixture
def lab(monkeypatch, tmp_path):
    """Board 2, claimed by THIS Harness Manager, behind the hub, reached over its claim."""
    made = []

    def make(*, running=AS_BOARD1, claimed=True, pinned=True, mine=True, **kw):
        fake = identity_board(running=running, ssh_claimed=claimed,
                              slots={"trusted_peer": TRUSTED},
                              ssh_host_key_sha256=board_key_fp(), **kw)
        monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
        ssh = BoardSsh(fake)
        monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
        monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
        session = board_session(fake)
        if pinned:
            pin_claim(session)
        session.hub = FakeHub("mps3_02_pl")
        session.controller = SpyController()
        seen = SeenIdentities(tmp_path / "identity")
        board1_seen(seen)
        svc = IdentityService(leases=FakeLeases(mine), seen=seen)
        made.append((fake, session, ssh))
        return fake, session, svc

    yield make
    for fake, session, ssh in made:
        session.close()
        ssh.close()
        fake.stop()


def nothing_sent(fake) -> bool:
    return fake.identity_sets == [] and fake.reboots == []


# --- detection ----------------------------------------------------------------------------------


def test_board2_reporting_board1s_identity_is_a_clash(lab):
    fake, session, svc = lab()
    st = svc.status(session, refresh=True)
    assert st["status"] == "clash" and st["level"] == "err"
    clashes = {(f["field"], f["other"]) for f in st["findings"] if f["kind"] == "clash"}
    # the hub lists .10.101 and MPS3-01 for mps3_01_pl; this HM saw board 1 with all three
    assert ("ip", "mps3_01_pl") in clashes and ("label", "mps3_01_pl") in clashes
    assert {("mac", "mps3-01"), ("ip", "mps3-01"), ("label", "mps3-01")} <= clashes
    # the hub's board_mac for mps3_01_pl is the hub's own adapter: never a clash on it
    assert ("mac", "mps3_01_pl") not in clashes
    kinds = {f["kind"] for f in st["findings"]}
    assert {"unset", "differs"} <= kinds
    # the fix the tile offers: the hub record, minus nothing (board 2's hub MAC is good)
    fix = st["fix"]
    assert {c["field"]: c["to"] for c in fix["changes"]} == {
        "label": "MPS3-02", "ip": "192.168.11.101/24", "mac": "02:00:00:00:02:fe"}
    assert fix["phrase"] == "MPS3-02" and fix["ready"] is True and fix["refusal"] is None


def test_twin_a_board_that_matches_its_hub_entry_has_no_warning(lab):
    fake, session, svc = lab(running=AS_BOARD2)
    st = svc.status(session, refresh=True)
    assert st["status"] == "ok" and st["level"] == "ok"
    assert st["findings"] == [] and st["notes"] == []
    assert st["fix"]["changes"] == [] and st["fix"]["ready"] is False


def test_info_carries_the_clash_as_a_health_note_without_a_control_connection(lab):
    from harness_manager.engine import Engine

    fake, session, svc = lab()
    svc.status(session, refresh=True)        # one explicit read, as the tile or the CLI makes
    reads = fake.identity_reads
    eng = Engine()
    eng._services["board_identity"] = svc
    entry = type("E", (), {"session": session, "candidate": session.candidate})()
    out = eng._net_identity(entry)
    assert out["status"] == "clash" and fake.identity_reads == reads   # info: the cached read


# --- the fix ------------------------------------------------------------------------------------


def test_the_fix_sets_reboots_warm_and_verifies(lab):
    fake, session, svc = lab()
    said: list[str] = []
    out = svc.fix(session, confirm="MPS3-02", from_hub=True, wait_s=20, progress=said.append)
    assert out["action"] == "set" and out["verified"] is True
    # set once, from the board itself (the claim forward), then ONE reboot verb
    assert [p for p, _ in fake.identity_sets] == [TRUSTED]
    assert fake.identity_sets[0][1] == {"label": "MPS3-02", "ip": "192.168.11.101/24",
                                        "mac": "0200000002fe"}
    assert len(fake.reboots) == 1 and out["set"]["route"] == "board-ssh"
    assert out["reboot"]["summary"].startswith("reboot witnessed")
    after = out["identity"]
    assert after["reported"]["label"] == "MPS3-02" and after["reported"]["mac"] == \
        "02:00:00:00:02:fe" and after["reported"]["pending"] is None
    assert any("never an MCC REBOOT" in s for s in said)


def test_never_an_mcc_reboot_the_reset_path_is_the_harness_reboot_verb(lab, monkeypatch):
    fake, session, svc = lab()
    used: list[str] = []
    real = session.os_slots.reboot

    def spy(*a, **kw):
        used.append("harness reboot verb")
        return real(*a, **kw)

    monkeypatch.setattr(session.os_slots, "reboot", spy)
    svc.fix(session, confirm="MPS3-02", from_hub=True, wait_s=20)
    assert used == ["harness reboot verb"] and session.controller.calls == []
    assert len(fake.reboots) == 1


def test_twin_the_wrong_phrase_changes_nothing(lab):
    fake, session, svc = lab()
    with pytest.raises(RefusedError, match="typed phrase"):
        svc.fix(session, confirm="yes", from_hub=True)
    assert nothing_sent(fake)


def test_twin_a_card_job_refuses_the_fix(lab):
    fake, session, svc = lab()
    fake.hold_job("writing", act="push", slot="B")
    with pytest.raises(HeldError, match="being written"):
        svc.fix(session, confirm="MPS3-02", from_hub=True)
    assert nothing_sent(fake)


def test_twin_without_the_lease_the_fix_is_refused(lab):
    fake, session, svc = lab(mine=None)
    with pytest.raises(HeldError, match="changing the board's identity is for the lease holder"):
        svc.fix(session, confirm="MPS3-02", from_hub=True)
    assert nothing_sent(fake)
    fake2, session2, svc2 = lab(mine=False)
    with pytest.raises(HeldError, match="someone@else holds"):
        svc2.fix(session2, confirm="MPS3-02", from_hub=True)
    assert nothing_sent(fake2)


def test_twin_a_claim_that_is_not_ours_refuses_the_fix(lab):
    fake, session, svc = lab(pinned=False)           # claimed, by a key this HM never pinned
    st = svc.status(session, refresh=True)
    assert st["fix"]["ready"] is False and st["fix"]["refusal"]["name"] == "REFUSED"
    with pytest.raises(RefusedError, match="claim-locked"):
        svc.fix(session, confirm="MPS3-02", from_hub=True)
    assert nothing_sent(fake)


def test_twin_an_unclaimed_board_must_be_claimed_first(lab):
    fake, session, svc = lab(claimed=False, pinned=False)
    with pytest.raises(RefusedError, match="claimed by this Harness Manager") as e:
        svc.fix(session, confirm="MPS3-02", from_hub=True)
    assert "board claim TARGET" in e.value.hint and nothing_sent(fake)


def test_twin_a_netbooted_board_is_refused_with_the_stage0_bake(lab):
    fake, session, svc = lab(persist=False)
    st = svc.status(session, refresh=True)
    assert st["fix"]["refusal"]["name"] == "REFUSED"
    assert "stage0 bake" in st["fix"]["refusal"]["message"]
    with pytest.raises(RefusedError, match="stage0 bake") as e:
        svc.fix(session, confirm="MPS3-02", from_hub=True)
    assert "re-bake stage0" in e.value.hint and nothing_sent(fake)


def test_twin_the_board_says_no_persist_even_when_the_read_did_not(lab, monkeypatch):
    fake, session, svc = lab()
    fake.persist = False                            # the card went away after the read
    session.net_identity.setter = HarnessdSetter()
    monkeypatch.setattr(session.net_identity, "fix_reason", lambda reported: ("", "", ""))
    with pytest.raises(RefusedError, match="stage0 bake"):
        svc.fix(session, confirm="MPS3-02", from_hub=True)
    assert nothing_sent(fake)


def test_twin_an_image_without_the_identity_verbs_is_pending_the_linux_lead(lab):
    fake, session, svc = lab(has_identity=False)
    st = svc.status(session, refresh=True)
    assert st["reported"]["via"] == "identify" and st["reported"]["mac"] == "02:00:00:4d:50:53"
    assert st["status"] == "clash"                    # detection still works from identify
    assert "pending the Linux lead's interface" in st["fix"]["refusal"]["message"]
    with pytest.raises(UnavailableError, match="pending the Linux lead's interface"):
        svc.fix(session, confirm="MPS3-02", from_hub=True)
    assert nothing_sent(fake)


def test_twin_bare_metal_is_refused_with_the_reason(monkeypatch, tmp_path):
    from pyverify.testing.fakeshell import FakeShell

    fake = FakeShell.ephemeral(profile="bare-metal", mac="0200004d5053", v011_verbs=True)
    fake.start()
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    session = board_session(fake)
    try:
        svc = IdentityService(leases=FakeLeases(True), seen=SeenIdentities(tmp_path / "i"))
        st = svc.status(session, refresh=True)
        assert st["reported"]["impl"] == "bare-metal" and st["status"] == "unset"
        with pytest.raises(UnavailableError, match="bare-metal harness has no identity store"):
            svc.fix(session, confirm="MPS3-02", want={"label": "MPS3-02"})
    finally:
        session.close()
        fake.stop()


def test_the_ssh_command_setter_runs_mps3_identity_over_the_pinned_ssh(lab):
    fake, session, svc = lab()
    runs = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    session.net_identity.setter = SshCommandSetter(run=lambda argv, t: runs.append(argv) or Done())
    out = session.net_identity.set_identity({"label": "MPS3-02", "mac": "02:00:00:00:02:fe"})
    assert out["setter"] == "ssh" and len(runs) == 1
    assert runs[0][-1] == "mps3-identity set label=MPS3-02 mac=02:00:00:00:02:fe"
    assert "-l" in runs[0] and fake.identity_sets == []


def test_the_verify_says_so_when_the_board_did_not_take_it(lab, monkeypatch):
    fake, session, svc = lab()
    real = fake._simulate_restart

    def forgetful():
        fake._next = None                            # the reboot came up with the old identity
        real()

    monkeypatch.setattr(fake, "_simulate_restart", forgetful)
    with pytest.raises(ActionFailedError, match="does not report the new identity"):
        svc.fix(session, confirm="MPS3-02", from_hub=True, wait_s=20)
    assert len(fake.identity_sets) == 1 and len(fake.reboots) == 1


# --- V7-ALIGN: the shipped contract (net-protocol v0.16, platform 18622e5) ----------------------


def test_v7_board2_tonight_is_identity_not_set_and_its_only_clash_is_the_mac(lab):
    """Board 2 keeps label MPS3 and the old MAC; this HM saw board 1 with the same MAC. The
    label alone is never a clash; the duplicate MAC still is."""
    fake, session, svc = lab(running=BOARD2_TONIGHT)
    st = svc.status(session, refresh=True)
    clashes = {(f["field"], f["other"]) for f in st["findings"] if f["kind"] == "clash"}
    assert clashes == {("mac", "mps3-01")}
    unset = [f["text"] for f in st["findings"] if f["kind"] == "unset"]
    assert unset and unset[0].startswith("identity not set (default label, MAC)")
    assert st["reported"]["hostname"] == "mps3"                  # the label lower-cased
    differs = {f["field"] for f in st["findings"] if f["kind"] == "differs"}
    assert "label" not in differs and "mac" in differs, "default label: unset, not differs"


def test_v7_twin_board1_with_its_own_mac_leaves_board2_identity_not_set(lab, tmp_path):
    import time

    fake, session, svc = lab(running=BOARD2_TONIGHT)
    svc.seen.update("mps3@192.168.10.101:6900", label="MPS3-01", label_source="stage0",
                    mac="02:00:00:00:01:fe", at=time.time())
    st = svc.status(session, refresh=True)
    assert [f for f in st["findings"] if f["kind"] == "clash"] == []
    assert st["status"] == "unset"


def test_v7_the_claim_is_checked_before_the_card_like_the_board(lab):
    """The board's order: ``locked`` first, then ``no_persist``. A netbooted board that is
    claimed by a key this HM never pinned is refused for the CLAIM."""
    fake, session, svc = lab(pinned=False, persist=False)
    st = svc.status(session, refresh=True)
    assert "claim-locked" in st["fix"]["refusal"]["message"]
    with pytest.raises(RefusedError, match="claim-locked"):
        svc.fix(session, confirm="MPS3-02", from_hub=True)
    assert nothing_sent(fake)
    # the board itself says the same, whatever the request holds
    reply = fake.handle_control({"op": "identity_set", "label": "no good"}, peer="10.9.9.9")
    assert reply == {"ok": False, "err": "identity locked: board claimed (use ssh)",
                     "code": "locked"}


def test_v7_twin_our_claim_on_a_netbooted_board_is_refused_for_the_card(lab):
    fake, session, svc = lab(persist=False)
    with pytest.raises(RefusedError, match="stage0 bake"):
        svc.fix(session, confirm="MPS3-02", from_hub=True)
    assert nothing_sent(fake)


def test_v7_a_bad_value_on_a_netbooted_board_is_refused_for_the_card_not_the_value(lab):
    """``invalid`` comes last: a label the board would refuse, on a board that cannot take a
    change at all, is refused for the card (REFUSED), not for the label (USAGE)."""
    fake, session, svc = lab(persist=False)
    with pytest.raises(RefusedError, match="stage0 bake"):
        svc.fix(session, confirm="x", want={"label": "lower case"})
    assert nothing_sent(fake)
    assert fake.handle_control({"op": "identity_set", "label": "lower-case"}, peer=TRUSTED) == {
        "ok": False, "err": "identity: no persistent /persist (use the card)",
        "code": "no_persist"}


def test_v7_twin_a_bad_value_on_a_board_that_can_take_it_is_a_usage_error(lab):
    from harness_manager.core.errors import UsageError

    fake, session, svc = lab()
    with pytest.raises(UsageError, match="A-Z"):
        svc.fix(session, confirm="x", want={"label": "lower case"})   # IDENTITY: a space
    assert nothing_sent(fake)
    reply = fake.handle_control({"op": "identity_set", "label": "lower-case"}, peer=TRUSTED)
    assert reply == {"ok": False, "err": "invalid label: not [A-Z0-9-]", "code": "invalid"}


def test_v7_the_boards_refusal_codes_map_to_hm_errors_through_the_setter(lab):
    """Straight to the board (no pre-check): each shipped code as its HM error."""
    from harness_manager.core.errors import UsageError

    fake, session, svc = lab()
    setter = HarnessdSetter()
    with pytest.raises(UsageError, match="invalid label: longer than 19"):
        setter(session, {"label": "X" * 20})
    fake.persist = False
    with pytest.raises(RefusedError, match="stage0 bake"):
        setter(session, {"label": "X" * 20})         # no_persist beats invalid
    assert fake.identity_sets == []


BOARD2_BAKE = {"label": "MPS3-02", "ip": "192.168.11.101/24", "mac": BOARD2_MAC}


def test_v7_an_empty_string_drops_the_key_from_the_boards_override(lab):
    fake, session, svc = lab(running=AS_BOARD2, stage0=BOARD2_BAKE)
    fake.override = {"hostname": "bench"}
    fake.running = {**fake.running, "hostname": "bench",
                    "source": {**fake.running["source"], "hostname": "override"}}
    out = svc.fix(session, confirm="MPS3-02", want={"hostname": ""}, wait_s=20)
    assert fake.identity_sets[0][1] == {"hostname": ""}, "the wire's empty string"
    assert out["verified"] is True and fake.override is None
    rep = out["identity"]["reported"]
    assert rep["hostname"] == "mps3-02" and rep["source"]["hostname"] == "label"


def test_v7_twin_dropping_a_key_the_override_does_not_hold_sends_nothing(lab):
    fake, session, svc = lab(running=AS_BOARD2, stage0=BOARD2_BAKE)
    out = svc.fix(session, confirm="MPS3-02", want={"hostname": ""}, wait_s=20)
    assert out["action"] == "none" and nothing_sent(fake)


def test_v7_a_reply_without_op_is_taken_as_well_as_one_with_it(lab):
    """The new replies carry ``op`` (shipped); a draft's did not. Both are taken."""
    for reply_op in (True, False):
        fake, session, svc = lab(reply_op=reply_op)
        out = svc.fix(session, confirm="MPS3-02", from_hub=True, wait_s=20)
        assert out["verified"] is True and out["set"]["applies"] == "reboot"


def test_v7_identify_carries_the_label_before_ports(lab):
    from harness_manager_mps3 import identify as I

    fake, session, svc = lab(running=AS_BOARD2)
    reply = I.identify("127.0.0.1", port=fake.identify_port, timeout=2.0)
    keys = list(reply.raw)
    assert reply.raw["label"] == "MPS3-02" and keys[-1] == "ports"
    assert keys.index("label") == keys.index("ssh") + 1
    got = session.net_identity._from_identify(impl="linux")
    assert got["label"] == "MPS3-02" and got["ip"] == "192.168.11.101"


def test_v7_twin_identify_on_a_dhcp_lease_is_not_the_boards_ip(lab):
    fake, session, svc = lab(running=AS_BOARD2)
    fake.dhcp = True                                 # identify's ip is now the lease
    got = session.net_identity._from_identify(impl="linux")
    assert got["ip"] == "" and got["lease"] == "192.168.11.101"
    assert got["label"] == "MPS3-02"


def test_v7_bare_metal_not_supported_is_unavailable_with_the_reason(lab):
    """A v0.16 bare-metal coordinator declines both verbs: ``identity not supported``, code
    ``not_supported``: UNAVAILABLE, bare metal named."""
    fake, session, svc = lab(has_identity=False, decline="not_supported")
    with pytest.raises(UnavailableError, match="bare-metal harness has no identity store"):
        HarnessdSetter()(session, {"label": "MPS3-02"})
    assert fake.identity_sets == []


def test_v7_twin_an_image_older_than_v016_is_pending_the_linux_lead(lab):
    fake, session, svc = lab(has_identity=False)                 # "unknown op"
    with pytest.raises(UnavailableError, match="pending the Linux lead's interface"):
        HarnessdSetter()(session, {"label": "MPS3-02"})
    assert fake.identity_sets == []
