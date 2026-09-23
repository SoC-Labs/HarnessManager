"""T7 end to end (scenario S9): channel -> verify -> harness install on the virtual MPS3.

The board is ``VirtualMps3(usb=True)``: FakeShell (Ethernet), FakeMcc (REBOOT
with the real boot banner and a witness), FakeSdVolume (the config SD), opened
through the real Engine and MPS3 pack, so ``session.storage`` is T3's
``Mps3Storage`` (backup gate, journal, never an .ebf) and ``session.controller``
is T3's ``Mps3Controller`` (paced REBOOT, witnessed). ``bind_identity_to_sd``
makes the shell come back as whatever bitstream the SD now names.

The channel is served by ``FakeChannelServer`` on 127.0.0.1 and signed with the
test keys. The MCC runs on a fake clock (T3's pattern), so a reboot costs no
wall time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from socharness.core.errors import RefusedError
from socharness.core.services import EngineConfig
from socharness.engine import Engine
from socharness.services.update import (
    RESULT_INSTALLED,
    RESULT_RESTORED,
    RESULT_STORED,
    RESULT_WRITTEN,
    UpdateService,
)
from socharness_board_mps3 import mcc as mccmod
from socharness_board_mps3 import sd as sdmod
from socharness_board_mps3.overlays import OverlayCatalogue
from socharness_board_mps3.pack import Mps3Pack
from tests.fakes.fake_channel import ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t3_clock import FakeClock
from tests.fakes.t7_board import bind_identity_to_sd
from tests.fakes.t7_bundles import FIELDED_STATIC, NEW_STATIC, NEW_USERCODE, Release
from tests.fakes.virtual_board import VirtualMps3

KEYS = TestKeys()


@pytest.fixture
def vb(tmp_path):
    with VirtualMps3(tmp_path / "board", usb=True) as board:
        yield board


@pytest.fixture
def fake_time(vb, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
    monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
    vb.mcc.clock = clock
    vb.mcc.down_s, vb.mcc.boot_s, vb.mcc.autoboot_window_s = 1.0, 25.0, 3.0
    return clock


@pytest.fixture
def server(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        yield srv


@pytest.fixture
def world(vb, fake_time, server, tmp_path):
    eng = Engine(EngineConfig(state_dir=tmp_path / "state"),
                 packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
    session = eng.open(vb.candidate(usb=True), note="t7 update")
    events = []
    eng.bus.subscribe("update.*", events.append)
    svc = UpdateService(eng, trust=KEYS.trust(), token="", app_version="0.1.0")
    builder = ChannelBuilder(server.root, KEYS)
    try:
        yield {"eng": eng, "session": session, "svc": svc, "builder": builder, "vb": vb,
               "server": server, "events": events, "art": tmp_path / "art", "clock": fake_time}
    finally:
        eng.close_all()


def publish(w, *releases: Release, serial: int = 1, **kw) -> None:
    for i, r in enumerate(releases):
        r.add_to(w["builder"], w["art"], current=(i == len(releases) - 1), **kw)
    w["builder"].publish(serial=serial)


def plan(w, **kw):
    return w["svc"].plan_harness(w["session"], source=w["server"].source(), **kw)


# --- the good path ------------------------------------------------------------------------


def test_s9_good_harness_update_is_backed_up_installed_rebooted_and_confirmed(world):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb)
    publish(w, Release.fielded(), Release("1.1.0"))
    before = vb.sd.snapshot()
    p, verified = plan(w)
    assert p.running_release == "1.0.0" and p.base and not p.rekey and not p.blockers

    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)

    assert out.result == RESULT_INSTALLED, out.detail
    assert vb.reboots == 1 and vb.boots == 1
    # backup: taken before the write, verifiable, and it holds the OLD SD
    backup = Path(out.backup["path"])
    assert backup.is_file() and backup.with_name(backup.name + ".sha256").is_file()
    record = w["session"].storage.load_backup(backup)
    assert record.sha256 == out.backup["sha256"]
    # the SD now holds the new base; the MCC BIOS image was never touched
    after = vb.sd.snapshot()
    assert after["MB/HBI0309C/Nanosoc/nanosoc.bit"] != before["MB/HBI0309C/Nanosoc/nanosoc.bit"]
    assert after["MB/HBI0309C/mbb_v132.ebf"] == before["MB/HBI0309C/mbb_v132.ebf"]
    # the board itself says so, after a witnessed reboot
    assert "REBOOT witnessed" in out.evidence["summary"]
    assert out.evidence["shell_id_after"].lower() == FIELDED_STATIC
    ident = w["session"].identity()
    assert ident.harness_version == "1.1.0" and ident.firmware_sha == "c0ffee00"
    assert {c.name: c.check.value for c in out.checks}["harness version"] == "ok"
    # overlays went to the store, where the deploy catalogue finds them
    names = {r.name for r in OverlayCatalogue(store=w["eng"].store, use_env=False).refs()}
    assert {"synth", "synth2"} <= names
    topics = [e.topic for e in w["events"]]
    assert topics[0] == "update.started" and topics[-1] == "update.done"
    assert w["events"][-1].data["result"] == RESULT_INSTALLED
    assert any(e.data.get("phase", "").startswith("reboot:") for e in w["events"])
    # and a second look finds nothing to do
    again, _ = plan(w)
    assert again.up_to_date


def test_a_tampered_asset_is_refused_before_the_board_is_touched(world):
    w, vb = world, world["vb"]
    publish(w, Release("1.1.0"))
    p, verified = plan(w)
    sd_zip = next((w["server"].root / "assets").glob("*sd-HBI0309C.zip"))
    sd_zip.write_bytes(sd_zip.read_bytes()[:-8] + b"TAMPERED")
    before = vb.sd.snapshot()
    with pytest.raises(RefusedError, match="sha256"):
        w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert vb.sd.snapshot() == before and vb.reboots == 0
    assert not (w["svc"].state.backups(w["session"].candidate.board_id)).exists()
    assert w["events"][-1].topic == "update.failed"


# --- written, not running --------------------------------------------------------------


def test_written_but_not_running_offers_the_restore_and_the_restore_works(world):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb, stale=True)        # the board keeps booting the old image
    publish(w, Release.fielded(), Release("1.1.0"))
    before = vb.sd.snapshot()
    p, verified = plan(w)

    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)

    assert out.result == RESULT_WRITTEN and not out.ok
    assert "written, not running" in out.detail and "1.0.0" in out.detail
    assert out.backup["path"] in out.restore_hint and vb.reboots == 1
    assert w["events"][-1].data["result"] == RESULT_WRITTEN

    back = w["svc"].rollback_harness(w["session"])
    assert back.result == RESULT_RESTORED, back.detail
    assert vb.sd.snapshot() == before and vb.reboots == 2
    assert w["session"].identity().harness_version == "1.0.0"


# --- re-key ----------------------------------------------------------------------------------


def test_a_rekey_without_consent_is_refused_and_nothing_is_touched(world):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb)
    publish(w, Release("2.0.0", static_id=NEW_STATIC, usercode=NEW_USERCODE), rekey=True)
    before = vb.sd.snapshot()
    p, verified = plan(w)
    assert p.rekey and p.consent_phrase == f"REKEY {NEW_STATIC}"
    with pytest.raises(RefusedError, match="RE-KEYS"):
        p.approve()
    with pytest.raises(RefusedError, match="not approved"):   # no approval: the executor refuses
        w["svc"].install_harness(w["session"], p, None, verified)
    assert vb.sd.snapshot() == before and vb.reboots == 0


def test_a_rekey_with_typed_consent_installs_the_new_shell(world):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb)
    publish(w, Release("2.0.0", static_id=NEW_STATIC, usercode=NEW_USERCODE), rekey=True)
    p, verified = plan(w)
    out = w["svc"].install_harness(w["session"], p, p.approve(consent=f"REKEY {NEW_STATIC}"),
                                   verified)
    assert out.result == RESULT_INSTALLED, out.detail
    assert w["session"].identity().shell_id.lower() == NEW_STATIC


# --- overlay-only ------------------------------------------------------------------------------


def test_overlay_only_update_touches_neither_the_sd_nor_the_board(world):
    w, vb = world, world["vb"]
    publish(w, Release.fielded())               # the board already runs 1.0.0
    before = vb.sd.snapshot()
    p, verified = plan(w)
    assert p.mode == "overlays" and not p.base
    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert out.result == RESULT_STORED and sorted(out.stored) == ["overlays-open/synth",
                                                                  "overlays-open/synth2"]
    assert vb.sd.snapshot() == before and vb.reboots == 0
    stored = w["eng"].store.find("overlay", static_id=FIELDED_STATIC)
    assert len(stored) == 2
    again, _ = plan(w)
    assert again.up_to_date                     # the same overlays are not offered again


def test_overlays_only_flag_skips_the_base(world):
    w, vb = world, world["vb"]
    publish(w, Release.fielded(), Release("1.1.0"))
    p, verified = plan(w, overlays_only=True)
    assert p.mode == "overlays" and not p.base
    w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert vb.reboots == 0


# --- interrupted install -------------------------------------------------------------------


def test_an_interrupted_sd_write_is_refused_until_it_is_restored(world, monkeypatch):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb)
    publish(w, Release.fielded(), Release("1.1.0"))
    before = vb.sd.snapshot()
    real = sdmod._copy_stream

    def dies_on_the_bitstream(read, dest, tick):
        if dest.name == "nanosoc.bit":
            raise OSError(5, "I/O error (the USB cable was pulled)")
        return real(read, dest, tick)

    monkeypatch.setattr(sdmod, "_copy_stream", dies_on_the_bitstream)
    p, verified = plan(w)
    with pytest.raises(Exception, match="writing the config SD failed"):
        w["svc"].install_harness(w["session"], p, p.approve(), verified)
    monkeypatch.setattr(sdmod, "_copy_stream", real)
    assert w["session"].storage.pending() is not None
    assert vb.reboots == 0                                   # never reboot a half-written SD

    p2, verified = plan(w)
    with pytest.raises(RefusedError, match="rollback"):
        w["svc"].install_harness(w["session"], p2, p2.approve(), verified)

    back = w["svc"].rollback_harness(w["session"])
    assert back.result == RESULT_RESTORED, back.detail
    assert vb.sd.snapshot() == before and w["session"].storage.pending() is None

    p3, verified = plan(w)                                   # and now the update goes through
    out = w["svc"].install_harness(w["session"], p3, p3.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail


def test_an_up_to_date_board_has_nothing_to_install(world):
    w = world
    publish(w, Release.fielded(with_overlays=False))
    p, verified = plan(w)
    assert p.up_to_date
    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert out.result == "up-to-date" and w["vb"].reboots == 0


def test_check_is_read_only_and_announces_the_update(world):
    w, vb = world, world["vb"]
    publish(w, Release.fielded(), Release("1.1.0"))
    before = vb.sd.snapshot()
    report = w["svc"].check(source=w["server"].source(), session=w["session"])
    assert report["available"] and report["plan"]["version"] == "1.1.0"
    assert vb.sd.snapshot() == before and vb.reboots == 0
    assert [e.topic for e in w["events"]] == ["update.available"]


def test_rollback_without_any_backup_is_refused(world):
    with pytest.raises(RefusedError, match="no backup"):
        world["svc"].rollback_harness(world["session"])


def test_app_update_when_none_is_published(world):
    publish(world, Release.fielded())
    with pytest.raises(RefusedError, match="no app release"):
        world["svc"].update_app(source=world["server"].source())


# --- a journal left by an interrupted update (crash, Ctrl-C, pulled cable) -----------------


def _journal(w):
    from socharness.services.update.state import Journal

    return Journal(w["svc"].state, w["session"].candidate.board_id)


def test_a_journal_that_stopped_before_the_sd_write_is_dropped_safely(world):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb)
    publish(w, Release.fielded(), Release("1.1.0"))
    _journal(w).write(phase="backed-up", version="1.1.0", static_id=FIELDED_STATIC)
    p, verified = plan(w)
    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail
    assert any(e.data.get("phase") == "journal-dropped" for e in w["events"])
    assert _journal(w).read() is None


def test_a_journal_after_the_sd_write_is_recovered_when_the_board_runs_the_release(world):
    w, vb = world, world["vb"]
    bind_identity_to_sd(vb)
    publish(w, Release.fielded(), Release("1.1.0"))
    p, verified = plan(w)
    w["svc"].install_harness(w["session"], p, p.approve(), verified)
    # as if that process had died right after the reboot, before recording the result
    _journal(w).write(phase="rebooted", version="1.1.0", static_id=FIELDED_STATIC)
    p2, verified = plan(w)
    out = w["svc"].install_harness(w["session"], p2, p2.approve(), verified)
    assert out.result == "up-to-date" and _journal(w).read() is None
    from socharness.services.update.state import InstallRecords

    rec = InstallRecords(w["svc"].state).get(w["session"].candidate.board_id)
    assert rec["result"] == RESULT_INSTALLED and rec["detail"].startswith("recovered")


def test_a_journal_after_the_sd_write_is_refused_when_the_board_does_not_run_it(world):
    w, vb = world, world["vb"]
    publish(w, Release.fielded(), Release("1.1.0"))
    _journal(w).write(phase="written", version="1.1.0", static_id=FIELDED_STATIC,
                      backup={"path": "/somewhere/backup.zip", "sha256": "0" * 64})
    p, verified = plan(w)
    with pytest.raises(RefusedError, match="does not report it") as exc:
        w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert "update rollback" in exc.value.hint and "/somewhere/backup.zip" in exc.value.hint
    assert vb.reboots == 0


# --- the real transitions of 09-24: the fielded shell -> the ILA mint, then v0.11 -> v0.12 -----


def test_the_ila_mint_rekey_0x3f1a560f_to_its_real_static_with_consent(world):
    # The first harness update the lab board takes is a RE-KEY (T12's FIELDED_ILA_* profiles).
    from tests.fakes.t7_bundles import ILA_SHA, ILA_STATIC

    w, vb = world, world["vb"]
    bind_identity_to_sd(vb)
    publish(w, Release.fielded(), Release("1.0.1", static_id=ILA_STATIC, sha=ILA_SHA,
                                          usercode="0x5eed0001"), rekey=True)
    p, verified = plan(w)
    assert p.rekey and p.consent_phrase == f"REKEY {ILA_STATIC}"
    assert any("in the local store" in u or "built yourself" in u for u in p.unusable)
    out = w["svc"].install_harness(w["session"], p, p.approve(consent=p.consent_phrase), verified)
    assert out.result == RESULT_INSTALLED, out.detail
    assert w["session"].identity().shell_id.lower() == ILA_STATIC


@pytest.fixture
def ila_world(tmp_path, monkeypatch):
    """The ILA mint board with a firmware that reports its usercode (v0.12 on the wire)."""
    from tests.fakes.virtual_board import FIELDED_ILA_V011

    with VirtualMps3(tmp_path / "board", FIELDED_ILA_V011, usb=True) as board, \
            FakeChannelServer(tmp_path / "www") as srv:
        clock = FakeClock()
        monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
        monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
        board.mcc.clock = clock
        board.mcc.down_s, board.mcc.boot_s, board.mcc.autoboot_window_s = 1.0, 25.0, 3.0
        eng = Engine(EngineConfig(state_dir=tmp_path / "state"),
                     packs={"mps3": Mps3Pack(console_ports=board.console_ports)})
        try:
            yield {"eng": eng, "session": eng.open(board.candidate(usb=True)), "vb": board,
                   "svc": UpdateService(eng, trust=KEYS.trust(), token="", app_version="0.1.0"),
                   "builder": ChannelBuilder(srv.root, KEYS), "server": srv, "events": [],
                   "art": tmp_path / "art", "clock": clock}
        finally:
            eng.close_all()


def _ila_release(version: str, **kw) -> Release:
    from tests.fakes.t7_bundles import ILA_STATIC
    from tests.fakes.virtual_board import FIELDED_ILA_V011

    return Release(version, static_id=ILA_STATIC, usercode="0x5eed0001",
                   features=list(FIELDED_ILA_V011.features), **kw)


def test_a_usercode_reported_on_the_wire_confirms_the_install(ila_world):
    w = ila_world
    bind_identity_to_sd(w["vb"])
    running = _ila_release("1.0.0", sha=w["vb"].profile.harness_sha)
    publish(w, running, _ila_release("1.1.0", wire_usercode="0x5eed0001"))
    p, verified = plan(w)
    assert p.running_release == "1.0.0" and not p.rekey
    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert out.result == RESULT_INSTALLED, out.detail
    checks = {c.name: c.check.value for c in out.checks}
    assert checks["usercode"] == "ok" and w["session"].identity().usercode == "0x5eed0001"


def test_a_wrong_usercode_on_the_wire_is_written_not_running(ila_world):
    # Same static_id, other implementation: exactly what wiped the FPGA twice in July.
    w = ila_world
    bind_identity_to_sd(w["vb"])
    running = _ila_release("1.0.0", sha=w["vb"].profile.harness_sha)
    publish(w, running, _ila_release("1.1.0", wire_usercode="0x0badc0de"))
    p, verified = plan(w)
    out = w["svc"].install_harness(w["session"], p, p.approve(), verified)
    assert out.result == RESULT_WRITTEN and "usercode" in out.detail
