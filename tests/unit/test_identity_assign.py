"""Lane IDENTITY (HM v0.1.1, david 2 Oct): the core's naming rules, the random MAC, the pool's
IP, the registry (seen.json) and the boards.toml move. Board-agnostic: the MPS3 values come
from a policy built here. Each behaviour has its negative twin."""

from __future__ import annotations

import pytest

from harness_manager.core.errors import RefusedError, UsageError
from harness_manager.services import identity_assign as IA
from harness_manager.services.board_identity import SeenIdentities

MPS3 = IA.IdentityPolicy(pack="mps3", reserved_mac_prefixes=("02:00:00",),
                         reserved_mac_why="the image's range", ip_pool="192.168.10.110-199",
                         ip_pool_setting="mps3.identity.ip_pool",
                         reserved_ips=("192.168.10.101",))


def rolls(*hexes: str):
    it = iter(bytes.fromhex(h) for h in hexes)
    return lambda n: next(it)


# --- names ---------------------------------------------------------------------------------------


def test_a_name_is_upper_cased_and_up_to_16():
    assert IA.check_name(" lab-07 ") == "LAB-07"
    assert IA.check_name("x" * 16) == "X" * 16
    assert IA.check_name("A") == "A"
    assert IA.name_problem("bench-0123456789") == ""                     # 16 exactly


@pytest.mark.parametrize("value,words", [
    ("X" * 17, "it is 17 characters (at most 16: the panel shows 16)"),
    ("lab 07", "it has a space (only A-Z, 0-9 and - are allowed)"),
    ("lab_07", "it has '_' (only A-Z, 0-9 and - are allowed)"),
    ("café", "it has 'É'"),
    ("", "it is empty"),
])
def test_twin_a_name_outside_the_rule_is_refused_with_the_rule(value, words):
    with pytest.raises(UsageError) as exc:
        IA.check_name(value)
    assert words in exc.value.message
    assert "A name is 1-16 characters of A-Z, 0-9 and - (lower case is upper-cased)" in \
        exc.value.message and exc.value.hint == "e.g. LAB-07"


# --- MACs ----------------------------------------------------------------------------------------


def test_a_random_mac_starts_02_from_urandom():
    mac = IA.random_mac(MPS3, urandom=rolls("ab5e3a91c017"))
    assert mac == "02:5e:3a:91:c0:17"                                      # byte 0 forced to 0x02
    assert IA.mac_problem(mac, MPS3) == ""
    many = {IA.random_mac(MPS3) for _ in range(50)}
    assert len(many) == 50 and all(m.startswith("02:") and not m.startswith("02:00:00:")
                                   for m in many)


def test_twin_the_image_range_is_never_handed_out_it_is_rolled_again():
    mac = IA.random_mac(MPS3, urandom=rolls("0200004d5053", "02000000020e", "02c0ffee0001"))
    assert mac == "02:c0:ff:ee:00:01"


def test_twin_a_mac_in_the_registry_is_rolled_again_and_never_twice():
    taken = {"02:11:11:11:11:11": ["mps3@a"], "02:22:22:22:22:22": ["mps3@b"]}
    mac = IA.random_mac(MPS3, taken, urandom=rolls("021111111111", "022222222222",
                                                   "023333333333"))
    assert mac == "02:33:33:33:33:33"
    with pytest.raises(RefusedError, match="no random MAC that is free"):
        IA.random_mac(MPS3, taken, urandom=lambda n: bytes.fromhex("021111111111"), tries=5)


@pytest.mark.parametrize("value,words", [
    ("01:00:5e:00:00:01", "multicast"), ("00:00:00:00:00:00", "all zeros"),
    ("02:00:00:00:02:fe", "02:00:00:* is reserved (the image's range)"),
    ("02:00:00:4d:50:53", "reserved"), ("zz:00:00:00:00:00", "not a MAC"),
])
def test_a_mac_of_a_persons_own_meets_the_rule(value, words):
    with pytest.raises(UsageError, match=words.replace("(", r"\(").replace(")", r"\)")
                       .replace("*", r"\*")):
        IA.check_mac(value, MPS3)


def test_twin_a_good_mac_is_normalised_and_the_generic_policy_has_no_reserved_range():
    assert IA.check_mac("02-5E-00-00-02-FE", MPS3) == "02:5e:00:00:02:fe"
    assert IA.check_mac("02:00:00:00:02:fe") == "02:00:00:00:02:fe"      # generic: allowed


# --- IPs -----------------------------------------------------------------------------------------


def test_an_ip_is_a_host_of_a_24():
    assert IA.check_ip("192.168.10.117") == "192.168.10.117/24"
    assert IA.check_ip("192.168.10.117/24") == "192.168.10.117/24"


