"""Lane IDENTITY (HM v0.1.1, david 2 Oct; the Linux lead's answers of 2 Oct): name a board, give
it a random MAC and an IP of its own, on the real MPS3 pack against HM's model of the identity
verbs (``tests/fakes/idn_board``).

Every behaviour has its negative twin: the hub guard (refused until the hub is named; a hub
record's own MAC needs nothing), ``--ip auto`` behind a hub, the subnet guard, the pool
(answering and registry addresses skipped; exhausted), and a board that MOVES: found at its new
address by its pinned SSH host key and its records follow it; a different host key there is
never adopted; nothing there is the Linux lead's timeout words. No datagram leaves the test:
identify at the new address is this file's stand-in.
"""

from __future__ import annotations

import json

import pytest

from harness_manager.core.errors import ActionFailedError, RefusedError, UnreachableError
from harness_manager.services import identity_assign as IA
from harness_manager.services.board_identity import IdentityService, SeenIdentities
from harness_manager_mps3 import identify as IDF
from harness_manager_mps3 import net_identity as NI
from harness_manager_mps3 import tunnel as T
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV, IdentifyReply
from tests.fakes.claimed_lock import board_key_fp, pin_claim
from tests.fakes.idn_board import BOARD1_MAC, FakeHub, FakeLeases, SpyController, identity_board
from tests.fakes.lxslots_board import TRUSTED, BoardSsh, board_session


@pytest.fixture(autouse=True)
def _pool_from_its_first_address(monkeypatch):
    """These tests read the pool from its first address: ``--ip auto``'s start from the
    random MAC (``pool_start``) is tested in tests/unit/test_identity_assign.py."""
    from harness_manager.services import identity_assign as _IA

    monkeypatch.setattr(_IA, "pool_start", lambda addrs, mac=None: 0)

AS_DEFAULT = {"label": "MPS3", "ip": "192.168.10.101/24", "mac": BOARD1_MAC,
              "source": {"label": "default", "ip": "default", "mac": "default"}}
#: A board this session reaches AT its identity IP (the fake answers on 127.0.0.1, so its
#: identity IP is 127.0.0.1 here): changing the IP moves it away from this session.
AT_ITS_IP = {**AS_DEFAULT, "ip": "127.0.0.1/24", "source": {"label": "default", "ip": "stage0",
                                                            "mac": "default"}}
HUB_NAME = "hub.board-id.test"                         # FakeHub's host


@pytest.fixture
def lab(monkeypatch, tmp_path):
    made = []

    def make(*, running=AS_DEFAULT, hub=False, **kw):
        fake = identity_board(running=running, ssh_claimed=True, slots={"trusted_peer": TRUSTED},
                              ssh_host_key_sha256=board_key_fp(), **kw)
        monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
        ssh = BoardSsh(fake)
        monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
        monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
        session = board_session(fake)
        pin_claim(session)
        if hub:
            session.hub = FakeHub("mps3_02_pl")
        session.controller = SpyController()
        session.os_slots.reboot_poll_s = 0.02
        seen = SeenIdentities(tmp_path / "identity")
        svc = IdentityService(leases=FakeLeases(True), seen=seen)
        made.append((fake, session, ssh))
        return fake, session, svc

    yield make
    for fake, session, ssh in made:
        session.close()
        ssh.close()
        fake.stop()


def nothing_sent(fake) -> bool:
    return fake.identity_sets == [] and fake.reboots == []


def sent(fake) -> dict:
    (_peer, body), = fake.identity_sets
    return body


# --- the choices: random, auto, the registry ------------------------------------------------------


