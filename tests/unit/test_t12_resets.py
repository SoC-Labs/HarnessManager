"""T12: reset targets read from the harness (declared, then learned), not hard-coded."""

from __future__ import annotations

import pytest
from pyverify.testing.fakeshell import FakeShell

from socharness.core.errors import ActionFailedError, UsageError
from socharness_board_mps3.shell import Mps3Shell, ShellResets, make_reset_adapter
from tests.fakes.t12_harness_shell import HarnessFakeShell
from tests.fakes.t12_raw_servers import PING, VERSION_BARE, RawShell


def shell_for(fake) -> Mps3Shell:
    return Mps3Shell(fake.host, fake.control_port, timeout=2)


def test_v011_declares_dut_only():
    with FakeShell.ephemeral(reset_targets=("dut",), features=("windowed",)) as fake:
        assert tuple(ShellResets(shell_for(fake)).reset_targets()) == ("dut",)


def test_a_version_reset_targets_array_is_read():
    with HarnessFakeShell.ephemeral(reset_targets=("dut", "rp", "dbg"),
                                    version_extra={"reset_targets": ["dut", "rp", "dbg"]}) as fake:
        resets = ShellResets(shell_for(fake))
        assert tuple(resets.reset_targets()) == ("dut", "rp", "dbg")
        resets.reset("dbg")
        assert fake.resets == ["dbg"]


def test_a_feature_name_declares_a_target():
    fake = HarnessFakeShell.ephemeral(reset_targets=("dut", "rp"))
    fake.features = ("windowed", "reset_rp")      # a name no codec validates yet
    with fake:
        assert tuple(ShellResets(shell_for(fake)).reset_targets()) == ("dut", "rp")


def test_an_undeclared_vocabulary_target_is_tried_and_learned():
    """A v0.12 harness that accepts "rp" but does not declare it: the answer teaches us."""
    with FakeShell.ephemeral(reset_targets=("dut", "rp")) as fake:
        resets = ShellResets(shell_for(fake))
        assert "rp" not in resets.reset_targets()
        resets.reset("rp")
        assert fake.resets == ["rp"] and "rp" in resets.reset_targets()


def test_negative_twin_a_refused_target_is_usage_and_remembered():
    with FakeShell.ephemeral(reset_targets=("dut",)) as fake:
        resets = ShellResets(shell_for(fake))
        with pytest.raises(UsageError) as exc:
            resets.reset("rp")
        assert "targets: dut" in exc.value.hint and fake.resets == []
        with pytest.raises(UsageError):
            resets.reset("rp")                        # not sent again
        assert fake.resets == []


def test_negative_twin_an_unknown_word_is_never_sent():
    with FakeShell.ephemeral(reset_targets=("dut",)) as fake:
        with pytest.raises(UsageError):
            ShellResets(shell_for(fake)).reset("everything")
        assert fake.resets == []


def test_firmware_wording_bad_target_is_usage():
    """The real coordinator says "bad target" (coordinator.c), FakeShell says "unknown ..."."""
    with RawShell(replies={"ping": PING, "version": VERSION_BARE,
                           "reset": {"ok": False, "err": "bad target"}}) as srv:
        with pytest.raises(UsageError):
            Mps3Shell("127.0.0.1", srv.port, timeout=1).reset("rp")


def test_negative_twin_another_refusal_is_action_failed():
    with RawShell(replies={"reset": {"ok": False, "err": "decoupled"}}) as srv:
        with pytest.raises(ActionFailedError, match="decoupled"):
            Mps3Shell("127.0.0.1", srv.port, timeout=1).reset("dut")


def test_unreadable_harness_still_offers_dut():
    resets = ShellResets(Mps3Shell("127.0.0.1", 1, timeout=0.2))
    assert tuple(resets.reset_targets()) == ("dut",)


def test_reset_hook_needs_a_shell():
    class NoShell:
        shell = None

    assert make_reset_adapter(NoShell()) is None
