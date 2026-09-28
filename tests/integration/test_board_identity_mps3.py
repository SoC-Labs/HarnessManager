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

#: Board 2 today: board 1's identity, every field the image default.
AS_BOARD1 = {"label": "MPS3-01", "ip": "192.168.10.101/24", "mac": BOARD1_MAC,
             "source": {"label": "default", "ip": "default", "mac": "default"}}
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
