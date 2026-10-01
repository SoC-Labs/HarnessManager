"""RELEASE-PIPE: ``scripts/publish_harness_release.sh``, which uploads a BUILT harness release.

``gh`` on PATH is a recorder that fails every call: the dry run (the default) must never
run it at all, and no test here reaches GitHub. ``--publish`` needs a terminal and the typed
phrase; neither is given here except to the confirmation function itself, so nothing is ever
uploaded (and the key is never pinned in a test, so the checks would refuse it anyway).
"""

from __future__ import annotations

import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fakes.test_release_platform import linux_platform, release_args
from tools.release import signer as signer_mod
from tools.release.cli import main

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "publish_harness_release.sh"
REPO = "SoC-Labs/HarnessManager"
V = "2.0.0-rc1"


@pytest.fixture
def fake_gh(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 1\n')
    gh.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "log": log}


def script(env: dict, *args: str, stdin=subprocess.DEVNULL, **kw) -> subprocess.CompletedProcess:
    full = {**os.environ, "PATH": env["PATH"], "HM_PYTHON": sys.executable}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env=full, stdin=stdin, timeout=120, **kw)


def build(tmp_path: Path, *extra: str) -> Path:
    out = tmp_path / "out"
    lines: list[str] = []
    rc = main(release_args(linux_platform(tmp_path / "plat"), out, V, *extra),
              printer=lines.append)
    assert rc == 0, "\n".join(lines)
    return out


def test_the_dry_run_is_the_default_and_prints_every_gh_command_without_running_gh(
        tmp_path, fake_gh):
    out = build(tmp_path, "--test-key")
    res = script(fake_gh, "--repo", REPO, str(out))
    assert res.returncode == 0, res.stderr
    text = res.stdout
    assert f"gh release create mps3-harness-v{V} --repo {REPO}" in text
    assert f"gh release upload channel-mps3-harness-beta --repo {REPO} --clobber" in text
    assert text.index(f"release create mps3-harness-v{V}") < \
        text.index("release upload channel-mps3-harness-beta")     # assets before the channel
    assert "gh release download channel-mps3-harness-beta" in text   # the live check, shown
    assert "DRY RUN: nothing was uploaded" in text
    assert "a TEST release: --publish refuses it" in text
    assert f"harness list --source github:{REPO} --channel beta" in text
    assert not fake_gh["log"].exists()                              # gh never ran
    # the printed paths exist after the run: a command can be pasted
    notes = out / "plans" / "mps3-harness-beta" / "publish" / "notes.md"
    assert str(notes) in text and notes.read_text().startswith("TEST BUILD")


def test_twin_publish_without_a_terminal_is_refused_before_anything(tmp_path, fake_gh):
    out = build(tmp_path, "--test-key")
    res = script(fake_gh, "--publish", "--repo", REPO, str(out))
    assert res.returncode == 2 and "needs an interactive terminal" in res.stderr
    assert "nothing was uploaded" in res.stderr and not fake_gh["log"].exists()


def _on_a_terminal(env: dict, *args: str) -> tuple[int, str]:
    """Run the script with a pty as stdin/stdout (a terminal), typing nothing."""
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(["bash", str(SCRIPT), *args], stdin=slave, stdout=slave,
                                stderr=slave, close_fds=True,
                                env={**os.environ, "PATH": env["PATH"],
                                     "HM_PYTHON": sys.executable})
        os.close(slave)
        chunks = []
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        rc = proc.wait(timeout=120)
    finally:
        os.close(master)
    return rc, b"".join(chunks).decode("utf-8", "replace")


def test_twin_publish_on_a_terminal_refuses_a_test_release_at_the_checks(tmp_path, fake_gh):
    out = build(tmp_path, "--test-key")
    rc, text = _on_a_terminal(fake_gh, "--publish", "--repo", REPO, str(out))
    assert rc == 2 and "TEST release" in text and "nothing was uploaded" in text
    assert not fake_gh["log"].exists()


def test_twin_publish_on_a_terminal_refuses_an_unpinned_key(tmp_path, fake_gh):
    sk, _pk = signer_mod.keygen_throwaway(tmp_path / "keys", "release")
    out = build(tmp_path, "--key", str(sk), "--signer", "python")
    rc, text = _on_a_terminal(fake_gh, "--publish", "--repo", REPO, str(out))
    assert rc == 2 and "not pinned" in text and not fake_gh["log"].exists()


def test_the_typed_confirmation_takes_only_the_exact_phrase():
    phrase = f"PUBLISH mps3-harness {V} TO {REPO}"

    def confirm(typed: str) -> int:
        return subprocess.run(
            ["bash", "-c", f'. "{SCRIPT}"; confirm_typed "$1"', "bash", phrase],
            input=typed, capture_output=True, text=True, timeout=30).returncode

    assert confirm(phrase + "\n") == 0
    assert confirm(phrase.lower() + "\n") == 1
    assert confirm("y\n") == 1
    assert confirm("") == 1                                    # no answer is a no


def test_twin_a_tree_built_for_another_repo_is_refused(tmp_path, fake_gh):
    out = build(tmp_path, "--test-key")
    res = script(fake_gh, "--repo", "Someone/Else", str(out))
    assert res.returncode == 2 and "rebuild with --repo Someone/Else" in res.stdout + res.stderr
    assert not fake_gh["log"].exists()


def test_usage_errors(tmp_path, fake_gh):
    assert script(fake_gh, str(tmp_path)).returncode == 2                   # no --repo
    assert script(fake_gh, "--repo", REPO).returncode == 2                  # no tree
    assert script(fake_gh, "--repo", "noslash", str(tmp_path)).returncode == 2
    res = script(fake_gh, "--help")
    assert res.returncode == 0 and "--dry-run is the DEFAULT" in res.stdout
