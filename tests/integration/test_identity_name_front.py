"""Lane IDENTITY over the real daemon (``identity_api.py``: the proposal, POST with random/auto,
the guards before the job, a board that moves) and the real CLI (``board identity --mac random
--ip auto``, the NEW IP line, the refusals), on the real MPS3 pack against HM's model of the
identity verbs. Each behaviour has its negative twin."""

from __future__ import annotations

import io
import sys
import time
import warnings
from collections.abc import Iterator

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager.services import identity_assign as IA
from harness_manager_mps3 import net_identity as NI
from harness_manager_mps3 import tunnel as T
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.claimed_lock import board_key_fp, pin_claim
from tests.fakes.idn_board import BOARD1_MAC, identity_board
from tests.fakes.lxslots_board import TRUSTED, BoardSsh
from tests.fakes.t13_daemon import TOKEN, bid_path, headers, state_dir
from tests.integration.test_identity_name import AT_ITS_IP, at_new_address


@pytest.fixture(autouse=True)
def _pool_from_its_first_address(monkeypatch):
    """These tests read the pool from its first address: ``--ip auto``'s start from the
    random MAC (``pool_start``) is tested in tests/unit/test_identity_assign.py."""
    from harness_manager.services import identity_assign as _IA

    monkeypatch.setattr(_IA, "pool_start", lambda addrs, mac=None: 0)

AS_DEFAULT = {"label": "MPS3", "ip": "192.168.10.101/24", "mac": BOARD1_MAC,
              "source": {"label": "default", "ip": "default", "mac": "default"}}


@pytest.fixture
def api(monkeypatch) -> Iterator:
    made = []

    def make(running=AS_DEFAULT):
        fake = identity_board(running=running, ssh_claimed=True, slots={"trusted_peer": TRUSTED},
                              ssh_host_key_sha256=board_key_fp())
        monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
        ssh = BoardSsh(fake)
        monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
        monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
        eng = Engine(EngineConfig(state_dir=state_dir(), pack_overrides={"mps3": {
            "console_ports": fake.console_ports, "push_port": fake.raw_tcp_port,
            "tftp_port": fake.tftp_port}}))
        client = TestClient(create_app(eng, token=TOKEN, static_dir=None))
        client.__enter__()
        r = client.post("/api/v1/boards", json={"target": f"{fake.host}:{fake.control_port}",
                                                "note": "identity"}, headers=headers())
        assert r.status_code == 200, r.text
        bid = r.json()["board_id"]
        pin_claim(eng.session(bid))
        eng.session(bid).os_slots.reboot_poll_s = 0.02
        made.append((fake, client, eng, ssh))
        return fake, client, bid, eng

    yield make
    for fake, client, eng, ssh in made:
        client.__exit__(None, None, None)
        eng.close_all()
        ssh.close()
        fake.stop()


