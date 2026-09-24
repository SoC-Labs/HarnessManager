"""Q3 install (lane Q2's findings): daemon.log has a size cap, and a read-only state dir
is a message.

- Q2's soak: daemon.log grew 0.5-0.6 MB an hour and nothing trimmed it. Now it rotates
  (``daemon.log`` -> ``.1`` -> ... -> ``.3``) at ``daemon start`` and, once a minute, in
  the running daemon, which points its stdout and stderr at the fresh file.
- ``daemon start`` on a state dir it cannot write printed "internal error:
  PermissionError" (exit 1). Now: "cannot start harness-manager-daemon: cannot write ...:
  it is not writable" and the next step (exit 6).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from harness_manager.core.errors import ActionFailedError, ExitCode
from harness_manager.daemon import control, logfile
from harness_manager.daemon.state import daemon_log_path


def _fill(path: Path, n: int, ch: str = "x") -> None:
    path.write_text(ch * n)


def test_rotate_shifts_the_backups_and_drops_the_oldest(tmp_path: Path):
    log = tmp_path / "daemon.log"
    for generation in "abcde":                 # five logs over the cap, three backups kept
        _fill(log, 101, generation)
        assert logfile.rotate(log, max_bytes=100, backups=3) is True
        assert not log.exists()
    assert [logfile.backup_path(log, n).read_text()[0] for n in (1, 2, 3)] == ["e", "d", "c"]
    assert not logfile.backup_path(log, 4).exists()


def test_negative_twin_a_log_under_the_cap_is_left_alone(tmp_path: Path):
    log = tmp_path / "daemon.log"
    _fill(log, 100)
    assert logfile.rotate(log, max_bytes=100) is False
    assert log.read_text() == "x" * 100 and not logfile.backup_path(log, 1).exists()
    assert logfile.rotate(tmp_path / "missing.log") is False


def test_daemon_start_rotates_an_oversized_log(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    log = daemon_log_path(state)
    _fill(log, logfile.LOG_MAX_BYTES + 1, "o")
    started: list[list[str]] = []

    class FakePopen:
        pid = 424242

        def __init__(self, cmd, **_kw):
            started.append(cmd)

    monkeypatch.setattr(control.subprocess, "Popen", FakePopen)
    control._spawn(state, 0, "127.0.0.1")
    assert started and "harness_manager.daemon" in started[0]
    assert logfile.backup_path(log, 1).stat().st_size == logfile.LOG_MAX_BYTES + 1
    assert log.read_text().startswith("--- harness-manager daemon start:")   # a fresh log


ROTATING_CHILD = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    from harness_manager.daemon import logfile

    log = Path(sys.argv[1])
    fd = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.dup2(fd, 1); os.dup2(fd, 2); os.close(fd)      # as `daemon start` redirects them
    print("old" * 100, flush=True)
    first = logfile.rotate_running(log, max_bytes=100, backups=2)
    print("new line on stdout", flush=True)
    sys.stderr.write("new line on stderr\\n"); sys.stderr.flush()
    again = logfile.rotate_running(log, max_bytes=100, backups=2)   # small now: no-op
    sys.stderr.write(f"{first} {again}\\n")
""")


@pytest.mark.skipif(os.name == "nt", reason="an open file cannot be renamed on Windows")
def test_the_running_daemon_rotates_its_own_stdout_and_stderr(tmp_path: Path):
    log = tmp_path / "daemon.log"
    subprocess.run([sys.executable, "-c", ROTATING_CHILD, str(log)], check=True, timeout=60)
    old = logfile.backup_path(log, 1).read_text()
    assert old.startswith("old") and "new line" not in old
    new = log.read_text()
    assert "new line on stdout" in new and "new line on stderr" in new
    assert new.rstrip().endswith("True False")


def test_negative_twin_a_foreground_daemon_writing_elsewhere_is_left_alone(tmp_path: Path):
    log = tmp_path / "daemon.log"
    _fill(log, 1000)
    # This process's stdout is not that file (pytest's capture, or a terminal).
    assert logfile.rotate_running(log, max_bytes=10) is False
    assert log.stat().st_size == 1000 and not logfile.backup_path(log, 1).exists()


@pytest.fixture
def read_only_dir(tmp_path: Path):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("needs POSIX permissions and a non-root user")
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    yield ro
    ro.chmod(0o755)


def test_daemon_start_on_a_read_only_state_dir_is_a_message(read_only_dir: Path):
    with pytest.raises(ActionFailedError) as got:
        control.start(read_only_dir / "state")
    assert got.value.code == ExitCode.ACTION_FAILED == 6
    assert "cannot start harness-manager-daemon: cannot write" in got.value.message
    assert "it is not writable" in got.value.message
    assert "HARNESS_MANAGER_STATE_DIR" in got.value.hint


def test_the_cli_says_it_with_exit_6_not_internal_error(read_only_dir: Path, monkeypatch,
                                                        capsys):
    from harness_manager.cli.main import main

    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(read_only_dir / "state"))
    rc = main(["--json", "daemon", "start"])
    out = capsys.readouterr().out
    assert rc == 6, out
    err = json.loads(out)["error"]
    assert "internal error" not in err["message"].lower()
    assert "it is not writable" in err["message"]
