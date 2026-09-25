"""LINUX-READY end to end: B1 v4's findings (silicon, 2026-09-25) against a virtual board.

- the busy-ICAP race: swapping away streams the outgoing clearing into the ICAP first,
  and a partial that arrives meanwhile is PARKED on 6910 but REJECTED over TFTP.
  Harness Manager pushes to the Linux harness over plain tcp with pyverify's 30 s
  inactivity limit, so the park is waited out;
- after a failed push the harness turns new 6900 clients away until the swap's idle
  timeout: the board reads "busy" with the swap hint, never "offline".

pyverify's FakeShell models the race (``clearing_stream_bytes_per_s``);
``HarnessFakeShell`` the turned-away clients (``refuse_after_failed_push_s``,
``refuse_clients_for``). Every byte crosses real sockets on 127.0.0.1.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pyverify.pusher import BitstreamKind, PushError

from harness_manager.core.errors import ActionFailedError, HeldError
from harness_manager.core.services import EngineConfig
from harness_manager.engine import Engine
from harness_manager.services.deploy import ITEM_TRANSPORT
from harness_manager_mps3 import deploy as dep
from harness_manager_mps3 import shell as sh
from harness_manager_mps3.capabilities import HARNESS_STATES
from harness_manager_mps3.pack import Mps3Pack
from harness_manager_mps3.shell import SwapSettlingError
from tests.fakes.t2_overlays import make_overlay, point_pushes_at, use_overlay_dirs
from tests.fakes.virtual_board import (
    FIELDED_ILA_V011,
    LINUX_HARNESSD,
    PRODUCT_V011_FEATURES,
    VirtualMps3,
)

SETTLING = HARNESS_STATES["harness.swap_settling"]
#: A v0.11 bare-metal harness built without WINDOWED: Harness Manager pushes it over TFTP.
V011_TFTP = replace(FIELDED_ILA_V011, name="ila-v011-tftp",
                    features=tuple(f for f in PRODUCT_V011_FEATURES if f != "windowed"))
#: The boot greybox's resident clearing is 16 B (FakeShell's default): 16 B/s = a 1 s
#: busy-ICAP window when the first swap streams it out.
ONE_SECOND_STREAM = 16.0


@pytest.fixture(autouse=True)
def _no_failed_pushes():
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()
    yield
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()


@pytest.fixture
def overlays(tmp_path, monkeypatch):
    root = tmp_path / "overlays"
    for prof in (LINUX_HARNESSD, FIELDED_ILA_V011):     # V011_TFTP shares ILA's static
        make_overlay(root / prof.name, "synth", static_id=prof.static_id)
    use_overlay_dirs(monkeypatch, root / LINUX_HARNESSD.name, root / FIELDED_ILA_V011.name)
    return root


def open_and_find(vb: VirtualMps3, monkeypatch):
    point_pushes_at(monkeypatch, vb)
    session = Mps3Pack(console_ports=vb.console_ports).open(vb.candidate())
    ref = next(r for r in session.deploy.overlays()
               if r.name == "synth" and int(r.static_id, 16) == vb.profile.static_id)
    return session, ref


# -- the push rule --------------------------------------------------------------------


def test_linux_pushes_plain_tcp_with_30_s_and_waits_out_the_busy_icap_park(
        tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        vb.shell.clearing_stream_bytes_per_s = ONE_SECOND_STREAM
        session, ref = open_and_find(vb, monkeypatch)
        item = {i.name: i for i in session.deploy.preflight(ref)}[ITEM_TRANSPORT]
        result = session.deploy.deploy(ref)
        pusher = session.deploy.last_pusher
        parks, rejects = vb.shell.icap_defer_parks, vb.shell.icap_busy_rejects
    assert result.verified and result.transport == "tcp"
    assert (pusher.transport, pusher.windowed, pusher.timeout_s) == ("tcp", False, 30.0)
    assert "push inactivity limit 30 s" in item.detail
    assert (parks, rejects) == (1, 0)          # the partial arrived early, and was parked


def test_negative_twin_bare_metal_windowed_keeps_2_s(tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, FIELDED_ILA_V011) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        item = {i.name: i for i in session.deploy.preflight(ref)}[ITEM_TRANSPORT]
        result = session.deploy.deploy(ref)
        pusher = session.deploy.last_pusher
    assert result.transport == "tcp+windowed"
    assert (pusher.transport, pusher.windowed, pusher.timeout_s) == ("tcp", True, 2.0)
    assert "inactivity limit" not in item.detail


def test_negative_twin_bare_metal_tftp_keeps_2_s(tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, V011_TFTP) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        result = session.deploy.deploy(ref)
        pusher = session.deploy.last_pusher
    assert result.transport == "tftp"
    assert (pusher.transport, pusher.timeout_s) == ("tftp", 2.0)


def test_the_swap_never_persists_to_the_card(tmp_path, monkeypatch, overlays):
    # pyverify's deploy commits the pair to the user microSD by default now (v0.13, D1);
    # this adapter's deploy is the swap only, so it asks for no persist.
    seen: list[bool] = []
    real = dep.SwapOrchestrator.deploy

    def spy(self, overlay, **kwargs):
        seen.append(kwargs.get("persist", True))
        return real(self, overlay, **kwargs)

    monkeypatch.setattr(dep.SwapOrchestrator, "deploy", spy)
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        session, ref = open_and_find(vb, monkeypatch)
        result = session.deploy.deploy(ref)
    assert result.verified and seen == [False]


def test_negative_twin_pyverify_alone_would_persist():
    assert inspect.signature(dep.SwapOrchestrator.deploy).parameters["persist"].default is True


# -- 6900 after a failed push ---------------------------------------------------------


def test_after_the_race_fails_over_tftp_the_board_reads_busy_not_offline(
        tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, V011_TFTP) as vb:
        vb.shell.clearing_stream_bytes_per_s = ONE_SECOND_STREAM
        vb.shell.swap_await_timeout = 2.0
        vb.shell.refuse_after_failed_push_s = 5.0
        session, ref = open_and_find(vb, monkeypatch)
        with pytest.raises(ActionFailedError, match="TFTP ERROR 0"):
            session.deploy.deploy(ref)
        health = session.health()
        with pytest.raises(SwapSettlingError) as err:
            session.identity()             # bare metal: no identify to fall back on
        turned_away = len(vb.shell.refused_clients)
        rejects = vb.shell.icap_busy_rejects
    assert rejects == 1
    assert (health.reachable, health.control_channel) == (True, "busy")
    assert health.notes[0] == SETTLING
    assert isinstance(err.value, HeldError) and err.value.hint == SETTLING
    assert turned_away >= 2


def test_negative_twin_the_same_refusal_with_no_push_of_ours_is_another_client(
        tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, V011_TFTP) as vb:
        session, _ = open_and_find(vb, monkeypatch)
        vb.shell.refuse_clients_for(5.0)
        health = session.health()
    assert health.control_channel == "busy"
    assert health.notes[0] == HARNESS_STATES["harness.busy"] and SETTLING not in health.notes


def test_negative_twin_a_dead_harness_with_no_push_of_ours_stays_offline(
        tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, V011_TFTP) as vb:
        session, _ = open_and_find(vb, monkeypatch)
        vb.kill_harness()
        health = session.health()
    assert (health.reachable, health.control_channel) == (False, "offline")


def test_linux_after_a_host_side_push_failure_the_board_shows_busy(
        tmp_path, monkeypatch, overlays):
    """A tcp push that fails on the host side (the inactivity limit, a reset): the fake
    cannot see it, so the harness's refusal is started by hand, as the harness would."""
    with VirtualMps3(tmp_path, LINUX_HARNESSD) as vb:
        monkeypatch.setenv("HARNESS_MANAGER_MPS3_IDENTIFY_PORT", str(vb.identify_port))
        real_send = dep._ReportingPusher._send

        def failing_send(self, frame, *, kind):
            if kind is BitstreamKind.PARTIAL:
                vb.shell.refuse_clients_for(5.0)
                raise PushError("raw-TCP push failed: timed out")
            return real_send(self, frame, kind=kind)

        monkeypatch.setattr(dep._ReportingPusher, "_send", failing_send)
        point_pushes_at(monkeypatch, vb)
        eng = Engine(EngineConfig(state_dir=tmp_path / "state"),
                     packs={"mps3": Mps3Pack(console_ports=vb.console_ports)})
        cand = vb.candidate()
        eng.open(cand, note="linux-ready")
        try:
            session = eng.session(cand.board_id)
            ref = next(r for r in session.deploy.overlays()
                       if r.name == "synth" and int(r.static_id, 16) == vb.profile.static_id)
            with pytest.raises(ActionFailedError, match="timed out"):
                session.deploy.deploy(ref)
            info = eng.info(cand.board_id)
        finally:
            eng.close(cand.board_id)
    # identify still answers (the Linux harness), so the board is there, and busy.
    assert info.identity.harness_impl == "linux"
    assert info.health.control_channel == "busy" and info.health.notes[0] == SETTLING


