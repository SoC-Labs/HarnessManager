"""LINUX-CLAIM: the Linux harness's SSH claim, the host-key pin, the board-SSH reach and the
claim lock's refusal, against pyverify's FakeShell (``profile="linux"``: the identify
responder and the TOFU ``authorized_keys`` claim) and a fake ssh (``lc_fake_board_ssh``;
``l1_fake_ssh`` for the tunnel). Every check has a negative twin.

The hub is faked two ways: ``hub.DEFAULT_RUNNER_FACTORY`` replaced by a runner that runs the
hub command HERE (so the pyverify modules Harness Manager ships to the hub really run, under
the same ``sh -c`` interpreter pick, and really talk UDP to the FakeShell), and the tunnel's
launcher replaced by ``FakeSsh``. Nothing reaches a real host.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pyverify.testing.fakeshell import FakeShell

from harness_manager.core.errors import (
    AlreadyError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.model import BoardIdentity, Candidate, Link, LinkKind
from harness_manager.services.claim import ClaimService
from harness_manager_mps3 import claim as CL
from harness_manager_mps3 import hub as hubmod
from harness_manager_mps3 import tunnel as tunmod
from harness_manager_mps3.constants import FABRIC_MISMATCH_ERRS
from tests.fakes.l1_fake_ssh import FakeSsh
from tests.fakes.lc_fake_board_ssh import FakeBoardSsh, make_key_line, write_key_pair

HUB = "mapstone-dev.ecs.soton.ac.uk"
BOARD_KEY = make_key_line("the-board")
OTHER_BOARD_KEY = make_key_line("a-reprovisioned-board")


# --- rig ------------------------------------------------------------------------------------------


@dataclass
class FakeSession:
    """What ``Mps3Claim`` reads of a session: the candidate, a shell, the reach, the ports."""

    candidate: Candidate
    impl: str = "linux"
    reach: Any = None
    tftp_port: int | None = None
    hub: Any = None
    shell: Any = field(default_factory=object)
    claim: Any = None                                       # the pack hook's adapter

    def identity(self) -> BoardIdentity:
        return BoardIdentity(board_type="mps3", harness_impl=self.impl)


@dataclass
class Leases:
    mine: bool = True
    holder: str = "harness-manager-you@here"

    def view(self, hub: Any) -> dict[str, Any]:
        return {"lease": {"mine": self.mine, "holder": self.holder}}


@dataclass
class Rig:
    shell: FakeShell
    session: FakeSession
    claim: CL.Mps3Claim
    ssh: FakeBoardSsh
    private: Path
    public: Path
    state: Path
    hub_calls: list[tuple[str, str, list[str]]] = field(default_factory=list)


def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def boards_toml(tmp_path: Path) -> Path:
    return state_dir(tmp_path) / "boards.toml"


def _candidate(shell: FakeShell, via: str = "") -> Candidate:
    addr = f"127.0.0.1:{shell.control_port}"
    cand = Candidate(pack="mps3", board_id=f"mps3@{addr}",
                     links=(Link(LinkKind.ETHERNET, addr, "shell control channel"),),
                     identity=BoardIdentity(board_type="mps3", harness_impl="linux"))
    return tunmod.with_via(cand, via) if via else cand


def _local_hub(rig_calls: list[tuple[str, str, list[str]]]):
    """A hub runner factory whose 'hub' is this machine: the command runs here, for real."""

    def factory(host: str, group: str | None, jump: str = "") -> Any:
        def run(argv: list[str], timeout: float | None = None) -> Any:
            rig_calls.append((host, jump, list(argv)))
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return CL.RunResult(p.returncode, p.stdout, p.stderr)
        return run

    return factory


@pytest.fixture
def rig_factory(tmp_path, monkeypatch):
    shells: list[FakeShell] = []

    def make(*, via: str = "", claimed: bool = False, impl: str = "linux",
             host_key: str = BOARD_KEY, mitm: str = "") -> Rig:
        fp = CL.fingerprint(host_key.split()[1])
        shell = FakeShell("127.0.0.1", control_port=0, tftp_port=0, raw_tcp_port=0,
                          uart0_port=0, uart1_port=0, swo_port=0, identify_port=0,
                          profile="linux", ssh_claimed=claimed, ssh_host_key_sha256=fp).start()
        shells.append(shell)
        monkeypatch.setenv("HARNESS_MANAGER_MPS3_IDENTIFY_PORT", str(shell.identify_port))
        monkeypatch.setenv("HARNESS_MANAGER_MPS3_TFTP_PORT", str(shell.tftp_port))
        session = FakeSession(_candidate(shell, via), impl=impl, tftp_port=shell.tftp_port)
        ssh = FakeBoardSsh(shell, host_key, mitm_key=mitm)
        monkeypatch.setattr(CL, "DEFAULT_RUN", ssh)
        monkeypatch.setattr(CL, "KEYS_SYNC_RETRIES_S", ())
        private, public = write_key_pair(tmp_path / "keys", "id_test")
        session.claim = CL.Mps3Claim(session)
        rig = Rig(shell, session, session.claim, ssh, private, public, state_dir(tmp_path))
        monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", _local_hub(rig.hub_calls))
        return rig

    yield make
    for s in shells:
        s.stop()


def claim_it(rig: Rig, **kw: Any) -> dict[str, Any]:
    svc = ClaimService(None, leases=Leases())
    return svc.claim(rig.session, confirm=True, key=str(rig.public), **kw)


# --- 1. the claim state: mine, another key's, unclaimed ------------------------------------------


def test_an_unclaimed_linux_board_says_so(rig_factory):
    rig = rig_factory()
    st = rig.claim.claim_status()
    assert st["state"] == "unclaimed" and st["claimed"] is None and st["live"] is True
    assert st["host_key"]["reported"] == CL.fingerprint(BOARD_KEY.split()[1])
    assert st["host_key"]["pinned"] is None and st["route"] == "lan"


def test_negative_twin_a_board_claimed_by_this_harness_manager_is_mine(rig_factory):
    rig = rig_factory()
    claim_it(rig)
    st = rig.claim.claim_status(refresh=True)
    assert st["state"] == "mine" and st["claimed"]["mine"] is True
    key_fp = CL.fingerprint(rig.public.read_text().split()[1])
    assert st["claimed"]["key_fp"] == key_fp
    assert st["claimed"]["by"] and st["claimed"]["at"].endswith("Z")
    assert st["host_key"]["match"] is True


def test_negative_twin_a_board_claimed_by_another_key_is_not_mine(rig_factory):
    rig = rig_factory(claimed=True)
    st = rig.claim.claim_status()
    assert st["state"] == "other"
    assert st["claimed"] == {"by": "another key", "key_fp": None, "at": None, "mine": False}
    assert any("--adopt" in n for n in st["notes"])


# --- 2. the claim needs a confirmation and the lease; never a second one -------------------------


def test_a_claim_without_confirmation_sends_nothing(rig_factory):
    rig = rig_factory()
    with pytest.raises(RefusedError, match="confirmation"):
        ClaimService(None, leases=Leases()).claim(rig.session, confirm=False,
                                                  key=str(rig.public))
    assert rig.shell.authorized_keys is None and not rig.shell.ssh_claimed
    assert not boards_toml(rig.state.parent).exists()


def test_negative_twin_a_confirmed_claim_sends_the_key_and_pins_the_host_key(rig_factory):
    rig = rig_factory()
    st = claim_it(rig)
    assert st["action"] == "claimed" and st["state"] == "mine"
    assert rig.shell.authorized_keys == rig.public.read_bytes() and rig.shell.ssh_claimed
    toml = boards_toml(rig.state.parent).read_text()
    assert f'host_key = "{BOARD_KEY}"' in toml and f'key = "{rig.private}"' in toml


def test_a_claim_on_a_leased_board_is_for_the_lease_holder_only(rig_factory):
    rig = rig_factory(via=f"ssh:{HUB}")
    rig.session.hub = object()
    with pytest.raises(HeldError, match="lease holder only: bob holds"):
        ClaimService(None, leases=Leases(mine=False, holder="bob")).claim(
            rig.session, confirm=True, key=str(rig.public))
    assert rig.shell.authorized_keys is None and rig.hub_calls == []


def test_negative_twin_the_lease_holder_claims_through_the_hub(rig_factory):
    rig = rig_factory(via=f"ssh:{HUB}")
    rig.session.hub = object()
    st = ClaimService(None, leases=Leases()).claim(rig.session, confirm=True,
                                                   key=str(rig.public))
    assert st["state"] == "mine" and st["route"] == f"hub {HUB}"
    assert rig.shell.authorized_keys == rig.public.read_bytes()
    # identify and the TFTP put ran ON the hub (pyverify's modules, shipped in the command)
    ops = [argv[4] for _h, _j, argv in rig.hub_calls]
    assert ops == ["identify", "claim"] and all(h == HUB for h, _j, _a in rig.hub_calls)
    assert rig.hub_calls[0][2][:2] == ["sh", "-c"]


def test_a_claimed_board_is_never_claimed_again(rig_factory):
    rig = rig_factory(claimed=True)
    with pytest.raises(AlreadyError, match="already claimed by another key"):
        claim_it(rig)
    assert rig.shell.authorized_keys is None and rig.ssh.calls == []


def test_negative_twin_adopt_pins_a_claim_made_with_your_key_elsewhere(rig_factory):
    rig = rig_factory(claimed=True)
    rig.shell.authorized_keys = rig.public.read_bytes()      # pyverify claim, the B1 runbook
    st = claim_it(rig, adopt=True)
    assert st["action"] == "adopted" and st["state"] == "mine"
    assert f'host_key = "{BOARD_KEY}"' in boards_toml(rig.state.parent).read_text()


def test_adopt_with_a_key_the_board_does_not_know_pins_nothing(rig_factory):
    rig = rig_factory(claimed=True)
    rig.shell.authorized_keys = f"{make_key_line('someone-else')} x\n".encode()
    with pytest.raises(RefusedError, match="refused your key"):
        claim_it(rig, adopt=True)
    assert not boards_toml(rig.state.parent).exists()


# --- 3. the host key is pinned; a changed one is refused loudly --------------------------------


def test_after_a_claim_ssh_trusts_exactly_the_pinned_key(rig_factory):
    rig = rig_factory()
    claim_it(rig)
    argv = rig.claim.ssh_argv(["true"])
    o = {argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
         for i, a in enumerate(argv[:-1]) if a == "-o" and "=" in argv[i + 1]}
    assert o["StrictHostKeyChecking"] == "yes" and o["GlobalKnownHostsFile"] == "/dev/null"
    assert o["HostKeyAlias"] == CL.host_key_alias(rig.session.candidate.board_id)
    kh = Path(o["UserKnownHostsFile"])
    assert kh.read_text().strip() == f"{o['HostKeyAlias']} {BOARD_KEY}"
    assert str(kh).startswith(str(rig.state))              # HM's own file, never ~/.ssh
    assert argv[argv.index("-i") + 1] == str(rig.private) and "IdentitiesOnly=yes" in argv
    assert rig.ssh(argv, 5).returncode == 0                 # and the board accepts it


def test_negative_twin_a_changed_host_key_is_refused_loudly(rig_factory):
    rig = rig_factory()
    claim_it(rig)
    rig.shell.ssh_host_key_sha256 = CL.fingerprint(OTHER_BOARD_KEY.split()[1])   # re-imaged
    st = rig.claim.claim_status(refresh=True)
    assert st["host_key"]["match"] is False and st["state"] != "mine"
    assert any("HOST KEY CHANGED" in n for n in st["notes"])
    with pytest.raises(CL.HostKeyChangedError, match="HOST KEY CHANGED"):
        rig.claim.ssh_argv()
    with pytest.raises(CL.HostKeyChangedError):
        rig.claim.open_forward({"control": 6900})
    # ssh's own refusal, when identify has not shown it yet, is the same loud error
    rig.ssh.host_key_line = OTHER_BOARD_KEY
    res = rig.ssh(["ssh", "-o", f"HostKeyAlias={CL.host_key_alias(rig.session.candidate.board_id)}",
                   "-o", f"UserKnownHostsFile={CL.known_hosts_path(rig.session.candidate.board_id)}",
                   "-o", "StrictHostKeyChecking=yes", "-l", "root", "127.0.0.1"], 5)
    mapped = rig.claim.map_ssh_failure(UnreachableError(f"ssh exited ({res.stderr})"))
    assert isinstance(mapped, CL.HostKeyChangedError)


def test_a_re_provisioned_board_is_re_claimed_only_with_replace_host_key(rig_factory):
    rig = rig_factory()
    claim_it(rig)
    rig.shell.ssh_claimed, rig.shell.authorized_keys = False, None        # a new card
    rig.shell.ssh_host_key_sha256 = CL.fingerprint(OTHER_BOARD_KEY.split()[1])
    rig.ssh.host_key_line = OTHER_BOARD_KEY
    with pytest.raises(CL.HostKeyChangedError, match="CHANGED"):
        claim_it(rig)
    assert rig.shell.authorized_keys is None                # nothing sent, nothing re-pinned
    st = claim_it(rig, replace_host_key=True)
    assert st["state"] == "mine" and st["host_key"]["pinned"] == \
        CL.fingerprint(OTHER_BOARD_KEY.split()[1])


# --- 3b. a key that changes BACK to one pinned before (/persist on the user microSD, SMALL-4) ----------

THIRD_BOARD_KEY = make_key_line("never-seen-before")


def _board_offers(rig: Rig, line: str) -> None:
    """The board's identify and its dropbear now carry ``line``'s key."""
    rig.shell.ssh_host_key_sha256 = CL.fingerprint(line.split()[1])
    rig.ssh.host_key_line = line


def _fp(line: str) -> str:
    return CL.fingerprint(line.split()[1])


def _pinned_line(rig: Rig) -> str:
    toml = boards_toml(rig.state.parent).read_text()
    return next(ln.split("=", 1)[1].strip().strip('"') for ln in toml.splitlines()
                if ln.strip().startswith("host_key"))


def test_a_host_key_that_changes_back_to_one_seen_before_says_so_plainly(rig_factory):
    rig = rig_factory()
    claim_it(rig)                                              # K1 pinned (the card's /persist)
    _board_offers(rig, OTHER_BOARD_KEY)                        # K2: tmpfs, never seen: loud
    st = rig.claim.claim_status(refresh=True)
    assert st["host_key"]["seen_before"] is None
    assert any("HOST KEY CHANGED" in n for n in st["notes"])
    claim_it(rig, adopt=True, replace_host_key=True)           # the user trusts K2
    recs = CL.ClaimRecords().get(rig.session.candidate.board_id)["host_keys_seen"]
    assert [e["fp"] for e in recs] == [_fp(BOARD_KEY), _fp(OTHER_BOARD_KEY)]   # K1 kept
    _board_offers(rig, BOARD_KEY)                              # the card mounts again: K1
    st = rig.claim.claim_status(refresh=True)
    hk = st["host_key"]
    assert hk["match"] is False and hk["pinned"] == _fp(OTHER_BOARD_KEY)
    assert hk["seen_before"] == recs[0]["last"] and hk["seen_before"].endswith("Z")
    note = next(n for n in st["notes"] if "changed back" in n)
    assert note.startswith(f"host key changed back to one seen on {hk['seen_before'][:10]}")
    assert "/persist (the user microSD) mounting or not" in note
    assert "re-pin with `harness-manager board claim TARGET --adopt` if you trust it" in note
    assert not any("HOST KEY CHANGED" in n for n in st["notes"])
    from harness_manager.cli.cmd_claim import claim_human

    human = claim_human("b", st)
    assert any(ln.startswith("host key   changed back: ") for ln in human)
    assert not any("CHANGED" in ln for ln in human if ln.startswith("host key"))
    # never auto-accepted: SSH, a forward and a plain claim are all refused, nothing re-pinned
    with pytest.raises(CL.HostKeyChangedError, match="changed back to one seen on"):
        rig.claim.ssh_argv()
    with pytest.raises(CL.HostKeyChangedError, match="changed back"):
        rig.claim.open_forward({"control": 6900})
    with pytest.raises(CL.HostKeyChangedError, match="changed back"):
        claim_it(rig)
    assert _pinned_line(rig) == OTHER_BOARD_KEY
    # the user re-pins it, as the words say: --adopt, no --replace-host-key
    st = claim_it(rig, adopt=True)
    assert st["host_key"]["pinned"] == _fp(BOARD_KEY) and st["host_key"]["match"] is True
    assert _pinned_line(rig) == BOARD_KEY
    rig.claim.ssh_argv(["true"])                               # SSH works again


def test_negative_twin_a_never_seen_key_keeps_the_loud_warning(rig_factory):
    rig = rig_factory()
    claim_it(rig)
    _board_offers(rig, OTHER_BOARD_KEY)
    claim_it(rig, adopt=True, replace_host_key=True)           # K1 and K2 both seen
    _board_offers(rig, THIRD_BOARD_KEY)                        # K3: never pinned here
    for _ in range(2):                                         # looking twice does not soften it
        st = rig.claim.claim_status(refresh=True)
        assert st["host_key"]["seen_before"] is None
        assert any(n.startswith("HOST KEY CHANGED") for n in st["notes"])
        assert not any("changed back" in n for n in st["notes"])
    from harness_manager.cli.cmd_claim import claim_human

    assert any(ln.startswith("host key   CHANGED: ") for ln in claim_human("b", st))
    with pytest.raises(CL.HostKeyChangedError, match="HOST KEY CHANGED"):
        rig.claim.ssh_argv()
    with pytest.raises(CL.HostKeyChangedError, match="HOST KEY CHANGED"):
        claim_it(rig, adopt=True)                              # --adopt is not enough
    assert _pinned_line(rig) == OTHER_BOARD_KEY
    seen = CL.ClaimRecords().get(rig.session.candidate.board_id)["host_keys_seen"]
    assert _fp(THIRD_BOARD_KEY) not in [e["fp"] for e in seen]   # observed is not seen


def test_an_unclaimed_board_back_on_a_seen_key_is_re_claimed_without_replace(rig_factory):
    rig = rig_factory()
    claim_it(rig)
    _board_offers(rig, OTHER_BOARD_KEY)
    claim_it(rig, adopt=True, replace_host_key=True)
    rig.shell.ssh_claimed, rig.shell.authorized_keys = False, None   # the claim was on tmpfs
    _board_offers(rig, BOARD_KEY)
    st = rig.claim.claim_status(refresh=True)
    note = next(n for n in st["notes"] if "changed back" in n)
    assert "re-claim with `harness-manager board claim TARGET` if you trust it" in note
    with pytest.raises(UsageError, match="no claim to adopt"):
        claim_it(rig, adopt=True)
    st = claim_it(rig)                                         # explicit: the TOFU claim + pin
    assert st["action"] == "claimed" and st["host_key"]["pinned"] == _fp(BOARD_KEY)
    # the twin: unclaimed on a never-seen key still needs --replace-host-key
    rig.shell.ssh_claimed, rig.shell.authorized_keys = False, None
    _board_offers(rig, THIRD_BOARD_KEY)
    with pytest.raises(CL.HostKeyChangedError, match="HOST KEY CHANGED"):
        claim_it(rig)
    assert rig.shell.authorized_keys is None


def test_the_seen_list_is_small_keeps_first_and_reads_old_records():
    a, b, c, d, e = ("SHA256:" + ch * 43 for ch in "abcde")
    seen: list[dict[str, str]] = []
    for i, fp in enumerate((a, b, a, c, d, e)):
        seen = CL.with_seen_host_key(seen, fp, f"2026-09-2{i}T00:00:00Z")
    assert [x["fp"] for x in seen] == [a, c, d, e]            # b, the oldest, went
    assert CL.SEEN_HOST_KEYS_MAX == 4
    got_a = next(x for x in seen if x["fp"] == a)
    assert got_a == {"fp": a, "first": "2026-09-20T00:00:00Z", "last": "2026-09-22T00:00:00Z"}
    # a record from before the list: its host_key_fp counts, seen at its claim time
    old = {"host_key_fp": a, "at": "2026-09-24T10:00:00Z"}
    assert CL.seen_host_keys(old) == [{"fp": a, "first": old["at"], "last": old["at"]}]
    # the twins: junk is ignored, and an empty record has seen nothing
    assert CL.seen_host_keys({"host_keys_seen": [{"fp": "nope"}, "x", {"fp": b}]}) == \
        [{"fp": b, "first": "", "last": ""}]
    assert CL.seen_host_keys({}) == []


def test_the_pinned_key_seen_again_moves_its_last_seen_on(rig_factory):
    rig = rig_factory()
    claim_it(rig)
    bid = rig.session.candidate.board_id
    records = CL.ClaimRecords()
    first = records.get(bid)["host_keys_seen"][0]
    old = "2026-01-01T00:00:00Z"
    records.update(bid, host_keys_seen=[{**first, "last": old}],
                   observed={**records.get(bid)["observed"], "at": 0})   # due a write
    rig.claim.claim_status(refresh=True)                       # identify reports the pinned key
    now = records.get(bid)["host_keys_seen"][0]
    assert now["first"] == first["first"] and now["last"] > old
    # the twin: another key reported is never added (observed is not seen)
    _board_offers(rig, OTHER_BOARD_KEY)
    rig.claim.claim_status(refresh=True)
    assert [e["fp"] for e in CL.ClaimRecords().get(bid)["host_keys_seen"]] == [_fp(BOARD_KEY)]


def test_a_host_key_other_than_the_one_identify_publishes_is_never_pinned(rig_factory):
    rig = rig_factory(mitm=make_key_line("someone-in-the-middle"))
    with pytest.raises(CL.HostKeyChangedError, match="NOT pinned"):
        claim_it(rig)
    assert "host_key" not in (boards_toml(rig.state.parent).read_text()
                              if boards_toml(rig.state.parent).exists() else "")


def test_negative_twin_without_a_pin_ssh_is_refused_with_the_way_out(rig_factory):
    rig = rig_factory()
    with pytest.raises(UsageError, match="not pinned"):
        rig.claim.ssh_argv()


def test_the_host_key_setting_accepts_a_key_or_a_fingerprint_and_nothing_else():
    assert CL.check_pin(BOARD_KEY) == "" and CL.check_pin("") == ""
    assert CL.check_pin(CL.fingerprint(BOARD_KEY.split()[1])) == ""
    assert CL.check_pin("ssh-ed25519 not-base64!") != "" and CL.check_pin("hello") != ""


# --- 4. the claim lock's refusal, mapped ----------------------------------------------------------


def test_the_claim_lock_refusal_names_the_claiming_key(rig_factory):
    rig = rig_factory()
    claim_it(rig)
    exc = rig.claim.refusal_error(CL.SLOT_LOCKED_ERR, "a slot commit")
    assert isinstance(exc, CL.ClaimLockedError) and isinstance(exc, RefusedError)
    key_fp = CL.fingerprint(rig.public.read_text().split()[1])
    assert exc.message.startswith(f"this board is claimed by {key_fp}; a slot commit needs "
                                  "the claiming key (use `board claim` only if the board was "
                                  "re-provisioned)")


def test_negative_twin_on_a_board_claimed_by_another_key_it_says_so(rig_factory):
    rig = rig_factory(claimed=True)
    exc = rig.claim.refusal_error(CL.SLOT_LOCKED_ERR)
    assert isinstance(exc, CL.ClaimLockedError) and "claimed by another key" in exc.message
    assert "mps3-unclaim" in exc.hint
    assert rig.claim.refusal_error("EBUSY") is None                    # not a lock: untouched
    assert CL.refusal_error("slot B bad: overlaps slot A") is None


def test_the_fabric_identity_lock_is_its_own_lock_not_a_claim_matter():
    # LINUX-ANSWERS (their assumption 1): two locks; the identity lock is fixable, never
    # "incompatible, give up"
    exc = CL.refusal_error("identity lock: image 0x0badcafe != fabric 0x5a5a0001", "the swap")
    assert isinstance(exc, CL.IdentityLockError) and "0x0badcafe" in exc.message
    assert not isinstance(exc, (CL.ClaimLockedError, IncompatibleError))
    assert exc.kind == "mismatch" and "push" in exc.hint and "commit" in exc.hint
    # deploy's swap refusal reads the same line as a fabric mismatch (no push, 14)
    assert any(m in "identity lock: no valid stage0 status block" for m in FABRIC_MISMATCH_ERRS)


def test_negative_twin_a_fabric_marker_list_without_it_would_not_match():
    old = ("fabric mismatch", "static mismatch", "card mismatch", "efabric", "eskew", "skew")
    assert not any(m in "identity lock: no valid stage0 status block" for m in old)


# --- 5. ProxyJump through the hub, direct on the LAN ---------------------------------------------


def _pin(rig: Rig) -> None:
    CL.write_ssh_settings(rig.session.candidate, {"host_key": BOARD_KEY})


def test_through_the_hub_ssh_jumps_via_the_hub_to_root_on_the_board(rig_factory):
    rig = rig_factory(via=f"ssh:{HUB}")
    _pin(rig)
    argv = rig.claim.ssh_argv(["uptime"])
    assert argv[argv.index("-J") + 1] == HUB
    assert argv[-3:] == ["root", "127.0.0.1", "uptime"] and argv[-4] == "-l"
    assert "ControlPath=none" in argv and "ControlMaster=no" in argv


def test_negative_twin_on_the_lan_ssh_goes_direct(rig_factory):
    rig = rig_factory()
    _pin(rig)
    argv = rig.claim.ssh_argv()
    assert "-J" not in argv and argv[-3:] == ["-l", "root", "127.0.0.1"]


def test_a_named_hubs_jump_host_comes_first(rig_factory, tmp_path):
    rig = rig_factory(via=f"ssh:{HUB}")
    sd = state_dir(tmp_path)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "settings.toml").write_text(
        f'schema = 1\n[hubs.lab]\ntransport = "ssh"\nhost = "{HUB}"\njump = "bastion.example"\n')
    boards_toml(tmp_path).write_text(
        f'[boards.lab]\nmatch = ["127.0.0.1"]\nvia = "ssh:{HUB}"\n'
        f'hub = {{ use = "lab", target = "mps3_01_pl" }}\n'
        f'ssh = {{ host_key = "{BOARD_KEY}", user = "admin" }}\n')
    argv = rig.claim.ssh_argv()
    assert argv[argv.index("-J") + 1] == f"bastion.example,{HUB}"
    assert argv[-2:] == ["admin", "127.0.0.1"]
    assert CL.board_key(rig.session.candidate) == "lab"


def test_the_forward_rides_the_hub_to_the_boards_loopback_with_the_pin(rig_factory, monkeypatch):
    rig = rig_factory(via=f"ssh:{HUB}")
    _pin(rig)
    fake = FakeSsh()
    monkeypatch.setattr(tunmod, "DEFAULT_LAUNCHER", fake)
    monkeypatch.setattr(tunmod, "DEFAULT_SSH_G", fake.ssh_g)
    tunnel = rig.claim.open_forward({"control": 6900, "push": 6910}, restart=False)
    try:
        argv = fake.launches[-1]
        assert argv[argv.index("-J") + 1] == HUB and argv[argv.index("-l") + 1] == "root"
        assert argv[-1] == "127.0.0.1"
        specs = [argv[i + 1] for i, a in enumerate(argv) if a == "-L"]
        assert all(s.startswith("127.0.0.1:") for s in specs)
        assert {s.split(":", 2)[2] for s in specs} == {"127.0.0.1:6900", "127.0.0.1:6910"}
        assert "StrictHostKeyChecking=yes" in argv and "ControlPath=none" in argv
    finally:
        tunnel.close()


def test_negative_twin_a_tunnel_without_options_keeps_its_argv():
    t = tunmod.SshTunnel(HUB, [tunmod.Forward("control", "192.168.10.101", 6900, 40001)],
                         ssh_g=lambda argv: "")
    argv = t.build_argv()
    assert argv[:len(tunmod.SSH_OPTIONS) + 1] == ["ssh", *tunmod.SSH_OPTIONS]
    assert argv[len(tunmod.SSH_OPTIONS) + 1:len(tunmod.SSH_OPTIONS) + 3] == ["-N", "-T"]


def test_info_through_the_hub_never_asks_the_hub_but_claim_status_does(rig_factory):
    rig = rig_factory(via=f"ssh:{HUB}")
    st = rig.claim.claim_status()
    assert st["state"] == "unknown" and rig.hub_calls == [] and st["live"] is False
    st = ClaimService().refresh(rig.session)
    assert st["state"] == "unclaimed" and st["live"] is True and len(rig.hub_calls) == 1
    again = rig.claim.claim_status()                         # the last check, on record
    assert again["state"] == "unclaimed" and "(last check)" in again["source"]
    assert len(rig.hub_calls) == 1


# --- 6. bare metal: nothing to claim, nothing changes ----------------------------------------------


def test_bare_metal_has_no_claim(rig_factory):
    rig = rig_factory(impl="bare-metal")
    assert rig.claim.claim_status() is None
    assert ClaimService().status(rig.session) is None
    with pytest.raises(UnavailableError, match="no SSH to claim"):
        claim_it(rig)
    assert rig.shell.authorized_keys is None


def test_negative_twin_a_session_with_no_claim_adapter_is_refused_too():
    s = FakeSession(Candidate(pack="demo", board_id="demo@1", links=()), claim=None)
    with pytest.raises(UnavailableError, match="Linux harness"):
        ClaimService(None, leases=Leases()).claim(s, confirm=True)


def test_the_hub_script_is_pyverifys_own_modules():
    import base64
    import zlib

    import pyverify.identify
    import pyverify.pusher

    script = CL._hub_script()
    blob = script.split("\n", 1)[0].split("=", 1)[1].strip().strip("'")
    mods = json.loads(zlib.decompress(base64.b64decode(blob)))
    assert mods["identify"] == Path(pyverify.identify.__file__).read_text()
    assert mods["pusher"] == Path(pyverify.pusher.__file__).read_text()
    assert len(" ".join(CL.hub_argv("identify", "192.168.10.101", 6899, 2.0))) < 100_000


# --- LINUX-ANSWERS (C1, C4, C5): the claim's key, the hub's python, ssh after a claim -----------


def test_identify_key_sha256_is_shown_beside_the_host_key(rig_factory):
    from harness_manager.cli.cmd_claim import claim_human

    rig = rig_factory(claimed=True)
    fp = "SHA256:" + "k" * 43
    obs = CL.Mps3Claim._from_raw({"ssh": {"claimed": True, "host_key_sha256": "SHA256:h",
                                          "key_sha256": fp}}, "identify x", 1.0)
    assert obs.key_fp == fp
    st = rig.claim.compose(obs)
    assert st["claim_key"] == fp and st["claimed"]["key_fp"] == fp
    assert any(fp in n for n in st["notes"])
    assert any(line.startswith("claim key  " + fp) for line in claim_human("b", st))
    exc = CL.refusal_error(CL.SLOT_LOCKED_ERR, status=st)
    assert fp in exc.message


def test_negative_twin_without_key_sha256_nothing_is_invented(rig_factory):
    from harness_manager.cli.cmd_claim import claim_human

    rig = rig_factory(claimed=True)
    obs = CL.Mps3Claim._from_raw({"ssh": {"claimed": True, "host_key_sha256": "SHA256:h"}},
                                 "identify x", 1.0)
    st = rig.claim.compose(obs)
    assert obs.key_fp == "" and st["claim_key"] is None and st["claimed"]["key_fp"] is None
    assert not any(line.startswith("claim key") for line in claim_human("b", st))
    assert any("does not publish which" in n for n in st["notes"])


#: REVIEW-W5 16: these stand the hub's interpreters in as /bin/sh scripts; Windows has none.
NEEDS_SH = pytest.mark.skipif(os.name != "posix" or not Path("/bin/sh").exists(),
                              reason="the hub's shell and interpreters are /bin/sh scripts here")


def _interpreters(tmp_path: Path, **kinds: str) -> Path:
    """Fake hub interpreters in a PATH of their own: ``good`` runs this test's python, ``old``
    behaves like the hub's 3.6 (fails the version check, prints 3.6). Each logs its name."""
    import sys

    bindir = tmp_path / "hub-bin"
    bindir.mkdir()
    mark = tmp_path / "ran"
    for name, kind in kinds.items():
        body = (f'exec "{sys.executable}" "$@"' if kind == "good" else
                'case "$2" in *">="*) exit 1;; *print*) echo 3.6; exit 0;; esac\n'
                'echo "SyntaxError: future feature annotations is not defined" >&2; exit 1')
        p = bindir / name.replace("_", ".")
        p.write_text(f'#!/bin/sh\necho {p.name} >> "{mark}"\n{body}\n')
        p.chmod(0o755)
    return bindir


