"""L8 HUB-SD: what the window can and cannot do for a config SD behind a hub.

A hub board has no Debug USB, so ``session.storage`` stays None on purpose (a non-None storage
would make the update planner think a card reader is on this PC and skip the hub door). The
window says why in plain words; the hub door itself backs up and installs only inside an
update. No daemon, board or hub: the fake hub and a static read of the page's code.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager_mps3 import sd, sd_ab
from harness_manager_mps3.hub_sd import HubSdDoor
from tests.fakes.hubsd_fakes import FakeBoard, FakeHubHandle, FakeSdHub, HubSession, ssh_client
from tests.fakes.t3_clock import FakeClock

JS = Path(__file__).resolve().parents[2] / "src/harness_manager/web/static/js"


def _hub_session(tmp_path: Path) -> HubSession:
    clock = FakeClock(1_758_000_000.0)
    hub = FakeSdHub(clock)
    handle = FakeHubHandle(ssh_client(hub))
    door = HubSdDoor(handle, state_dir=tmp_path, clock=clock, sleep=clock.sleep)
    return HubSession(hub, FakeBoard(), door, handle)


def test_a_hub_board_has_no_usb_storage_but_has_the_hub_door(tmp_path):
    s = _hub_session(tmp_path)
    assert sd.make_storage_adapter(s) is None
    assert sd_ab.make_ab_storage_adapter(s) is None
    assert s.hub_sd is not None


def test_the_hub_door_backup_needs_the_running_release_and_says_so(tmp_path):
    s = _hub_session(tmp_path)
    with pytest.raises(RefusedError, match="none was prepared"):
        s.hub_sd.backup(tmp_path / "b")


def test_the_gate_says_what_the_hub_does_instead_of_no_adapter():
    src = (JS / "actions.js").read_text(encoding="utf-8")
    assert "export function hubSdReason" in src
    assert 'opts.adapter === "storage" && row.hub' in src
    assert "reached through the hub" in src and "Debug USB" in src
    # the old build-speak is still there for every other adapter
    assert "has no ${opts.adapter} adapter in this build" in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_reason_names_the_hub_and_is_plain(tmp_path):
    src = (JS / "actions.js").read_text(encoding="utf-8")
    m = re.search(r"export function hubSdReason.*?\n}\n", src, re.S)
    code = m.group(0).replace("export ", "") + "\nconsole.log(hubSdReason('lab-hub'));"
    f = tmp_path / "r.js"
    f.write_text(code, encoding="utf-8")
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, check=True).stdout
    assert "(lab-hub)" in out and "no adapter" not in out and "Versions" in out


def test_the_sd_page_falls_back_to_the_hub_reason():
    src = (JS / "sections/sd.js").read_text(encoding="utf-8")
    assert src.count("hubSdOut(bid)") == 3  # the definition and two uses
