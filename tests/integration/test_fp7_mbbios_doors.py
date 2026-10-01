"""FIX-PACK-7 (G8): MBBIOS is never changed by Harness Manager, through every door that writes
the config SD. Each check has its twin.

- **The update's Debug USB door** on ``VirtualMps3(usb=True)`` (T7's world: the real Engine,
  MPS3 pack, ``Mps3Storage`` on FakeSdVolume, the MCC's witnessed REBOOT): the card's line is
  kept, the bundle's never reaches the card; a card with no line but the ``.ebf`` the bundle
  names is refused (15) before anything is written, and ``allow_mcc_update`` overrides it.
- **The hub door** (HUB-SD's world: ``FakeSdHub`` as fpgahub): a release whose board.txt
  differs from the running one's only by MBBIOS goes through and the card keeps its line;
  one that differs anywhere else is still refused. The planner defers board.txt to the door.
- **The CLI and the API**: ``sd install --allow-mcc-update`` and the notes.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from harness_manager.core.errors import ExitCode, RefusedError
from harness_manager.services.update import RESULT_INSTALLED, hub_door
from tests.fakes.t7_bundles import Release
from tests.integration import test_hubsd_install as _hub
from tests.integration import test_t7_harness_update as _t7

# the fixtures: T7's world (Debug USB) and HUB-SD's channel server
vb, fake_time, server, world = _t7.vb, _t7.fake_time, _t7.server, _t7.world

BOARD = "MB/HBI0309C/board.txt"
CARD_LINE = b"MBBIOS: mbb_v132.ebf  ;MB BIOS IMAGE\n"
APP = b"APPFILE: Nanosoc\\nanosoc.txt\n"


def board_txt(mbbios: bytes = b"", app: bytes = APP) -> bytes:
    return b"BOARD: HBI0309C\n[MCCS]\n" + mbbios + b"[FPGAS]\n" + app


def new_release(mbbios: bytes, app: bytes = APP) -> Release:
    return Release("1.1.0", sd_extra={BOARD: board_txt(mbbios, app)})


def card_board(vb) -> bytes:
    return (vb.sd.root / "MB" / "HBI0309C" / "board.txt").read_bytes()


def set_card(vb, data: bytes) -> None:
    (vb.sd.root / "MB" / "HBI0309C" / "board.txt").write_bytes(data)


def mbbios_said(events) -> list[str]:
    return [e.data.get("text", "") for e in events
            if e.topic == "update.progress" and e.data.get("phase") == "sd:mbbios"]


# --- the Debug USB door --------------------------------------------------------------------------


def test_the_usb_door_keeps_the_cards_line_and_says_so(world):
    w, vb = world, world["vb"]
    _t7.bind_identity_to_sd(vb)
    set_card(vb, board_txt(CARD_LINE))
    _t7.publish(w, Release.fielded(), new_release(b"MBBIOS: mbb_v141.ebf\n"))
    p, verified = _t7.plan(w)
    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail
    assert card_board(vb) == board_txt(CARD_LINE)
    assert out.notes == ["MBBIOS kept: mbb_v132.ebf"] and out.as_dict()["notes"] == out.notes
    assert mbbios_said(w["events"]) == ["MBBIOS kept: mbb_v132.ebf"]


def test_twin_the_bundles_line_never_reaches_the_card(world):
    w, vb = world, world["vb"]
    _t7.bind_identity_to_sd(vb)
    set_card(vb, board_txt(CARD_LINE))
    _t7.publish(w, Release.fielded(), new_release(b"MBBIOS: mbb_v141.ebf\n",
                                                  app=b"APPFILE: Nanosoc\\nanosoc.txt\n"))
    p, verified = _t7.plan(w)
    w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert b"mbb_v141" not in card_board(vb)


def test_the_usb_door_refuses_an_mcc_update_before_writing_and_allow_overrides(world):
    w, vb = world, world["vb"]
    _t7.bind_identity_to_sd(vb)
    set_card(vb, board_txt())                              # no MBBIOS line on the card
    before = vb.sd.snapshot()
    _t7.publish(w, Release.fielded(), new_release(b"MBBIOS: mbb_v132.ebf\n"))  # on the card
    p, verified = _t7.plan(w)
    with pytest.raises(RefusedError) as exc:
        w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert exc.value.code == ExitCode.REFUSED
    assert exc.value.message == ("this card would make the MCC update itself to mbb_v132.ebf: "
                                 "remove mbb_v132.ebf from the card, or add --allow-mcc-update")
    assert vb.sd.snapshot() == before and vb.reboots == 0      # nothing written, no reboot
    # the override (and the journal the refusal left does not block it: nothing was written)
    p, verified = _t7.plan(w)
    out = w["svc"].install_harness(w["session"], p, p.approve(allow_mcc_update=True), verified)
    assert out.result == RESULT_INSTALLED, out.detail
    assert b"MBBIOS: mbb_v132.ebf" in card_board(vb)
    assert "allowed by --allow-mcc-update" in out.notes[0]


def test_twin_no_line_and_no_such_ebf_writes_the_bundles_line(world):
    w, vb = world, world["vb"]
    _t7.bind_identity_to_sd(vb)
    set_card(vb, board_txt())
    _t7.publish(w, Release.fielded(), new_release(b"MBBIOS: mbb_v141.ebf\n"))
    p, verified = _t7.plan(w)
    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail
    assert b"MBBIOS: mbb_v141.ebf" in card_board(vb)
    assert out.notes == ["MBBIOS: mbb_v141.ebf from the bundle (the card has no mbb_v141.ebf, "
                         "so the MCC will not update)"]


# --- the hub door ------------------------------------------------------------------------------------


def _hub_world(tmp_path, server, new_app: bytes = APP):
    old = dataclasses.replace(_hub.OLD, sd_extra={BOARD: board_txt(CARD_LINE)})
    new = dataclasses.replace(_hub.NEW, sd_extra={BOARD: board_txt(b"MBBIOS: mbb_v141.ebf\n",
                                                                     new_app)})
    return _hub.World(tmp_path, server, releases=(old, new))


def test_the_hub_door_keeps_the_cards_line_when_only_mbbios_differs(tmp_path, server):
    w = _hub_world(tmp_path, server)
    out, _plan = w.install()
    assert out.result == RESULT_INSTALLED, out.detail
    assert out.notes == ["MBBIOS kept: mbb_v132.ebf"]
    assert len(w.program_calls()) == 1                         # nanosoc.bit only: board.txt kept
    assert "MBBIOS kept: mbb_v132.ebf" in [e.data.get("text") for e in w.events
                                           if e.topic == "update.progress"]


def test_twin_the_hub_door_still_refuses_any_other_board_txt_change(tmp_path, server):
    w = _hub_world(tmp_path, server, new_app=b"APPFILE: Other\\other.txt\n")
    with pytest.raises(Exception, match=r"changes more of the config SD than \S*nanosoc.bit "
                                        r"\(MB/HBI0309C/board.txt\)"):
        w.install()
    assert w.program_calls() == []


def test_the_planner_defers_board_txt_to_the_hub_door():
    from tests.unit.test_hubsd_units import DOOR, NANOSOC_BIT, _plan, _rel

    door = {**DOOR, "deferred_paths": [BOARD]}
    old = _rel("1.0.0", {BOARD: "1" * 64, NANOSOC_BIT: "2" * 64})
    new = _rel("1.1.0", {BOARD: "9" * 64, NANOSOC_BIT: "3" * 64})
    plan = _plan()
    hub_door.apply(plan, new, None, SimpleNamespace(hub_sd=door, has_storage=False,
                                                    has_controller=True),
                   via=None, running=old, have_token=False)
    assert not plan.blockers and any("compares it after the download" in x for x in plan.warnings)
    # twin: without the door's deferral it is a blocker, as before
    plan = _plan()
    hub_door.apply(plan, new, None, SimpleNamespace(hub_sd=DOOR, has_storage=False,
                                                    has_controller=True),
                   via=None, running=old, have_token=False)
    assert any("board.txt" in b for b in plan.blockers)


# --- the CLI: sd install --allow-mcc-update, and the notes ----------------------------------------------


@pytest.fixture
def cli():
    from harness_manager.cli.engine import set_engine_factory
    from tests.fakes.t5_fake_engine import FakeEngine

    eng = FakeEngine()
    previous = set_engine_factory(lambda _args: eng)
    yield eng
    set_engine_factory(previous)


def _sd_install(cli, capsys, tmp_path, *extra: str) -> tuple[int, str]:
    from harness_manager.cli.main import main

    bundle = tmp_path / "bundle" / "MB" / "HBI0309C"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "board.txt").write_bytes(board_txt(b"MBBIOS: mbb_v141.ebf\n"))
    backup = cli.st.adapters["storage"].backup(tmp_path)
    rc = main(["--json", "sd", "127.0.0.1", "install", str(tmp_path / "bundle"),
               "--backup", backup.path, "--yes", *extra])
    return rc, capsys.readouterr()[0]


def test_cli_sd_install_passes_allow_mcc_update_and_prints_the_notes(cli, capsys, tmp_path):
    storage = cli.st.adapters["storage"]
    storage.notes_to_say = ["MBBIOS kept: mbb_v132.ebf"]
    rc, out = _sd_install(cli, capsys, tmp_path, "--allow-mcc-update")
    assert rc == 0, out
    assert json.loads(out)["notes"] == ["MBBIOS kept: mbb_v132.ebf"]
    assert storage.install_kw == {"allow_mcc_update": True}
    rc = __import__("harness_manager.cli.main", fromlist=["main"]).main(
        ["sd", "127.0.0.1", "install", str(tmp_path / "bundle"), "--backup",
         str(tmp_path / "sd-backup.zip"), "--yes"])
    assert rc == 0 and "MBBIOS kept: mbb_v132.ebf" in capsys.readouterr()[0].splitlines()


def test_twin_cli_sd_install_without_the_flag_sends_no_keyword(cli, capsys, tmp_path):
    rc, out = _sd_install(cli, capsys, tmp_path)
    assert rc == 0, out
    assert cli.st.adapters["storage"].install_kw == {}
    assert json.loads(out)["notes"] == []


# --- item 2: the board's "v1.3.2" is the release's "1.3.2" (no false MCC warning) ------------------


def _mcc_warnings(plan) -> list[str]:
    return [x for x in plan.warnings if x.startswith("MCC firmware")]


def _booted(w) -> None:
    """One witnessed REBOOT, so the plan knows the MCC's firmware from its boot banner
    ("ARM V2M-MPS3 Firmware v1.3.2")."""
    w["session"].controller.reboot()
    assert w["session"].controller.last_reboot.boot.firmware == "v1.3.2"


def test_a_board_on_v132_gets_no_mcc_warning_for_a_release_tested_on_132(world):
    w = world
    _booted(w)
    _t7.publish(w, Release.fielded(), Release("1.1.0"))         # mcc_fw_tested: ["1.3.2"]
    p, _ = _t7.plan(w)
    assert p.base and _mcc_warnings(p) == [], p.warnings


def test_twin_a_release_tested_on_another_mcc_still_warns(world):
    w = world
    _booted(w)
    Release.fielded().add_to(w["builder"], w["art"], current=False)
    Release("1.1.0").add_to(w["builder"], w["art"], current=True,
                            compat={"min_app": "0.0.1", "board_revs": ["HBI0309C"],
                                    "mcc_fw_tested": ["1.4.1"]})
    w["builder"].publish(serial=1)
    p, _ = _t7.plan(w)
    assert _mcc_warnings(p) == ["MCC firmware v1.3.2 was not tested with harness 1.1.0 "
                                "(tested: 1.4.1)"], p.warnings