def test_random_and_auto_are_chosen_set_and_recorded_as_assigned(lab):
    fake, session, svc = lab()
    out = svc.fix(session, confirm="LAB-07", want={"label": "lab-07", "mac": "random",
                                                   "ip": "auto"}, wait_s=20)
    body = sent(fake)
    assert body["label"] == "LAB-07" and body["ip"] == "192.168.10.110/24"
    assert body["mac"].startswith("02") and not body["mac"].startswith("020000")
    assert out["verified"] is True and out["moved"] is None
    assert out["address"] == {"ip": "192.168.10.110", "pc_example": "192.168.10.1/24",
                              "same_net": "this PC must be on the same /24 (e.g. 192.168.10.1/24)"}
    hist = svc.seen.get(session.candidate.board_id)["history"]
    assert hist["ip"]["192.168.10.110"]["how"] == "assigned"
    mac = ":".join(body["mac"][i:i + 2] for i in range(0, 12, 2))
    assert hist["mac"][mac]["how"] == "assigned"
    assert any("in recovery mode the board still answers on 192.168.10.101" in n for n in out["notes"])


def test_twin_auto_skips_what_answers_and_what_the_registry_holds(lab, monkeypatch):
    fake, session, svc = lab()
    svc.seen.record("mps3@other:6900", ip="192.168.10.110", how="assigned")
    monkeypatch.setattr(NI, "DEFAULT_ANSWERING", lambda ip: ip == "192.168.10.111")
    svc.fix(session, confirm="LAB-08", want={"label": "LAB-08", "ip": "auto"}, wait_s=20)
    assert sent(fake)["ip"] == "192.168.10.112/24"


def test_twin_an_exhausted_pool_is_refused_and_nothing_is_sent(lab, monkeypatch):
    fake, session, svc = lab()
    monkeypatch.setenv(NI.IP_POOL_ENV, "192.168.10.110-111")
    monkeypatch.setattr(NI, "DEFAULT_ANSWERING", lambda ip: True)
    with pytest.raises(RefusedError, match="no free address in the pool 192.168.10.110-111"):
        svc.fix(session, confirm="LAB-09", want={"label": "LAB-09", "ip": "auto"})
    assert nothing_sent(fake)


def test_twin_a_persons_own_mac_in_the_image_range_is_refused(lab):
    from harness_manager.core.errors import UsageError

    fake, session, svc = lab()
    with pytest.raises(UsageError, match="02:00:00:\\* is reserved"):
        svc.fix(session, confirm="MPS3", want={"mac": "02:00:00:12:34:56"})
    with pytest.raises(UsageError, match="prefix is /16"):
        svc.fix(session, confirm="MPS3", want={"ip": "10.0.0.5/16"})
    assert nothing_sent(fake)


def test_a_new_mac_on_the_same_ip_carries_the_arp_note(lab):
    fake, session, svc = lab()
    out = svc.fix(session, confirm="MPS3", want={"mac": "random"}, wait_s=20)
    assert any("sudo arp -d 192.168.10.101" in n and "never runs sudo" in n
               for n in out["notes"])


def test_twin_a_new_mac_with_a_new_ip_has_no_arp_note(lab):
    fake, session, svc = lab()
    out = svc.fix(session, confirm="MPS3", want={"mac": "random", "ip": "auto"}, wait_s=20)
    assert not any("arp -d" in n for n in out["notes"])


# --- the hub guard ---------------------------------------------------------------------------------


def test_the_hub_guard_refuses_a_mac_change_until_the_hub_is_named(lab):
    fake, session, svc = lab(hub=True)
    with pytest.raises(RefusedError) as exc:
        svc.fix(session, confirm="MPS3", want={"mac": "random"})
    msg = exc.value.message
    assert msg.startswith(f"this board is behind the hub {HUB_NAME}, whose DHCP (dnsmasq) knows "
                          "it by its MAC (mps3_02_pl): changing its MAC before the hub's record "
                          "is fixed loses the board's address")
    assert "Mps3_01_pl's hub record is known to be wrong today (its board_mac " \
           "00:e0:4c:46:dc:f8 is the hub's own USB adapter, not the board)" in msg
    assert exc.value.hint == (f"fix mps3_02_pl's record on {HUB_NAME} first (fpgahub), then "
                              f"confirm by naming the hub: --hub-fixed {HUB_NAME}")
    with pytest.raises(RefusedError, match="behind the hub"):
        svc.fix(session, confirm="MPS3", want={"mac": "random"}, hub_fixed="another-hub")
    assert nothing_sent(fake)