@pytest.mark.parametrize("value,words", [
    ("10.0.0.5/16", "prefix is /16"), ("192.168.10.0", "network address"),
    ("192.168.10.255", "broadcast"), ("300.1.1.1", "dotted quad"), ("auto ", "dotted quad"),
])
def test_twin_an_ip_outside_the_rule_is_refused(value, words):
    with pytest.raises(UsageError, match=words):
        IA.check_ip(value)


def test_the_pool_parses_and_refuses_what_is_not_one():
    assert IA.parse_pool("192.168.10.110-112") == ["192.168.10.110", "192.168.10.111",
                                                   "192.168.10.112"]
    assert IA.parse_pool("192.168.10.110-192.168.10.111") == ["192.168.10.110",
                                                              "192.168.10.111"]
    for bad in ("192.168.10.110", "192.168.10.120-110", "192.168.10.0-5", "10.0.0.1-10.0.1.5",
                "192.168.10.250-255"):
        assert IA.pool_problem(bad), bad
    assert IA.pool_problem("") == ""


def test_auto_takes_the_first_free_address_of_the_pool():
    assert IA.allocate_ip(MPS3, [], answering=lambda ip: False) == "192.168.10.110/24"


def test_auto_starts_at_the_macs_last_byte_mod_the_pool():
    # david 2 Oct, the lab's seat sheet: the first try is 192.168.10.(110 + mac[5] mod 90)
    assert IA.pool_start(IA.parse_pool("192.168.10.110-199"), "02:5e:3a:91:c0:17") == 23
    assert IA.allocate_ip(MPS3, [], answering=lambda ip: False,
                          mac="02:5e:3a:91:c0:17") == "192.168.10.133/24"
    assert IA.allocate_ip(MPS3, [], answering=lambda ip: False,
                          mac="02:5e:3a:91:c0:b4") == "192.168.10.110/24"     # 180 mod 90 = 0
    assert IA.allocate_ip(MPS3, [], answering=lambda ip: False,
                          mac="02-5E-3A-91-C0-FF") == "192.168.10.185/24"     # 255 mod 90 = 75


def test_twin_the_mac_start_still_skips_and_wraps_and_no_mac_starts_at_the_first():
    policy = IA.IdentityPolicy(ip_pool="192.168.10.100-105", reserved_ips=("192.168.10.101",))
    asked: list[str] = []

    def answering(ip: str) -> bool:
        asked.append(ip)
        return ip == "192.168.10.105"

    # 0x04 mod 6 = 4: .104 is in the registry, .105 answers, wrap: .100 is free
    got = IA.allocate_ip(policy, ["192.168.10.104"], answering=answering,
                         mac="02:11:22:33:44:04")
    assert got == "192.168.10.100/24" and asked == ["192.168.10.105", "192.168.10.100"]
    assert IA.pool_start(IA.parse_pool("192.168.10.100-105"), None) == 0
    assert IA.pool_start(IA.parse_pool("192.168.10.100-105"), "not a mac") == 0
    assert IA.allocate_ip(policy, [], answering=lambda ip: False) == "192.168.10.100/24"


def test_twin_auto_skips_the_reserved_address_the_registry_and_what_answers():
    policy = IA.IdentityPolicy(ip_pool="192.168.10.100-105", reserved_ips=("192.168.10.101",))
    asked: list[str] = []

    def answering(ip: str) -> bool:
        asked.append(ip)
        return ip == "192.168.10.103"

    got = IA.allocate_ip(policy, ["192.168.10.100", "192.168.10.102/24"], answering=answering)
    assert got == "192.168.10.104/24"
    assert asked == ["192.168.10.103", "192.168.10.104"]        # .101 and the registry: not asked


def test_twin_an_exhausted_pool_is_a_clear_refusal():
    policy = IA.IdentityPolicy(ip_pool="192.168.10.100-103", reserved_ips=("192.168.10.101",),
                               ip_pool_setting="mps3.identity.ip_pool")
    with pytest.raises(RefusedError) as exc:
        IA.allocate_ip(policy, ["192.168.10.100"], answering=lambda ip: ip.endswith(".102")
                       or ip.endswith(".103"))
    assert exc.value.message == (
        "no free address in the pool 192.168.10.100-103: all 4 are taken (1 given to or seen "
        "on boards here, 2 answering now, 1 reserved); nothing was changed")
    assert "harness-manager config set mps3.identity.ip_pool" in exc.value.hint
    with pytest.raises(RefusedError, match="has no address pool"):
        IA.allocate_ip(IA.GENERIC_POLICY, [])


def test_the_same_24_note_and_the_example_for_this_pc():
    assert IA.same_net_note("192.168.10.117/24") == \
        "this PC must be on the same /24 (e.g. 192.168.10.1/24)"
    assert IA.pc_example("192.168.11.1") == "192.168.11.2/24"            # the board is .1
    assert IA.same_net_note("") == ""