def _run_pick(bindir: Path) -> tuple[int, str, list[str]]:
    argv = CL.hub_argv("nosuch", "127.0.0.1", 1, 0.1)
    argv[0] = "/bin/sh"
    p = subprocess.run(argv, env={"PATH": str(bindir)}, capture_output=True, text=True,
                       timeout=60)
    ran = (bindir.parent / "ran").read_text().split() if (bindir.parent / "ran").exists() else []
    return p.returncode, p.stdout.strip(), ran


@NEEDS_SH
def test_the_hub_helper_refuses_clearly_when_the_hub_has_only_python_3_6(tmp_path):
    rc, out, ran = _run_pick(_interpreters(tmp_path, python3="old"))
    reply = json.loads(out.splitlines()[-1])
    assert rc == 127 and reply["ok"] is False and reply["kind"] == CL.NO_PYTHON
    assert "no python 3.8+ on the hub (python3 is 3.6)" in reply["err"]
    assert "future feature" not in out                    # the helper never ran on 3.6


@NEEDS_SH
def test_negative_twin_python3_11_is_tried_first_and_runs_the_helper(tmp_path):
    rc, out, ran = _run_pick(_interpreters(tmp_path, python3_13="good", python3_11="good",
                                           python3="old"))
    assert json.loads(out.splitlines()[-1]) == {"ok": False, "err": "unknown op nosuch"}
    assert ran and set(ran) == {"python3.11"}             # 3.13 and python3 never asked


