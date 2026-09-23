"""Hardware-in-the-loop, READ-ONLY tier. Skipped unless HARNESS_MANAGER_HIL=1.

Run only in a board window david has opened, with the lease held:

    HARNESS_MANAGER_HIL=1 HARNESS_MANAGER_HIL_SHELL=192.168.10.101 pytest -m hil tests/hil

The read-only tier never changes board state: ping, version and diag only.
Mutating tiers (deploy, reset, reboot) live in separate files and need
HARNESS_MANAGER_HIL_MUTATE=1 as well.
"""

from __future__ import annotations

import os

import pytest

from harness_manager.cli.main import main

pytestmark = [
    pytest.mark.hil,
    pytest.mark.skipif(os.environ.get("HARNESS_MANAGER_HIL") != "1", reason="needs HARNESS_MANAGER_HIL=1"),
]


def test_real_board_identity(capsys):
    target = os.environ.get("HARNESS_MANAGER_HIL_SHELL", "192.168.10.101")
    assert main(["--json", "info", target]) == 0