def test_the_subnet_check_and_its_twin_through_a_tunnel():
    assert IA.subnet_check("192.168.10.117", "192.168.10.1")["same"] is True
    chk = IA.subnet_check("192.168.11.7", "192.168.10.1")
    assert chk == {"local": "192.168.10.1", "network": "192.168.10.0/24", "same": False}
    assert IA.other_subnet_text("192.168.11.7", chk) == (
        "192.168.11.7 is not on this PC's network (this PC is 192.168.10.1 in 192.168.10.0/24): "
        "after the restart this PC cannot reach the board until it has an address in "
        "192.168.11.0/24 (e.g. 192.168.11.1/24)")
    assert IA.subnet_check("192.168.11.7", "")["same"] is None              # not known: no check
    assert IA.local_address_toward("127.0.0.1") == ""                       # a tunnel or a fake


def test_resolve_random_auto_keep_and_values():
    cur = {"mac": "02:00:00:4d:50:53", "ip": "192.168.10.101/24"}
    got = IA.resolve(MPS3, mac="random", ip="auto", current=cur, answering=lambda ip: False,
                     urandom=rolls("025e3a91c017"))
    assert got == {"mac": "02:5e:3a:91:c0:17", "mac_how": "random", "ip": "192.168.10.133/24",
                   "ip_how": "auto"}                      # 110 + 0x17 (23): from the new MAC
    # ip auto with the MAC kept: the board's own MAC picks the start (0x53 = 83 -> .193)
    assert IA.resolve(MPS3, ip="auto", current=cur, answering=lambda ip: False)["ip"] == \
        "192.168.10.193/24"
    assert IA.resolve(MPS3, mac="keep", ip="keep", current=cur) == {
        "mac": "02:00:00:4d:50:53", "mac_how": "keep", "ip": "192.168.10.101/24",
        "ip_how": "keep"}
    assert IA.resolve(MPS3)["mac"] is None                                  # not given
    with pytest.raises(UsageError):
        IA.resolve(MPS3, mac="02:00:00:aa:bb:cc")


def test_the_arp_note_for_a_new_mac_on_the_same_ip():
    note = IA.arp_note("192.168.10.104/24")
    assert "sudo arp -d 192.168.10.104" in note and "never runs sudo" in note
    assert "--ip auto" in note


# --- the registry (seen.json) -------------------------------------------------------------------


def test_the_registry_keeps_every_mac_and_ip_assigned_or_seen_with_the_date(tmp_path):
    seen = SeenIdentities(tmp_path)
    seen.update("mps3@a", label="MPS3", ip="192.168.10.101/24", mac="02:00:00:4d:50:53", at=1.0)
    seen.record("mps3@a", mac="02:00:00:4d:50:53", ip="192.168.10.101/24", at=1.0)
    seen.record("mps3@a", mac="02:5e:00:00:00:01", ip="192.168.10.110/24", how="assigned",
                at=86400.0)
    seen.record("mps3@a", ip="192.168.10.110", at=2 * 86400.0)            # seen after: still assigned
    rec = seen.get("mps3@a")["history"]
    assert rec["ip"]["192.168.10.110"] == {"how": "assigned", "first": "1970-01-02T00:00:00Z",
                                           "last": "1970-01-03T00:00:00Z"}
    assert rec["mac"]["02:00:00:4d:50:53"]["how"] == "seen"
    assert seen.taken("mac") == {"02:00:00:4d:50:53": ["mps3@a"], "02:5e:00:00:00:01": ["mps3@a"]}
    assert set(seen.taken("ip")) == {"192.168.10.101", "192.168.10.110"}


def test_twin_the_registry_follows_a_board_that_moved(tmp_path):
    seen = SeenIdentities(tmp_path)
    seen.update("mps3@192.168.10.101:6900", label="LAB-07", ip="192.168.10.101/24", at=5.0)
    seen.record("mps3@192.168.10.101:6900", ip="192.168.10.110", how="assigned", at=5.0)
    seen.move("mps3@192.168.10.101:6900", "mps3@192.168.10.110:6900")
    assert seen.get("mps3@192.168.10.101:6900") == {}
    moved = seen.get("mps3@192.168.10.110:6900")
    assert moved["moved_from"] == "mps3@192.168.10.101:6900"
    assert moved["history"]["ip"]["192.168.10.110"]["how"] == "assigned"
    assert seen.taken("ip")["192.168.10.110"] == ["mps3@192.168.10.110:6900"]
    seen.move("mps3@nothing", "mps3@else")                                 # nothing to move
    assert seen.get("mps3@else") == {}


# --- boards.toml: the table follows the board ---------------------------------------------------


def _boards(tmp_path, text: str):
    (tmp_path / "boards.toml").write_text(text, encoding="utf-8")