@NEEDS_SH
def test_negative_twin_a_bare_python3_that_is_new_enough_is_used(tmp_path):
    rc, out, ran = _run_pick(_interpreters(tmp_path, python3="good"))
    assert json.loads(out.splitlines()[-1])["err"] == "unknown op nosuch"
    assert CL.HUB_PYTHONS[0] == "python3.11" and CL.HUB_PY_MIN == (3, 8)


@NEEDS_SH
def test_the_hub_call_turns_no_python_into_a_hint(tmp_path, monkeypatch):
    bindir = _interpreters(tmp_path, python3="old")

    def factory(host, group, jump=""):
        def run(argv, timeout=None):
            argv = ["/bin/sh", *argv[1:]]
            p = subprocess.run(argv, env={"PATH": str(bindir)}, capture_output=True, text=True,
                               timeout=timeout)
            return CL.RunResult(p.returncode, p.stdout, p.stderr)
        return run

    monkeypatch.setattr(hubmod, "DEFAULT_RUNNER_FACTORY", factory)
    with pytest.raises(UnreachableError, match="python 3.8") as exc:
        CL._hub_call(HUB, "", CL.hub_argv("identify", "192.168.10.101", 6899, 1.0), 30)
    assert "python3.11" in exc.value.hint


def test_the_shipped_helper_needs_exactly_python_3_8():
    # pusher.py imports typing.Literal (3.8) under `from __future__ import annotations`
    import pyverify.pusher

    src = Path(pyverify.pusher.__file__).read_text()
    assert "from typing import Literal" in src and "from __future__ import annotations" in src
    assert CL.HUB_PY_MIN == (3, 8)