def test_twin_naming_the_hub_lets_the_mac_change_and_its_own_record_needs_nothing(lab):
    fake, session, svc = lab(hub=True)
    out = svc.fix(session, confirm="MPS3", want={"mac": "random"}, hub_fixed=HUB_NAME,
                  wait_s=20)
    assert out["verified"] is True and len(fake.identity_sets) == 1
    fake2, session2, svc2 = lab(hub=True)
    out = svc2.fix(session2, confirm="MPS3-02", from_hub=True, wait_s=20)   # the record's MAC
    assert out["verified"] is True and sent(fake2)["mac"] == "0200000002fe"


def test_twin_ip_auto_behind_a_hub_is_refused_the_hub_gives_its_address(lab):
    fake, session, svc = lab(hub=True)
    with pytest.raises(RefusedError, match="--ip auto picks from this PC's own network; this "
                                           f"board is behind the hub {HUB_NAME}"):
        svc.fix(session, confirm="MPS3", want={"ip": "auto"}, hub_fixed=HUB_NAME)
    assert nothing_sent(fake)


# --- the subnet guard ------------------------------------------------------------------------------


def test_the_subnet_guard_refuses_an_ip_outside_this_pcs_24(lab, monkeypatch):
    fake, session, svc = lab()
    monkeypatch.setattr(IA, "local_address_toward", lambda host: "192.168.10.1")
    with pytest.raises(RefusedError) as exc:
        svc.fix(session, confirm="MPS3", want={"ip": "192.168.11.7"})
    assert exc.value.message.startswith("192.168.11.7 is not on this PC's network (this PC is "
                                        "192.168.10.1 in 192.168.10.0/24)")
    assert "--other-subnet" in exc.value.hint
    assert nothing_sent(fake)


def test_twin_other_subnet_goes_ahead_and_says_so(lab, monkeypatch):
    fake, session, svc = lab()
    monkeypatch.setattr(IA, "local_address_toward", lambda host: "192.168.10.1")
    out = svc.fix(session, confirm="MPS3", want={"ip": "192.168.11.7"}, other_subnet=True,
                  wait_s=20)
    assert sent(fake)["ip"] == "192.168.11.7/24"
    assert any(n.startswith("192.168.11.7 is not on this PC's network") for n in out["notes"])


# --- a board that moves: found by its host key ----------------------------------------------------


def at_new_address(fake, monkeypatch, *, host_key: str | None = None, never: bool = False,
                   new_ip: str = "192.168.10.110"):
    """identify, as the network would answer it at the NEW address: the fake board once it has
    restarted with that address (its running IP), with its own host key; or (``host_key``)
    ANOTHER board holding that address; or (``never``) nothing. Every other address is asked
    for real (the fake on 127.0.0.1)."""
    real = IDF.identify
    asked: list[str] = []

    def ask(host, port=None, *, timeout=1.0, retries=1, nonce=None):
        if host != new_ip:
            return real(host, port, timeout=timeout, retries=retries, nonce=nonce)
        asked.append(host)
        moved = str(fake.running["ip"]).split("/")[0] == new_ip
        if never or (host_key is None and not moved):
            raise UnreachableError(f"nothing answers identify at {host}")
        raw = fake.identify_reply("0" * 16)
        if host_key is not None:
            raw = {**raw, "ssh": {**raw.get("ssh", {}), "host_key_sha256": host_key}}
        return IdentifyReply(raw=raw, source=(host, 6899))

    monkeypatch.setattr(IDF, "identify", ask)
    return asked


