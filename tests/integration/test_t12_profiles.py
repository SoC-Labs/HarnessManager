"""T12 end to end: Engine -> MPS3 pack -> pyverify -> the two harness generations.

- ``FIELDED_ILA_MINT_BAKE`` / ``FIELDED_ILA_V011``: the ILA mint (lab board from 09-24).
- ``LINUX_HARNESSD``: mps3-harnessd on the MicroBlaze V (mint 3, ~10-08 -> 10-12).

Each profile's claims are checked against the fielded ``0x3F1A560F`` as the
negative twin. Every byte crosses real sockets on 127.0.0.1.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest
from pyverify.client import ShellClient

from socharness.core import capabilities as C
from socharness.core.errors import ExitCode, IncompatibleError, UnreachableError
from socharness.core.model import Check
from socharness.core.pack import ProbeHints
from socharness.core.services import EngineConfig
from socharness.engine import Engine
from socharness.services.deploy import ITEM_CLEARING_FITS, ITEM_TRANSPORT
from socharness_board_mps3.constants import reboot_wait_s
from socharness_board_mps3.pack import Mps3Pack
from socharness_board_mps3.shell import ShellRescueError, ShellWedgedError
from tests.fakes.t2_overlays import (
    FIELDED_USERCODE,
    NON_WINDOWED,
    make_overlay,
    point_pushes_at,
    use_overlay_dirs,
)
from tests.fakes.t12_harness_shell import LINUX_OMITTED_DIAG_KEYS
from tests.fakes.virtual_board import (
    FIELDED_3F1A560F,
    FIELDED_ILA_MINT_BAKE,
    FIELDED_ILA_V011,
    LINUX_HARNESSD,
    VirtualMps3,
    linux_harnessd_profile,
)


def pack_for(vb: VirtualMps3) -> Mps3Pack:
    return Mps3Pack(console_ports=vb.console_ports)


def engine_for(vb: VirtualMps3, tmp_path: Path) -> Engine:
    return Engine(EngineConfig(state_dir=tmp_path / "state"), packs={"mps3": pack_for(vb)})


def info_of(vb: VirtualMps3, tmp_path: Path, candidate=None):
    eng = engine_for(vb, tmp_path)
    cand = candidate or vb.candidate()
    eng.open(cand, note="t12")
    try:
        return eng.info(cand.board_id)
    finally:
        eng.close(cand.board_id)


# -- the ILA mint -----------------------------------------------------------------------------


def test_ila_v011_identity_and_capabilities(tmp_path):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        info = info_of(vb, tmp_path)
    ident = info.identity
    assert int(ident.shell_id, 16) == FIELDED_ILA_V011.static_id != FIELDED_3F1A560F.static_id
    assert {"stats", "log", "reboot", "touch_cal", "windowed"} <= set(ident.features)
    assert ident.harness_impl == "bare-metal" and ident.build_check == Check.OK   # USRACC reads
    assert C.RESET_SHELL in info.capabilities                 # v0.11 `reboot`
    assert C.CONSOLE_SHELL not in info.capabilities           # Ethernet only, no SSH


def test_negative_twin_the_fielded_board_has_neither(tmp_path):
    with VirtualMps3(tmp_path, FIELDED_3F1A560F) as vb:
        info = info_of(vb, tmp_path)
    assert info.identity.build_check == Check.UNCHECKED and "stats" not in info.identity.features
    assert info.identity.harness_impl == "bare-metal" and C.RESET_SHELL not in info.capabilities


def test_ila_mint_bake_is_the_old_feature_set_with_a_readable_usracc(tmp_path):
    with VirtualMps3(tmp_path, FIELDED_ILA_MINT_BAKE) as vb:
        ident = pack_for(vb).open(vb.candidate()).identity()
    assert ident.features == ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed")
    assert ident.build_check == Check.OK and ident.shell_id == f"0x{vb.profile.static_id:08x}"


def test_placeholder_static_ids_are_flagged_and_overridable(monkeypatch):
    assert not FIELDED_ILA_V011.static_id_final and not LINUX_HARNESSD.static_id_final
    monkeypatch.setenv("SOCHARNESS_T12_LINUX_STATIC_ID", "0x2B082E1B")
    p = linux_harnessd_profile()
    assert (p.static_id, p.static_id_final) == (0x2B082E1B, True)


# -- the Linux harness: identity, health, timings -----------------------------------------------


def test_linux_identity_health_and_reboot_budget(tmp_path):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        session = pack_for(vb).open(vb.candidate())
        ident = session.identity()
        health = session.health()
    assert ident.harness_impl == "linux" and "windowed" not in ident.features
    assert reboot_wait_s(ident) == 180.0
    assert health.control_channel == "idle"
    assert not set(LINUX_OMITTED_DIAG_KEYS) & set(health.counters)    # absent, not 0
    assert "icap_bytes" in health.counters


def test_negative_twin_bare_metal_budget_and_full_counters(tmp_path):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        session = pack_for(vb).open(vb.candidate())
        assert reboot_wait_s(session.identity()) == 120.0
        assert set(LINUX_OMITTED_DIAG_KEYS) <= set(session.health().counters)


def test_shell_console_over_ssh_lights_up_on_linux(tmp_path):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        with_ssh = info_of(vb, tmp_path, vb.candidate(ssh=True))
        without = info_of(vb, tmp_path / "b", vb.candidate())
    assert C.CONSOLE_SHELL in with_ssh.capabilities
    assert C.CONSOLE_SHELL not in without.capabilities
    assert "SSH" in without.unavailable[C.CONSOLE_SHELL]


def test_hung_harnessd_is_wedged_not_offline(tmp_path):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        session = pack_for(vb).open(vb.candidate())
        session.shell.timeout = 0.3
        vb.hang()
        h = session.health()
        with pytest.raises(ShellWedgedError) as exc:
            session.identity()
        vb.unhang()
        assert session.health().control_channel == "idle"      # it recovers
    assert (h.reachable, h.control_channel) == (True, "wedged")
    assert exc.value.code == ExitCode.UNREACHABLE


def test_dead_harnessd_is_offline_and_identify_says_why(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        monkeypatch.setenv("SOCHARNESS_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        session = pack_for(vb).open(vb.candidate())
        vb.kill_harness(keep_identify=True)
        told = session.health()
        vb.revive()
        vb.kill_harness()
        bare = session.health()
    assert told.control_channel == "offline" and "harness service" in told.notes[0]
    assert (bare.reachable, bare.control_channel) == (False, "offline")
    assert "harness service" not in bare.notes[0]


def test_ebusy_does_not_break_info(tmp_path, monkeypatch):
    """A3's busy line: info still shows the board (identify), and health says busy."""
    with VirtualMps3(tmp_path, LINUX_HARNESSD, unit="dna-0a0b0c0d") as vb:
        monkeypatch.setenv("SOCHARNESS_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        eng = engine_for(vb, tmp_path)
        cand = vb.candidate()
        eng.open(cand)
        vb.set_busy()
        info = eng.info(cand.board_id)
        eng.close(cand.board_id)
    assert info.identity.harness_impl == "linux" and info.identity.unit_id == "dna-0a0b0c0d"
    assert info.health.control_channel == "busy"


# -- rescue -----------------------------------------------------------------------------------


def test_rescue_board_is_found_by_probe_and_explained(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        endpoint = vb.shell_endpoint
        vb.enter_rescue("slot A and B failed CRC")
        monkeypatch.setenv("SOCHARNESS_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        (cand,) = pack_for(vb).probe(ProbeHints(hosts=(endpoint,), scan_usb=False,
                                                timeout_s=0.5))
        session = pack_for(vb).open(cand)
        health = session.health()
        # The shell itself still raises (no 6900 in rescue)...
        with pytest.raises(ShellRescueError) as exc:
            session.shell.identity()
        # ...but the session reports what stage0 said (lead CCR T12-2), so info never fails.
        session_ident = session.identity()
    assert "RESCUE" in cand.evidence and "slot A and B failed CRC" in cand.evidence
    assert cand.identity.shell_id == f"0x{LINUX_HARNESSD.static_id:08x}"
    assert cand.identity.harness_impl == "" and "rescue" in cand.links[0].detail
    assert (health.control_channel, health.reachable) == ("rescue", True)
    assert exc.value.identity == cand.identity
    assert session_ident == cand.identity


def test_negative_twin_a_running_board_is_found_by_ping_not_identify(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        monkeypatch.setenv("SOCHARNESS_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        (cand,) = pack_for(vb).probe(ProbeHints(hosts=(vb.shell_endpoint,), scan_usb=False))
        asked = len(vb.shell.identify_requests)
    assert cand.evidence == "answered ping" and cand.identity.harness_impl == "linux"
    assert asked == 0


def test_probe_keys_a_board_by_its_unit(tmp_path, monkeypatch):
    with VirtualMps3(tmp_path, LINUX_HARNESSD, unit="dna-feedface") as vb:
        endpoint = vb.shell_endpoint
        monkeypatch.setenv("SOCHARNESS_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        vb.kill_harness(keep_identify=True)
        (cand,) = pack_for(vb).probe(ProbeHints(hosts=(endpoint,), scan_usb=False,
                                                timeout_s=0.5))
    assert cand.board_id == "mps3@dna-feedface" and cand.links[0].address == endpoint


# -- the reboot verb: a long outage on Linux ----------------------------------------------------


def raw(port: int, op: dict) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(json.dumps(op).encode() + b"\n")
        return json.loads(s.makefile().readline())


def test_linux_reboot_is_an_outage_then_up_ms_restarts(tmp_path):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        session = pack_for(vb).open(vb.candidate())
        port = vb.shell.control_port
        time.sleep(0.2)
        before = raw(port, {"op": "stats"})["up_ms"]
        assert raw(port, {"op": "reboot"})["ok"]
        time.sleep(0.3)
        assert session.health().control_channel == "offline"   # the OS is booting
        deadline = time.monotonic() + 5
        while session.health().control_channel != "idle" and time.monotonic() < deadline:
            time.sleep(0.1)
        after = raw(port, {"op": "stats"})
    assert after["up_ms"] < before and "os_up_ms" in after


# -- deploy ---------------------------------------------------------------------------------------


@pytest.fixture
def overlays(tmp_path, monkeypatch):
    root = tmp_path / "overlays"
    for prof in (LINUX_HARNESSD, FIELDED_ILA_V011):
        make_overlay(root / prof.name, "synth", static_id=prof.static_id,
                     static_usercode=FIELDED_USERCODE)
    make_overlay(root / "big", "bigclear", static_id=LINUX_HARNESSD.static_id,
                 clearing=b"\x00\x11\x22\x33" * 80_000)          # 320000 B > 262144
    make_overlay(root / "fielded", "synth_fielded", static_usercode=FIELDED_USERCODE)
    use_overlay_dirs(monkeypatch, root / LINUX_HARNESSD.name, root / FIELDED_ILA_V011.name,
                     root / "big", root / "fielded")
    return root


def deploy_synth(vb, monkeypatch, name="synth", candidate=None):
    point_pushes_at(monkeypatch, vb)
    session = pack_for(vb).open(candidate or vb.candidate())
    ref = next(r for r in session.deploy.overlays()
               if r.name == name and int(r.static_id, 16) == vb.profile.static_id)
    items = {i.name: i for i in session.deploy.preflight(ref)}
    return session, ref, items


def test_linux_deploy_uses_a_plain_tcp_push(tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        session, ref, items = deploy_synth(vb, monkeypatch)
        assert items[ITEM_TRANSPORT].detail.startswith("tcp: mps3-harnessd")
        result = session.deploy.deploy(ref)
        pusher = session.deploy.last_pusher
        assert result.verified and result.transport == "tcp"
        assert (pusher.transport, pusher.windowed) == ("tcp", False)
        assert [e.transport for e in vb.shell.push_events] == ["tcp", "tcp"]
        assert vb.shell.swaps[-1]["src"] == "tcp"


def test_negative_twin_ila_v011_deploy_stays_windowed(tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        session, ref, items = deploy_synth(vb, monkeypatch)
        assert session.deploy.deploy(ref).transport == "tcp+windowed"


def test_a_tunnelled_link_never_uses_tftp(tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, NON_WINDOWED) as vb:
        session, ref, items = deploy_synth(vb, monkeypatch, "synth_fielded",
                                           candidate=vb.candidate(tunnel=True))
        assert items[ITEM_TRANSPORT].detail.startswith("tcp: the link is a TCP tunnel")
        assert session.deploy.deploy(ref).transport == "tcp"
        assert [e.transport for e in vb.shell.push_events] == ["tcp", "tcp"]


def test_negative_twin_the_same_board_direct_uses_tftp(tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, NON_WINDOWED) as vb:
        session, ref, items = deploy_synth(vb, monkeypatch, "synth_fielded")
        assert items[ITEM_TRANSPORT].detail.startswith("tftp")


def test_clr_max_from_version_lets_a_bigger_clearing_through(tmp_path, monkeypatch, overlays):
    prof = LINUX_HARNESSD.__class__(**{**LINUX_HARNESSD.__dict__,
                                       "version_extra": {"clr_max": 1 << 20}})
    with VirtualMps3(tmp_path, prof) as vb:
        _, _, items = deploy_synth(vb, monkeypatch, "bigclear")
    assert items[ITEM_CLEARING_FITS].check == Check.OK
    assert "1048576 B the harness's clr_max" in items[ITEM_CLEARING_FITS].detail


def test_clr_max_from_stats_when_pyverify_can_send_stats(tmp_path, monkeypatch, overlays):
    monkeypatch.setattr(ShellClient, "stats", lambda self: self._request({"op": "stats"}),
                        raising=False)        # the v0.11 codec pyverify's HOST lane is adding
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        vb.shell.stats_extra = {"clr_max": 1 << 20}
        _, _, items = deploy_synth(vb, monkeypatch, "bigclear")
    assert items[ITEM_CLEARING_FITS].check == Check.OK


def test_negative_twin_without_clr_max_the_arena_default_refuses(tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        _, _, items = deploy_synth(vb, monkeypatch, "bigclear")
    assert items[ITEM_CLEARING_FITS].check == Check.MISMATCH
    assert "262144 B clearing arena" in items[ITEM_CLEARING_FITS].detail


def test_a_fabric_mismatch_refusal_is_incompatible_and_loads_nothing(tmp_path, monkeypatch,
                                                                      overlays):
    """S1: card and fabric disagree -> the shell refuses swap with a distinct line."""
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        session, ref, _ = deploy_synth(vb, monkeypatch)
        monkeypatch.setattr(vb.shell, "_op_swap", lambda req: {
            "ok": False, "err": "fabric mismatch: card 0x2b082e1b, fabric 0x11c30003"})
        with pytest.raises(IncompatibleError, match="fabric mismatch"):
            session.deploy.deploy(ref)
        assert vb.shell.current_rm_id == 0 and vb.shell.accepted_pushes == []


def test_negative_twin_an_unreachable_board_is_not_incompatible(tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        session, ref, _ = deploy_synth(vb, monkeypatch)
        vb.kill_harness()
        with pytest.raises(UnreachableError):
            session.deploy.deploy(ref)
