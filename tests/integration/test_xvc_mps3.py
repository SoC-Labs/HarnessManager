"""Lane XVC-CORE (X2 + X3): the MPS3's XVC through the real Engine, pack and deploy service.

The board is a ``VirtualMps3``; the harness's XVC server is the fake one on 127.0.0.1
(``tests/fakes/xvc_server.py``). The lab rig (``tests/fakes/l1_rig.py``) fakes ssh and the
hub, so the two reaches run for real without a network:

- bare-metal, and a Linux board this Harness Manager has not claimed (XVC-UNCLAIMED: no
  lock, no key on its SSH yet): the hub tunnel's own forward of 2542 (``FakeSsh`` routes
  the board's 2542);
- a Linux board this Harness Manager claimed: ``ssh -J HUB root@BOARD -L
  127.0.0.1:p:127.0.0.1:2542`` (the board's loopback 2542 routed to the fake; the session's
  claim forward, CLAIMED-LOCK), CCR X-1.

No hw_server runs (``byo``) except where a test names it (the fake one). Each check has
a negative twin.
"""

from __future__ import annotations

import json
import os
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

from harness_manager.core import capabilities as C
from harness_manager.core.errors import HeldError, UnavailableError
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services import xvc as X
from harness_manager.services.lease import LeaseService
from harness_manager_mps3 import xvc as MX
from harness_manager_mps3.pack import Mps3Pack
from harness_manager_mps3.statics import StaticStore
from tests.fakes.claimed_lock import board_key_fp, observed_claimed, pin_claim
from tests.fakes.l1_rig import BOARD_IP, HUB, lab
from tests.fakes.t2_overlays import (
    FIELDED_USERCODE,
    make_overlay,
    point_pushes_at,
    use_overlay_dirs,
)
from tests.fakes.virtual_board import (
    FIELDED_3F1A560F,
    FIELDED_ILA_V011,
    LINUX_HARNESSD,
    VirtualMps3,
)
from tests.fakes.xvc_server import FakeXvcServer
from tests.unit.test_xvc_service import IDCODE, read_idcode, wait_for

NANOSOC_ILA = 0x0100000A


def state_dir() -> Path:
    return Path(os.environ["HARNESS_MANAGER_STATE_DIR"])


@pytest.fixture
def fake():
    with FakeXvcServer() as srv:
        yield srv


@pytest.fixture
def engine():
    eng = Engine(EngineConfig(state_dir=state_dir()))
    yield eng
    eng.close_all()


def direct_engine(vb: VirtualMps3) -> Engine:
    return Engine(EngineConfig(state_dir=state_dir()),
                  packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})


def take_lease(session) -> None:
    LeaseService(state_dir()).acquire(session.hub, board_id=session.candidate.board_id,
                                      ttl_s=600, holder="hm-test", heartbeat=False)


def with_ltx(directory: Path, name: str, body: str = '{"probes": []}') -> Path:
    """Give an overlay directory made by ``make_overlay`` its ILA probes file."""
    ltx = directory / f"{name}.ltx"
    ltx.write_text(body)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["ltx"] = ltx.name
    manifest["ltx_crc32"] = f"0x{zlib.crc32(ltx.read_bytes()) & 0xFFFFFFFF:08x}"
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return ltx


# --- capability and reasons -----------------------------------------------------------------------


def test_debug_fabric_lights_up_with_xvc_dbgbr_and_says_it_is_partition_scoped(tmp_path):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        eng = direct_engine(vb)
        try:
            cand = vb.candidate()
            eng.open(cand)
            assert C.DEBUG_FABRIC in eng.info(cand.board_id).capabilities
            title = {s.name: s.title for s in eng.packs()["mps3"].capability_specs()}
            assert "partition" in title[C.DEBUG_FABRIC] and "XVC" in title[C.DEBUG_FABRIC]
        finally:
            eng.close_all()


def test_negative_twin_the_fielded_harness_has_no_xvc_dbgbr(tmp_path):
    with VirtualMps3(tmp_path, FIELDED_3F1A560F) as vb:
        eng = direct_engine(vb)
        try:
            cand = vb.candidate()
            session = eng.open(cand)
            info = eng.info(cand.board_id)
            assert C.DEBUG_FABRIC not in info.capabilities
            assert info.unavailable[C.DEBUG_FABRIC] == "needs harness firmware with 'xvc_dbgbr'"
            with pytest.raises(UnavailableError) as exc:
                eng.xvc.open(session, byo=True)
            assert "xvc_dbgbr" in exc.value.reason
        finally:
            eng.close_all()


