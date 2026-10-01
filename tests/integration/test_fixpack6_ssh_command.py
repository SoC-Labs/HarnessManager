"""FIX-PACK-6 item 2: ``board ssh TARGET -c CMD`` sends CMD as ONE remote command.

H1 (board 1, 1 Oct): ``-c 'uptime; … | grep …'`` was split into words before ssh ran, so ssh
received ``'uptime;' '(logread' … '|' …`` and the board's shell re-parsed the words with their
quoting gone. ssh joins its command arguments with spaces and the remote shell parses the
result, so the only faithful form is the one ``ssh host 'CMD'`` uses: one argument.

The board is pyverify's ``FakeShell(profile="linux")`` claimed through the real CLI with
``lc_fake_board_ssh.FakeBoardSsh``; ssh itself is a fake runner that records its argv
(``cmd_claim.run_ssh``). Each check has its negative twin.
"""

from __future__ import annotations

import io
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from pyverify.testing.fakeshell import FakeShell

from harness_manager.cli import cmd_claim
from harness_manager.cli.main import main
from harness_manager.core.errors import ExitCode
from harness_manager_mps3 import claim as CL
from tests.fakes.lc_fake_board_ssh import FakeBoardSsh, make_key_line, write_key_pair

BOARD_KEY = make_key_line("the-board")
#: What H1 ran, with the quoting a word split loses (the alternation inside single quotes).
H1_CMD = "uptime; (logread | grep -E 'harnessd|stage0' | tail -n 5)"


@pytest.fixture
def claimed(tmp_path, monkeypatch, capsys):
    fp = CL.fingerprint(BOARD_KEY.split()[1])
    shell = FakeShell("127.0.0.1", control_port=0, tftp_port=0, raw_tcp_port=0, uart0_port=0,
                      uart1_port=0, swo_port=0, identify_port=0, profile="linux",
                      ssh_host_key_sha256=fp).start()
    monkeypatch.setenv("HARNESS_MANAGER_MPS3_IDENTIFY_PORT", str(shell.identify_port))
    monkeypatch.setenv("HARNESS_MANAGER_MPS3_TFTP_PORT", str(shell.tftp_port))
    monkeypatch.setenv("HARNESS_MANAGER_NO_DAEMON", "1")
    monkeypatch.setattr(CL, "DEFAULT_RUN", FakeBoardSsh(shell, BOARD_KEY))
    monkeypatch.setattr(CL, "KEYS_SYNC_RETRIES_S", ())
    _private, public = write_key_pair(tmp_path / "keys", "id_test")
    target = f"127.0.0.1:{shell.control_port}"
    try:
        rc, _out, err = cli(capsys, monkeypatch, "board", "claim", target, "--key", str(public),
                            "--yes")
        assert rc == ExitCode.OK, err
        yield target
    finally:
        shell.stop()


def cli(capsys, monkeypatch, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


class RecordingSsh:
    """The fake ssh: records each argv, and what ssh would hand the board's shell (its
    command arguments joined with spaces, as OpenSSH does)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        return 0

    def remote_line(self, host: str = "127.0.0.1") -> str:
        argv = self.calls[-1]
        return " ".join(argv[argv.index(host) + 1:])


def test_the_command_is_one_argument_and_reaches_the_boards_shell_as_typed(claimed, capsys,
                                                                           monkeypatch):
    ssh = RecordingSsh()
    monkeypatch.setattr(cmd_claim, "run_ssh", ssh)
    rc, _out, err = cli(capsys, monkeypatch, "board", "ssh", claimed, "-c", H1_CMD)
    assert rc == ExitCode.OK, err
    argv = ssh.calls[-1]
    assert argv[-3:] == ["root", "127.0.0.1", H1_CMD], argv
    assert ssh.remote_line() == H1_CMD
    assert "-t" not in argv                         # a command: no tty asked for
    # the note on stderr is the line to paste: the command quoted as one word
    assert f"ssh: {shlex.join(argv)}" in err and shlex.split(shlex.join(argv))[-1] == H1_CMD


def test_twin_the_word_split_ssh_got_before_loses_the_quoting(claimed, capsys, monkeypatch):
    ssh = RecordingSsh()
    monkeypatch.setattr(cmd_claim, "run_ssh", ssh)
    # The pre-fix argv: shlex.split(CMD) after the host, as H1 saw it ('uptime;' '(logread' ...)
    split = shlex.split(H1_CMD)
    assert split[:2] == ["uptime;", "(logread"] and "|" in split
    assert " ".join(split) != H1_CMD                 # the board's shell got another line
    assert "'harnessd|stage0'" not in " ".join(split)
    # ... and no -c is the interactive shell, unchanged: a tty, nothing after the host
    rc, _out, err = cli(capsys, monkeypatch, "board", "ssh", claimed)
    assert rc == ExitCode.OK, err
    argv = ssh.calls[-1]
    assert argv[-2:] == ["root", "127.0.0.1"] and "-t" in argv


def test_print_shows_the_one_argument_and_json_keeps_it_whole(claimed, capsys, monkeypatch):
    import json

    rc, out, _err = cli(capsys, monkeypatch, "--json", "board", "ssh", claimed, "--print",
                        "-c", H1_CMD)
    assert rc == ExitCode.OK
    assert json.loads(out)["argv"][-1] == H1_CMD
    rc, out, _err = cli(capsys, monkeypatch, "board", "ssh", claimed, "--print", "-c", H1_CMD)
    assert out.rstrip().endswith(shlex.quote(H1_CMD))


def test_through_the_service_the_command_survives_the_round_trip(claimed, capsys, monkeypatch):
    """RemoteClaim joins the argv into ``?command=`` and the daemon splits it: one argument
    in, one argument out (the daemon's GET /boards/{bid}/ssh)."""
    from harness_manager.client.remote import RemoteEngine
    from harness_manager.core.services import EngineConfig
    from harness_manager.engine import Engine
    from tests.fakes.t13_daemon import LiveDaemon

    eng = Engine(EngineConfig(state_dir=Path(os.environ["HARNESS_MANAGER_STATE_DIR"])))
    try:
        with LiveDaemon(eng, write_json=False) as live:
            live.app.state.daemon.presence._stop.set()
            remote = RemoteEngine(live.base_url, live.token,
                                  state_dir=Path(os.environ["HARNESS_MANAGER_STATE_DIR"]))
            session = remote.open(remote.candidate_for(claimed))
            argv = remote.board_claim.ssh_argv(session, cmd_claim.remote_command(H1_CMD))
            assert argv[-1] == H1_CMD and argv[-2] == "127.0.0.1"
            # Twin: the pre-fix word list arrives as words (the daemon kept them apart)
            words = remote.board_claim.ssh_argv(session, shlex.split(H1_CMD))
            assert words[-len(shlex.split(H1_CMD)):] == shlex.split(H1_CMD)
            remote.close_all()
    finally:
        eng.close_all()


def test_on_windows_the_line_is_quoted_for_cmd_and_ssh_exe_gets_one_argument(monkeypatch):
    argv = ["ssh", "-l", "root", "192.168.10.101", *cmd_claim.remote_command(H1_CMD)]
    monkeypatch.setattr(cmd_claim.os, "name", "nt")
    line = cmd_claim.command_line(argv)
    assert line == subprocess.list2cmdline(argv)
    assert line.endswith(f'"{H1_CMD}"')               # one quoted argument for ssh.exe
    # Twin: on POSIX the same argv is the shell's quoting.
    monkeypatch.setattr(cmd_claim.os, "name", "posix")
    assert cmd_claim.command_line(argv) == shlex.join(argv)
    assert cmd_claim.remote_command("") == []
