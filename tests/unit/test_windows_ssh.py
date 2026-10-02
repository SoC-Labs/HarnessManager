"""Lane WINDOWS: the OpenSSH client on Windows (core/sshcmd.py, the claim's board SSH, the
tunnels through a hub). Windows is pretended (``sys.platform``) only while an argv is BUILT;
nothing runs ssh.exe: the claim rig's fake ssh (tests/unit/test_linux_claim.py) and the
tunnel's fake launcher stand in. Each behaviour has its negative twin."""

from __future__ import annotations

import subprocess
import sys

import pytest

from harness_manager.core import sshcmd
from harness_manager_mps3 import tunnel as T
from tests.unit.test_linux_claim import claim_it, rig_factory  # noqa: F401 - the fixture

SSH_EXE = "C:\\Windows\\System32\\OpenSSH\\ssh.exe"


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: SSH_EXE if name == "ssh" else None)


def opts(argv):
    return {argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
            for i, a in enumerate(argv[:-1]) if a == "-o" and "=" in argv[i + 1]}


def test_ssh_is_the_full_path_of_ssh_exe_on_windows():
    assert sshcmd.ssh_program("win32", which=lambda n: SSH_EXE) == SSH_EXE
    # not on PATH: Windows' own OpenSSH folder, when it is there
    got = sshcmd.ssh_program("win32", which=lambda n: None, environ={"SystemRoot": "D:\\Win"},
                             exists=lambda p: p == "D:\\Win\\System32\\OpenSSH\\ssh.exe")
    assert got == "D:\\Win\\System32\\OpenSSH\\ssh.exe"


def test_twin_ssh_stays_ssh_off_windows_and_when_windows_has_none():
    assert sshcmd.ssh_program("linux", which=lambda n: "/usr/bin/ssh") == "ssh"
    assert sshcmd.ssh_program("win32", which=lambda n: None, environ={},
                              exists=lambda p: False) == "ssh"
    assert "Add-WindowsCapability -Online -Name OpenSSH.Client" in sshcmd.install_hint("win32")
    assert sshcmd.install_hint("linux") == "install the OpenSSH client"


@pytest.mark.parametrize(("path", "plat", "want"), [
    ("C:\\Users\\ann\\.config\\harness-manager\\ssh\\known_hosts.ab12", "win32",
     "C:/Users/ann/.config/harness-manager/ssh/known_hosts.ab12"),
    ("C:\\Users\\Ann Lee\\.config\\harness-manager\\ssh\\known_hosts.ab12", "win32",
     '"C:/Users/Ann Lee/.config/harness-manager/ssh/known_hosts.ab12"'),
    ("/home/ann/.config/harness-manager/ssh/known_hosts.ab12", "linux",
     "/home/ann/.config/harness-manager/ssh/known_hosts.ab12"),
    ("/Users/Ann Lee/.config/x", "darwin", '"/Users/Ann Lee/.config/x"'),
])
def test_a_path_in_an_ssh_option_survives_ssh_splitting_it(path, plat, want):
    assert sshcmd.option_path(path, plat) == want


def test_the_system_config_is_programdatas_on_windows():
    assert str(sshcmd.system_config("win32", {"ProgramData": "C:\\ProgramData"})) in (
        "C:\\ProgramData/ssh/ssh_config", "C:\\ProgramData\\ssh\\ssh_config")
    assert str(sshcmd.system_config("linux")) == "/etc/ssh/ssh_config"


def test_the_pinned_board_ssh_on_windows_runs_ssh_exe_with_no_control_master(
        rig_factory, request):  # noqa: F811
    rig = rig_factory()
    claim_it(rig)                             # the claim runs the rig's fake ssh, here
    request.getfixturevalue("on_windows")     # then the argv is built as on Windows
    argv = rig.claim.ssh_argv(["true"])
    o = opts(argv)
    assert argv[0] == SSH_EXE
    assert o["ControlPath"] == "none" and o["ControlMaster"] == "no"
    assert "ControlPersist" not in o
    assert o["IdentitiesOnly"] == "yes" and o["StrictHostKeyChecking"] == "yes"
    assert o["UserKnownHostsFile"] == sshcmd.option_path(o["UserKnownHostsFile"])
    assert "\\" not in o["UserKnownHostsFile"]


def test_twin_the_pinned_board_ssh_on_linux_is_unchanged(rig_factory):  # noqa: F811
    rig = rig_factory()
    claim_it(rig)
    assert rig.claim.ssh_argv(["true"])[0] == "ssh"


def test_a_tunnel_through_the_hub_on_windows_is_ssh_exe_with_proxyjump(on_windows):
    t = T.SshTunnel("192.168.10.101", [T.Forward("control", "127.0.0.1", 6900)],
                    ssh_g=lambda argv: "", jump="david@mapstone-dev", user="root",
                    restart=False)
    argv = t.build_argv()
    assert argv[0] == SSH_EXE
    assert argv[argv.index("-J") + 1] == "david@mapstone-dev"
    o = opts(argv)
    assert o["ControlPath"] == "none" and o["ControlMaster"] == "no"
    assert "ControlPersist" not in o and argv[-1] == "192.168.10.101"


def test_twin_a_tunnel_off_windows_keeps_plain_ssh():
    t = T.SshTunnel("hub", [T.Forward("control", "127.0.0.1", 6900)], ssh_g=lambda argv: "",
                    restart=False)
    assert t.build_argv()[0] == "ssh"


def test_the_filtered_config_includes_the_system_config_by_a_path_ssh_can_read(tmp_path):
    sysconf = tmp_path / "Program Data" / "ssh" / "ssh_config"
    sysconf.parent.mkdir(parents=True)
    sysconf.write_text("Host *\n")
    text = T.filtered_config_text("Host hub\n    LocalForward 1 x:2\n", system_config=sysconf)
    assert f'    Include "{sysconf}"' in text                       # quoted: it has a space
    assert "# [harness-manager: forward removed]     LocalForward 1 x:2" in text


def test_twin_no_system_config_no_include(tmp_path):
    text = T.filtered_config_text("Host hub\n", system_config=tmp_path / "none")
    assert "Include" not in text


def test_the_tunnels_ssh_starts_with_no_console_window_on_windows(monkeypatch):
    seen: dict = {}

    class FakePopen:
        pid = 4242
        stderr = None

        def __init__(self, argv, **kw):
            seen.update(kw)

        def poll(self):
            return None

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(T.subprocess, "Popen", FakePopen)
    T._PopenProcess(["ssh", "-N", "hub"])
    assert seen["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert seen["stdin"] is subprocess.DEVNULL