def test_an_image_whose_xvc_drives_jtag_bb_is_refused_with_the_reason(tmp_path):
    jtagbb = replace(FIELDED_ILA_V011, name="jtagbb", features=tuple(
        "xvc_jtagbb" if f == "xvc_dbgbr" else f for f in FIELDED_ILA_V011.features))
    with VirtualMps3(tmp_path, jtagbb) as vb:
        eng = direct_engine(vb)
        try:
            session = eng.open(vb.candidate())
            with pytest.raises(UnavailableError) as exc:
                eng.xvc.open(session, byo=True)
            assert exc.value.reason == MX.JTAGBB_REASON
        finally:
            eng.close_all()


# --- bare-metal: direct, and through the hub tunnel (X6) -----------------------------------------------


def test_a_direct_bare_metal_board_opens_with_the_x6_warning(tmp_path, monkeypatch, fake):
    monkeypatch.setenv(MX.XVC_PORT_ENV, str(fake.port))
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        eng = direct_engine(vb)
        try:
            session = eng.open(vb.candidate())
            st = eng.xvc.open(session, byo=True)
            assert st.reach == "direct" and X.UNAUTHENTICATED_WARNING in st.warnings
            assert "never whole-device JTAG" in st.scope
            with X.XvcClient("127.0.0.1", st.relay_port) as c:
                assert read_idcode(c) == IDCODE
        finally:
            eng.close_all()                           # closing the board closes XVC too
        wait_for(lambda: not fake.attached, what="the board's slot to free")


def test_bare_metal_behind_the_hub_uses_the_hub_tunnel_for_the_lease_holder_only(
        tmp_path, monkeypatch, engine, fake):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb, \
            lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        rig.ssh.routes[(BOARD_IP, 2542)] = ("127.0.0.1", fake.port)
        cand = engine.candidate_for(BOARD_IP)
        session = engine.open(cand, note="xvc test")
        with pytest.raises(HeldError) as exc:               # twin first: no lease, no XVC
            engine.xvc.open(session, byo=True)
        assert "lease holder only" in exc.value.message
        assert fake.stats.connections == 0                  # never even probed
        take_lease(session)
        st = engine.xvc.open(session, byo=True)
        assert st.reach == "hub-tunnel" and X.UNAUTHENTICATED_WARNING in st.warnings
        assert len(rig.ssh.launches) == 1                   # the board's one tunnel, no new ssh
        with X.XvcClient("127.0.0.1", st.relay_port) as c:
            assert read_idcode(c) == IDCODE
        engine.xvc.close(session)
        wait_for(lambda: not fake.attached, what="the board's slot to free")


# --- Linux: board SSH through the hub when claimed from here (D-X1, CCR X-1) -------------------------


def linux_lab_open(tmp_path, monkeypatch, engine, fake, profile, *, claimed: bool = True):
    """A Linux board behind the hub; ``claimed``: this Harness Manager claimed it (the pin
    and its record), else nothing is known of a claim here."""
    vb = VirtualMps3(tmp_path, profile).__enter__()
    ctx = lab(vb, monkeypatch, state_dir=state_dir())
    rig = ctx.__enter__()
    rig.ssh.routes[("127.0.0.1", 2542)] = ("127.0.0.1", fake.port)     # the BOARD's loopback
    rig.ssh.routes[(BOARD_IP, 2542)] = ("127.0.0.1", fake.port)        # the hub's view of it
    cand = engine.candidate_for(BOARD_IP)
    session = engine.open(cand, note="xvc linux")
    if claimed:
        pin_claim(session)
    take_lease(session)
    return vb, ctx, rig, session


def test_linux_uses_ssh_jump_to_the_boards_loopback_and_warns_without_the_lock(
        tmp_path, monkeypatch, engine, fake):
    vb, ctx, rig, session = linux_lab_open(tmp_path, monkeypatch, engine, fake, LINUX_HARNESSD)
    try:
        st = engine.xvc.open(session, byo=True)
        assert st.reach == "board-ssh"
        argv = rig.ssh.launches[-1]
        assert argv[-1] == BOARD_IP
        assert argv[argv.index("-J") + 1] == HUB and argv[argv.index("-l") + 1] == "root"
        specs = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-L"]
        assert any(s.startswith("127.0.0.1:") and s.endswith(":127.0.0.1:2542") for s in specs)
        assert not any(BOARD_IP in s for s in specs)
        assert "ControlPath=none" in argv and "ExitOnForwardFailure=yes" in argv
        # No lock on this harness yet: the same warning as bare-metal, and why.
        assert X.UNAUTHENTICATED_WARNING in st.warnings
        facts = session.xvc.xvc_facts()
        assert facts["authenticated"] is False and MX.NO_LOCK_NOTE in facts["notes"]
        with X.XvcClient("127.0.0.1", st.relay_port) as c:
            assert read_idcode(c) == IDCODE
        engine.xvc.close(session)
        assert rig.ssh.procs[-1].returncode is not None      # the -J forward went with it
    finally:
        engine.close_all()
        ctx.__exit__(None, None, None)
        vb.__exit__(None, None, None)


