"""REVIEW-W5 3: the hub-side MCC reader listens at least 3.5 s before its first CR.

The MCC's banner prints "Press Enter to stop auto boot..." and then waits ~3 s in SILENCE; a
CR inside that window STOPS the FPGA boot. 0.3 s of silence could not tell that window from
an idle MCC. The reader (``hub_mcc.HUB_MCC_READ_PY``) runs FOR REAL against a ``FakeMcc``
behind a pseudo-terminal (``PtyMcc``), as it would on the hub. Own file: lane SMALL-5 appends
to ``test_mcc_fix_hub.py``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest

from harness_manager_mps3 import hub_mcc
from harness_manager_mps3 import mcc as mccmod
from harness_manager_mps3.hub_mcc import HUB_MCC_READ_PY
from tests.fakes.fake_mcc import FakeMcc
from tests.fakes.hub_mcc_fakes import MCC_TTY, HubTool, PtyMcc
from tests.unit.test_mcc_fix_hub import hub_controller


def test_the_hub_reader_listens_at_least_3_5_s_and_its_floor_matches():
    import re as _re

    assert hub_mcc.READ_LISTEN_S >= 3.5 and mccmod.MCC_QUIET_BEFORE_S == 3.5
    floor = _re.search(r"^LISTEN_MIN_S = ([0-9.]+)$", HUB_MCC_READ_PY, _re.M)
    assert floor and float(floor.group(1)) == mccmod.MCC_QUIET_BEFORE_S
    tool = HubTool(mcc=FakeMcc())
    tool.others = [[4242, "cat " + MCC_TTY]]             # rc 3 at once: only the args matter
    hub_controller(tool).temperatures()
    assert tool.reader_runs[-1]["listen_s"] >= 3.5


def _window_read(listen_s: float | None) -> tuple[Any, dict[str, Any]]:
    """The hub reader, for real, against an MCC in its silent auto-boot window."""
    from tests.fakes.fake_mcc import SilentWindowMcc

    mcc = SilentWindowMcc()
    with PtyMcc(mcc) as pty:
        mcc.enter_window(3.0)
        tool = HubTool(pty=pty, python=sys.executable)
        if listen_s is None:
            ctl = hub_controller(tool)
            (temp,) = ctl.temperatures()
            return mcc, dict(ctl.last_info or {}, reason_text=temp.reason)
        args = {"tty": MCC_TTY, "baud": 115200, "pace": 0.1, "menu": "debug",
                "lines": ["CFG R TEMP 0"], "prompt_s": 3.0, "reply_s": 5.0, "listen_s": listen_s}
        res = tool(["sh", "-c", hub_mcc._py_pick(), HUB_MCC_READ_PY,
                    json.dumps(args, sort_keys=True)], timeout=60)
        return mcc, json.loads(res.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="no PTYs on this system")
def test_the_hub_reader_types_nothing_in_the_silent_autoboot_window():
    """REVIEW-W5 3: 0.3 s of silence could not tell the MCC's 3 s auto-boot window from an
    idle MCC, and the reader's CR STOPPED the boot. Now it listens 3.5 s: it hears the banner
    resume and refuses (rc 6, booting) with nothing typed."""
    mcc, info = _window_read(None)
    assert not mcc.autoboot_aborted and mcc.accepted_lines == [], info
    assert info["rc"] == 6 and info["heard"]


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="no PTYs on this system")
def test_twin_the_script_holds_its_floor_whatever_it_is_sent():
    """An old caller that still sends ``listen_s`` 0.3 gets 3.5 s all the same."""
    mcc, info = _window_read(0.3)
    assert not mcc.autoboot_aborted and mcc.accepted_lines == [], info
    assert info["rc"] == 6