def test_ssh_refused_after_an_accepted_claim_is_not_lag(rig_factory, monkeypatch):
    rig = rig_factory()

    def keys_never_synced(argv, timeout):          # mps3-keys-sync missing: dropbear never
        keep = rig.shell.authorized_keys           # learns the claimed key
        rig.shell.authorized_keys = b""
        try:
            return rig.ssh(argv, timeout)
        finally:
            rig.shell.authorized_keys = keep

    monkeypatch.setattr(CL, "DEFAULT_RUN", keys_never_synced)
    with pytest.raises(RefusedError, match="the claim was accepted") as exc:
        claim_it(rig)
    assert "not key-sync lag" in exc.value.hint and "tofu:" in exc.value.hint
    assert "next boot" in exc.value.hint and rig.shell.ssh_claimed


def test_negative_twin_an_adopt_refused_by_the_board_keeps_the_key_hint(rig_factory):
    rig = rig_factory(claimed=True)
    rig.shell.authorized_keys = f"{make_key_line('someone-else')} x\n".encode()
    with pytest.raises(RefusedError, match="claimed by another key") as exc:
        claim_it(rig, adopt=True)
    assert "key-sync" not in exc.value.hint


def test_the_keys_sync_retry_is_still_6_5_s():
    assert sum(CL.KEYS_SYNC_RETRIES_S) == 6.5
