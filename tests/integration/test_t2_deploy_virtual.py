"""T2 end to end: DeployService -> MPS3 pack -> pyverify -> FakeShell (the fielded profile).

Every byte crosses real sockets on 127.0.0.1 ephemeral ports. The FakeShell's
own observability (``push_events``, ``swaps``) is the witness that a refused
deploy pushed nothing. Each check has a negative twin.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_manager.core.errors import (
    AbsentError,
    ActionFailedError,
    HeldError,
    IncompatibleError,
    RefusedError,
    UnreachableError,
)
from harness_manager.core.events import EventBus
from harness_manager.core.model import Check
from harness_manager.core.pack import DeployAdapter
from harness_manager.services.deploy import (
    ITEM_CLEARING_FITS,
    ITEM_FILES,
    ITEM_PAIR,
    ITEM_SHELL_ID,
    ITEM_TRANSPORT,
    ITEM_USERCODE,
    DeployService,
)
from harness_manager_mps3.deploy import CLEARING_ARENA_BYTES, Mps3Deploy
from harness_manager_mps3.overlays import OVERLAY_DIRS_ENV
from harness_manager_mps3.pack import Mps3Pack
from tests.fakes.t2_overlays import (
    FIELDED_USERCODE,
    GREYBOX_RM_ID,
    NON_WINDOWED,
    OTHER_STATIC_ID,
    SYNTH2_RM_ID,
    make_overlay,
    point_pushes_at,
    ref_named,
    use_overlay_dirs,
)
from tests.fakes.virtual_board import VirtualMps3

NANOSOC_RM_ID = 0x01000001


@pytest.fixture
def engine():
    bus = EventBus()
    seen: list = []
    bus.subscribe("deploy.*", seen.append)
    return SimpleNamespace(bus=bus, events=seen)


@pytest.fixture
def overlay_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "overlays"
    make_overlay(root, "greybox", rm_id=GREYBOX_RM_ID, static_usercode=FIELDED_USERCODE)
    make_overlay(root, "synth", static_usercode=FIELDED_USERCODE)
    use_overlay_dirs(monkeypatch, root)
    return root


def open_session(vb: VirtualMps3, monkeypatch):
    point_pushes_at(monkeypatch, vb)
    return Mps3Pack(console_ports=vb.console_ports).open(vb.candidate())


def assert_nothing_pushed(vb: VirtualMps3) -> None:
    assert vb.shell.push_events == []
    assert vb.shell.swaps == []
    assert vb.shell.awaiting is None


def topics(engine) -> list[str]:
    return [e.topic for e in engine.events]


# -- wiring -------------------------------------------------------------------------


def test_pack_wires_the_deploy_adapter_for_an_ethernet_session(vboard, monkeypatch, overlay_root):
    session = open_session(vboard, monkeypatch)
    assert isinstance(session.deploy, Mps3Deploy)
    assert isinstance(session.deploy, DeployAdapter)


def test_usb_only_session_has_no_deploy_adapter(tmp_path):
    with VirtualMps3(tmp_path / "u", usb=True) as vb:
        session = Mps3Pack().open(vb.candidate(ethernet=False))
        assert session.deploy is None


# -- success, windowed transport (the fielded firmware) ------------------------------


def test_deploy_succeeds_over_windowed_tcp_and_identity_changes(vboard, monkeypatch, engine,
                                                                overlay_root):
    session = open_session(vboard, monkeypatch)
    assert session.identity().rm_id == "0x00000000"
    synth = ref_named(DeployService(engine).overlays(session), "synth")

    result = DeployService(engine).deploy(session, synth)

    assert result.verified and result.rm_id == "0x01007a57"
    assert result.transport == "tcp+windowed" and result.seconds > 0
    assert session.identity().rm_id == "0x01007a57"
    # The pusher really was windowed, on the board's push port...
    pusher = session.deploy.last_pusher
    assert (pusher.transport, pusher.windowed, pusher.tcp_port) == \
        ("tcp", True, vboard.shell.raw_tcp_port)
    # ...and the shell saw clearing then partial over TCP, then a verified swap.
    assert [(e.transport, e.info.kind.name) for e in vboard.shell.push_events] == \
        [("tcp", "CLEARING"), ("tcp", "PARTIAL")]
    assert vboard.shell.swaps[-1]["src"] == "tcp" and vboard.shell.swaps[-1]["final"] == "DONE"

    # Events: started -> progress... -> done, phases in order, push bytes monotonic.
    t = topics(engine)
    assert t[0] == "deploy.started" and t[-1] == "deploy.done"
    assert set(t[1:-1]) == {"deploy.progress"}
    phases = [e.data["phase"] for e in engine.events[1:-1]]
    assert phases == ["guard", "guard", "swap", "push", "push", "push", "verify", "verify"]
    pushed = [e.data["bytes"] for e in engine.events[1:-1] if e.data["phase"] == "push"]
    assert pushed == [0, 128, 512] and engine.events[4].data["total"] == 512
    assert engine.events[-1].data == {**engine.events[-1].data,
                                      "rm_id": "0x01007a57", "verified": True}


def test_non_windowed_firmware_gets_tftp(tmp_path, monkeypatch, engine, overlay_root):
    with VirtualMps3(tmp_path / "b", profile=NON_WINDOWED) as vb:
        session = open_session(vb, monkeypatch)
        synth = ref_named(DeployService(engine).overlays(session), "synth")
        items = {i.name: i for i in DeployService().preflight(session, synth)}
        assert items[ITEM_TRANSPORT].check is Check.OK
        assert items[ITEM_TRANSPORT].detail.startswith("tftp")

        result = DeployService(engine).deploy(session, synth)

        assert result.transport == "tftp" and result.verified
        pusher = session.deploy.last_pusher
        assert (pusher.transport, pusher.windowed, pusher.tftp_port) == \
            ("tftp", False, vb.shell.tftp_port)
        assert [e.transport for e in vb.shell.push_events] == ["tftp", "tftp"]
        assert vb.shell.swaps[-1]["src"] == "tftp"
        assert session.identity().rm_id == "0x01007a57"


def test_windowed_firmware_preflight_names_the_deadlock(vboard, monkeypatch, overlay_root):
    session = open_session(vboard, monkeypatch)
    synth = ref_named(session.deploy.overlays(), "synth")
    item = {i.name: i for i in session.deploy.preflight(synth)}[ITEM_TRANSPORT]
    assert item.check is Check.OK and item.detail.startswith("tcp+windowed")
    assert "deadlock" in item.detail


# -- refused before any push -------------------------------------------------------


def test_wrong_static_id_is_incompatible_and_the_shell_sees_nothing(vboard, monkeypatch, engine,
                                                                    tmp_path):
    root = tmp_path / "stale"
    make_overlay(root, "stale", static_id=OTHER_STATIC_ID)
    use_overlay_dirs(monkeypatch, root)
    session = open_session(vboard, monkeypatch)
    stale = ref_named(session.deploy.overlays(), "stale")

    with pytest.raises(IncompatibleError, match="0xdeadbeef"):
        DeployService(engine).deploy(session, stale)
    assert_nothing_pushed(vboard)
    assert topics(engine) == ["deploy.failed"]
    assert session.identity().rm_id == "0x00000000"

    # The adapter alone refuses too, before a single byte leaves the host.
    with pytest.raises(IncompatibleError):
        session.deploy.deploy(stale)
    assert_nothing_pushed(vboard)


def test_corrupt_crc_is_refused_and_the_shell_sees_nothing(vboard, monkeypatch, engine, tmp_path):
    root = tmp_path / "bad"
    make_overlay(root, "corrupt", corrupt_partial=True)
    use_overlay_dirs(monkeypatch, root)
    session = open_session(vboard, monkeypatch)
    corrupt = ref_named(session.deploy.overlays(), "corrupt")

    items = {i.name: i for i in session.deploy.preflight(corrupt)}
    assert items[ITEM_FILES].check is Check.MISMATCH and "crc32 mismatch" in items[ITEM_FILES].detail
    assert items[ITEM_SHELL_ID].check is Check.OK                  # only the payload is wrong
    with pytest.raises(RefusedError, match="crc32 mismatch"):
        DeployService(engine).deploy(session, corrupt)
    with pytest.raises(RefusedError):
        session.deploy.deploy(corrupt)
    assert_nothing_pushed(vboard)
    assert topics(engine) == ["deploy.failed"]


def test_clearing_over_the_arena_is_refused_and_the_shell_sees_nothing(vboard, monkeypatch,
                                                                       tmp_path):
    root = tmp_path / "big"
    make_overlay(root, "big", clearing=b"\x00\x00\x00\x20" * (CLEARING_ARENA_BYTES // 4 + 1))
    use_overlay_dirs(monkeypatch, root)
    session = open_session(vboard, monkeypatch)
    big = ref_named(session.deploy.overlays(), "big")

    item = {i.name: i for i in session.deploy.preflight(big)}[ITEM_CLEARING_FITS]
    assert item.check is Check.MISMATCH and "262148 B" in item.detail
    with pytest.raises(RefusedError, match="clearing arena"):
        DeployService().deploy(session, big)
    assert_nothing_pushed(vboard)


def test_clearing_exactly_the_arena_fits_and_deploys(vboard, monkeypatch, tmp_path):
    root = tmp_path / "edge"
    make_overlay(root, "edge", clearing=b"\x00\x00\x00\x20" * (CLEARING_ARENA_BYTES // 4))
    use_overlay_dirs(monkeypatch, root)
    session = open_session(vboard, monkeypatch)
    edge = ref_named(session.deploy.overlays(), "edge")

    item = {i.name: i for i in session.deploy.preflight(edge)}[ITEM_CLEARING_FITS]
    assert item.check is Check.OK
    assert DeployService().deploy(session, edge).verified
    assert vboard.shell.accepted_pushes[0].info.len_words * 4 == CLEARING_ARENA_BYTES


def test_unpaired_clearing_is_refused(vboard, monkeypatch, tmp_path):
    root = tmp_path / "unpaired"
    make_overlay(root, "unpaired", clearing_name="someone_else_clear.bin")
    use_overlay_dirs(monkeypatch, root)
    session = open_session(vboard, monkeypatch)
    ref = ref_named(session.deploy.overlays(), "unpaired")
    assert {i.name: i for i in session.deploy.preflight(ref)}[ITEM_PAIR].check is Check.MISMATCH
    with pytest.raises(RefusedError):
        DeployService().deploy(session, ref)
    assert_nothing_pushed(vboard)


# -- static_usercode (needs JTAG) --------------------------------------------------


def test_usercode_is_unchecked_without_jtag_and_does_not_block(vboard, monkeypatch, overlay_root):
    session = open_session(vboard, monkeypatch)
    synth = ref_named(session.deploy.overlays(), "synth")
    item = {i.name: i for i in session.deploy.preflight(synth)}[ITEM_USERCODE]
    assert item.check is Check.UNCHECKED and item.detail.startswith("needs JTAG")
    assert "0xd46fcdcb" in item.detail


def test_provided_usercode_is_compared(vboard, monkeypatch, overlay_root):
    session = open_session(vboard, monkeypatch)
    synth = ref_named(session.deploy.overlays(), "synth")
    session.deploy.running_usercode = "0xD46FCDCB"
    item = {i.name: i for i in session.deploy.preflight(synth)}[ITEM_USERCODE]
    assert item.check is Check.OK

    session.deploy.running_usercode = 0x12345678                   # another implementation run
    with pytest.raises(IncompatibleError, match="destroy the FPGA configuration"):
        DeployService().deploy(session, synth)
    assert_nothing_pushed(vboard)


# -- compatible --------------------------------------------------------------------


def test_compatible_gives_a_reason_for_each_incompatible_overlay(vboard, monkeypatch, tmp_path):
    root = tmp_path / "mixed"
    make_overlay(root, "good")
    make_overlay(root, "stale", rm_id=SYNTH2_RM_ID, static_id=OTHER_STATIC_ID)
    make_overlay(root, "corrupt", rm_id=0x0100_7A59, corrupt_partial=True)
    make_overlay(root, "big", rm_id=0x0100_7A5A,
                 clearing=b"\x00\x00\x00\x20" * (CLEARING_ARENA_BYTES // 4 + 1))
    use_overlay_dirs(monkeypatch, root)
    session = open_session(vboard, monkeypatch)

    loadable, reasons = DeployService().compatible(session)

    assert [o.name for o in loadable] == ["good"]
    assert set(reasons) == {"stale", "corrupt", "big"}
    assert reasons["stale"].startswith(ITEM_SHELL_ID) and "0xdeadbeef" in reasons["stale"]
    assert reasons["corrupt"].startswith(ITEM_FILES)
    assert reasons["big"].startswith(ITEM_CLEARING_FITS)
    assert_nothing_pushed(vboard)


# -- restore_baseline --------------------------------------------------------------


def test_restore_baseline_loads_the_greybox(tmp_path, monkeypatch, engine, overlay_root):
    with VirtualMps3(tmp_path / "b", boot_rm_id=NANOSOC_RM_ID) as vb:
        session = open_session(vb, monkeypatch)
        assert session.identity().rm_id == "0x01000001"
        assert session.deploy.baseline().name == "greybox"

        result = DeployService(engine).restore_baseline(session)

        assert result.rm_id == "0x00000000" and result.verified
        assert session.identity().rm_id == "0x00000000"
        assert vb.shell.swaps[-1]["rm"] == "greybox"
        assert topics(engine)[-1] == "deploy.done"


def test_restore_baseline_without_a_greybox_for_this_shell_is_absent(tmp_path, monkeypatch):
    root = tmp_path / "other_shell"
    make_overlay(root, "greybox", rm_id=GREYBOX_RM_ID, static_id=OTHER_STATIC_ID)
    use_overlay_dirs(monkeypatch, root)
    with VirtualMps3(tmp_path / "b", boot_rm_id=NANOSOC_RM_ID) as vb:
        session = open_session(vb, monkeypatch)
        assert session.deploy.baseline() is None
        with pytest.raises(AbsentError):
            DeployService().restore_baseline(session)
        assert_nothing_pushed(vb)


# -- failures after the push -------------------------------------------------------


def test_swap_rejected_by_the_shell_is_action_failed(vboard, monkeypatch, engine, overlay_root):
    session = open_session(vboard, monkeypatch)
    synth = ref_named(session.deploy.overlays(), "synth")
    vboard.shell.rm_id_readback_override = 0x0100_0BAD          # VERIFY reads back the wrong id

    with pytest.raises(ActionFailedError, match="refused the swap"):
        DeployService(engine).deploy(session, synth)
    assert vboard.shell.swaps[-1]["final"] == "FAILED"
    assert topics(engine)[0] == "deploy.started" and topics(engine)[-1] == "deploy.failed"
    assert "deploy.done" not in topics(engine)


def test_verified_false_is_action_failed(vboard, monkeypatch, engine, overlay_root):
    session = open_session(vboard, monkeypatch)
    synth = ref_named(session.deploy.overlays(), "synth")
    real = vboard.shell._run_swap

    def unverified(rm, src):
        reply = real(rm, src)
        return {**reply, "verified": False}

    monkeypatch.setattr(vboard.shell, "_run_swap", unverified)
    with pytest.raises(ActionFailedError, match="verified:false"):
        DeployService(engine).deploy(session, synth)
    assert topics(engine)[-1] == "deploy.failed"


def test_push_port_not_listening_is_action_failed(vboard, monkeypatch, overlay_root):
    session = open_session(vboard, monkeypatch)
    synth = ref_named(session.deploy.overlays(), "synth")
    vboard.shell.swap_await_timeout = 0.2                       # let the parked swap give up
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    monkeypatch.setenv("HARNESS_MANAGER_MPS3_PUSH_PORT", str(dead_port))

    with pytest.raises(ActionFailedError, match="bitstream push"):
        DeployService().deploy(session, synth)
    assert vboard.shell.push_events == []


def test_explicit_port_arguments_win_over_the_env(vboard, monkeypatch, overlay_root):
    monkeypatch.setenv("HARNESS_MANAGER_MPS3_PUSH_PORT", "1")
    adapter = Mps3Deploy(Mps3Pack().open(vboard.candidate()).shell,
                         push_port=vboard.shell.raw_tcp_port)
    assert adapter.push_port == vboard.shell.raw_tcp_port
    monkeypatch.delenv("HARNESS_MANAGER_MPS3_PUSH_PORT")
    monkeypatch.delenv("HARNESS_MANAGER_MPS3_TFTP_PORT", raising=False)
    assert Mps3Deploy(adapter._shell).push_port == 6910 and Mps3Deploy(adapter._shell).tftp_port == 69


# -- the single-client control port ------------------------------------------------


def test_held_control_port_stops_the_preflight(monkeypatch, overlay_root):
    """The real shell turns a second 6900 client away with accept-then-EOF.

    The preflight must raise, never return items. The class SHOULD be
    HeldError, but today it is UnreachableError: pyverify raises ConnectionError
    on EOF, and shell.py's ``except OSError`` catches that before its HeldError
    branch (contract change request in the T2 hand-back). Tighten this to
    HeldError once shell.py is fixed.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    stop = threading.Event()

    def turn_away():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            conn.settimeout(1.0)
            try:
                conn.recv(4096)                 # take the request, then EOF (no RST)
            except OSError:
                pass
            conn.close()

    thread = threading.Thread(target=turn_away, daemon=True)
    thread.start()
    try:
        pack = Mps3Pack()
        session = pack.open(pack.candidate_for_host(f"127.0.0.1:{server.getsockname()[1]}"))
        synth = ref_named(session.deploy.overlays(), "synth")
        with pytest.raises((HeldError, UnreachableError), match="closed"):
            session.deploy.preflight(synth)
        with pytest.raises((HeldError, UnreachableError)):
            DeployService().deploy(session, synth)
    finally:
        stop.set()
        thread.join()
        server.close()


