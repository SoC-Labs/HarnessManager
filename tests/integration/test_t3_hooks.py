"""T3 end to end through the lead's pack hooks, on the virtual MPS3.

``VirtualMps3(usb=True)`` registers its FakeMcc as ``fake://…`` and its
FakeSdVolume as a directory; ``Mps3Pack.open`` then wires T3's
``mcc.make_controller_adapter`` and ``sd.make_storage_adapter`` in.

VirtualMps3 (lead-owned) only counts a REBOOT today. For the Ethernet witness
these tests chain FakeMcc's ``on_reboot``/``on_boot`` to stop and restart the
FakeShell, which is what the real board does (the fabric reloads from SD).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from socharness.core.model import LinkKind
from socharness.core.pack import ProbeHints
from socharness_board_mps3 import mcc as mccmod
from socharness_board_mps3.mcc import Mps3Controller
from socharness_board_mps3.pack import Mps3Pack
from socharness_board_mps3.sd import Mps3Storage
from tests.fakes.t3_clock import FakeClock
from tests.fakes.virtual_board import VirtualMps3


@pytest.fixture
def vb(tmp_path: Path):
    with VirtualMps3(tmp_path, usb=True) as board:
        yield board


@pytest.fixture
def fake_time(vb: VirtualMps3, monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """One fake clock for the driver and the board's MCC, with a realistic boot."""
    clock = FakeClock()
    monkeypatch.setattr(mccmod, "DEFAULT_CLOCK", clock)
    monkeypatch.setattr(mccmod, "DEFAULT_SLEEP", clock.sleep)
    vb.mcc.clock = clock
    vb.mcc.down_s, vb.mcc.boot_s, vb.mcc.autoboot_window_s = 1.0, 25.0, 3.0
    return clock


def open_session(vb: VirtualMps3, *, ethernet: bool = True):
    pack = Mps3Pack(console_ports=vb.console_ports)
    return pack.open(vb.candidate(ethernet=ethernet, usb=True))


def test_the_session_adapters_are_t3s_and_read_the_mcc_in_real_time(vb):
    session = open_session(vb)
    assert isinstance(session.controller, Mps3Controller)
    assert isinstance(session.storage, Mps3Storage)
    # Real clock, real 60 ms pacing, FakeMcc's real 50 ms drop rule.
    (temp,) = session.controller.temperatures()
    assert temp.available and temp.value == 35.5 and vb.mcc.dropped_chars == 0


def test_without_usb_links_there_is_no_controller_or_storage(vb):
    # Negative twin: an Ethernet-only session gets neither adapter.
    session = Mps3Pack(console_ports=vb.console_ports).open(vb.candidate(usb=False))
    assert session.controller is None and session.storage is None


def test_oscillators_through_the_session(vb, fake_time):
    oscs = open_session(vb).controller.oscillators()
    assert [r.value for r in oscs] == [25.0, 50.0, 50.0, 50.0, 24.576, 23.75]


def test_reboot_through_the_session_is_witnessed_by_the_shell(vb, fake_time):
    counted = vb.mcc.on_reboot
    vb.mcc.on_reboot = lambda: (counted(), vb.shell.stop())
    vb.mcc.on_boot = vb.shell.start
    session = open_session(vb)
    phases = []
    session.controller.reboot(progress=lambda p, d, t: phases.append(p), wait_s=120)
    assert phases == ["sent", "down", "up"] and vb.reboots == 1
    witness = session.controller.last_reboot
    assert witness.shell_id_after == "0x3f1a560f"
    assert session.identity().shell_id.lower() == "0x3f1a560f"      # the shell is back


def test_s6_reboot_over_ethernet_and_usb_is_witnessed(vb, fake_time):
    # S6 (lead, after the T3 merge): VirtualMps3 now models REBOOT. The shell goes away,
    # the MCC boots, and the shell comes back with the SD's boot design (greybox).
    vb.shell.current_rm_id = 0x01000001          # pretend nanosoc was swapped in
    session = open_session(vb)
    session.controller.reboot(wait_s=120)
    assert vb.reboots == 1 and vb.boots == 1
    assert session.identity().rm_id.lower() == "0x00000000"   # a reload from SD reverts to greybox


