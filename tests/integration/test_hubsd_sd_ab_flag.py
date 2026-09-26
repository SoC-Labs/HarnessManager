"""HUB-SD U8 behind ``updates.sd_ab``: on, a local install flips the pointer; off, T7's in-place
path runs unchanged. The virtual MPS3 with its Debug USB (T7's world, fake clock); the
board boots whatever ``board.txt -> APPFILE -> F0FILE`` names (``bind_identity_to_sd``).
"""

from __future__ import annotations

import pytest

from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.update import RESULT_INSTALLED, RESULT_RESTORED, UpdateService
from harness_manager.services.update.service import sd_ab_setting
from harness_manager_mps3 import mcc as mccmod
from harness_manager_mps3.pack import Mps3Pack
from harness_manager_mps3.sd_ab import read_pointer
from tests.fakes.fake_channel import ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t7_board import bind_identity_to_sd
from tests.fakes.t7_bundles import Release
from tests.fakes.virtual_board import VirtualMps3

KEYS = TestKeys()
BIT = "MB/HBI0309C/Nanosoc/nanosoc.bit"


@pytest.fixture
def vb(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path / "board", usb=True) as board:
        clock = FakeClock()
        monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
        monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
        board.mcc.clock = clock
        board.mcc.down_s, board.mcc.boot_s, board.mcc.autoboot_window_s = 1.0, 25.0, 3.0
        yield board


def world(vb, tmp_path, *, sd_ab):
    server = FakeChannelServer(tmp_path / "www").__enter__()
    eng = Engine(EngineConfig(state_dir=tmp_path / "state"),
                 packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
    session = eng.open(vb.candidate(usb=True), note="sd_ab")
    svc = UpdateService(eng, trust=KEYS.trust(), token="", app_version="0.1.0", sd_ab=sd_ab)
    builder = ChannelBuilder(server.root, KEYS)
    Release.fielded().add_to(builder, tmp_path / "art", current=False)
    Release("1.1.0").add_to(builder, tmp_path / "art", current=True)
    builder.publish(serial=1)
    vb.sd.bit.write_bytes(Release.fielded().bit())      # the card holds what the board runs
    bind_identity_to_sd(vb)
    return eng, session, svc, server


def test_with_the_flag_on_an_install_flips_the_pointer_and_rollback_flips_it_back(vb, tmp_path):
    eng, session, svc, server = world(vb, tmp_path, sd_ab=True)
    try:
        before = vb.sd.snapshot()
        plan, verified = svc.plan_harness(session, source=server.source())
        out = svc.install_harness(session, plan, plan.approve(), verified)
        assert out.result == RESULT_INSTALLED, out.detail
        after = vb.sd.snapshot()
        assert after[BIT] == before[BIT]                          # the running image stayed
        assert read_pointer(vb.sd.root).image_name == "nanosoca.bit"
        assert session.identity().harness_version == "1.1.0"
        assert svc.installer("mps3").records.get(session.candidate.board_id)["via"] == "ab"
        back = svc.rollback_harness(session)                       # the flip back
        assert back.result == RESULT_RESTORED, back.detail
        assert read_pointer(vb.sd.root).image_name == "nanosoc.bit"
        assert session.identity().harness_version == "1.0.0"
        assert vb.sd.snapshot()[BIT] == before[BIT]
    finally:
        eng.close_all()
        server.__exit__(None, None, None)


def test_twin_with_the_flag_off_the_install_is_in_place(vb, tmp_path):
    eng, session, svc, server = world(vb, tmp_path, sd_ab=False)
    try:
        before = vb.sd.snapshot()
        plan, verified = svc.plan_harness(session, source=server.source())
        out = svc.install_harness(session, plan, plan.approve(), verified)
        assert out.result == RESULT_INSTALLED, out.detail
        after = vb.sd.snapshot()
        assert after[BIT] != before[BIT]                          # rewritten in place
        assert not any(k.endswith(("nanosoca.bit", "nanosocb.bit")) for k in after)
        assert read_pointer(vb.sd.root).image_name == "nanosoc.bit"
        assert "via" not in svc.installer("mps3").records.get(session.candidate.board_id)
    finally:
        eng.close_all()
        server.__exit__(None, None, None)


class _Resolved:
    def __init__(self, value):
        self.value = value


class _Resolver:
    def __init__(self, value):
        self.v = value

    def resolve(self, key):
        assert key == "updates.sd_ab"
        return _Resolved(self.v)


def test_the_setting_is_off_unless_it_is_exactly_true():
    assert sd_ab_setting(_Resolver(True)) is True


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_twin_anything_else_or_an_unreadable_setting_is_off(value):
    assert sd_ab_setting(_Resolver(value)) is False
    assert sd_ab_setting(object()) is False                       # no resolve(): off