# -- the real fielded overlays (skipped where the .bin payloads are absent) ---------

_REAL = Path(os.environ.get(
    "HARNESS_MANAGER_T2_REAL_OVERLAYS",
    Path(__file__).resolve().parents[3] / "mps3-nanosoc-platform" / "fpga" / "dfx" / "overlay"))


def _real_overlays_present() -> bool:
    return all((_REAL / rm / f"{rm}.bin").is_file() for rm in ("greybox", "led"))


@pytest.mark.skipif(not _real_overlays_present(), reason=f"no real overlay payloads at {_REAL}")
def test_real_fielded_overlays_are_all_loadable_and_led_round_trips(vboard, monkeypatch, engine):
    monkeypatch.setenv(OVERLAY_DIRS_ENV, str(_REAL))
    session = open_session(vboard, monkeypatch)
    service = DeployService(engine)

    loadable, reasons = service.compatible(session)
    assert reasons == {}                            # every real clearing fits the 256 KiB arena
    assert {"greybox", "led", "nanosoc"} <= {o.name for o in loadable}

    led = ref_named(loadable, "led")
    assert service.deploy(session, led).verified
    assert session.identity().rm_id == "0x0100001e"
    assert service.restore_baseline(session).rm_id == "0x00000000"
    assert session.identity().rm_id == "0x00000000"
