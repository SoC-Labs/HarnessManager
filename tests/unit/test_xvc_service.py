"""Lane XVC-CORE (X1): the relay, HM's own hw_server, swaps and leases, against the fake XVC
server and a fake hw_server on 127.0.0.1. No board, no hub, no Vivado. Each check has a
negative twin.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from harness_manager.core.errors import (
    AlreadyError,
    HeldError,
    PortBoundError,
    UnavailableError,
    UnreachableError,
    UsageError,
)
from harness_manager.core.events import Event, EventBus
from harness_manager.core.model import BoardIdentity, Candidate, Health
from harness_manager.core.pack import BoardSession
from harness_manager.services import xvc as X
from tests.fakes.xvc_server import FakeXvcServer, Tap

REPO = Path(__file__).resolve().parents[2]
IDCODE = 0x0A003093

posix_only = pytest.mark.skipif(os.name != "posix", reason="a /bin/sh shim stands in for hw_server")


# --- helpers ---------------------------------------------------------------------------------


def free_pair() -> int:
    """A base port whose next port is free too (the relay and hw_server pair)."""
    for _ in range(200):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            base = s.getsockname()[1]
        if base < 65000 and not X.port_in_use(base + 1):
            return base
    raise AssertionError("no free port pair")


def wait_for(pred, timeout: float = 10.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def read_idcode(client: X.XvcClient) -> int:
    """Test-Logic-Reset, then shift 32 bits of IDCODE out of DR (the fake's real TAP)."""
    # TMS: 5 x 1 (TLR), 0 (RTI), 1 (SEL-DR), 0 (CAP-DR), 0 (SHIFT-DR) ... 32 bits, then 1 (EXIT1)
    bits = [1, 1, 1, 1, 1, 0, 1, 0, 0] + [0] * 31 + [1]
    n = len(bits)
    tms = bytearray((n + 7) // 8)
    for i, b in enumerate(bits):
        tms[i >> 3] |= b << (i & 7)
    tdo = client.shift(n, bytes(tms), bytes(len(tms)))
    value = 0
    for i in range(32):                  # IDCODE bit i leaves on TDO at clock 9 + i
        k = 9 + i
        value |= ((tdo[k >> 3] >> (k & 7)) & 1) << i
    return value


class FakeAdapter:
    """A board pack's ``XvcAdapter`` pointed at the fake XVC server."""

    def __init__(self, fake: FakeXvcServer, *, reason: str = "", warnings=(),
                 reach: str = "direct", probes: dict | None = None) -> None:
        self.fake = fake
        self.reason = reason
        self.warnings = list(warnings)
        self.reach = reach
        self.probes = probes
        self.released = 0
        self.identities: list[Any] = []

    def xvc_endpoint(self) -> tuple[str, int]:
        return "127.0.0.1", self.fake.port

    def xvc_reason(self) -> str:
        return self.reason

    def xvc_probes(self, rm_id: str) -> dict:
        return self.probes or {"rm": None, "static": None, "full": None, "vivado": ""}

    def xvc_facts(self) -> dict:
        return {"scope": X.PARTITION_SCOPE, "reach": self.reach, "warnings": self.warnings}

    def xvc_release(self) -> None:
        self.released += 1

    def xvc_note_identity(self, ident: Any) -> None:
        self.identities.append(ident)


class FakeSession(BoardSession):
    def __init__(self, adapter: Any, *, board_id: str = "mps3@test", hub: Any = None,
                 rm_id: str = "0x0100000a") -> None:
        self.candidate = Candidate(pack="mps3", board_id=board_id, links=())
        self.xvc = adapter
        self.hub = hub
        self.rm_id = rm_id

    def identity(self) -> BoardIdentity:
        return BoardIdentity("mps3", shell_id="0x72bb0a36", rm_id=self.rm_id,
                             rm_name="nanosoc_ila", features=("xvc_dbgbr",))

    def health(self) -> Health:
        return Health(reachable=True, control_channel="idle")


class Leases:
    """``LeaseService.view`` stand-in: what the hub says about the board's lease."""

    def __init__(self, lease: dict | None = None, error: Exception | None = None) -> None:
        self.lease = lease
        self.error = error
        self.calls = 0

    def view(self, hub: Any) -> dict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"lease": self.lease, "hub": "hub"}


class Hub:
    host = "mapstone-dev.ecs.soton.ac.uk"
    target = "mps3_01_pl"


@pytest.fixture
def fake():
    with FakeXvcServer(tap=Tap(IDCODE)) as srv:
        yield srv


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def events(bus):
    seen: list[dict] = []
    bus.subscribe(X.TOPIC, lambda ev: seen.append({"board": ev.board_id, **ev.data}))
    return seen


@pytest.fixture
def hw_shim(tmp_path, monkeypatch) -> Path:
    """A fake ``hw_server`` where a Vivado 2024.1 install keeps it; argv logged to a file."""
    bindir = tmp_path / "Xilinx" / "Vivado" / "2024.1" / "bin"
    bindir.mkdir(parents=True)
    shim = bindir / "hw_server"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m tests.fakes.fake_hw_server "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    monkeypatch.setenv(X.HW_SERVER_ENV, str(shim))
    monkeypatch.setenv("FAKE_HW_SERVER_ARGV_LOG", str(tmp_path / "hw_argv.jsonl"))
    return shim


def service(bus, tmp_path, **kw) -> X.XvcService:
    class Eng:
        pass

    eng = Eng()
    eng.bus = bus
    eng.state_dir = tmp_path / "state"
    kw.setdefault("port_base", free_pair())
    kw.setdefault("start_timeout", 30.0)
    return X.XvcService(eng, **kw)


@pytest.fixture
def svc(bus, tmp_path):
    s = service(bus, tmp_path)
    yield s
    s.shutdown()


def relay_for(fake: FakeXvcServer, **kw) -> X.XvcRelay:
    return X.XvcRelay(lambda: ("127.0.0.1", fake.port), identify_peer=False, **kw).start()


# --- the wire -----------------------------------------------------------------------------------


def test_the_relay_forwards_whole_xvc_commands_to_the_board(fake):
    relay = relay_for(fake)
    try:
        relay.acquire(5)
        with X.XvcClient("127.0.0.1", relay.port) as c:
            assert c.getinfo() == "xvcServer_v1.0:2048"
            assert c.settck(100) == 100
            assert read_idcode(c) == IDCODE                 # through the relay to the TAP
        assert fake.stats.connections == 1                  # one upstream: the relay's
        assert fake.stats.dropped == 0
    finally:
        relay.close()


def test_negative_twin_a_client_that_breaks_the_protocol_is_dropped_and_the_board_stays_in_step(fake):
    relay = relay_for(fake)
    try:
        relay.acquire(5)
        with socket.create_connection(("127.0.0.1", relay.port), timeout=3) as s:
            s.sendall(b"hello, world")                     # not XVC
            s.settimeout(3)
            assert s.recv(10) == b""                        # dropped, as the firmware would
        wait_for(lambda: relay.served == 1, what="the client to go")
        with X.XvcClient("127.0.0.1", relay.port) as c:
            assert c.getinfo().startswith("xvcServer_v")
            assert read_idcode(c) == IDCODE
        assert fake.stats.dropped == 0                      # the board never saw the garbage
    finally:
        relay.close()


def test_a_client_that_leaves_mid_command_never_desyncs_the_board(fake):
    relay = relay_for(fake)
    try:
        relay.acquire(5)
        with socket.create_connection(("127.0.0.1", relay.port), timeout=3) as s:
            s.sendall(X.SHIFT + struct.pack("<I", 64) + bytes(5))   # 5 of 16 payload bytes
        wait_for(lambda: relay.served == 1, what="the client to go")
        with X.XvcClient("127.0.0.1", relay.port) as c:
            assert c.getinfo() == "xvcServer_v1.0:2048"   # a clean start, not the old shift
            assert read_idcode(c) == IDCODE
    finally:
        relay.close()


def test_negative_twin_a_client_that_leaves_before_its_reply_does_not_desync_either(fake):
    relay = relay_for(fake)
    try:
        relay.acquire(5)
        with socket.create_connection(("127.0.0.1", relay.port), timeout=3) as s:
            s.sendall(X.SHIFT + struct.pack("<I", 64) + bytes(16))   # whole; reply unread
        wait_for(lambda: relay.served == 1, what="the client to go")
        with X.XvcClient("127.0.0.1", relay.port) as c:
            assert c.settck(250) == 250                     # the old reply was consumed
            assert read_idcode(c) == IDCODE
    finally:
        relay.close()


# --- the board's slot -------------------------------------------------------------------------


def test_the_relay_holds_the_boards_one_slot_while_open(fake):
    relay = relay_for(fake)
    try:
        relay.acquire(5)
        assert relay.board_slot == "ours" and fake.attached
        assert X.probe("127.0.0.1", fake.port)["state"] == "held"   # anyone else: refused
        assert fake.stats.refused_second == 1
    finally:
        relay.close()
    wait_for(lambda: not fake.attached, what="the slot to free")
    assert X.probe("127.0.0.1", fake.port)["state"] == "free"         # twin: after close


def test_a_second_local_client_is_refused_and_the_first_is_undisturbed(fake):
    relay = relay_for(fake)
    try:
        relay.acquire(5)
        with X.XvcClient("127.0.0.1", relay.port) as a:
            assert a.getinfo().startswith("xvcServer_v")
            wait_for(lambda: relay.status()["attached"], what="A attached")
            with socket.create_connection(("127.0.0.1", relay.port), timeout=3) as b:
                b.settimeout(3)
                b.sendall(X.GETINFO)
                assert b.recv(64) == b""                       # accept, then close
            assert relay.refusals[-1][2] == "another client is attached"
            assert read_idcode(a) == IDCODE                    # A carries on
        wait_for(lambda: relay.served == 1, what="A to leave")
        with X.XvcClient("127.0.0.1", relay.port) as b:     # twin: B attaches once A left
            assert b.getinfo().startswith("xvcServer_v")
    finally:
        relay.close()


def test_acquire_says_held_when_someone_else_has_the_boards_slot(fake):
    squatter = X.XvcClient("127.0.0.1", fake.port)
    try:
        assert squatter.getinfo().startswith("xvcServer_v")
        relay = relay_for(fake)
        try:
            with pytest.raises(HeldError) as exc:
                relay.acquire(0.5)
            assert "held by another client" in exc.value.message
            assert relay.board_slot == "held"
        finally:
            relay.close()
    finally:
        squatter.close()
    relay = relay_for(fake)                                 # twin: once it leaves, ours
    try:
        relay.acquire(5)
        assert relay.board_slot == "ours"
    finally:
        relay.close()


def test_negative_twin_acquire_says_unreachable_when_nothing_serves_xvc():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    relay = X.XvcRelay(lambda: ("127.0.0.1", dead), identify_peer=False).start()
    try:
        with pytest.raises(UnreachableError):
            relay.acquire(0.5)
        assert relay.board_slot == "refused"
    finally:
        relay.close()


def test_hold_kicks_the_client_frees_the_slot_and_refuses_attaches_until_released(fake):
    relay = relay_for(fake)
    try:
        relay.acquire(5)
        with socket.create_connection(("127.0.0.1", relay.port), timeout=3) as a:
            wait_for(lambda: relay.status()["attached"], what="A attached")
            relay.hold("partition swap")
            a.settimeout(3)
            assert a.recv(10) == b""                        # kicked
        wait_for(lambda: not fake.attached, what="the board's slot to free")
        with socket.create_connection(("127.0.0.1", relay.port), timeout=3) as b:
            b.settimeout(3)
            assert b.recv(10) == b""                        # refused while held
        assert relay.refusals[-1][2] == "partition swap"
        relay.release()
        relay.acquire(5)                                    # twin: back after the swap
        with X.XvcClient("127.0.0.1", relay.port) as c:
            assert read_idcode(c) == IDCODE
    finally:
        relay.close()


def test_the_relay_takes_the_slot_back_after_the_board_drops_it(fake):
    changes: list[str] = []
    relay = X.XvcRelay(lambda: ("127.0.0.1", fake.port), identify_peer=False,
                       backoff_s=(0.1, 0.2), on_change=lambda: changes.append("x")).start()
    try:
        relay.acquire(5)
        fake.kick()                                         # a harness restart
        wait_for(lambda: relay.status()["reconnects"] == 1, what="the reconnect")
        assert relay.board_slot == "ours" and fake.stats.connections == 2
    finally:
        relay.close()


def test_negative_twin_a_board_that_stays_gone_keeps_the_relay_down_and_refusing(fake):
    relay = X.XvcRelay(lambda: ("127.0.0.1", fake.port), identify_peer=False,
                       backoff_s=(0.1, 0.2)).start()
    try:
        relay.acquire(5)
        fake.close()                                        # nothing serves 2542 any more
        wait_for(lambda: relay.board_slot in ("down", "refused"), what="the loss")
        time.sleep(0.5)
        assert relay.board_slot in ("down", "refused")
        with socket.create_connection(("127.0.0.1", relay.port), timeout=3) as c:
            c.settimeout(3)
            assert c.recv(10) == b""
        assert "slot is" in relay.refusals[-1][2]
    finally:
        relay.close()


# --- hw_server -------------------------------------------------------------------------------------


def test_hw_server_argv_is_private_with_no_gdb_ports_and_no_daemon_or_idle_linger():
    argv = X.hw_server_argv("/v/2024.1/bin/hw_server", 23601, "127.0.0.1:23600")
    assert argv == ["/v/2024.1/bin/hw_server", "-q", "-p0", "-s", "TCP:127.0.0.1:23601",
                    "-e", "set auto-open-servers xilinx-xvc:127.0.0.1:23600",
                    "-e", "set jtag-port-filter Xilinx/XVC/127.0.0.1:23600"]
    assert "-d" not in argv and not any(a.startswith("-I") for a in argv)
    assert X.vivado_version_of(argv[0]) == "2024.1"


@pytest.mark.parametrize("bad", ["-d", "-I20", "-I"])
def test_negative_twin_the_argv_refuses_daemon_and_idle_options(bad):
    with pytest.raises(UsageError):
        X.hw_server_argv("hw_server", 23601, "127.0.0.1:23600", extra=[bad])


def test_hw_server_opens_the_xvc_cable_only_never_a_local_usb_one():
    """XVC-UI hardening (david's scope rule): hw_server's default ``auto-open-servers *``
    would offer a local USB cable as whole-device JTAG. The argv names the one XVC server
    and filters ports to its name (docs/assessment/xvc_ui_2026-09-25/)."""
    argv = X.hw_server_argv("hw_server", 23601, "127.0.0.1:23600")
    settings = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert settings == ["set auto-open-servers xilinx-xvc:127.0.0.1:23600",
                        "set jtag-port-filter Xilinx/XVC/127.0.0.1:23600"]
    assert not any("*" in s for s in settings)                  # never "every cable type"
    assert X.xvc_port_filter("127.0.0.1:23600") == "Xilinx/XVC/127.0.0.1:23600"
    # A filter is a substring match on "<maker>/<product>/<serial>": this one admits the
    # relay's own XVC port and no other XVC port, nor a Digilent/Xilinx USB cable.
    flt = X.xvc_port_filter("127.0.0.1:23600")
    assert flt in "Xilinx/XVC/127.0.0.1:23600"
    for other in ("Xilinx/XVC/127.0.0.1:23602", "Digilent/JTAG-SMT2NC/210251A08870",
                  "Xilinx/Platform Cable USB II/000013e8b58201"):
        assert flt not in other


@pytest.mark.parametrize("bad", [["-e", "set auto-open-servers *"],
                                 ["-e", "set jtag-port-filter Digilent"],
                                 ["-e", "set always-open-jtag 1"], ["--init=/tmp/x.tcl"]])
def test_negative_twin_extra_options_cannot_reopen_local_cables(bad):
    with pytest.raises(UsageError):
        X.hw_server_argv("hw_server", 23601, "127.0.0.1:23600", extra=bad)


def test_negative_twin_byo_builds_no_hw_server_argv_and_so_no_filter(fake, svc, monkeypatch,
                                                                     tmp_path):
    """--byo: your own hw_server, your own cables. HM builds no argv, so adds no filter."""
    calls: list[tuple] = []
    real = X.hw_server_argv
    monkeypatch.setattr(X, "hw_server_argv", lambda *a, **k: calls.append(a) or real(*a, **k))
    monkeypatch.setenv(X.HW_SERVER_ENV, str(tmp_path / "no-such-hw_server"))
    s = FakeSession(FakeAdapter(fake))
    st = svc.open(s, byo=True)
    try:
        assert st.mode == "byo" and st.hw_server_pid == 0
        assert calls == []
        assert "jtag-port-filter" not in svc.tcl(s)["tcl"]      # the snippet sets nothing either
    finally:
        svc.close(s)


@posix_only
def test_negative_twin_a_port_filter_that_does_not_admit_the_cable_never_opens_it(
        fake, bus, tmp_path, monkeypatch, hw_shim):
    """The fake hw_server obeys jtag-port-filter as the real one does: a filter that is not
    the relay's own port name hides the cable, and the board's slot sees no shift."""
    real = X.hw_server_argv

    def wrong_filter(binary, port, xvc, **kw):
        argv = real(binary, port, xvc, **kw)
        return [*argv[:-1], "set jtag-port-filter Xilinx/XVC/127.0.0.1:1"]

    monkeypatch.setattr(X, "hw_server_argv", wrong_filter)
    svc = service(bus, tmp_path)
    s = FakeSession(FakeAdapter(fake))
    try:
        st = svc.open(s)
        shifts = fake.stats.shifts
        with socket.create_connection(("127.0.0.1", st.hw_server_port), timeout=5):
            time.sleep(1.0)
            assert svc.status(s).state == "ready"             # nothing came through the relay
        assert fake.stats.shifts == shifts
    finally:
        svc.shutdown()


def test_find_hw_server_prefers_the_env_then_the_one_beside_vivado(tmp_path, monkeypatch):
    viv = tmp_path / "Vivado" / "2024.1" / "bin"
    viv.mkdir(parents=True)
    for name in ("vivado", "hw_server"):
        (viv / name).write_text("#!/bin/sh\n")
        (viv / name).chmod(0o755)
    monkeypatch.delenv(X.HW_SERVER_ENV, raising=False)
    monkeypatch.delenv("XILINX_VIVADO", raising=False)
    monkeypatch.setenv("PATH", str(viv))
    assert X.find_hw_server() == str(viv / "hw_server")
    mine = tmp_path / "mine"
    mine.write_text("#!/bin/sh\n")
    monkeypatch.setenv(X.HW_SERVER_ENV, str(mine))
    assert X.find_hw_server() == str(mine)


def test_negative_twin_no_hw_server_is_unavailable_and_names_byo(tmp_path, monkeypatch):
    monkeypatch.delenv(X.HW_SERVER_ENV, raising=False)
    monkeypatch.delenv("XILINX_VIVADO", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(UnavailableError) as exc:
        X.find_hw_server()
    assert "--byo" in exc.value.reason
    monkeypatch.setenv(X.HW_SERVER_ENV, str(tmp_path / "nope"))
    with pytest.raises(UnavailableError):
        X.find_hw_server()


# --- the service: open, status, close ---------------------------------------------------------------


@posix_only
def test_open_runs_hms_own_hw_server_behind_the_relay_and_close_stops_it(fake, svc, hw_shim,
                                                                        events, tmp_path):
    adapter = FakeAdapter(fake, warnings=[X.UNAUTHENTICATED_WARNING])
    s = FakeSession(adapter)
    st = svc.open(s)
    assert st.state == "ready" and st.mode == "m1" and st.board_slot == "ours"
    assert st.url == f"localhost:{st.hw_server_port}" and st.hw_server_pid > 0
    assert st.hw_server_port == st.relay_port + 1 and "(2024.1)" in st.hw_server
    argv = json.loads((tmp_path / "hw_argv.jsonl").read_text().splitlines()[-1])["argv"]
    assert argv[:4] == ["-q", "-p0", "-s", f"TCP:127.0.0.1:{st.hw_server_port}"]
    assert "-d" not in argv and not any(a.startswith("-I") for a in argv)
    assert argv[-3:] == [f"set auto-open-servers xilinx-xvc:127.0.0.1:{st.relay_port}", "-e",
                         f"set jtag-port-filter Xilinx/XVC/127.0.0.1:{st.relay_port}"]
    assert st.scope == X.PARTITION_SCOPE and X.UNAUTHENTICATED_WARNING in st.warnings
    # Vivado connects to hw_server; hw_server opens the XVC target through the relay.
    # The relay reads "attached" at the connect, before hw_server's first command; a shift
    # counts once the board has answered it. So wait for the scan itself, not the attach.
    with socket.create_connection(("127.0.0.1", st.hw_server_port), timeout=5):
        wait_for(lambda: svc.status(s).state == "attached", what="hw_server to attach")
        wait_for(lambda: (svc.status(s).attached or {}).get("shifts", 0) >= 1,
                 what="hw_server's IDCODE scan through the relay")
        att = svc.status(s).attached
        assert att["shifts"] >= 1
        if os.path.isdir("/proc"):
            assert att["pid"] == st.hw_server_pid       # who holds it: HM's own hw_server
    assert fake.stats.connections == 1                  # still the relay's one upstream
    assert [e["state"] for e in events][:2] == ["starting", "ready"]
    pid = st.hw_server_pid
    down = svc.close(s)
    assert down.state == "down" and adapter.released == 1
    wait_for(lambda: not X._pid_alive(pid), what="hw_server to exit")
    wait_for(lambda: not fake.attached, what="the board's slot to free")
    assert not (tmp_path / "state" / "xvc" / "mps3_test.json").exists()


def test_negative_twin_byo_runs_no_hw_server_and_the_tcl_opens_the_relay(fake, svc,
                                                                        monkeypatch, tmp_path):
    monkeypatch.setenv(X.HW_SERVER_ENV, str(tmp_path / "no-such-hw_server"))  # never needed
    s = FakeSession(FakeAdapter(fake))
    st = svc.open(s, byo=True)
    try:
        assert st.mode == "byo" and st.hw_server_pid == 0 and st.hw_server_port == 0
        assert st.url == f"127.0.0.1:{st.relay_port}"
        tcl = svc.tcl(s)["tcl"]
        assert f"open_hw_target -xvc_url 127.0.0.1:{st.relay_port}" in tcl
        assert "connect_hw_server -url localhost:3121" in tcl
        assert any("lingers 20 s" in w for w in st.warnings)
        with X.XvcClient("127.0.0.1", st.relay_port) as c:     # your own tool, straight in
            assert read_idcode(c) == IDCODE
    finally:
        svc.close(s)


def test_the_partition_scope_is_in_status_the_tcl_and_the_events(fake, svc, events):
    s = FakeSession(FakeAdapter(fake))
    down = svc.status(s)
    assert down.state == "down" and "never whole-device JTAG" in down.scope
    assert "reconfigurable partition" in svc.tcl(s)["tcl"].splitlines()[0]
    st = svc.open(s, byo=True)
    try:
        assert "never whole-device JTAG" in st.scope
        assert all("never whole-device JTAG" in e["scope"] for e in events)
    finally:
        svc.close(s)


def test_negative_twin_an_adapter_reason_refuses_before_anything_starts(fake, svc):
    adapter = FakeAdapter(fake, reason="2542 on this image drives jtag_bb")
    with pytest.raises(UnavailableError) as exc:
        svc.open(FakeSession(adapter), byo=True)
    assert "jtag_bb" in exc.value.reason
    assert fake.stats.connections == 0 and svc.open_boards() == []


def test_a_second_open_is_already_and_another_processes_session_is_held(fake, svc, tmp_path):
    s = FakeSession(FakeAdapter(fake))
    svc.open(s, byo=True)
    try:
        with pytest.raises(AlreadyError):
            svc.open(s, byo=True)
    finally:
        svc.close(s)
    rec = tmp_path / "state" / "xvc" / "mps3_test.json"
    rec.write_text(json.dumps({"board_id": "mps3@test", "owner": {
        "pid": os.getppid(), "host": socket.gethostname(), "user": "someone"}}))
    with pytest.raises(HeldError) as exc:                  # another live process has it
        svc.open(s, byo=True)
    assert f"pid {os.getppid()}" in exc.value.holder
    rec.write_text(json.dumps({"board_id": "mps3@test", "owner": {
        "pid": 999999, "host": socket.gethostname(), "user": "gone"}}))
    st = svc.open(s, byo=True)                              # twin: a dead owner is cleared
    assert st.state == "ready"
    svc.close(s)


def test_negative_twin_a_pinned_port_that_is_taken_is_port_bound(fake, bus, tmp_path):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        base = blocker.getsockname()[1]
        svc = service(bus, tmp_path, port_base=base)
        try:
            with pytest.raises(PortBoundError):
                svc.open(FakeSession(FakeAdapter(fake)), byo=True)
            assert fake.stats.connections == 0
        finally:
            svc.shutdown()


# --- leases (X6: the lease holder only) ---------------------------------------------------------------


def test_open_is_for_the_lease_holder_only(fake, bus, tmp_path):
    held = Leases({"target": "mps3_01_pl", "holder": "david@mapstone-dev", "mine": True})
    svc = service(bus, tmp_path, leases=held)
    try:
        s = FakeSession(FakeAdapter(fake), hub=Hub())
        assert svc.open(s, byo=True).state == "ready" and held.calls == 1
        svc.close(s)
        for leases, holder in ((Leases({"holder": "alice@lab", "mine": False}), "alice@lab"),
                               (Leases(None), "nobody"),
                               (Leases(error=UnreachableError("hub down")), "unknown")):
            svc.leases = leases
            with pytest.raises(HeldError) as exc:
                svc.open(s, byo=True)
            assert holder in exc.value.holder
        assert fake.stats.connections == 1               # only the lease holder's session
    finally:
        svc.shutdown()


def test_negative_twin_a_board_with_no_hub_has_no_lease_to_hold(fake, bus, tmp_path):
    leases = Leases(error=AssertionError("must not be asked"))
    svc = service(bus, tmp_path, leases=leases)
    try:
        s = FakeSession(FakeAdapter(fake), hub=None)
        assert svc.open(s, byo=True).state == "ready" and leases.calls == 0
    finally:
        svc.shutdown()


def test_the_real_lease_service_gates_open_through_the_fake_hub(fake, bus, tmp_path):
    from harness_manager.services.lease import LeaseService
    from harness_manager_mps3 import hub as hubmod
    from tests.fakes.l1_fake_hub import FakeHub

    fh = FakeHub()
    try:
        class RealHub:
            host = Hub.host
            target = Hub.target
            client = hubmod.HubClient(Hub.host, Hub.target, runner=fh)

        leases = LeaseService(tmp_path / "state", bus)
        svc = service(bus, tmp_path, leases=leases)
        try:
            s = FakeSession(FakeAdapter(fake), hub=RealHub())
            with pytest.raises(HeldError):                 # nobody holds it yet
                svc.open(s, byo=True)
            leases.acquire(RealHub(), board_id="mps3@test", ttl_s=600, holder="hm-test",
                           heartbeat=False)
            assert svc.open(s, byo=True).state == "ready"
            svc.close(s)
            fh.steal("alice-hm")                           # twin: someone else holds it now
            leases._forget(RealHub())
            with pytest.raises(HeldError):
                svc.open(s, byo=True)
        finally:
            svc.shutdown()
    finally:
        fh.close()


def test_losing_the_lease_closes_the_session(fake, svc, bus, events):
    s = FakeSession(FakeAdapter(fake))
    svc.open(s, byo=True)
    bus.publish(Event("lease.state", "mps3@test", {"state": "held", "target": "t"}))
    assert svc.open_boards() == ["mps3@test"]               # twin: held changes nothing
    bus.publish(Event("lease.state", "mps3@test", {"state": "lost", "target": "t"}))
    for t in svc.threads:
        t.join(5)
    assert svc.open_boards() == []
    assert events[-1]["state"] == "down" and "lease was lost" in events[-1]["detail"]
    wait_for(lambda: not fake.attached, what="the board's slot to free")


# --- swaps (X3: drop before, re-attach after) ------------------------------------------------------------


@posix_only
def test_a_swap_drops_the_slot_and_hw_server_then_reattaches_on_a_fresh_one(fake, svc, bus,
                                                                            hw_shim, events):
    s = FakeSession(FakeAdapter(fake))
    st = svc.open(s)
    old_pid, port = st.hw_server_pid, st.hw_server_port
    bus.publish(Event("deploy.started", "mps3@test", {"overlay": "nanosoc", "rm_id": "0x01000001"}))
    # Synchronous: before the swap RPC the slot is free and hw_server is gone.
    assert svc.status(s).state == "swapping"
    wait_for(lambda: not fake.attached, what="the board's slot to free", timeout=3)
    wait_for(lambda: not X._pid_alive(old_pid), what="the old hw_server to exit")
    with socket.create_connection(("127.0.0.1", st.relay_port), timeout=3) as c:
        c.settimeout(3)
        assert c.recv(10) == b""                           # no attach mid-swap
    bus.publish(Event("deploy.done", "mps3@test", {"rm_id": "0x01000001", "verified": True}))
    for t in svc.threads:
        t.join(30)
    new = svc.status(s)
    assert new.state == "ready" and new.board_slot == "ours"
    assert new.hw_server_port == port and new.hw_server_pid not in (0, old_pid)
    assert new.rm_id == "0x01000001" and "reopened" in new.detail
    assert fake.stats.connections == 2                     # dropped once, taken back once
    assert [e["state"] for e in events][-2:] == ["swapping", "ready"]
    svc.close(s)


def test_negative_twin_a_failed_or_unverified_swap_closes_with_the_reason(fake, svc, bus, events):
    s = FakeSession(FakeAdapter(fake))
    # A preflight refusal publishes deploy.failed alone: nothing was held, nothing changes.
    svc.open(s, byo=True)
    bus.publish(Event("deploy.failed", "mps3@test", {"stage": "preflight", "reason": "x"}))
    assert svc.status(s).state == "ready"
    bus.publish(Event("deploy.started", "mps3@test", {"overlay": "nanosoc"}))
    bus.publish(Event("deploy.failed", "mps3@test", {"stage": "deploy", "reason": "timeout"}))
    assert svc.open_boards() == []
    assert "the swap failed at deploy (timeout)" in events[-1]["detail"]
    svc.open(s, byo=True)
    bus.publish(Event("deploy.started", "mps3@test", {"overlay": "nanosoc"}))
    bus.publish(Event("deploy.done", "mps3@test", {"rm_id": "0x0", "verified": False}))
    assert svc.open_boards() == []
    assert "not verified" in events[-1]["detail"]


def test_a_harness_restart_is_reconnected_without_closing_the_session(fake, bus, tmp_path, events):
    svc = service(bus, tmp_path)
    try:
        s = FakeSession(FakeAdapter(fake))
        svc.open(s, byo=True)
        live = svc._get("mps3@test")
        live.relay._backoff = (0.1, 0.2)
        fake.kick()
        wait_for(lambda: any(e["state"] == "down" for e in events), what="down")
        wait_for(lambda: svc.status(s).state == "ready" and live.relay.reconnects == 1,
                 what="ready again")
        assert svc.open_boards() == ["mps3@test"]
    finally:
        svc.shutdown()


# --- probes files ---------------------------------------------------------------------------------


def test_the_full_design_probes_file_is_preferred_and_the_tcl_loads_it(fake, svc, tmp_path):
    rm = tmp_path / "nanosoc_ila.ltx"
    full = tmp_path / "nanosoc_ila_full.ltx"
    rm.write_text("{}")
    full.write_text("{}")
    probes = {"rm": {"path": rm, "name": rm.name}, "full": {"path": full, "name": full.name},
              "static": None, "vivado": ""}
    s = FakeSession(FakeAdapter(fake, probes=probes))
    st = svc.open(s, byo=True)
    try:
        assert st.ltx["preferred"] == "full"
        assert f"PROBES.FILE {{{full}}}" in svc.tcl(s)["tcl"]
        assert svc.ltx(s, "rm")["path"] == str(rm)
    finally:
        svc.close(s)


def test_negative_twin_without_a_full_file_the_rm_file_is_offered_and_none_is_absent(fake, svc,
                                                                                     tmp_path):
    from harness_manager.core.errors import AbsentError

    rm = tmp_path / "nanosoc_ila.ltx"
    rm.write_text("{}")
    s = FakeSession(FakeAdapter(fake, probes={"rm": {"path": rm, "name": rm.name}, "full": None,
                                              "static": None, "vivado": "2026.1"}))
    st = svc.open(s, byo=True)
    try:
        assert st.ltx["preferred"] == "rm"
        assert svc.ltx(s)["which"] == "rm"
        with pytest.raises(AbsentError):
            svc.ltx(s, "static")
    finally:
        svc.close(s)
