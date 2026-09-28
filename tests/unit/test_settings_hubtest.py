"""SET-HUBS: Test connection and discovery (spikes S3 and S4, promoted).

REST runs against T8's fake fpgahub 0.3.0 on 127.0.0.1; SSH runs real subprocesses through
fake ``ssh``, ``sg``, ``id`` and ``fpgahub`` executables (``tests/fakes/settings_fake_bin``),
so the quoting crosses two real shells. Host names are under ``.invalid``: nothing can reach
a real host. Every failure has its twin (the ``ok`` scenario, or the same call fixed).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from harness_manager.core.errors import ExitCode
from harness_manager.settings import hubs as H
from harness_manager.settings import hubtest as HT
from tests.fakes import settings_fake_bin as fakebin
from tests.fakes.t8_hub_rest import FakeFpgahub

HOST = "hub.invalid"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="/bin/sh shims stand in for ssh")


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "POLICY_PATH", tmp_path / "no-policy.toml")
    monkeypatch.setenv("FPGAHUB_CLIENT_CONFIG", str(tmp_path / "no-login.toml"))
    monkeypatch.delenv("FPGAHUB_TOKEN", raising=False)
    monkeypatch.delenv("FPGAHUB_ADDR", raising=False)


def state() -> Path:
    root = Path(os.environ["HARNESS_MANAGER_STATE_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def fake_bin(tmp_path):
    bindir = fakebin.install(tmp_path / "bin")
    log = tmp_path / "fake.log"

    def runner(scenario: str):
        env = fakebin.env_for(bindir, scenario, log)
        return lambda argv: subprocess.run(list(argv), capture_output=True, text=True,
                                           timeout=60, env=env, cwd=fakebin.REPO)
    runner.log = log
    return runner


@pytest.fixture
def lab():
    (state() / "settings.toml").write_text(
        f'[hubs.lab]\nhost = "{HOST}"\njump = "bastion.invalid"\n')
    (state() / "boards.toml").write_text(
        '[boards.lab]\nmatch = ["192.168.10.101"]\nhub = { use = "lab", target = "mps3_01_pl" }\n')
    return H.load_resolver()


@pytest.fixture
def lease_spy(monkeypatch):
    """Every way a lease could be taken, joined, released or a share started: each raises."""
    import pyverify.lease as pl

    from harness_manager.services.lease import LeaseService
    from harness_manager.transports.hub_rest import RestHubClient
    from harness_manager_mps3.hub import HubClient

    calls: list[str] = []

    def spy(name):
        def boom(*a, **k):
            calls.append(name)
            raise AssertionError(f"Test connection called {name}")
        return boom

    for cls, names in ((pl.LeaseClient, ("acquire", "release", "heartbeat", "cancel")),
                       (HubClient, ("lease_acquire", "lease_release", "lease_heartbeat",
                                    "lease_cancel", "share_start", "lease_revoke")),
                       (RestHubClient, ("lease_acquire", "lease_release", "lease_heartbeat",
                                        "lease_cancel", "share_start", "lease_revoke")),
                       (LeaseService, ("acquire", "release", "request", "force"))):
        for n in names:
            monkeypatch.setattr(cls, n, spy(f"{cls.__name__}.{n}"))
    return calls


# --- SSH ---------------------------------------------------------------------------------------


SSH_WANT = {"dns": "reach", "timeout": "reach", "hostkey": "auth", "auth": "auth",
            "nogroup": "group", "socket": "group", "nofpgahub": "targets", "badjson": "targets"}


@pytest.mark.parametrize("scenario, failed", sorted(SSH_WANT.items()))
def test_ssh_each_failure_stops_at_its_step_with_a_hint(lab, fake_bin, lease_spy, scenario,
                                                       failed):
    rep = HT.test_hub("lab", resolver=lab, run=fake_bin(scenario))
    assert rep.failed == failed and rep.failure().hint
    assert [s.step for s in rep.steps] == list(HT.STEPS[:HT.STEPS.index(failed) + 1])
    assert rep.code == int(HT.STEP_CODES[failed]) and lease_spy == []


def test_negative_twin_ssh_ok_passes_every_step_and_lists_the_targets(lab, fake_bin, lease_spy):
    rep = HT.test_hub("lab", resolver=lab, run=fake_bin("ok"))
    assert rep.ok and rep.code == 0 and [s.step for s in rep.steps] == list(HT.STEPS)
    assert {t["target"] for t in rep.targets} == {"mps3_01_pl", "kr260_01_ps", "kr260_01_pl"}
    assert rep.steps[-1].detail == "mps3_01_pl"            # the board using the hub
    assert lease_spy == []


def test_ssh_exit_255_permission_denied_is_auth_not_reach(lab, fake_bin):
    rep = HT.test_hub("lab", resolver=lab, run=fake_bin("auth"))
    reach, auth = rep.steps[1], rep.steps[2]
    assert reach.ok and not auth.ok and "Permission denied" in auth.detail
    assert "ssh-add" in auth.hint and rep.code == ExitCode.UNREACHABLE


def test_ssh_missing_fpga_group_says_the_admins_command(lab, fake_bin):
    rep = HT.test_hub("lab", resolver=lab, run=fake_bin("nogroup"))
    assert rep.failed == "group" and "usermod -aG fpga" in rep.failure().hint


def test_ssh_is_one_round_trip_with_pyverifys_quoting_a_timeout_and_the_jump(lab, fake_bin):
    HT.test_hub("lab", resolver=lab, run=fake_bin("ok"))
    lines = [json.loads(x) for x in fake_bin.log.read_text().splitlines()]
    ssh = [x["argv"] for x in lines if "argv" in x]
    hub_calls = [x for x in lines if "fpgahub" in x]
    assert len(ssh) == 1
    argv = ssh[0]
    assert "BatchMode=yes" in argv and "ConnectTimeout=10" in argv
    assert argv[argv.index("-J") + 1] == "bastion.invalid" and argv[-2] == HOST
    assert [x["fpgahub"] for x in hub_calls] == [["board", "list", "--json"]]  # no lease verb
    assert all(x["COLUMNS"] == "400" for x in hub_calls)                     # crossed 2 shells


def test_an_unknown_target_fails_at_target_with_the_offer(lab, fake_bin):
    rep = HT.test_hub("lab", resolver=lab, run=fake_bin("ok"), targets=["mps3_09_pl"])
    assert rep.failed == "target" and rep.code == ExitCode.ABSENT
    assert "mps3_01_pl" in rep.failure().hint


def test_a_hub_with_no_host_fails_at_config_without_running_anything(fake_bin):
    (state() / "settings.toml").write_text('[hubs.lab]\ngroup = "fpga"\n')
    rep = HT.test_hub("lab", resolver=H.load_resolver(), run=fake_bin("ok"))
    assert rep.failed == "config" and rep.code == ExitCode.USAGE
    assert not fake_bin.log.exists()
    rep = HT.test_hub("nosuch", resolver=H.load_resolver(), run=fake_bin("ok"))
    assert rep.failed == "config" and "no hub named" in rep.failure().detail


def test_ssh_target_details_read_fpgahub_target_show(lab, fake_bin):
    got = HT.target_details("lab", "mps3_01_pl", resolver=lab, run=fake_bin("ok"))
    assert got["network"]["board_ip"] == "192.168.10.101" and got["description"]
    lines = [json.loads(x) for x in fake_bin.log.read_text().splitlines()]
    assert [x["fpgahub"] for x in lines if "fpgahub" in x] == [["target", "show", "mps3_01_pl"]]
    from harness_manager.core.errors import AbsentError

    with pytest.raises(AbsentError):                          # twin: a target it lacks
        HT.target_details("lab", "mps3_09_pl", resolver=lab, run=fake_bin("ok"))


# --- FIX-PACK-2 item 3: the group is what `sg` says ---------------------------------------------


def test_a_stale_group_cache_passes_the_group_with_a_note(lab, fake_bin, lease_spy):
    """The lab hub: `id -Gn` misses fpga (stale sssd/nscd), `sg fpga` works: HM's way works."""
    rep = HT.test_hub("lab", resolver=lab, run=fake_bin("stalecache"))
    assert rep.ok and rep.code == 0 and [s.step for s in rep.steps] == list(HT.STEPS)
    group = next(s for s in rep.steps if s.step == "group")
    assert group.ok and group.detail == "`sg fpga` works"
    assert "`id -Gn`" in group.note and "stale" in group.note and "sg fpga" in group.note
    assert group.view()["note"] == group.note
    sg = [json.loads(x)["sg"] for x in fake_bin.log.read_text().splitlines() if '"sg"' in x]
    assert ["fpga", "-c", "true"] in sg                    # the check HM relies on ran
    assert lease_spy == []