def test_a_board_that_moves_is_found_by_its_host_key_and_its_records_follow(lab, monkeypatch):
    from harness_manager_mps3.claim import ClaimRecords, known_hosts_path, ssh_config

    fake, session, svc = lab(running=AT_ITS_IP)
    old = session.candidate.board_id
    session.net_identity.move_poll_s = 0.01
    asked = at_new_address(fake, monkeypatch)
    said: list[str] = []
    out = svc.fix(session, confirm="LAB-07", want={"label": "LAB-07", "mac": "random",
                                                   "ip": "auto"}, wait_s=20,
                  progress=said.append)
    new = f"mps3@192.168.10.110:{fake.control_port}"
    assert out["board_id"] == new and out["verified"] is True
    moved = out["moved"]
    assert moved["from"] == old and moved["to"] == new and moved["host"] == "192.168.10.110"
    assert moved["old_host"] == "127.0.0.1"
    assert asked and set(asked) == {"192.168.10.110"}           # only the new address
    assert len(fake.reboots) == 1                         # the warm reboot verb, once
    assert out["identity"]["reported"]["label"] == "LAB-07"
    assert any(f"host key {board_key_fp()}" in s for s in said)
    # the records: the claim, the pinned key (boards.toml) and known_hosts, the registry
    assert ClaimRecords().get(new)["host_key_fp"] == board_key_fp()
    assert ClaimRecords().get(old) == {}
    cand = type("C", (), {"board_id": new, "links": ()})()
    assert ssh_config(cand)["host_key"].startswith("ssh-")
    assert known_hosts_path(new).read_text().startswith("harness-manager-")
    assert not known_hosts_path(old).exists()
    assert svc.seen.get(new)["moved_from"] == old and svc.seen.get(old) == {}
    assert svc.seen.get(new)["history"]["ip"]["192.168.10.110"]["how"] == "assigned"


def test_twin_another_host_key_at_the_new_address_is_never_adopted(lab, monkeypatch):
    from harness_manager_mps3.claim import ClaimRecords

    fake, session, svc = lab(running=AT_ITS_IP)
    old = session.candidate.board_id
    session.net_identity.move_poll_s = 0.01
    at_new_address(fake, monkeypatch, host_key="SHA256:" + "A" * 43)
    with pytest.raises(RefusedError) as exc:
        svc.fix(session, confirm="LAB-07", want={"label": "LAB-07", "ip": "auto"}, wait_s=20)
    assert exc.value.message.startswith("a different board answers at 192.168.10.110: its SSH "
                                        "host key is SHA256:AAAA")
    assert "It was not adopted" in exc.value.message
    assert ClaimRecords().get(old)["host_key_fp"] == board_key_fp()          # nothing moved
    assert ClaimRecords().get(f"mps3@192.168.10.110:{fake.control_port}") == {}
    assert svc.seen.get(old)                                                 # still the old id


def test_twin_nothing_at_the_new_address_is_the_linux_leads_timeout(lab, monkeypatch):
    fake, session, svc = lab(running=AT_ITS_IP)
    session.net_identity.move_poll_s = 0.01
    at_new_address(fake, monkeypatch, never=True)
    with pytest.raises(ActionFailedError) as exc:
        svc.fix(session, confirm="LAB-07", want={"label": "LAB-07", "ip": "auto"}, wait_s=0.2)
    assert exc.value.message == (
        "the identity was set and the board restarted, but board not seen on 192.168.10.110 "
        "after 1 min: it may be on DHCP or the address was taken (DAD); check the panel, which "
        "shows the IP on row 5")
    assert len(fake.identity_sets) == 1 and len(fake.reboots) == 1


def test_twin_a_board_with_no_pinned_key_does_not_move(lab, monkeypatch):
    fake, session, svc = lab(running=AT_ITS_IP)
    monkeypatch.setattr(type(session.net_identity), "host_key", lambda self: "")
    with pytest.raises(RefusedError, match="SSH host key is not pinned here"):
        svc.fix(session, confirm="LAB-07", want={"label": "LAB-07", "ip": "auto"})
    assert nothing_sent(fake)


def test_twin_a_board_reached_another_way_does_not_move(lab):
    """Reached at 127.0.0.1 while its identity IP is .10.101 (a tunnel, a forward): the
    session's address does not change, so the plain warm reboot is witnessed there."""
    fake, session, svc = lab()
    out = svc.fix(session, confirm="MPS3", want={"ip": "auto"}, wait_s=20)
    assert out["moved"] is None and out["board_id"] == session.candidate.board_id


# --- the proposal (what the dialog shows) ----------------------------------------------------------