def test_negative_twin_a_linux_harness_with_the_xvc_lock_carries_no_warning(
        tmp_path, monkeypatch, engine, fake):
    locked = replace(LINUX_HARNESSD, name="linux-locked",
                     features=LINUX_HARNESSD.features + (MX.LOCK_FEATURE,))
    vb, ctx, rig, session = linux_lab_open(tmp_path, monkeypatch, engine, fake, locked)
    try:
        st = engine.xvc.open(session, byo=True)
        assert st.reach == "board-ssh" and X.UNAUTHENTICATED_WARNING not in st.warnings
        assert session.xvc.xvc_facts()["authenticated"] is True
        assert "never whole-device JTAG" in st.scope
    finally:
        engine.close_all()
        ctx.__exit__(None, None, None)
        vb.__exit__(None, None, None)


@pytest.mark.parametrize("known", ["unclaimed", "not known"])
def test_negative_twin_an_unclaimed_linux_board_uses_the_hub_tunnel_like_bare_metal(
        tmp_path, monkeypatch, engine, fake, known):
    """XVC-UNCLAIMED: an unclaimed board has no lock (2542 answers the hub) and no key on
    its SSH, so ``auto`` goes the hub tunnel's way; so does a board whose claim is not known
    here (a claim met there is the typed lock error: tests/integration/test_claimed_lock.py)."""
    vb, ctx, rig, session = linux_lab_open(tmp_path, monkeypatch, engine, fake, LINUX_HARNESSD,
                                           claimed=False)
    try:
        if known == "unclaimed":
            observed_claimed(session.candidate.board_id, board_key_fp(), claimed=False)
        assert session.xvc.reach() == MX.REACH_HUB
        st = engine.xvc.open(session, byo=True)
        assert st.reach == "hub-tunnel" and X.UNAUTHENTICATED_WARNING in st.warnings
        assert len(rig.ssh.launches) == 1                   # the board's one tunnel, no new ssh
        assert all(a[-1] != BOARD_IP for a in rig.ssh.launches)
        facts = session.xvc.xvc_facts()
        assert facts["authenticated"] is False and MX.NOT_CLAIMED_NOTE in facts["notes"]
        assert "board_ssh" not in facts
        with X.XvcClient("127.0.0.1", st.relay_port) as c:
            assert read_idcode(c) == IDCODE
        engine.xvc.close(session)
    finally:
        engine.close_all()
        ctx.__exit__(None, None, None)
        vb.__exit__(None, None, None)


def test_an_explicit_board_ssh_reach_on_an_unclaimed_linux_board_is_still_honoured(
        tmp_path, monkeypatch, engine, fake):
    vb, ctx, rig, session = linux_lab_open(tmp_path, monkeypatch, engine, fake, LINUX_HARNESSD,
                                           claimed=False)
    try:
        rig.boards_toml.write_text(rig.boards_toml.read_text().replace(
            "[boards.lab]\n", '[boards.lab]\nxvc = { reach = "board-ssh" }\n'))
        assert session.xvc.reach() == MX.REACH_BOARD_SSH
        st = engine.xvc.open(session, byo=True)
        assert st.reach == "board-ssh" and rig.ssh.launches[-1][-1] == BOARD_IP
    finally:
        engine.close_all()
        ctx.__exit__(None, None, None)
        vb.__exit__(None, None, None)


def test_board_ssh_on_a_bare_metal_board_is_refused_and_auto_picks_the_hub(
        tmp_path, monkeypatch, engine, fake):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb, \
            lab(vb, monkeypatch, state_dir=state_dir()) as rig:
        toml = rig.boards_toml.read_text().replace(
            "[boards.lab]\n", '[boards.lab]\nxvc = { reach = "board-ssh" }\n')
        rig.boards_toml.write_text(toml)
        session = engine.open(engine.candidate_for(BOARD_IP))
        take_lease(session)
        with pytest.raises(UnavailableError) as exc:
            engine.xvc.open(session, byo=True)
        assert "needs the Linux harness" in exc.value.reason
        rig.boards_toml.write_text(toml.replace('reach = "board-ssh"', 'reach = "auto"'))
        assert session.xvc.reach() == MX.REACH_HUB           # twin: auto on bare-metal