def test_negative_twin_when_sg_fails_too_the_group_fails_with_the_admins_command(lab, fake_bin):
    rep = HT.test_hub("lab", resolver=lab, run=fake_bin("nogroup"))
    bad = rep.failure()
    assert rep.failed == "group" and "`sg fpga -c true` fails" in bad.detail
    assert "Invalid password" in bad.detail and "usermod -aG fpga" in bad.hint


def test_twin_id_lists_the_group_but_sg_refuses_it_is_a_failure_not_a_note(lab, fake_bin):
    rep = HT.test_hub("lab", resolver=lab, run=fake_bin("sgrefuses"))
    bad = rep.failure()
    assert rep.failed == "group" and bad.detail.startswith("`id -Gn` lists 'fpga', but")
    assert "log in again" in bad.hint and "usermod" not in bad.hint


def test_twin_a_healthy_hub_has_no_note(lab, fake_bin):
    rep = HT.test_hub("lab", resolver=lab, run=fake_bin("ok"))
    assert all(s.note == "" for s in rep.steps) and rep.ok


def test_an_answer_without_the_sg_marker_falls_back_to_id(lab):
    """A hub whose shell printed no sg verdict (the old line): `id -Gn` decides, as before."""
    def run(argv):
        out = f"{HT.LOGIN_MARK}\nme users\n{HT.IDS_MARK}\n{{\"groups\": []}}\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    rep = HT.test_hub("lab", resolver=lab, run=run)
    assert rep.failed == "group" and "is not in 'fpga'" in rep.failure().detail