def test_the_proposal_for_a_board_on_the_image_default(lab):
    fake, session, svc = lab()
    svc.status(session, refresh=True)
    p = svc.propose(session, label="lab-07")
    assert p["label"] == "LAB-07" and p["label_problem"] == "" and p["phrase"] == "LAB-07"
    assert p["defaults"] == {"mac": "random", "ip": "auto"}
    assert p["mac_how"] == "random" and p["mac"].startswith("02:") and p["ip"] == \
        "192.168.10.110/24"
    assert {c["field"] for c in p["changes"]} == {"label", "mac", "ip"}
    assert p["address"]["same_net"] == "this PC must be on the same /24 (e.g. 192.168.10.1/24)"
    assert p["hub"] is None and p["pool"] == {"range": "192.168.10.110-199",
                                              "setting": "mps3.identity.ip_pool"}
    assert p["rules"]["name_max"] == 16 and "not 02:00:00:*" in p["rules"]["mac"]
    assert any("in recovery mode" in n for n in p["notes"])
    assert svc.propose(session, mac="random")["mac"] != p["mac"]          # regenerate
    assert svc.seen.taken("ip").get("192.168.10.110") is None             # nothing reserved
    assert nothing_sent(fake)


def test_twin_the_proposal_keeps_what_a_board_already_has_and_checks_the_name(lab):
    fake, session, svc = lab(running={"label": "LAB-07", "ip": "192.168.10.117/24",
                                      "mac": "025e00000117",
                                      "source": {"label": "override", "ip": "override",
                                                 "mac": "override"}})
    svc.status(session, refresh=True)
    p = svc.propose(session, label="lab 07!")
    assert p["defaults"] == {"mac": "keep", "ip": "keep"} and p["changes"] == []
    assert p["label_problem"] == "it has a space, '!' (only A-Z, 0-9 and - are allowed)"
    assert p["phrase"] == "LAB-07"


def test_twin_the_proposal_behind_a_hub_keeps_and_names_the_hub(lab):
    fake, session, svc = lab(hub=True)
    svc.status(session, refresh=True)
    p = svc.propose(session, mac="random", ip="auto")
    assert p["hub"]["name"] == HUB_NAME and p["hub"]["target"] == "mps3_02_pl"
    assert p["hub"]["guard"] is True and "mps3_01_pl" in p["hub"]["known_bad"]
    assert p["errors"]["ip"].startswith("--ip auto picks from this PC's own network")
    assert svc.propose(session)["defaults"] == {"mac": "keep", "ip": "keep"}
    json.dumps(p)                                                         # the API's answer


# --- a board outside the pool's network (name-subnet) ---------------------------------------------

OFF_POOL = {"label": "MPS3", "ip": "192.168.11.101/24", "mac": "025e00000102",
            "source": {"label": "default", "ip": "stage0", "mac": "override"}}
WARNING = ("This board is on 192.168.11.0/24, outside the address pool (192.168.10.110-199): "
           "its current address 192.168.11.101 is kept. Choose another address only if the "
           "board's network will reach it.")


def test_the_proposal_for_a_board_outside_the_pool_keeps_its_ip_and_warns(lab):
    fake, session, svc = lab(running=OFF_POOL)
    svc.status(session, refresh=True)
    p = svc.propose(session, label="lab-02", ip="auto")
    assert p["ip"] == "192.168.11.101/24" and p["ip_how"] == "keep"
    assert p["subnet"]["warning"] == WARNING and p["subnet"]["kept"] is True
    assert p["subnet"]["confirm_needed"] is False
    assert {c["field"] for c in p["changes"]} == {"label"}              # no IP change offered
    q = svc.propose(session, ip="192.168.10.120")                       # a pool address: confirm
    assert q["subnet"]["confirm_needed"] is True and q["subnet"]["warning"] == WARNING
    assert svc.propose(session, ip="192.168.11.150")["subnet"]["confirm_needed"] is False
    assert nothing_sent(fake)


def test_twin_the_proposal_for_a_board_on_the_pools_network_offers_a_pool_ip(lab):
    fake, session, svc = lab()
    svc.status(session, refresh=True)
    p = svc.propose(session, label="lab-07", ip="auto")
    assert p["ip"] == "192.168.10.110/24" and p["ip_how"] == "auto"
    assert p["subnet"] == {"outside_pool": False, "board_network": "192.168.10.0/24",
                           "pool_network": "192.168.10.0/24", "kept": False, "warning": "",
                           "confirm_needed": False, "confirm_text": ""}