def test_a_table_keyed_by_the_old_board_id_moves_with_its_pinned_key(tmp_path):
    import tomllib

    _boards(tmp_path, '[boards."mps3@192.168.10.101:6900"]\nname = "lab-07"\n'
                      'match = ["192.168.10.101", "spare"]\n'
                      '[boards."mps3@192.168.10.101:6900".ssh]\nhost_key = "ssh-ed25519 AAAA"\n'
                      '[boards.other]\nname = "x"\n')
    what = IA.move_board_table("mps3@192.168.10.101:6900", "mps3@192.168.10.110:6900",
                               "192.168.10.101", "192.168.10.110", state_dir=tmp_path)
    assert what.endswith('moved to [boards."mps3@192.168.10.110:6900"]')
    data = tomllib.loads((tmp_path / "boards.toml").read_text())["boards"]
    assert "mps3@192.168.10.101:6900" not in data and data["other"] == {"name": "x"}
    new = data["mps3@192.168.10.110:6900"]
    assert new["ssh"]["host_key"] == "ssh-ed25519 AAAA" and new["name"] == "lab-07"
    assert new["match"] == ["192.168.10.110", "spare"]
    assert list(tmp_path.glob("boards.toml.bak-*"))                        # the writer's backup


def test_twin_a_table_found_by_match_keeps_its_key_and_gets_the_new_address(tmp_path):
    import tomllib

    _boards(tmp_path, '[boards.lab]\nmatch = ["192.168.10.101:6900"]\n')
    what = IA.move_board_table("mps3@192.168.10.101:6900", "mps3@192.168.10.110:6900",
                               "192.168.10.101", "192.168.10.110", state_dir=tmp_path)
    assert what == "boards.toml [boards.lab] match now has 192.168.10.110"
    data = tomllib.loads((tmp_path / "boards.toml").read_text())["boards"]
    assert data == {"lab": {"match": ["192.168.10.110:6900"]}}


def test_twin_no_table_and_a_table_already_at_the_new_id(tmp_path):
    assert IA.move_board_table("mps3@a:6900", "mps3@b:6900", "a", "b", state_dir=tmp_path) == ""
    _boards(tmp_path, '[boards."mps3@a:6900"]\nname = "a"\n[boards."mps3@b:6900"]\nname = "b"\n')
    with pytest.raises(RefusedError, match="already has a table"):
        IA.move_board_table("mps3@a:6900", "mps3@b:6900", "a", "b", state_dir=tmp_path)


# --- the policy comes from the pack ------------------------------------------------------------


def test_the_mps3_pack_gives_its_policy_and_a_pack_without_one_the_generic():
    from harness_manager_mps3.pack import Mps3Pack

    class Engine:
        def packs(self):
            return {"mps3": Mps3Pack()}

    p = IA.policy_for(Engine())
    assert p.reserved_mac_prefixes == ("02:00:00",) and p.ip_pool == "192.168.10.110-199"
    assert p.reserved_ips == ("192.168.10.101",) and p.ip_pool_setting == "mps3.identity.ip_pool"
    assert "mps3_01_pl" in p.known_bad_hub_records and "recovery mode" in p.rescue_note
    assert p.answering("192.168.10.110") is False                          # the test seam
    assert IA.policy_for(object()) is IA.GENERIC_POLICY


def test_twin_the_pool_is_a_setting(monkeypatch):
    from harness_manager_mps3 import net_identity as ni

    monkeypatch.setenv(ni.IP_POOL_ENV, "10.1.2.20-29")
    assert ni.identity_policy().ip_pool == "10.1.2.20-29"


# --- the board against the pool's network (name-subnet) ------------------------------------------


def _pool_policy():
    return IA.IdentityPolicy(ip_pool="192.168.10.110-199")


def test_outside_pool_compares_the_boards_network_with_the_pools():
    pol = _pool_policy()
    assert IA.pool_network(pol) == "192.168.10.0/24"
    assert IA.outside_pool(pol, "192.168.11.101/24") is True
    assert IA.outside_pool_text(pol, "192.168.11.101/24") == (
        "This board is on 192.168.11.0/24, outside the address pool (192.168.10.110-199): its "
        "current address 192.168.11.101 is kept. Choose another address only if the board's "
        "network will reach it.")


def test_twin_outside_pool_is_false_on_the_pool_with_no_pool_and_for_loopback():
    assert IA.outside_pool(_pool_policy(), "192.168.10.104") is False
    assert IA.outside_pool(IA.IdentityPolicy(), "192.168.11.101") is False
    assert IA.outside_pool(IA.IdentityPolicy(ip_pool="nonsense"), "192.168.11.101") is False
    assert IA.outside_pool(_pool_policy(), "127.0.0.1/24") is False
    assert IA.outside_pool(_pool_policy(), "") is False
