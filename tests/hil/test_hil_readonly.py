"""Hardware-in-the-loop, READ-ONLY tier. Skipped unless SOCHARNESS_HIL=1.

Run only in a board window david has opened, with the lease held:

    SOCHARNESS_HIL=1 SOCHARNESS_HIL_SHELL=192.168.10.101 pytest -m hil tests/hil

The read-only tier never changes board state: ping, version and diag only.
Mutating tiers (deploy, reset, reboot) live in separate files and need
SOCHARNESS_HIL_MUTATE=1 as well.
"""

from __future__ import annotations

import os

import pytest

from socharness.cli.main import main

pytestmark = [
    pytest.mark.hil,
    pytest.mark.skipif(os.environ.get("SOCHARNESS_HIL") != "1", reason="needs SOCHARNESS_HIL=1"),
]


def test_real_board_identity(capsys):
    target = os.environ.get("SOCHARNESS_HIL_SHELL", "192.168.10.101")
    assert main(["--json", "info", target]) == 0
