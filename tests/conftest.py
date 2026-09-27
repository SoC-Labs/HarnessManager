from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.virtual_board import VirtualMps3


@pytest.fixture(scope="session", autouse=True)
def _no_daemon_outlives_the_session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """A daemon (or OTA apply helper) that a test started must be gone when the session ends.
    One left running under this session's pytest tmp dir, or under an ``/tmp/otad-*``
    install this session made (``proc_sweep.track``), fails the run, naming it, and is then
    stopped. Only this session's own dirs are looked at: a daemon of another run, or the
    user's own, is never counted and never signalled."""
    from tests.fakes import proc_sweep

    base = str(tmp_path_factory.getbasetemp()).rstrip("/") + "/"
    yield
    markers = [base, *proc_sweep.session_markers()]
    left = proc_sweep.leaked_daemons(markers)
    if not left:
        return
    for marker in markers:
        proc_sweep.sweep(marker, only=proc_sweep.DAEMON_MODULE)
    pytest.fail("a daemon a test started outlived the session (stopped now):\n"
                + "\n".join(f"  pid {pid}: {cmd[:300]}" for pid, cmd in left), pytrace=False)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test touch the user's real ~/.config/harness-manager, nor the PTY links
    of a daemon the user runs on the same machine (/tmp/harness-manager-$USER): a PTY is
    only made on request, and the L2 tests pick their own directory (l2_rig.pty_dir), but
    a test that forgets must land here, not beside a live daemon's links."""
    monkeypatch.setenv("HARNESS_MANAGER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HARNESS_MANAGER_PTY_DIR", str(tmp_path / "ptys"))
    # OTA-C: an update channel fetched with no --source reads this (absent) local dir, never
    # the default GitHub repo; a test that wants a channel names its own source.
    monkeypatch.setenv("HARNESS_MANAGER_UPDATE_SOURCE", str(tmp_path / "no-update-source"))
    # Never run the machine's real Vivado (`vivado -version` from kit discovery): a test that
    # wants one points HARNESS_MANAGER_VIVADO at a fake (tests/fakes/kit_fakes.py).
    monkeypatch.setenv("HARNESS_MANAGER_VIVADO", "off")
    # SET-CORE: never reach the user's real OS keyring (the secret store falls back to its
    # 0600 file); a test that wants a keyring passes its own stub backend.
    monkeypatch.setenv("HARNESS_MANAGER_KEYRING", "off")


@pytest.fixture(autouse=True)
def _no_real_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test enumerate the test machine's real serial ports or volumes."""
    from harness_manager_mps3 import usb

    monkeypatch.setattr(usb, "DEFAULT_ENV", usb.UsbEnv(lambda: [], lambda: []))


@pytest.fixture
def vboard(tmp_path: Path) -> Iterator[VirtualMps3]:
    with VirtualMps3(tmp_path) as vb:
        yield vb


@pytest.fixture
def mps3_pack(vboard: VirtualMps3) -> Mps3Pack:
    """An MPS3 pack pointed at the virtual board's ephemeral ports."""
    return Mps3Pack(console_ports=vboard.console_ports,
                    push_port=vboard.shell.raw_tcp_port, tftp_port=vboard.shell.tftp_port)


def hil_enabled() -> bool:
    return os.environ.get("HARNESS_MANAGER_HIL") == "1"