def test_an_ip_in_another_network_for_a_board_outside_the_pool_is_refused_without_confirm(lab):
    fake, session, svc = lab(running=OFF_POOL)
    with pytest.raises(RefusedError) as exc:
        svc.fix(session, confirm="MPS3", want={"ip": "192.168.10.120"})
    assert exc.value.message == (
        "this board is on 192.168.11.0/24, outside the address pool (192.168.10.110-199), and "
        "192.168.10.120 is in 192.168.10.0/24: the board may be unreachable after its next "
        "restart; nothing was changed")
    assert "--allow-other-subnet" in exc.value.hint and "confirm_subnet" in exc.value.hint
    with pytest.raises(RefusedError):                                   # auto = a pool address
        svc.fix(session, confirm="MPS3", want={"ip": "auto"})
    assert nothing_sent(fake)


def test_twin_confirm_subnet_lets_it_through_and_a_same_network_ip_needs_none(lab):
    fake, session, svc = lab(running=OFF_POOL)
    svc.fix(session, confirm="MPS3", want={"ip": "192.168.11.150"}, wait_s=20)
    assert sent(fake)["ip"] == "192.168.11.150/24"                      # its own /24: no confirm
    fake, session, svc = lab(running=OFF_POOL)
    svc.fix(session, confirm="MPS3", want={"ip": "192.168.10.120"}, confirm_subnet=True,
            wait_s=20)
    assert sent(fake)["ip"] == "192.168.10.120/24"


def test_the_proposal_says_why_a_new_mac_is_required_for_the_images_range(lab):
    fake, session, svc = lab()
    svc.status(session, refresh=True)
    p = svc.propose(session, mac="keep")
    assert p["mac_keep"] == {"allowed": False, "why": (
        "the current MAC 02:00:00:4d:50:53 is the image's default range, which every board "
        "starts with: a new random MAC is required")}
    assert p["mac_how"] == "random" and not p["mac"].startswith("02:00:00")


def test_twin_a_mac_outside_the_images_range_may_be_kept(lab):
    fake, session, svc = lab(running=OFF_POOL)
    svc.status(session, refresh=True)
    p = svc.propose(session, mac="keep")
    assert p["mac_keep"] == {"allowed": True, "why": ""} and p["mac_how"] == "keep"
    assert p["mac"] == "02:5e:00:00:01:02"


# --- the Linux lead's refusal order: no_persist (a card written on a PC) -------------------------


def test_a_card_written_on_a_pc_is_refused_with_the_format_hint_never_formatted(lab):
    fake, session, svc = lab(persist=False)
    with pytest.raises(RefusedError) as exc:
        svc.fix(session, confirm="LAB-07", want={"label": "LAB-07", "mac": "random"})
    assert "a card written on a PC whose /persist partition is still blank" in exc.value.message
    assert "`harness-manager board ssh TARGET -c 'mps3-persist format --erase && mps3-reboot'`" \
        in exc.value.hint
    assert "--replace-host-key" in exc.value.hint and "never formats it for you" in exc.value.hint
    assert "comes back UNCLAIMED with a NEW SSH host key, so claim it again" in exc.value.hint
    assert nothing_sent(fake)


def test_twin_the_boards_own_no_persist_reply_carries_the_same_hint():
    err = NI.set_error({"ok": False, "err": "identity: no persistent /persist (use the card)",
                        "code": "no_persist"})
    assert isinstance(err, RefusedError) and "mps3-persist format --erase" in err.hint
    assert "[harness: identity: no persistent /persist (use the card)]" in err.message


# --- the broadcast fallback (the Linux lead, 2 Oct): DAD refused the new address --------------


