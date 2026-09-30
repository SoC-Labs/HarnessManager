"""UI v2 G7 (lane UI2-API-HUB): bare metal advertised ``reset_shell`` (the ``reboot`` feature) but had no
``shell`` reset target, so "Restart shell" could never run there. Now a harness that restarts
itself with the ``reboot`` verb lists ``shell``, whatever its implementation. Each check has a
negative twin. FakeShell only: no board."""

from __future__ import annotations

import pytest
from pyverify.testing.fakeshell import FakeShell

from harness_manager.core.errors import HarnessError
from harness_manager_mps3.shell import Mps3Shell, ShellResets

BARE_WITH_REBOOT = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "stats", "log", "reboot")


def test_bare_metal_with_the_reboot_feature_restarts_the_shell_with_the_reboot_verb():
    with FakeShell.ephemeral(static_id=0x72BB0A36, features=BARE_WITH_REBOOT,
                             v011_verbs=True) as fs:
        resets = ShellResets(Mps3Shell(fs.host, fs.control_port, timeout=2))
        assert "shell" in resets.reset_targets() and "dut" in resets.reset_targets()
        resets.reset("shell")
        assert len(fs.reboots) == 1                       # the watchdog restart, no FPGA reload
        assert fs.resets == []                            # never sent as `reset shell`


def test_twin_bare_metal_without_the_feature_has_no_shell_target():
    with FakeShell.ephemeral(static_id=0x72BB0A36) as fs:
        resets = ShellResets(Mps3Shell(fs.host, fs.control_port, timeout=2))
        assert "shell" not in resets.reset_targets()
        with pytest.raises(HarnessError):
            resets.reset("shell")
        assert fs.reboots == []
