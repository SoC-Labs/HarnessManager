"""BOARD-ID: ``harness-manager board identity`` as a user runs it (``main([...])``, the in-process
engine, the real MPS3 pack) against HM's model of the identity verbs (``tests/fakes/idn_board``).

Each behaviour has its negative twin: the read shows the image default as "identity not set";
a change asks for the typed phrase, and the wrong phrase (or ``--yes`` alone) changes nothing;
a netbooted board is refused with the stage0 bake; the reboot is the harness's own verb.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from harness_manager_mps3 import tunnel as T
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.claimed_lock import board_key_fp, pin_claim
from tests.fakes.idn_board import BOARD1_MAC, identity_board
from tests.fakes.lxslots_board import TRUSTED, BoardSsh

AS_DEFAULT = {"label": "MPS3", "ip": "192.168.10.101/24", "mac": BOARD1_MAC,
              "source": {"label": "default", "ip": "default", "mac": "default"}}


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

    def make(**kw):
        fake = identity_board(running=AS_DEFAULT, ssh_claimed=True,
                              slots={"trusted_peer": TRUSTED},
                              ssh_host_key_sha256=board_key_fp(), **kw)
        monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
        monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
        ssh = BoardSsh(fake)
        monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
        monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
        target = f"{fake.host}:{fake.control_port}"
        cand = Mps3Pack().candidate_for_host(target)
        pin_claim(type("S", (), {"candidate": cand})())
        made.append((fake, ssh))
        return fake, target

    yield make
    for fake, ssh in made:
        ssh.close()
        fake.stop()


def test_board_identity_shows_what_the_board_reports_and_that_it_is_not_set(capsys, board):
    fake, target = board()
    rc, out, err = run(capsys, "board", "identity", target)
    assert rc == 0, err
    assert "identity   " in out and ": UNSET" in out
    assert "label MPS3  ip 192.168.10.101/24  mac 02:00:00:4d:50:53" in out
    assert "identity not set" in out and "hub        no hub record" in out
    rc, out, _ = run(capsys, "--json", "board", "identity", target)
    data = json.loads(out)
    assert data["identity"]["status"] == "unset" and data["identity"]["reported"]["via"] == \
        "identity"
    assert fake.identity_sets == [] and fake.reboots == []          # a read changes nothing


def test_twin_a_board_whose_identity_is_set_reads_ok(capsys, board):
    fake, target = board()
    fake.running = {"label": "MPS3-02", "hostname": "mps3-02", "ip": "192.168.11.101/24",
                    "mac": "0200000002fe",
                    "source": {"label": "stage0", "ip": "stage0", "mac": "stage0"}}
    rc, out, err = run(capsys, "board", "identity", target)
    assert rc == 0, err
    assert ": OK" in out and "identity not set" not in out and "clash" not in out


def test_set_with_the_typed_phrase_reboots_warm_and_verifies(capsys, board):
    fake, target = board()
    rc, out, err = run(capsys, "board", "identity", target, "--label", "MPS3-02", "--ip",
                       "192.168.11.101", "--mac", "02:00:00:00:02:fe", "--wait", "20",
                       stdin="MPS3-02\n")
    assert rc == 0, err
    assert "To go ahead, type exactly: MPS3-02" in err
    assert "never an MCC REBOOT" in err
    assert "verified   yes" in out and "reboot     reboot witnessed" in out
    assert [p for p, _ in fake.identity_sets] == [TRUSTED] and len(fake.reboots) == 1
    assert fake.running["label"] == "MPS3-02" and fake.running["ip"] == "192.168.11.101/24"


def test_twin_the_wrong_phrase_or_yes_alone_changes_nothing(capsys, board):
    fake, target = board()
    rc, _, err = run(capsys, "board", "identity", target, "--label", "MPS3-02", stdin="yes\n")
    assert rc == 15 and "not confirmed" in err and "type exactly: MPS3-02" in err
    rc, _, err = run(capsys, "board", "identity", target, "--label", "MPS3-02", "--consent",
                     "MPS3-2")
    assert rc == 15
    assert fake.identity_sets == [] and fake.reboots == []


def test_twin_a_netbooted_board_is_refused_before_the_question(capsys, board):
    fake, target = board(persist=False)
    rc, _, err = run(capsys, "board", "identity", target, "--label", "MPS3-02",
                     "--consent", "MPS3-02")
    assert rc == 15 and "stage0 bake" in err and "re-bake stage0" in err
    assert "type exactly" not in err                 # refused before it asked
    assert fake.identity_sets == [] and fake.reboots == []


def test_twin_bad_values_are_usage_errors_and_nothing_is_sent(capsys, board):
    fake, target = board()
    rc, _, err = run(capsys, "board", "identity", target, "--mac", "01:00:5e:00:00:01",
                     "--consent", "MPS3")
    assert rc == 2 and "not a unicast" in err
    rc, _, err = run(capsys, "board", "identity", target, "--label", "X" * 24)
    assert rc == 2 and "does not fit the LCD row" in err
    rc, _, err = run(capsys, "board", "identity", target, "--clear", "--label", "A")
    assert rc == 2 and "--clear goes alone" in err
    assert fake.identity_sets == []


def test_info_shows_the_identity_line_and_its_json_key(capsys, board):
    fake, target = board()
    rc, out, err = run(capsys, "info", target)
    assert rc == 0, err
    line = next(ln for ln in out.splitlines() if ln.startswith("identity   "))
    assert line.startswith("identity   unset: ") and "02:00:00:4d:50:53" in line
    assert "identity not set" in line
    rc, out, _ = run(capsys, "--json", "info", target)
    assert json.loads(out)["net_identity"]["status"] == "unset"
    assert fake.identity_reads == 0                  # info asked identify only, never 6900


def test_twin_a_board_that_does_not_answer_identify_has_no_identity_in_info(capsys, board,
                                                                            monkeypatch):
    fake, target = board()
    monkeypatch.setenv(IDENTIFY_PORT_ENV, "9")       # nothing answers there
    rc, out, err = run(capsys, "--json", "info", target)
    assert rc == 0, err
    assert "net_identity" not in json.loads(out)


# --- V7-ALIGN: the shipped contract (net-protocol v0.16, platform 18622e5) ----------------------


def test_v7_the_label_check_is_the_boards_19_of_a_z_0_9(capsys, board):
    fake, target = board()
    rc, _, err = run(capsys, "board", "identity", target, "--label", "X" * 20)
    assert rc == 2 and "1-19 of A-Z" in err
    rc, _, err = run(capsys, "board", "identity", target, "--label", "mps3-02")
    assert rc == 2 and "does not fit the LCD row" in err
    assert fake.identity_sets == []


def test_v7_twin_a_19_character_label_is_taken(capsys, board):
    fake, target = board()
    label = "BENCH-0123456789-XY"
    assert len(label) == 19
    rc, out, err = run(capsys, "board", "identity", target, "--label", label, "--consent", label,
                       "--wait", "20")
    assert rc == 0, err
    assert fake.identity_sets[0][1] == {"label": label}


def test_v7_a_bad_value_on_a_netbooted_board_is_refused_for_the_card(capsys, board):
    """The board's order: ``no_persist`` before ``invalid``."""
    fake, target = board(persist=False)
    rc, _, err = run(capsys, "board", "identity", target, "--label", "lower-case")
    assert rc == 15 and "stage0 bake" in err
    assert fake.identity_sets == []


def test_v7_unset_drops_the_key_with_the_wires_empty_string(capsys, board):
    fake, target = board()
    fake.override = {"hostname": "bench"}
    fake.running = {**fake.running, "hostname": "bench",
                    "source": {**fake.running["source"], "hostname": "override"}}
    rc, out, err = run(capsys, "board", "identity", target, "--unset", "hostname",
                       "--consent", "MPS3", "--wait", "20")
    assert rc == 0, err
    assert fake.identity_sets[0][1] == {"hostname": ""} and fake.override is None
    assert "dropped" in out


def test_v7_twin_unset_of_a_key_not_in_the_override_changes_nothing(capsys, board):
    fake, target = board()
    rc, out, _ = run(capsys, "board", "identity", target, "--unset", "hostname")
    assert rc == 0 and "nothing to change" in out and fake.identity_sets == []
    rc, _, err = run(capsys, "board", "identity", target, "--unset", "label", "--label", "A")
    assert rc == 2 and "together" in err
