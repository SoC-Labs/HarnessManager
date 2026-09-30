"""UI2-API-BUILD G6: the update plan on a netbooted Linux board. With no user microSD holding
an OS slot, stage0 boots the image the hub serves over TFTP at every cold boot, so a release's
OS slot image cannot be written: the plan says so in those words (``NETBOOT_BLOCKER``), and
``os_boot`` says where the OS came from. Twins: a board whose slots are merely unusable keeps
the general text (with its reason), and a board booted from its card has no blocker."""

from __future__ import annotations

import pytest

from harness_manager.core.model import BoardIdentity
from harness_manager.services.update.channel import ChannelClient
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.planner import (
    NETBOOT_BLOCKER,
    OS_BOOT_CARD,
    OS_BOOT_NETBOOT,
    BoardView,
    make_plan,
    netboot_of,
    os_boot_of,
)
from harness_manager.services.update.state import UpdateState
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, TestKeys
from tests.fakes.s0lb_image import make_s0lb
from tests.fakes.t7_board import FakeOsSlots, StubSession
from tests.fakes.t7_bundles import FIELDED_STATIC, Release

KEYS = TestKeys()


@pytest.fixture
def channel(tmp_path):
    b = ChannelBuilder(tmp_path / "mirror", KEYS)
    rel = Release("1.2.0", impl="linux", with_sd=False, with_overlays=False)
    b.add_harness(rel.version, rel.identity(), [
        b.component("os-1.2.0", "user-usd", AssetFile("mps3-os-1.2.0.s0", make_s0lb()),
                    kind="os-slot")])
    b.publish(serial=1)
    state = UpdateState(tmp_path / "state" / "update")
    verified = ChannelClient(state, Downloader(state.cache), KEYS.trust()).fetch(
        "stable", str(b.root / "channel" / "stable"))
    return verified.channel, rel


def ident(rel) -> BoardIdentity:
    return BoardIdentity(board_type="mps3", shell_id=FIELDED_STATIC, harness_version="1.1.0",
                         harness_impl="linux", features=tuple(rel.features))


def test_a_netbooted_board_is_blocked_with_the_tftp_words(channel):
    ch, rel = channel
    view = BoardView(board_id="mps3@nb", pack="mps3", identity=ident(rel), has_os_slots=False,
                     os_boot=OS_BOOT_NETBOOT,
                     os_slots_reason="no user microSD card in the slot (the OS slots live on it)")
    plan = make_plan(ch, view, app_version="0.1.0")
    assert plan.os_slot and NETBOOT_BLOCKER in plan.blockers
    assert plan.blockers[0].startswith("the OS is the hub's TFTP image")
    assert plan.summary()["os_boot"] == "netboot"
    assert not any("offers no OS slot update" in b for b in plan.blockers)


def test_twin_unusable_slots_keep_the_general_words_with_the_reason(channel):
    ch, rel = channel
    view = BoardView(board_id="mps3@x", pack="mps3", identity=ident(rel), has_os_slots=False,
                     os_slots_reason="the harness did not answer: refused")
    plan = make_plan(ch, view, app_version="0.1.0")
    text = " ".join(plan.blockers)
    assert "offers no OS slot update" in text and "the harness did not answer" in text
    assert "TFTP" not in text and plan.summary()["os_boot"] == ""


def test_twin_a_board_booted_from_its_card_has_no_blocker(channel):
    ch, rel = channel
    view = BoardView(board_id="mps3@c", pack="mps3", identity=ident(rel), has_os_slots=True,
                     os_boot=OS_BOOT_CARD)
    plan = make_plan(ch, view, app_version="0.1.0")
    assert not plan.blockers and plan.summary()["os_boot"] == "card"
    # the fingerprint an approval binds is what it was: os_boot is not in it
    other = make_plan(ch, BoardView(board_id="mps3@c", pack="mps3", identity=ident(rel),
                                    has_os_slots=True), app_version="0.1.0")
    assert plan.fingerprint() == other.fingerprint()


def test_os_boot_of_the_running_slot():
    assert os_boot_of("A") == os_boot_of("B") == "card"
    assert os_boot_of("rescue") == os_boot_of("none") == "netboot"
    assert os_boot_of("unknown") == os_boot_of("") == ""


class _Reason:
    def __init__(self, reason=None, exc=None):
        self._r, self._e = reason, exc

    def slots_reason(self):
        if self._e is not None:
            raise self._e
        return self._r


def test_netboot_of_reads_the_adapters_reason():
    from harness_manager.core.errors import UnreachableError

    mps3 = "no user microSD card in the slot (the OS slots live on it)"
    assert netboot_of(_Reason(mps3)) == ("netboot", mps3)
    assert netboot_of(_Reason("no card in the USER microSD slot"))[0] == "netboot"   # the demo
    # twins: another reason, a reason that fails, no adapter
    assert netboot_of(_Reason("slot status refused: busy")) == ("", "slot status refused: busy")
    assert netboot_of(_Reason(exc=UnreachableError("no answer"))) == ("", "no answer")
    assert netboot_of(None) == ("", "")


def test_the_update_service_says_netboot_for_a_linux_board_with_no_card(channel):
    from harness_manager.services.update.service import UpdateService

    _ch, rel = channel

    class NoCard(FakeOsSlots):
        def slots_reason(self) -> str:
            return "no user microSD card in the slot (the OS slots live on it)"

    session = StubSession(ident(rel), os_slots=NoCard())
    svc = UpdateService.__new__(UpdateService)
    svc.os_slots_for = lambda s: None                 # unusable: no OS slot door
    svc.hub_door_view = lambda s: {}
    v = svc.board_view(session)
    assert v.os_boot == "netboot" and "no user microSD card" in v.os_slots_reason
    # twin: a card and its slots: booted from the card
    svc.os_slots_for = lambda s: s.os_slots
    card = svc.board_view(StubSession(ident(rel), os_slots=FakeOsSlots()))
    assert card.os_boot == "card" and card.os_slots_reason == ""
    # twin: bare metal is never called netboot
    bare = BoardIdentity(board_type="mps3", shell_id=FIELDED_STATIC, harness_impl="bare-metal")
    svc.os_slots_for = lambda s: None
    assert svc.board_view(StubSession(bare, os_slots=NoCard())).os_boot == ""