def by_broadcast(fake, monkeypatch, *, at: str, host_key: str | None = None) -> list:
    """identify.discover, as the network answers it: once the board restarted, it answers
    from ``at`` (its DHCP lease: the new static address was refused), with its own host key
    unless told otherwise."""
    asked: list[int] = []

    def discover(targets=None, *, timeout=2.0, nonce=None):
        asked.append(1)
        if not str(fake.running["ip"]).startswith("192.168.10.110"):
            return []                                   # not restarted with it yet
        raw = {**fake.identify_reply("0" * 16), "ip": at, "dhcp": True}
        if host_key is not None:
            raw["ssh"] = {**raw.get("ssh", {}), "host_key_sha256": host_key}
        return [IdentifyReply(raw=raw, source=(at, 6899))]

    monkeypatch.setattr(IDF, "discover", discover)
    return asked


def test_a_board_that_did_not_take_its_new_ip_is_found_by_broadcast_with_its_key(lab,
                                                                                monkeypatch):
    from harness_manager_mps3.claim import ClaimRecords

    fake, session, svc = lab(running=AT_ITS_IP)
    ad = session.net_identity
    ad.move_poll_s, ad.broadcast_after_s, ad.broadcast_every_s = 0.01, 0.0, 0.0
    at_new_address(fake, monkeypatch, never=True)                 # nothing at .110 (DAD)
    asked = by_broadcast(fake, monkeypatch, at="192.168.10.57")
    out = svc.fix(session, confirm="LAB-07", want={"label": "LAB-07", "ip": "auto"}, wait_s=20)
    new = f"mps3@192.168.10.57:{fake.control_port}"
    assert asked and out["verified"] is True and out["board_id"] == new
    assert out["moved"]["host"] == "192.168.10.57" and out["moved"]["elsewhere"] is True
    assert out["address"]["ip"] == "192.168.10.57"
    assert any(n.startswith("the board did not take 192.168.10.110 (the address may have been "
                            "taken: DAD, or it is on DHCP): it was found at 192.168.10.57")
               for n in out["notes"])
    assert ClaimRecords().get(new)["host_key_fp"] == board_key_fp()


def test_twin_a_broadcast_answer_with_another_key_is_never_adopted(lab, monkeypatch):
    from harness_manager_mps3.claim import ClaimRecords

    fake, session, svc = lab(running=AT_ITS_IP)
    old = session.candidate.board_id
    ad = session.net_identity
    ad.move_poll_s, ad.broadcast_after_s, ad.broadcast_every_s = 0.01, 0.0, 0.0
    at_new_address(fake, monkeypatch, never=True)
    asked = by_broadcast(fake, monkeypatch, at="192.168.10.57", host_key="SHA256:" + "C" * 43)
    with pytest.raises(ActionFailedError, match="board not seen on 192.168.10.110"):
        svc.fix(session, confirm="LAB-07", want={"label": "LAB-07", "ip": "auto"}, wait_s=0.3)
    assert asked
    assert ClaimRecords().get(old)["host_key_fp"] == board_key_fp()          # nothing moved
    assert ClaimRecords().get(f"mps3@192.168.10.57:{fake.control_port}") == {}


def test_twin_a_broadcast_answer_before_the_restart_is_not_the_moved_board(lab):
    """The board itself, still at its old address (or not restarted): never adopted."""
    from harness_manager_mps3.identify import IdentifyReply as R

    key = board_key_fp()
    stale = R(raw={"ok": True, "op": "identify", "ssh": {"host_key_sha256": key},
                   "up_ms": 900_000}, source=("192.168.10.57", 6899))
    old = R(raw={"ok": True, "op": "identify", "ssh": {"host_key_sha256": key}, "up_ms": 10},
            source=("127.0.0.1", 6899))
    fresh = R(raw={"ok": True, "op": "identify", "ssh": {"host_key_sha256": key}, "up_ms": 10},
              source=("192.168.10.57", 6899))
    import harness_manager_mps3.identify as idf

    real = idf.discover
    try:
        idf.discover = lambda *a, **k: [stale, old]
        assert NI.Mps3NetIdentity._by_broadcast(key, old_host="127.0.0.1", since_s=30) is None
        idf.discover = lambda *a, **k: [stale, old, fresh]
        assert NI.Mps3NetIdentity._by_broadcast(key, old_host="127.0.0.1", since_s=30) is fresh
    finally:
        idf.discover = real