def test_the_cli_prints_a_passed_steps_note():
    from harness_manager.cli import cmd_hubcfg

    line = cmd_hubcfg._step_line({"step": "group", "ok": True, "detail": "`sg fpga` works",
                                  "ms": 12, "note": "`id -Gn` ... stale"})
    assert "note: `id -Gn` ... stale" in line
    assert "note" not in cmd_hubcfg._step_line({"step": "reach", "ok": True, "detail": "x",
                                                "ms": 0, "note": ""})


# --- REST --------------------------------------------------------------------------------------


@pytest.fixture
def rest():
    with FakeFpgahub() as hub:
        (state() / "settings.toml").write_text(f'[hubs.remote]\nurl = "{hub.url}"\n')
        yield hub, H.load_resolver()


def test_rest_ok_uses_only_health_whoami_groups_and_never_shows_the_token(rest, lease_spy):
    hub, r = rest
    tok = hub.add_token("alice", "write")
    H.set_hub_token("remote", r, value=tok)
    rep = HT.test_hub("remote", resolver=r, targets=["mps3_01_pl"])
    assert rep.ok and [s.step for s in rep.steps] == ["config", "reach", "auth", "targets",
                                                      "target"]
    assert "alice@mapstone-dev (role write)" in rep.steps[2].detail
    routes = [f"{q['method']} {q['path']}" for q in hub.requests]
    assert routes == ["GET /api/v1/health", "GET /api/v1/whoami", "GET /api/v1/groups"]
    assert not hub.leases and not any(hub.queues.values()) and not hub.emitted("lease.")
    assert tok not in json.dumps(rep.view()) and lease_spy == []


def test_rest_no_token_fails_at_auth_with_the_way_to_set_it(rest):
    hub, r = rest
    rep = HT.test_hub("remote", resolver=r)
    assert rep.failed == "auth" and "hub token remote --stdin" in rep.failure().hint
    assert [f"{q['method']} {q['path']}" for q in hub.requests] == ["GET /api/v1/health"]


def test_rest_a_wrong_token_is_401_at_auth(rest):
    hub, r = rest
    H.set_hub_token("remote", r, value="not-a-real-token")
    rep = HT.test_hub("remote", resolver=r)
    assert rep.failed == "auth" and "401" in rep.failure().detail
    assert "hub token remote" in rep.failure().hint and rep.code == ExitCode.UNREACHABLE
    assert "not-a-real-token" not in json.dumps(rep.view())


def test_negative_twin_the_right_token_passes_auth(rest):
    hub, r = rest
    H.set_hub_token("remote", r, value=hub.add_token("bob", "read"))
    rep = HT.test_hub("remote", resolver=r)
    assert rep.ok and "a read token can see a lease, not take it" in rep.steps[2].detail


def test_rest_an_unknown_target_fails_at_target(rest):
    hub, r = rest
    H.set_hub_token("remote", r, value=hub.add_token("alice"))
    rep = HT.test_hub("remote", resolver=r, targets=["mps3_09_pl"])
    assert rep.failed == "target" and "kr260_01_ps" in rep.failure().hint


def test_rest_a_dead_port_fails_at_reach_quickly():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    (state() / "settings.toml").write_text(f'[hubs.remote]\nurl = "http://127.0.0.1:{dead}"\n')
    rep = HT.test_hub("remote", resolver=H.load_resolver(), rest_timeout_s=2)
    assert rep.failed == "reach" and rep.steps[-1].ms < 3000


def test_rest_plain_http_off_box_is_refused_at_config():
    (state() / "settings.toml").write_text('[hubs.remote]\nurl = "http://hub.invalid:7246"\n')
    rep = HT.test_hub("remote", resolver=H.load_resolver())
    assert rep.failed == "config" and "plain http" in rep.failure().detail


def test_rest_target_details_for_add_board(rest):
    hub, r = rest
    H.set_hub_token("remote", r, value=hub.add_token("alice"))
    got = HT.target_details("remote", "kr260_01_ps", resolver=r)
    assert got["network"]["board_ip"] == "192.168.20.101"
    out = H.add_board_for_target("remote", "kr260_01_ps", r, details=got)
    assert out["match"] == ["192.168.20.101"] and out["name"] == "KR260 PS"
    assert not hub.leases


def test_rest_a_token_in_an_unreachable_keyring_fails_at_auth_with_the_way_out(rest):
    from harness_manager.settings.secrets import KeyringBackend, SecretStore

    hub, r = rest
    r.secrets = SecretStore(state(), keyrings=[KeyringBackend("secret-service", env={})])
    r.secrets._write_index({"hubs.remote.token": {"backend": "secret-service"}})
    rep = HT.test_hub("remote", resolver=r)
    assert rep.failed == "auth" and "cannot reach" in rep.failure().detail
    assert rep.code == ExitCode.UNREACHABLE and "set the secret again" in rep.failure().hint
