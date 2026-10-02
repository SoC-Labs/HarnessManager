"""Lane IDENTITY through the service: `board identity` as a user runs it while
harness-manager-daemon is up (the CLI's engine is a ``RemoteEngine``; ``RemoteIdentity``
asks the job's own checks with ``dry_run``, then posts the job with the chosen values,
``hub_fixed`` and ``other_subnet``). A real daemon (``LiveDaemon``) on the real MPS3 pack
against HM's model of the identity verbs. Each behaviour has its negative twin."""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator

import pytest

from harness_manager.cli.engine import set_engine_factory
from harness_manager.client.remote import RemoteEngine
from harness_manager.core.errors import RefusedError, UsageError
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services import identity_assign as IA
from harness_manager_mps3 import net_identity as NI
from harness_manager_mps3 import tunnel as T
from harness_manager_mps3.identify import IDENTIFY_PORT_ENV
from tests.fakes.claimed_lock import board_key_fp, pin_claim
from tests.fakes.idn_board import BOARD1_MAC, identity_board
from tests.fakes.lxslots_board import TRUSTED, BoardSsh
from tests.fakes.t13_daemon import TOKEN, LiveDaemon, state_dir

AS_DEFAULT = {"label": "MPS3", "ip": "192.168.10.101/24", "mac": BOARD1_MAC,
              "source": {"label": "default", "ip": "default", "mac": "default"}}


@pytest.fixture
def served(monkeypatch) -> Iterator:
    fake = identity_board(running=AS_DEFAULT, ssh_claimed=True, slots={"trusted_peer": TRUSTED},
                          ssh_host_key_sha256=board_key_fp())
    monkeypatch.setenv(IDENTIFY_PORT_ENV, str(fake.identify_port))
    ssh = BoardSsh(fake)
    monkeypatch.setattr(T, "DEFAULT_LAUNCHER", ssh)
    monkeypatch.setattr(T, "DEFAULT_SSH_G", ssh.ssh_g)
    eng = Engine(EngineConfig(state_dir=state_dir(), pack_overrides={"mps3": {
        "console_ports": fake.console_ports, "push_port": fake.raw_tcp_port,
        "tftp_port": fake.tftp_port}}))
    target = f"{fake.host}:{fake.control_port}"
    with LiveDaemon(eng) as d:
        presence = getattr(d.app.state.daemon, "presence", None)
        if presence is not None:
            presence._stop.set()
        remote = RemoteEngine(d.base_url, TOKEN, state_dir=state_dir())
        cand = remote.candidate_for(target)
        session = remote.open(cand, note="identity-remote")
        pin_claim(eng.session(cand.board_id))
        eng.session(cand.board_id).os_slots.reboot_poll_s = 0.02
        try:
            yield fake, remote, session, target
        finally:
            if cand.board_id in remote.open_boards():
                remote.close(cand.board_id)
    eng.close_all()
    ssh.close()
    fake.stop()


def test_remote_preflight_chooses_random_and_auto_and_sends_nothing(served):
    fake, remote, session, _ = served
    pre = remote.board_identity.preflight(session, want={"label": "lab-07", "mac": "random",
                                                         "ip": "auto"})
    assert pre["want"]["ip"] == "192.168.10.110/24" and pre["want"]["mac"].startswith("02:")
    assert pre["plan"]["phrase"] == "LAB-07" and pre["address"]["ip"] == "192.168.10.110"
    assert fake.identity_sets == [] and fake.reboots == []
    out = remote.board_identity.fix(session, confirm="LAB-07", want=pre["want"], wait_s=20)
    assert out["verified"] is True
    (_peer, body), = fake.identity_sets
    assert body["ip"] == "192.168.10.110/24" and body["label"] == "LAB-07"


def test_twin_remote_preflight_refuses_what_the_job_would(served, monkeypatch):
    fake, remote, session, _ = served
    with pytest.raises(UsageError, match=r"02:00:00:\* is reserved"):
        remote.board_identity.preflight(session, want={"mac": "02:00:00:12:34:56"})
    monkeypatch.setattr(IA, "local_address_toward", lambda host: "192.168.10.1")
    monkeypatch.setattr(NI, "DEFAULT_ANSWERING", lambda ip: False)
    with pytest.raises(RefusedError, match="not on this PC's network"):
        remote.board_identity.preflight(session, want={"ip": "192.168.11.7"})
    pre = remote.board_identity.preflight(session, want={"ip": "192.168.11.7"},
                                          other_subnet=True)
    assert any("not on this PC's network" in n for n in pre["notes"])
    assert fake.identity_sets == []


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


def test_the_cli_through_the_service_names_the_board(served, capsys):
    fake, remote, session, target = served
    remote.close(session.candidate.board_id)          # the CLI opens it itself
    previous = set_engine_factory(lambda _args: remote)
    try:
        rc, out, err = run(capsys, "board", "identity", target, "--label", "lab-08", "--mac",
                           "random", "--ip", "auto", "--wait", "20", stdin="LAB-08\n")
    finally:
        set_engine_factory(previous)
    assert rc == 0, err
    assert "NEW IP     192.168.10.110" in err and "NEW IP     192.168.10.110" in out
    (_peer, body), = fake.identity_sets
    assert body["label"] == "LAB-08" and body["ip"] == "192.168.10.110/24"


def test_twin_the_cli_through_the_service_refuses_before_the_question(served, capsys):
    fake, remote, session, target = served
    remote.close(session.candidate.board_id)          # the CLI opens it itself
    previous = set_engine_factory(lambda _args: remote)
    try:
        rc, _, err = run(capsys, "board", "identity", target, "--label", "lab 08")
    finally:
        set_engine_factory(previous)
    assert rc == 2 and "a space" in err and "type exactly" not in err
    assert fake.identity_sets == []