def test_mcc_answers_straight_after_an_ethernet_witnessed_reboot(vb, fake_time):
    # T7-6: the shell answers ping before the MCC has finished its banner. The reboot must
    # read the console to its prompt before returning, so the very next command works.
    session = open_session(vb)
    evidence = session.controller.reboot(wait_s=120)
    assert isinstance(evidence, dict) and evidence["shell_id_after"]
    assert "REBOOT witnessed" in evidence["summary"] and evidence["down_evidence"]
    assert session.controller.oscillators()          # no clock advance in between


def test_s6_no_op_reboot_is_not_reported_as_done(vb, fake_time):
    # Negative twin: an MCC that accepts REBOOT and does nothing (the old tty_01 trap).
    from socharness.core.errors import ActionFailedError

    vb.mcc.ignore_reboot = True
    with pytest.raises(ActionFailedError):
        open_session(vb).controller.reboot(wait_s=40)
    assert vb.reboots == 0


def test_usb_only_reboot_is_witnessed_on_the_console(vb, fake_time):
    session = open_session(vb, ethernet=False)
    session.controller.reboot(wait_s=120)
    assert session.controller.last_reboot.boot.fpga_configured and vb.reboots == 1
    assert not vb.mcc.autoboot_aborted


def test_storage_round_trip_through_the_session(vb, tmp_path):
    session = open_session(vb)
    before = vb.sd.snapshot()
    rec = session.storage.backup(tmp_path / "backups")
    new_bit = tmp_path / "new.bit"
    new_bit.write_bytes(b"\x5a" * 256)
    session.storage.install({"MB/HBI0309C/Nanosoc/nanosoc.bit": new_bit}, backup=rec)
    assert vb.sd.bit.read_bytes() == b"\x5a" * 256 and vb.sd.ebf.read_bytes() == b"MCCBIOS"
    session.storage.restore(rec)
    assert vb.sd.snapshot() == before


def test_probe_with_hints_pairs_the_usb_links_with_the_shell(vb):
    pack = Mps3Pack(console_ports=vb.console_ports)
    hints = ProbeHints(hosts=(vb.shell_endpoint,), serial_ports=(vb.mcc_url,),
                       volumes=(str(vb.sd.root),), scan_usb=False, timeout_s=1.0)
    (cand,) = pack.probe(hints)
    assert cand.board_id == f"mps3@{vb.shell_endpoint}"
    assert [lk.kind for lk in cand.links] == [LinkKind.ETHERNET, LinkKind.USB_SERIAL, LinkKind.USB_MSD]
    session = pack.open(cand)
    assert session.controller is not None and session.storage is not None
    assert session.storage.locate() == str(vb.sd.root)


def test_probe_with_two_shells_does_not_pair(vb, tmp_path):
    # Negative twin: two shells on Ethernet, one board on USB -> three candidates.
    with VirtualMps3(tmp_path / "second") as other:
        pack = Mps3Pack(console_ports=vb.console_ports)
        hints = ProbeHints(hosts=(vb.shell_endpoint, other.shell_endpoint),
                           serial_ports=(vb.mcc_url,), volumes=(str(vb.sd.root),),
                           scan_usb=False, timeout_s=1.0)
        cands = pack.probe(hints)
    assert len(cands) == 3
    usb = [c for c in cands if not any(lk.kind == LinkKind.ETHERNET for lk in c.links)]
    assert len(usb) == 1 and "firmware A0" in usb[0].evidence


def test_engine_telemetry_reads_the_mcc_through_t3(vb, fake_time, tmp_path):
    from socharness.core.services import EngineConfig
    from socharness.engine import Engine

    eng = Engine(EngineConfig(state_dir=tmp_path / "state"),
                 packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
    try:
        session = eng.open(vb.candidate(usb=True))
        by_name = {r.name: r for r in eng.telemetry.readings(session)}
        assert by_name["mcc_temp"].value == 35.5 and by_name["mcc_temp"].source == "mcc-console"
        assert by_name["osc0"].value == 25.0 and by_name["osc5"].value == 23.75
    finally:
        eng.close_all()
