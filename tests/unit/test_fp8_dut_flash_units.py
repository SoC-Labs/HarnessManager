"""FIX-PACK-8, unit: a design whose boot code writes the DUT's flash needs consent to program.

The rule is the core's (``core.pack.dut_flash_refusal``), the declaration the pack's (the MPS3
pack's ``WRITES_DUT_FLASH``: nanosoc_multicore's CPU1 boot ROM writes one byte at DUT flash
0x20000 onward on every boot, until Linux v2.1). The deploy service refuses it in its preflight
stage (exit 15, ``data.dut_flash_write``) unless ``allow_dut_flash_write``: nothing is pushed and
nobody's OpenOCD is stopped. Board-agnostic: a scripted adapter (``test_t2_deploy_service``) and
a made-up pack's declaration go through the same rule. Each check has its negative twin.
"""

from __future__ import annotations

import dataclasses

import pytest

from harness_manager.core.errors import ExitCode, IncompatibleError, RefusedError
from harness_manager.core.events import EventBus
from harness_manager.core.model import Check
from harness_manager.core.pack import (
    DutFlashWrite,
    OverlayRef,
    dut_flash_data,
    dut_flash_refusal,
    dut_flash_text,
)
from harness_manager.services.deploy import ITEM_SHELL_ID, DeployService
from harness_manager_mps3.constants import KNOWN_DESIGNS, WRITES_DUT_FLASH
from harness_manager_mps3.overlays import writes_dut_flash
from tests.unit.test_t2_deploy_service import NANOSOC, FakeSession, ScriptedAdapter, with_item

WHY = ("nanosoc_multicore's boot code writes the DUT's QSPI flash (one byte at 0x20000 onward) "
       "on every boot, until Linux v2.1: it damages a MicroPython image there (nanosoc_upy).")
TEXT = WHY + " Type MULTICORE to program it anyway."
MULTICORE = OverlayRef(name="nanosoc_multicore", rm_id="0x01000003", static_id="0x3f1a560f",
                       source="m", writes_dut_flash=DutFlashWrite(WHY, "MULTICORE"))


class Engine:
    def __init__(self) -> None:
        self.bus = EventBus()
        self.events: list = []
        self.bus.subscribe("*", self.events.append)


def rig(*refs: OverlayRef) -> tuple[DeployService, FakeSession, ScriptedAdapter, Engine]:
    adapter = ScriptedAdapter(refs=refs or (MULTICORE, NANOSOC))
    engine = Engine()
    return DeployService(engine), FakeSession(adapter), adapter, engine


# --- the pack's declaration ------------------------------------------------------------------------


def test_the_mps3_pack_declares_nanosoc_multicore_by_design_id_and_by_name():
    assert KNOWN_DESIGNS[0x0003] == "nanosoc_multicore" and set(WRITES_DUT_FLASH) == {0x0003}
    want = DutFlashWrite(WHY, "MULTICORE")
    assert writes_dut_flash("nanosoc_multicore", 0x01000003) == want
    assert writes_dut_flash("renamed", "0x01000003") == want          # the design id
    assert writes_dut_flash("nanosoc_multicore", 0x0100BEEF) == want  # the name
    assert writes_dut_flash("nanosoc_multicore", "not hex") == want


def test_twin_every_other_design_declares_nothing():
    for design, name in KNOWN_DESIGNS.items():
        if design != 0x0003:
            assert writes_dut_flash(name, 0x01000000 | design) is None, name
    assert writes_dut_flash("my_rm", 0x0100_8003) is None             # a user design id


# --- the core's words and refusal ---------------------------------------------------------------------


def test_the_warning_is_the_packs_why_and_its_word():
    assert dut_flash_text(MULTICORE) == TEXT
    assert dut_flash_data(MULTICORE) == {"design": "nanosoc_multicore", "why": WHY,
                                         "word": "MULTICORE"}
    err = dut_flash_refusal(MULTICORE, False)
    assert isinstance(err, RefusedError) and err.code == ExitCode.REFUSED == 15
    assert err.message == TEXT
    assert err.hint.startswith("nothing was programmed")
    assert err.data["dut_flash_write"]["word"] == "MULTICORE"
    assert err.data["overlay"] is MULTICORE


def test_twin_allowed_or_undeclared_is_no_refusal():
    assert dut_flash_refusal(MULTICORE, True) is None
    assert dut_flash_refusal(NANOSOC, False) is None
    assert dut_flash_text(NANOSOC) == "" and dut_flash_data(NANOSOC) is None


# --- the deploy service's preflight -----------------------------------------------------------------------


def test_the_deploy_preflight_refuses_without_consent_and_pushes_nothing():
    svc, s, adapter, engine = rig()
    with pytest.raises(RefusedError) as e:
        svc.deploy(s, MULTICORE)
    assert e.value.code == 15 and e.value.message == TEXT
    assert e.value.data["dut_flash_write"] == {"design": "nanosoc_multicore", "why": WHY,
                                               "word": "MULTICORE"}
    assert adapter.deployed == []
    assert [(ev.topic, ev.data.get("stage")) for ev in engine.events] == [
        ("deploy.failed", "preflight")]
    assert engine.events[0].data["reason"].startswith(TEXT)
    # keep_on_card or force change nothing: only the consent does
    with pytest.raises(RefusedError):
        svc.deploy(s, MULTICORE, force=True)
    assert adapter.deployed == []


def test_twin_with_consent_it_programs():
    svc, s, adapter, engine = rig()
    r = svc.deploy(s, MULTICORE, allow_dut_flash_write=True)
    assert r.verified and adapter.deployed == ["nanosoc_multicore"]
    assert engine.events[0].topic == "deploy.started"
    assert engine.events[-1].topic == "deploy.done"


def test_twin_a_design_that_declares_nothing_needs_no_consent():
    svc, s, adapter, _engine = rig()
    assert svc.deploy(s, NANOSOC).verified
    assert adapter.deployed == ["nanosoc"]


def test_a_failed_preflight_still_wins_over_the_consent_question():
    """A design built for another shell is refused as that (14), word or no word."""
    svc, s, adapter, _engine = rig()
    adapter.items["nanosoc_multicore"] = with_item(ITEM_SHELL_ID, Check.MISMATCH, "0xdeadbeef")
    for allow in (False, True):
        with pytest.raises(IncompatibleError):
            svc.deploy(s, MULTICORE, allow_dut_flash_write=allow)
    assert adapter.deployed == []


def test_board_agnostic_any_packs_declaration_goes_through_the_same_rule():
    other = dataclasses.replace(NANOSOC, name="flashy", rm_id="0x00010007",
                                writes_dut_flash=DutFlashWrite("flashy erases sector 0.", "ERASE"))
    svc, s, adapter, _engine = rig(other)
    with pytest.raises(RefusedError) as e:
        svc.deploy(s, other)
    assert e.value.message == "flashy erases sector 0. Type ERASE to program it anyway."
    assert svc.deploy(s, other, allow_dut_flash_write=True).verified
    assert adapter.deployed == ["flashy"]
