"""LINUX-CLAIM through the real Engine, pack, CLI and daemon routes.

The board is pyverify's ``FakeShell(profile="linux")`` (6900, identify on UDP, the TOFU
``authorized_keys`` over TFTP) on 127.0.0.1; ssh is ``lc_fake_board_ssh.FakeBoardSsh``. The
bare-metal twin is the fielded ``VirtualMps3``. Each check has a negative twin.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pyverify.testing.fakeshell import FakeShell

from harness_manager.cli.engine import set_engine_factory
from harness_manager.cli.main import main
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ExitCode
from harness_manager.core.services import EngineConfig
from harness_manager.daemon.app import create_app
from harness_manager.engine import Engine
from harness_manager_mps3 import claim as CL
from tests.fakes.l1_rig import BOARD_IP, lab
from tests.fakes.lc_fake_board_ssh import FakeBoardSsh, make_key_line, write_key_pair
from tests.fakes.t13_daemon import TOKEN, bid_path, engine_for, headers
from tests.fakes.virtual_board import FIELDED_3F1A560F, LINUX_HARNESSD, VirtualMps3

BOARD_KEY = make_key_line("the-board")
H = headers()


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


@pytest.fixture
def linux(tmp_path, monkeypatch):
    fp = CL.fingerprint(BOARD_KEY.split()[1])
    shell = FakeShell("127.0.0.1", control_port=0, tftp_port=0, raw_tcp_port=0, uart0_port=0,
                      uart1_port=0, swo_port=0, identify_port=0, profile="linux",
                      ssh_host_key_sha256=fp).start()
    monkeypatch.setenv("HARNESS_MANAGER_MPS3_IDENTIFY_PORT", str(shell.identify_port))
    monkeypatch.setenv("HARNESS_MANAGER_MPS3_TFTP_PORT", str(shell.tftp_port))
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    ssh = FakeBoardSsh(shell, BOARD_KEY)
    monkeypatch.setattr(CL, "DEFAULT_RUN", ssh)
    monkeypatch.setattr(CL, "KEYS_SYNC_RETRIES_S", ())
    private, public = write_key_pair(tmp_path / "keys", "id_test")
    try:
        yield shell, ssh, public
    finally:
        shell.stop()


def target(shell: FakeShell) -> str:
    return f"127.0.0.1:{shell.control_port}"


def cli(capsys, monkeypatch, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


# --- the CLI ------------------------------------------------------------------------------------


def test_cli_claims_an_unclaimed_board_and_info_shows_it(linux, capsys, monkeypatch):
    shell, _ssh, public = linux
    rc, out, _ = cli(capsys, monkeypatch, "info", target(shell))
    assert rc == ExitCode.OK and "ssh claim  unclaimed" in out
    rc, out, err = cli(capsys, monkeypatch, "board", "claim", target(shell), "--key",
                       str(public), "--yes")
    assert rc == ExitCode.OK, err
    assert f"claim      mps3@{target(shell)}: claimed by you (SHA256:" in out
    assert "pinned" in out and shell.authorized_keys == public.read_bytes()
    rc, out, _ = cli(capsys, monkeypatch, "--json", "info", target(shell))
    info = json.loads(out)
    assert info["claim"]["state"] == "mine" and info["claim"]["claimed"]["key_fp"]
    rc, out, _ = cli(capsys, monkeypatch, "--json", "board", "ssh", target(shell), "--print")
    argv = json.loads(out)["argv"]
    assert "StrictHostKeyChecking=yes" in argv and argv[-2:] == ["root", "127.0.0.1"]
    rc, out, _ = cli(capsys, monkeypatch, "board", "ssh", target(shell), "--print", "-c",
                     "ls -l /persist")
    assert rc == ExitCode.OK and out.rstrip().endswith("root 127.0.0.1 ls -l /persist")


def test_negative_twin_cli_claim_unconfirmed_sends_nothing(linux, capsys, monkeypatch):
    shell, _ssh, public = linux
    rc, _, err = cli(capsys, monkeypatch, "board", "claim", target(shell), "--key", str(public),
                     stdin="n\n")
    assert rc == ExitCode.REFUSED and "not confirmed" in err
    assert shell.authorized_keys is None and not shell.ssh_claimed


def test_cli_claim_of_a_claimed_board_is_already_and_never_retried(linux, capsys, monkeypatch):
    shell, ssh, public = linux
    shell.ssh_claimed = True
    rc, _, err = cli(capsys, monkeypatch, "board", "claim", target(shell), "--key", str(public),
                     "--yes")
    assert rc == ExitCode.ALREADY and "never repeated or taken over" in err and "--adopt" in err
    assert shell.authorized_keys is None and ssh.calls == []


def test_cli_claim_status_tsv_layout(linux, capsys, monkeypatch):
    shell, _ssh, _public = linux
    rc, out, _ = cli(capsys, monkeypatch, "--tsv", "board", "claim-status", target(shell))
    assert rc == ExitCode.OK
    row = out.rstrip("\n").split("\t")
    assert len(row) == len(TSV_COLUMNS["board claim"]) and row[1] == "unclaimed"


def test_bare_metal_info_has_no_claim_and_claim_is_unavailable(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    with VirtualMps3(tmp_path / "vb", FIELDED_3F1A560F) as vb:
        previous = set_engine_factory(lambda _args: engine_for(vb))
        try:
            spec = f"127.0.0.1:{vb.shell.control_port}"
            rc, out, _ = cli(capsys, monkeypatch, "--json", "info", spec)
            assert rc == ExitCode.OK and "claim" not in json.loads(out)
            rc, out, _ = cli(capsys, monkeypatch, "info", spec)
            assert "ssh claim" not in out
            rc, _, err = cli(capsys, monkeypatch, "board", "claim", spec, "--yes")
            assert rc == ExitCode.UNAVAILABLE and "no SSH to claim" in err
        finally:
            set_engine_factory(previous)


# --- the daemon routes ---------------------------------------------------------------------------


@pytest.fixture
def client(linux):
    shell, _ssh, _public = linux
    eng = Engine(EngineConfig(state_dir=state_dir()))
    with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
        r = c.post("/api/v1/boards", json={"target": target(shell)}, headers=H)
        assert r.status_code == 200, r.text
        yield c, bid_path(r.json()["board_id"])
    eng.close_all()


def wait(c: TestClient, job: str) -> dict:
    import time

    for _ in range(500):
        body = c.get(f"/api/v1/jobs/{job}", headers=H).json()
        if body["state"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("job still running")


def test_api_claim_needs_confirm(client, linux):
    c, B = client
    shell, _ssh, public = linux
    r = c.post(f"{B}/claim", json={"key": str(public)}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    assert shell.authorized_keys is None


def test_negative_twin_api_claim_confirmed_is_a_job_and_info_carries_it(client, linux):
    c, B = client
    shell, _ssh, public = linux
    assert c.get(B, headers=H).json()["claim"]["state"] == "unclaimed"
    r = c.post(f"{B}/claim", json={"confirm": True, "key": str(public)}, headers=H)
    assert r.status_code == 202, r.text
    done = wait(c, r.json()["job"])
    assert done["state"] == "done", done
    assert done["result"]["claim"]["state"] == "mine"
    assert done["result"]["claim"]["action"] == "claimed"
    assert c.get(f"{B}/claim", headers=H).json()["claim"]["state"] == "mine"
    argv = c.get(f"{B}/ssh?command=uptime", headers=H).json()["argv"]
    assert argv[-1] == "uptime" and "StrictHostKeyChecking=yes" in argv


def test_api_bare_metal_claim_is_422_and_info_is_unchanged(tmp_path):
    with VirtualMps3(tmp_path / "vb", FIELDED_3F1A560F) as vb:
        eng = engine_for(vb)
        with TestClient(create_app(eng, token=TOKEN, static_dir=None)) as c:
            r = c.post("/api/v1/boards", json={"target": f"127.0.0.1:{vb.shell.control_port}"},
                       headers=H)
            B = bid_path(r.json()["board_id"])
            info = c.get(B, headers=H).json()
            assert set(info) == {"ok", "candidate", "identity", "health", "capabilities",
                                 "unavailable"}
            r = c.post(f"{B}/claim", json={"confirm": True}, headers=H)
            assert r.status_code == 422 and r.json()["error"]["name"] == "UNAVAILABLE"
            assert c.get(f"{B}/claim", headers=H).json()["claim"] is None
        eng.close_all()


# --- XVC's board-SSH reach uses the pin --------------------------------------------------------


def test_xvc_board_ssh_rides_the_pinned_host_key(tmp_path, monkeypatch):
    from harness_manager.services.lease import LeaseService

    with VirtualMps3(tmp_path / "vb", LINUX_HARNESSD) as vb, \
            lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        rig.ssh.routes[("127.0.0.1", 2542)] = ("127.0.0.1", 9)      # never connected here
        eng = Engine(EngineConfig(state_dir=state_dir()))
        try:
            session = eng.open(eng.candidate_for(BOARD_IP), note="claim xvc")
            CL.write_ssh_settings(session.candidate, {"host_key": BOARD_KEY})
            LeaseService(state_dir()).acquire(session.hub, board_id=session.candidate.board_id,
                                              ttl_s=600, holder="hm-test", heartbeat=False)
            tunnel = session.xvc._board_ssh()
            argv = tunnel.argv
            assert "StrictHostKeyChecking=yes" in argv
            kh = next(a.split("=", 1)[1] for a in argv if a.startswith("UserKnownHostsFile="))
            assert BOARD_KEY in Path(kh).read_text()
            assert argv[argv.index("-l") + 1] == "root" and argv[-1] == BOARD_IP
            session.xvc.xvc_release()
        finally:
            eng.close_all()


def test_negative_twin_xvc_board_ssh_without_a_pin_is_unchanged(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path / "vb", LINUX_HARNESSD) as vb, \
            lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        rig.ssh.routes[("127.0.0.1", 2542)] = ("127.0.0.1", 9)
        eng = Engine(EngineConfig(state_dir=state_dir()))
        try:
            session = eng.open(eng.candidate_for(BOARD_IP), note="claim xvc")
            argv = session.xvc._board_ssh().argv
            assert not any(a.startswith(("UserKnownHostsFile=", "StrictHostKeyChecking="))
                           for a in argv)
            session.xvc.xvc_release()
        finally:
            eng.close_all()