def _session_through_a_refusing_hub(vb: VirtualMps3):
    """The session as L1 opens it through ``ssh -L``: the hub's refused open is logged
    by ssh, and the local end is accepted, then closed."""
    tunnel = SimpleNamespace(host="hub.example", state="up", detail="",
                             open_failures_since=lambda started, wait_s=0.0: [
                                 "channel 2: open failed: connect failed: Connection refused"])
    session = Mps3Pack(console_ports=vb.console_ports).open(vb.candidate())
    session.reach = SimpleNamespace(tunnel=tunnel, close=lambda: None)
    return session


def test_through_a_tunnel_our_failed_push_keeps_the_hub_refusal_busy(
        tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, V011_TFTP) as vb:
        session = _session_through_a_refusing_hub(vb)
        vb.shell.refuse_clients_for(5.0)
        session.shell.note_failed_push()
        health = session.health()
    assert health.control_channel == "busy" and health.notes[0] == SETTLING


def test_negative_twin_through_a_tunnel_with_no_push_the_hub_refusal_is_offline(
        tmp_path, monkeypatch, overlays):
    with VirtualMps3(tmp_path, V011_TFTP) as vb:
        session = _session_through_a_refusing_hub(vb)
        vb.shell.refuse_clients_for(5.0)
        health = session.health()
    assert health.control_channel == "offline"
    assert "could not reach the shell" in health.notes[0]
