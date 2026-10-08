# ruff: noqa: F811
"""HOSTKEY through the real Engine, CLI and daemon routes: `board repin` shows both keys and
asks, pins exactly the key it showed, and `board ssh` refuses a changed key in one message.
Same board as test_linux_claim_cli (FakeShell linux + FakeBoardSsh). Each check has a twin."""

from __future__ import annotations

from pathlib import Path

from harness_manager.core.errors import ExitCode
from harness_manager_mps3 import claim as CL
from tests.fakes.lc_fake_board_ssh import make_key_line
from tests.fakes.t13_daemon import headers
from tests.integration.test_linux_claim_cli import (  # noqa: F401  (fixtures)
    BOARD_KEY,
    cli,
    client,
    linux,
    state_dir,
    target,
    wait,
)

H = headers()
NEW_KEY = make_key_line("a-reimaged-board")
FP_OLD = CL.fingerprint(BOARD_KEY.split()[1])
FP_NEW = CL.fingerprint(NEW_KEY.split()[1])


def claimed(linux, capsys, monkeypatch):
    shell, ssh, public = linux
    rc, _, err = cli(capsys, monkeypatch, "board", "claim", target(shell), "--key", str(public),
                     "--yes")
    assert rc == ExitCode.OK, err
    return shell, ssh


def reimage(shell, ssh, key=NEW_KEY):
    shell.ssh_host_key_sha256 = CL.fingerprint(key.split()[1])
    ssh.host_key_line = key


def toml_text() -> str:
    return (state_dir() / "boards.toml").read_text()


def test_cli_repin_shows_both_keys_and_pins_exactly_the_key_shown(linux, capsys, monkeypatch):
    shell, ssh = claimed(linux, capsys, monkeypatch)
    reimage(shell, ssh)
    rc, _, err = cli(capsys, monkeypatch, "board", "repin", target(shell), stdin="y\n")
    assert rc == ExitCode.OK, err
    assert FP_OLD in err and FP_NEW in err and "pinned 20" in err       # the question: old, new, when
    assert NEW_KEY in toml_text() and BOARD_KEY not in toml_text()
    rc, out, _ = cli(capsys, monkeypatch, "--json", "board", "claim-status", target(shell))
    assert rc == ExitCode.OK and '"match": true' in out


def test_twin_cli_repin_not_confirmed_pins_nothing(linux, capsys, monkeypatch):
    shell, ssh = claimed(linux, capsys, monkeypatch)
    reimage(shell, ssh)
    before = toml_text()
    rc, _, err = cli(capsys, monkeypatch, "board", "repin", target(shell), stdin="n\n")
    assert rc == ExitCode.REFUSED and "not confirmed" in err and FP_NEW in err
    assert toml_text() == before


def test_cli_repin_yes_needs_the_exact_fingerprint_and_pins_only_that(linux, capsys, monkeypatch):
    shell, ssh = claimed(linux, capsys, monkeypatch)
    reimage(shell, ssh)
    before = toml_text()
    rc, _, err = cli(capsys, monkeypatch, "board", "repin", target(shell), "--yes")
    assert rc == ExitCode.USAGE and "--yes needs --fingerprint" in err
    other = "SHA256:" + "B" * 43                      # approved X, but the board shows Y
    rc, _, err = cli(capsys, monkeypatch, "board", "repin", target(shell), "--yes",
                     "--fingerprint", other)
    assert rc == ExitCode.REFUSED and "not the one you approved" in err
    assert toml_text() == before
    rc, _, err = cli(capsys, monkeypatch, "board", "repin", target(shell), "--yes",
                     "--fingerprint", FP_NEW)
    assert rc == ExitCode.OK, err
    assert NEW_KEY in toml_text()


def test_cli_repin_of_an_unchanged_key_has_nothing_to_do(linux, capsys, monkeypatch):
    shell, _ssh = claimed(linux, capsys, monkeypatch)
    rc, _, err = cli(capsys, monkeypatch, "board", "repin", target(shell), stdin="y\n")
    assert rc == ExitCode.USAGE and "has not changed" in err


def test_cli_board_ssh_refuses_a_changed_key_in_one_message(linux, capsys, monkeypatch):
    shell, ssh = claimed(linux, capsys, monkeypatch)
    reimage(shell, ssh)
    rc, _, err = cli(capsys, monkeypatch, "board", "ssh", target(shell), "--print")
    assert rc == ExitCode.REFUSED
    assert FP_OLD in err and FP_NEW in err and "pinned on 20" in err
    assert f"board repin TARGET --fingerprint {FP_NEW}" in err and "status 255" not in err


def test_twin_cli_board_ssh_with_the_pinned_key_prints(linux, capsys, monkeypatch):
    shell, _ssh = claimed(linux, capsys, monkeypatch)
    rc, out, _ = cli(capsys, monkeypatch, "board", "ssh", target(shell), "--print")
    assert rc == ExitCode.OK and "StrictHostKeyChecking=yes" in out


def test_api_repin_needs_confirm_and_a_fingerprint_then_pins_exactly_it(client, linux,
                                                                       capsys, monkeypatch):
    c, B = client
    shell, ssh, public = linux
    r = c.post(f"{B}/claim", json={"confirm": True, "key": str(public)}, headers=H)
    assert wait(c, r.json()["job"])["state"] == "done"
    reimage(shell, ssh)
    st = c.get(f"{B}/claim?refresh=true", headers=H).json()["claim"]["host_key"]
    assert st["match"] is False and st["reported"] == FP_NEW and st["pinned_at"]
    assert FP_OLD in st["refusal"]["message"] and FP_NEW in st["refusal"]["hint"]
    r = c.post(f"{B}/repin", json={"fingerprint": FP_NEW}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["name"] == "REFUSED"
    r = c.post(f"{B}/repin", json={"confirm": True}, headers=H)
    assert r.status_code == 409 and "exact fingerprint" in r.json()["error"]["message"]
    assert BOARD_KEY in toml_text()                       # nothing pinned yet
    r = c.post(f"{B}/repin", json={"confirm": True, "fingerprint": FP_NEW}, headers=H)
    assert r.status_code == 202, r.text
    done = wait(c, r.json()["job"])
    assert done["state"] == "done", done
    claim = done["result"]["claim"]
    assert claim["action"] == "repinned" and claim["host_key"]["pinned"] == FP_NEW
    assert NEW_KEY in toml_text()
    assert Path(state_dir()).exists()