def wait_job(client, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/jobs/{job_id}", headers=headers()).json()
        if body.get("state") not in ("running", "queued"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running")


# --- the API -------------------------------------------------------------------------------------


def test_the_proposal_route_names_and_picks_and_reserves_nothing(api):
    fake, client, bid, _ = api()
    client.get(bid_path(bid) + "/identity", headers=headers())
    r = client.get(bid_path(bid) + "/identity/proposal", params={"label": "lab-07"},
                   headers=headers())
    assert r.status_code == 200, r.text
    p = r.json()["proposal"]
    assert p["label"] == "LAB-07" and p["phrase"] == "LAB-07"
    assert p["mac_how"] == "random" and p["ip"] == "192.168.10.110/24"
    assert p["address"]["same_net"] == "this PC must be on the same /24 (e.g. 192.168.10.1/24)"
    again = client.get(bid_path(bid) + "/identity/proposal", params={"mac": "random"},
                       headers=headers()).json()["proposal"]
    assert again["mac"] != p["mac"]                                     # regenerate
    assert fake.identity_sets == [] and fake.reboots == []


def test_twin_a_bad_name_or_value_in_the_proposal_is_said_not_an_error(api):
    fake, client, bid, _ = api()
    r = client.get(bid_path(bid) + "/identity/proposal",
                   params={"label": "x" * 17, "mac": "02:00:00:aa:bb:cc"}, headers=headers())
    assert r.status_code == 200
    p = r.json()["proposal"]
    assert p["label_problem"].startswith("it is 17 characters (at most 16")
    assert "02:00:00:* is reserved" in p["errors"]["mac"]


def test_post_random_and_auto_set_the_board_and_say_its_new_ip(api):
    fake, client, bid, eng = api()
    r = client.post(bid_path(bid) + "/identity", json={
        "confirm": "LAB-07", "label": "lab-07", "mac": "random", "ip": "auto", "wait_s": 20},
        headers=headers())
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["verified"] is True and res["moved"] is None
    assert res["address"]["ip"] == "192.168.10.110"
    (_peer, body), = fake.identity_sets
    assert body["label"] == "LAB-07" and body["ip"] == "192.168.10.110/24"
    assert body["mac"].startswith("02") and not body["mac"].startswith("020000")
    assert eng.board_identity.seen.taken("ip")["192.168.10.110"] == [bid]


@pytest.mark.parametrize("field,value,words", [
    ("mac", "02:00:00:12:34:56", "02:00:00:* is reserved"),
    ("ip", "10.0.0.5/16", "prefix is /16"),
    ("label", "lab 07", "a space"),
])
def test_twin_a_value_of_a_persons_own_outside_the_rules_is_400_before_the_job(api, field,
                                                                              value, words):
    fake, client, bid, _ = api()
    r = client.post(bid_path(bid) + "/identity", json={"confirm": "MPS3", field: value},
                    headers=headers())
    assert r.status_code == 400 and words in r.json()["error"]["message"], r.text
    assert fake.identity_sets == []


def test_the_subnet_guard_is_409_before_the_job_and_other_subnet_goes_ahead(api, monkeypatch):
    fake, client, bid, _ = api()
    monkeypatch.setattr(IA, "local_address_toward", lambda host: "192.168.10.1")
    r = client.post(bid_path(bid) + "/identity", json={"confirm": "MPS3", "ip": "192.168.11.7"},
                    headers=headers())
    assert r.status_code == 409 and "not on this PC's network" in r.json()["error"]["message"]
    assert fake.identity_sets == []
    r = client.post(bid_path(bid) + "/identity", json={"confirm": "MPS3", "ip": "192.168.11.7",
                                                       "other_subnet": True, "wait_s": 20},
                    headers=headers())
    assert r.status_code == 202, r.text
    assert wait_job(client, r.json()["job"])["state"] == "done"


def test_a_board_that_moves_answers_moved_through_the_api(api, monkeypatch):
    fake, client, bid, eng = api(running=AT_ITS_IP)
    eng.session(bid).net_identity.move_poll_s = 0.01
    at_new_address(fake, monkeypatch)
    r = client.post(bid_path(bid) + "/identity", json={
        "confirm": "LAB-07", "label": "LAB-07", "ip": "auto", "mac": "random", "wait_s": 20},
        headers=headers())
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "done", job
    moved = job["result"]["moved"]
    assert moved["from"] == bid and moved["to"] == f"mps3@192.168.10.110:{fake.control_port}"
    assert any("claims.json" in line for line in moved["records"])


def test_twin_another_board_at_the_new_address_fails_the_job_unadopted(api, monkeypatch):
    fake, client, bid, eng = api(running=AT_ITS_IP)
    eng.session(bid).net_identity.move_poll_s = 0.01
    at_new_address(fake, monkeypatch, host_key="SHA256:" + "B" * 43)
    r = client.post(bid_path(bid) + "/identity", json={
        "confirm": "LAB-07", "label": "LAB-07", "ip": "auto", "wait_s": 20}, headers=headers())
    job = wait_job(client, r.json()["job"])
    assert job["state"] == "failed" and job["error"]["name"] == "REFUSED"
    assert "a different board answers at 192.168.10.110" in job["error"]["message"]


OFF_POOL = {"label": "MPS3", "ip": "192.168.11.101/24", "mac": "025e00000102",
            "source": {"label": "default", "ip": "stage0", "mac": "override"}}


def test_post_an_ip_in_another_network_for_a_board_outside_the_pool_needs_confirm_subnet(api):
    fake, client, bid, _ = api(running=OFF_POOL)
    body = {"confirm": "MPS3", "ip": "192.168.10.120", "wait_s": 20}
    r = client.post(bid_path(bid) + "/identity", json=body, headers=headers())
    assert r.status_code == 409, r.text
    assert "outside the address pool (192.168.10.110-199)" in r.json()["error"]["message"]
    assert fake.identity_sets == []
    r = client.post(bid_path(bid) + "/identity", json={**body, "confirm_subnet": True},
                    headers=headers())
    assert r.status_code == 202, r.text
    assert wait_job(client, r.json()["job"])["state"] == "done"
    assert fake.identity_sets[0][1]["ip"] == "192.168.10.120/24"


def test_twin_post_a_board_on_the_pools_network_needs_no_confirm_subnet(api):
    fake, client, bid, _ = api()
    r = client.post(bid_path(bid) + "/identity", json={"confirm": "MPS3", "ip": "192.168.10.120",
                                                       "wait_s": 20}, headers=headers())
    assert r.status_code == 202, r.text


def test_the_proposal_route_keeps_the_ip_of_a_board_outside_the_pool_and_warns(api):
    fake, client, bid, _ = api(running=OFF_POOL)
    client.get(bid_path(bid) + "/identity", headers=headers())
    p = client.get(bid_path(bid) + "/identity/proposal", params={"ip": "auto"},
                   headers=headers()).json()["proposal"]
    assert p["ip"] == "192.168.11.101/24" and p["subnet"]["kept"] is True
    assert p["subnet"]["warning"].startswith("This board is on 192.168.11.0/24, outside the "
                                              "address pool (192.168.10.110-199)")


def test_twin_the_proposal_route_for_a_board_on_the_pool_has_no_warning(api):
    fake, client, bid, _ = api()
    client.get(bid_path(bid) + "/identity", headers=headers())
    p = client.get(bid_path(bid) + "/identity/proposal", params={"ip": "auto"},
                   headers=headers()).json()["proposal"]
    assert p["subnet"]["warning"] == "" and p["ip"] == "192.168.10.110/24"


# --- the CLI -------------------------------------------------------------------------------------


def run(capsys, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    from harness_manager.cli.main import main

    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        rc = main(list(argv))
    finally:
        sys.stdin = old
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def board(monkeypatch):
    made = []

    def make(running=AS_DEFAULT):
        fake = identity_board(running=running, ssh_claimed=True, slots={"trusted_peer": TRUSTED},
                              ssh_host_key_sha256=board_key_fp())
        monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
        monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
        ssh = BoardSsh(fake)
        monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
        monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
        target = f"{fake.host}:{fake.control_port}"
        pin_claim(type("S", (), {"candidate": Mps3Pack().candidate_for_host(target)})())
        made.append((fake, ssh))
        return fake, target

    yield make
    for fake, ssh in made:
        ssh.close()
        fake.stop()


def test_cli_random_and_auto_print_the_new_ip_where_nobody_misses_it(capsys, board):
    fake, target = board()
    rc, out, err = run(capsys, "board", "identity", target, "--label", "lab-07", "--mac",
                       "random", "--ip", "auto", "--wait", "20", stdin="LAB-07\n")
    assert rc == 0, err
    assert "NEW IP     192.168.10.110\n           this PC must be on the same /24 (e.g. " \
           "192.168.10.1/24)" in err                                       # in the question
    assert "To go ahead, type exactly: LAB-07" in err
    assert "NEW IP     192.168.10.110" in out and "verified   yes" in out
    (_peer, body), = fake.identity_sets
    assert body["ip"] == "192.168.10.110/24" and body["mac"].startswith("02")


def test_twin_cli_an_exhausted_pool_is_refused_with_the_rule(capsys, board, monkeypatch):
    fake, target = board()
    monkeypatch.setenv(NI.IP_POOL_ENV, "192.168.10.110-110")
    monkeypatch.setattr(NI, "DEFAULT_ANSWERING", lambda ip: True)
    rc, _, err = run(capsys, "board", "identity", target, "--ip", "auto", "--consent", "MPS3")
    assert rc == 15 and "no free address in the pool 192.168.10.110-110: all 1 are taken" in err
    assert fake.identity_sets == []


def test_twin_cli_a_reserved_mac_and_a_non_24_ip_are_usage_errors(capsys, board):
    fake, target = board()
    rc, _, err = run(capsys, "board", "identity", target, "--mac", "02:00:00:12:34:56")
    assert rc == 2 and "02:00:00:* is reserved" in err
    rc, _, err = run(capsys, "board", "identity", target, "--ip", "10.1.2.3/8")
    assert rc == 2 and "prefix is /8" in err
    assert fake.identity_sets == []


def test_cli_an_ip_in_another_network_for_a_board_outside_the_pool_needs_the_flag(capsys, board):
    fake, target = board(running=OFF_POOL)
    rc, _, err = run(capsys, "board", "identity", target, "--ip", "192.168.10.120",
                     "--consent", "MPS3", "--wait", "20")
    assert rc == 15 and "this board is on 192.168.11.0/24, outside the address pool " \
        "(192.168.10.110-199), and 192.168.10.120 is in 192.168.10.0/24" in err
    assert "--allow-other-subnet" in err and fake.identity_sets == []
    rc, _, err = run(capsys, "board", "identity", target, "--ip", "192.168.10.120",
                     "--allow-other-subnet", "--consent", "MPS3", "--wait", "20")
    assert rc == 0, err
    assert fake.identity_sets[0][1]["ip"] == "192.168.10.120/24"


def test_twin_cli_a_board_on_the_pools_network_needs_no_flag(capsys, board):
    fake, target = board()
    rc, _, err = run(capsys, "board", "identity", target, "--ip", "192.168.10.120",
                     "--consent", "MPS3", "--wait", "20")
    assert rc == 0, err


def test_cli_the_hub_guard_and_its_flag_are_in_the_help(capsys):
    rc, out, _ = run(capsys, "board", "identity", "--help")
    assert rc == 0
    out = " ".join(out.split())                                         # argparse wraps
    for words in ("--hub-fixed HUB", "--other-subnet", "--allow-other-subnet", "MAC|random", "A.B.C.D|auto",
                  "1-16 characters of A-Z, 0-9 and -"):
        assert words in out, words
