"""SET-HUBS: the ``harness-manager hub`` verb (list, add, token, test, targets, adopt, remove).

In-process ``main([...])`` over a temporary config dir; the REST hub is T8's fake on
127.0.0.1, the SSH hub the fake ``ssh``/``sg``/``id``/``fpgahub`` executables first on PATH
(host names under ``.invalid``). Each check has a twin.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode
from harness_manager.settings import hubs as H
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


def cli(capsys, *argv: str, stdin: str | None = None, monkeypatch=None) -> tuple[int, str, str]:
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def fake_path(tmp_path, monkeypatch):
    bindir = fakebin.install(tmp_path / "bin")
    for k, v in fakebin.env_for(bindir, "ok", tmp_path / "fake.log").items():
        if k in ("PATH", "PYTHONPATH", fakebin.SCENARIO_ENV, fakebin.LOG_ENV):
            monkeypatch.setenv(k, v)
    monkeypatch.chdir(fakebin.REPO)
    return monkeypatch


def test_list_with_no_hub_says_none_is_needed(capsys):
    rc, out, _ = cli(capsys, "hub", "list")
    assert rc == 0 and "No hub." in out and "hub add NAME --ssh HOST" in out
    rc, out, _ = cli(capsys, "--json", "hub", "list")
    assert json.loads(out)["hubs"] == []


def test_negative_twin_list_shows_a_hub_and_an_inline_board(capsys):
    (state() / "settings.toml").write_text(f'[hubs.lab]\nhost = "{HOST}"\n')
    (state() / "boards.toml").write_text(
        '[boards.old]\nmatch = ["10.0.0.2"]\nhub = { host = "x.invalid", target = "t" }\n')
    rc, out, _ = cli(capsys, "hub", "list")
    assert rc == 0 and "No hub." not in out and f"lab            SSH   {HOST}" in out
    assert "hub adopt old" in out


def test_add_ssh_then_test_passes_through_the_fakes(capsys, fake_path):
    rc, out, _ = cli(capsys, "hub", "add", "lab", "--ssh", HOST, "--jump", "bastion.invalid",
                     "--lease-ttl", "20m")
    assert rc == 0 and "added hub lab: SSH hub.invalid via bastion.invalid" in out
    rc, out, _ = cli(capsys, "hub", "test", "lab", "--target", "mps3_01_pl")
    assert rc == 0 and "every step passed" in out and "ok   target" in out


def test_negative_twin_a_failed_ssh_test_exits_7_naming_the_step(capsys, fake_path):
    cli(capsys, "hub", "add", "lab", "--ssh", HOST)
    fake_path.setenv(fakebin.SCENARIO_ENV, "auth")
    rc, out, err = cli(capsys, "hub", "test", "lab")
    assert rc == ExitCode.UNREACHABLE and out == ""
    assert "stopped at auth" in err and "FAIL auth" in err and "ssh-add" in err
    rc, out, _ = cli(capsys, "--json", "hub", "test", "lab")
    body = json.loads(out)
    assert rc == 7 and body["error"]["data"]["report"]["failed"] == "auth"


def test_rest_add_with_token_on_stdin_test_targets_and_add_board(capsys, monkeypatch):
    with FakeFpgahub() as hub:
        tok = hub.add_token("alice", "write")
        rc, out, _ = cli(capsys, "hub", "add", "remote", "--url", hub.url, "--token-stdin",
                         stdin=tok + "\n", monkeypatch=monkeypatch)
        assert rc == 0 and "token: in a private file" in out and tok not in out
        assert tok not in (state() / "settings.toml").read_text()
        rc, out, _ = cli(capsys, "--json", "hub", "test", "remote")
        assert rc == 0 and json.loads(out)["ok"] and tok not in out
        rc, out, _ = cli(capsys, "hub", "targets", "remote")
        assert rc == 0 and "kr260_01_ps" in out and "--add mps3_01_pl" in out
        rc, out, _ = cli(capsys, "hub", "targets", "remote", "--add", "mps3_01_pl",
                         "--board", "lab")
        assert rc == 0 and "added boards.lab" in out and "192.168.10.101" in out
        rc, out, _ = cli(capsys, "hub", "targets", "remote")
        assert "used by lab" in out
        assert not hub.leases


def test_negative_twin_rest_with_a_wrong_token_exits_7_at_auth(capsys, monkeypatch):
    with FakeFpgahub() as hub:
        cli(capsys, "hub", "add", "remote", "--url", hub.url)
        rc, _, err = cli(capsys, "hub", "token", "remote", "--stdin", stdin="wrong\n",
                         monkeypatch=monkeypatch)
        assert rc == 0
        rc, out, err = cli(capsys, "hub", "test", "remote")
        assert rc == ExitCode.UNREACHABLE and out == "" and "stopped at auth" in err
        assert "401" in err and "hub token remote" in err


def test_adopt_makes_an_inline_table_a_hub_and_is_idempotent(capsys):
    text = (f'[boards.old]   # keep me\nmatch = ["10.0.0.2"]\nvia = "ssh:{HOST}"\n'
            f'hub = {{ host = "{HOST}", target = "mps3_01_pl" }}\n')
    (state() / "boards.toml").write_text(text)
    rc, out, _ = cli(capsys, "hub", "adopt", "old", "--as", "lab")
    assert rc == 0 and "now uses the hub lab (new)" in out and 'via is now "hub"' in out
    after = (state() / "boards.toml").read_text()
    assert "# keep me" in after and 'use = "lab"' in after
    rc, out, _ = cli(capsys, "hub", "adopt", "old", "--as", "lab")
    assert rc == 0 and "nothing to do" in out and (state() / "boards.toml").read_text() == after


def test_a_machine_hub_is_refused_15_but_its_token_is_the_users(capsys, tmp_path, monkeypatch):
    pol = tmp_path / "policy.toml"
    pol.write_text('[hubs.lab]\nurl = "https://hub.invalid:7246"\n')
    monkeypatch.setattr(H, "POLICY_PATH", pol)
    rc, _, err = cli(capsys, "hub", "add", "lab", "--url", "https://mine.invalid:7246",
                     "--update")
    assert rc == ExitCode.REFUSED and "machine hub" in err
    rc, _, _ = cli(capsys, "hub", "remove", "lab")
    assert rc == ExitCode.REFUSED
    rc, out, _ = cli(capsys, "hub", "token", "lab", "--stdin", stdin="mine\n",
                     monkeypatch=monkeypatch)
    assert rc == 0 and "token in a private file" in out
    rc, out, _ = cli(capsys, "hub", "list")
    assert "machine hub" in out


def test_remove_refused_while_used_then_forced(capsys):
    (state() / "settings.toml").write_text(f'[hubs.lab]\nhost = "{HOST}"\n')
    (state() / "boards.toml").write_text('[boards.b]\nhub = { use = "lab", target = "t" }\n')
    rc, _, err = cli(capsys, "hub", "remove", "lab")
    assert rc == ExitCode.USAGE and "used by b" in err
    rc, out, _ = cli(capsys, "hub", "remove", "lab", "--force")
    assert rc == 0 and "removed hub lab" in out and "b" in out
    rc, _, _ = cli(capsys, "hub", "remove", "lab")
    assert rc == ExitCode.ABSENT


def test_a_token_is_never_taken_from_argv(capsys):
    rc, _, err = cli(capsys, "hub", "token", "lab", "sekrit")
    assert rc == ExitCode.USAGE
    rc, _, _ = cli(capsys, "hub", "add", "x", "--url", "https://h.invalid", "--token", "t")
    assert rc == ExitCode.USAGE


# --- the output contract: --tsv rows are exactly the layout's width, --json one object ----------


def _tsv_ok(out: str, layout: str) -> bool:
    from harness_manager.cli.output import TSV_COLUMNS

    rows = [line.split("\t") for line in out.splitlines()]
    return bool(rows) and all(len(r) == len(TSV_COLUMNS[layout]) for r in rows)


def test_every_hub_layout_prints_rows_of_its_width_and_one_json_object(capsys, fake_path,
                                                                       monkeypatch):
    with FakeFpgahub() as hub:
        tok = hub.add_token("alice")
        rc, out, _ = cli(capsys, "--tsv", "hub", "add", "remote", "--url", hub.url,
                         "--token-stdin", stdin=tok + "\n", monkeypatch=monkeypatch)
        assert rc == 0 and _tsv_ok(out, "hub")
        cli(capsys, "hub", "add", "lab", "--ssh", HOST)
        for argv, layout in ((["hub", "list"], "hub"),
                             (["hub", "token", "remote", "--clear"], "hub"),
                             (["hub", "test", "lab"], "hub test"),
                             (["hub", "targets", "lab"], "hub targets"),
                             (["hub", "targets", "lab", "--add", "mps3_01_pl"], "hub change"),
                             (["hub", "adopt", "mps3_01_pl"], "hub change"),
                             (["hub", "remove", "remote"], "hub change")):
            rc, out, _ = cli(capsys, "--tsv", *argv)
            assert rc == 0 and _tsv_ok(out, layout), (argv, out)
        rc, out, _ = cli(capsys, "--json", "hub", "list")
        body = json.loads(out)
        assert rc == 0 and body["ok"] and {h["name"] for h in body["hubs"]} == {"lab"}


def test_negative_twin_a_failing_hub_verb_prints_nothing_on_stdout_with_tsv(capsys, fake_path):
    cli(capsys, "hub", "add", "lab", "--ssh", HOST)
    fake_path.setenv(fakebin.SCENARIO_ENV, "dns")
    rc, out, err = cli(capsys, "--tsv", "hub", "test", "lab")
    assert rc == ExitCode.UNREACHABLE and out == "" and "reach" in err
    rc, out, err = cli(capsys, "--tsv", "hub", "remove", "nosuch")
    assert rc == ExitCode.ABSENT and out == "" and err.startswith("harness-manager: ")