# --- probes files ------------------------------------------------------------------------------------


def test_the_rm_ltx_comes_from_the_overlay_and_the_full_design_one_wins(tmp_path, monkeypatch,
                                                                       fake):
    monkeypatch.setenv(MX.XVC_PORT_ENV, str(fake.port))
    root = tmp_path / "ov"
    ila = make_overlay(root, "nanosoc_ila", rm_id=NANOSOC_ILA,
                       static_id=FIELDED_ILA_V011.static_id, static_usercode=FIELDED_USERCODE)
    rm_ltx = with_ltx(ila, "nanosoc_ila")
    use_overlay_dirs(monkeypatch, root)
    with VirtualMps3(tmp_path / "vb", FIELDED_ILA_V011, boot_rm_id=NANOSOC_ILA) as vb:
        eng = direct_engine(vb)
        try:
            session = eng.open(vb.candidate())
            st = eng.xvc.open(session, byo=True)
            assert st.ltx["rm"]["path"] == str(rm_ltx) and st.ltx["rm"]["crc_ok"] is True
            assert st.ltx["preferred"] == "rm" and st.ltx["static"] is None   # bare-metal
            eng.xvc.close(session)
            full = tmp_path / "full.ltx"
            full.write_text("{}")
            StaticStore().put_full_ltx(full, FIELDED_ILA_V011.static_id, "nanosoc_ila")
            st = eng.xvc.open(session, byo=True)                  # X5: the mint's file first
            assert st.ltx["preferred"] == "full"
            assert st.ltx["full"]["path"].endswith("full/nanosoc_ila.ltx")
            assert "full/nanosoc_ila.ltx" in eng.xvc.tcl(session)["tcl"]
        finally:
            eng.close_all()


def test_negative_twin_a_design_with_no_ltx_offers_none(tmp_path, monkeypatch, fake):
    monkeypatch.setenv(MX.XVC_PORT_ENV, str(fake.port))
    root = tmp_path / "ov"
    make_overlay(root, "nanosoc_ila", rm_id=NANOSOC_ILA, static_id=FIELDED_ILA_V011.static_id,
                 static_usercode=FIELDED_USERCODE)
    use_overlay_dirs(monkeypatch, root)
    with VirtualMps3(tmp_path / "vb", FIELDED_ILA_V011, boot_rm_id=NANOSOC_ILA) as vb:
        eng = direct_engine(vb)
        try:
            st = eng.xvc.open(eng.open(vb.candidate()), byo=True)
            assert st.ltx["rm"] is None and st.ltx["preferred"] is None
            assert "PROBES.FILE" not in eng.xvc.tcl(eng.session(vb.candidate().board_id))["tcl"]
        finally:
            eng.close_all()


# --- a real swap through the deploy service (X3) ------------------------------------------------------


def test_a_real_deploy_drops_the_xvc_slot_and_reattaches_on_the_new_design(
        tmp_path, monkeypatch, fake):
    monkeypatch.setenv(MX.XVC_PORT_ENV, str(fake.port))
    root = tmp_path / "ov"
    ila = make_overlay(root, "nanosoc_ila", rm_id=NANOSOC_ILA,
                       static_id=FIELDED_ILA_V011.static_id, static_usercode=FIELDED_USERCODE)
    with_ltx(ila, "nanosoc_ila")
    use_overlay_dirs(monkeypatch, root)
    with VirtualMps3(tmp_path / "vb", FIELDED_ILA_V011) as vb:
        point_pushes_at(monkeypatch, vb)
        eng = direct_engine(vb)
        seen: list[dict] = []
        eng.bus.subscribe(X.TOPIC, lambda ev: seen.append(ev.data))
        try:
            session = eng.open(vb.candidate())
            st = eng.xvc.open(session, byo=True)
            assert st.rm_id in ("", "0x00000000") and st.ltx["rm"] is None   # the greybox
            ref = next(r for r in eng.deploy.overlays(session) if r.name == "nanosoc_ila")
            eng.deploy.deploy(session, ref)
            for t in eng.xvc.threads:
                t.join(30)
            states = [e["state"] for e in seen]
            assert "swapping" in states and states[-1] == "ready"
            assert fake.stats.connections == 2                # dropped for the swap, back after
            after = eng.xvc.status(session)
            assert after.rm_id == "0x0100000a" and after.ltx["rm"]["name"] == "nanosoc_ila.ltx"
            assert "reopened on nanosoc_ila" in after.detail
        finally:
            eng.close_all()
