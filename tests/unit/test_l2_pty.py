"""Lane L2: PTYs for ``screen`` and serial baud, on the broker with a bare session.

The console behind the PTY is an ``l2loop://`` serial port (tests/fakes/l2_rig.py):
it records the rate of every open and echoes what it is sent. Clients are SEPARATE
processes (``PtyClient``), because the broker never counts its own process as a
client. Real PTYs, no board. Every check has a negative twin.
"""

from __future__ import annotations

import sys

import pytest

if not sys.platform.startswith("linux"):
    # Client counting (inotify) and TIOCINQ are Linux-only; the PTYs are untested elsewhere.
    pytest.skip("the PTY tests need Linux", allow_module_level=True)

import os
import socket
import stat
import termios
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_manager.core.errors import (
    AbsentError,
    ExitCode,
    RefusedError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.events import EventBus
from harness_manager.services import pty as ptymod
from harness_manager.services.console import ConsoleBroker
from tests.fakes.l2_rig import (
    LOOP,
    PtyClient,
    PtyHolder,
    fast_pty_options,
    in_ring,
    is_link_to,
    open_fails_busy,
    pty_dir,
    queued,
    wait_for,
)
from tests.fakes.t4_console_rig import BareSession, EventLog, read_until, recv_until

pytestmark = pytest.mark.skipif(not ptymod.supported(), reason="PTYs need a POSIX system")

LANE = "l2loop://lane0"
BOARD = "test@lab:6900"


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    r = pty_dir(tmp_path)
    monkeypatch.setenv(ptymod.PTY_DIR_ENV, str(r))
    return r


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def log(bus: EventBus) -> EventLog:
    return EventLog(bus)


@pytest.fixture
def broker(bus: EventBus, root: Path) -> Iterator[ConsoleBroker]:
    LOOP.reset()
    b = ConsoleBroker(bus, backoff=(0.05, 0.2), pty_options=fast_pty_options())
    yield b
    b.shutdown()


@pytest.fixture
def session() -> BareSession:
    return BareSession(BOARD, consoles={"fpga_uart0": LANE, "fpga_uart2": "l2loop://lane2"})


def pty_events(log: EventLog) -> list[dict]:
    return [d for t, d in log.topics("console.pty")]


# -- the path -----------------------------------------------------------------------------------


def test_the_pty_is_a_stable_symlink_in_a_private_directory(broker, session, root):
    info = broker.pty(session, "fpga_uart0")
    path = Path(info["path"])
    assert path == root / "test@lab-6900" / "fpga_uart0"
    assert path.is_symlink() and info["device"].startswith("/dev/pts/")
    assert is_link_to(path, info["device"])
    for d in (root, path.parent):
        assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700
    assert info["command"] == f"screen {path} 115200"        # the serial rate rides along
    # Idempotent: the same PTY, not a second one.
    again = broker.pty(session, "fpga_uart0")
    assert (again["path"], again["device"]) == (info["path"], info["device"])
    assert len(broker._ptys.ptys()) == 1


def test_negative_twin_closing_the_pty_removes_its_link_and_a_new_one_is_new(broker, session):
    first = broker.pty(session, "fpga_uart0")
    assert broker.close_pty(BOARD, "fpga_uart0") is True
    assert not os.path.lexists(first["path"]) and broker.pty_info(BOARD, "fpga_uart0") is None
    assert broker.close_pty(BOARD, "fpga_uart0") is False            # nothing left to close
    second = broker.pty(session, "fpga_uart0")
    assert second["path"] == first["path"] and is_link_to(second["path"], second["device"])


def test_an_alias_gets_its_targets_pty(broker, session):
    via_alias = broker.pty(session, "shell")                      # shell -> fpga_uart2
    assert via_alias["name"] == "fpga_uart2" and via_alias["path"].endswith("/fpga_uart2")
    assert broker.pty_info(BOARD, "shell")["path"] == via_alias["path"]


def test_an_unknown_console_has_no_pty(broker, session, root):
    with pytest.raises(AbsentError):
        broker.pty(session, "uart9")
    assert not (root / "test@lab-6900").exists() or not any((root / "test@lab-6900").iterdir())


def test_board_slug_is_a_safe_readable_name():
    assert ptymod.board_slug("mps3@192.168.10.101:6900") == "mps3@192.168.10.101-6900"
    assert ptymod.board_slug("mps3@usb:/dev/ttyUSB0") == "mps3@usb--dev-ttyUSB0"
    # Negative twin: nothing that could climb out of the directory survives.
    for bad in ("../../etc", "a/../b", "..", "/"):
        slug = ptymod.board_slug(bad)
        assert "/" not in slug and slug not in (".", "..")


def test_the_default_directory_is_tmp_harness_manager_user(monkeypatch):
    # docs/API.md; tests/conftest.py sets HARNESS_MANAGER_PTY_DIR for every test, so the
    # default is only ever read here, and nothing is created.
    monkeypatch.delenv(ptymod.PTY_DIR_ENV, raising=False)
    monkeypatch.setenv("USER", "ada.l")
    assert ptymod.runtime_dir() == Path("/tmp") / "harness-manager-ada.l"
    monkeypatch.setenv("USER", "a/../b")                       # a user name never climbs out
    assert ptymod.runtime_dir().parent == Path("/tmp")


def test_negative_twin_the_directory_follows_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv(ptymod.PTY_DIR_ENV, str(tmp_path / "mine"))
    assert ptymod.runtime_dir() == tmp_path / "mine"
    monkeypatch.setenv(ptymod.PTY_DIR_ENV, "  ")                # blank: the default again
    assert ptymod.runtime_dir().parent == Path("/tmp")


def test_a_planted_symlink_in_place_of_the_directory_is_refused(tmp_path, monkeypatch, bus,
                                                               session):
    target = tmp_path / "elsewhere"
    target.mkdir()
    planted = tmp_path / "ptys"
    planted.symlink_to(target)
    monkeypatch.setenv(ptymod.PTY_DIR_ENV, str(planted))
    b = ConsoleBroker(bus, pty_options=fast_pty_options())
    try:
        with pytest.raises(RefusedError) as err:
            b.pty(session, "fpga_uart0")
        assert "not a directory" in err.value.message
        assert not any(target.iterdir())                            # nothing written through it
    finally:
        b.shutdown()


def test_negative_twin_an_open_directory_of_ours_is_tightened_to_0700(root, broker, session):
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    broker.pty(session, "fpga_uart0")
    assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700


def test_without_ptys_the_error_names_the_tcp_export(broker, session, monkeypatch):
    monkeypatch.setattr(ptymod, "supported", lambda: False)
    with pytest.raises(UnavailableError) as err:
        broker.pty(session, "fpga_uart0")
    assert err.value.code == ExitCode.UNAVAILABLE and "--export" in err.value.hint
    assert "Windows" in err.value.reason


# -- bytes both ways, several readers ----------------------------------------------------------------


def test_a_pty_client_sees_the_console_and_its_typing_reaches_the_board(broker, session):
    info = broker.pty(session, "fpga_uart0")
    with PtyClient(info["path"]) as client:
        client.read_until(b"serial up at 115200")
        client.type("hello")
        client.read_until(b"hello\r")                             # the port echoed it back
    assert LOOP.ports[0].closed is False                          # the PTY still holds it


def test_two_readers_at_once_the_pty_the_gui_and_a_tcp_terminal_see_the_same_bytes(broker, session):
    info = broker.pty(session, "fpga_uart0")
    gui = broker.subscribe(session, "fpga_uart0")
    port = broker.export_tcp(session, "fpga_uart0")
    with PtyClient(info["path"]) as client, socket.create_connection(("127.0.0.1", port)) as term:
        client.read_until(b"serial up at 115200")
        read_until(gui, b"serial up at 115200")
        recv_until(term, b"serial up at 115200")
        client.type("from-screen")
        client.read_until(b"from-screen\r")
        read_until(gui, b"from-screen\r")
        recv_until(term, b"from-screen\r")
        gui.write(b"from-gui\r")
        client.read_until(b"from-gui\r")
    assert LOOP.opens == [("lane0", 115200)]                      # ONE port open for all three


def test_reattach_after_an_exclusive_client_exits(broker, session, log):
    info = broker.pty(session, "fpga_uart0")
    first = PtyClient(info["path"], exclusive=True)               # as `screen` opens it
    first.read_until(b"serial up at")
    wait_for(lambda: broker.pty_info(BOARD, "fpga_uart0")["clients"] == 1, what="one client")
    assert open_fails_busy(info["path"])                          # TIOCEXCL keeps others out
    first.close()
    wait_for(lambda: broker.pty_info(BOARD, "fpga_uart0")["clients"] == 0, what="no client")
    assert not open_fails_busy(info["path"])                      # the daemon cleared it
    with PtyClient(info["path"], exclusive=True) as second:
        second.type("again")
        second.read_until(b"again\r")
    counts = [e["clients"] for e in pty_events(log)]
    # The "created" event (clients 0) can be published after a fast client has already
    # attached, so do not pin the first value: an attach, then a detach after it.
    assert 1 in counts and 0 in counts[counts.index(1) + 1:], counts
    assert all(e["path"] == info["path"] for e in pty_events(log))


def test_negative_twin_without_the_reset_an_exclusive_client_locks_the_pty(broker, session,
                                                                           monkeypatch):
    monkeypatch.setattr(ptymod.ConsolePty, "reset_line", lambda self: None)
    info = broker.pty(session, "fpga_uart0")
    with PtyClient(info["path"], exclusive=True) as first:
        first.read_until(b"serial up at")
        wait_for(lambda: broker.pty_info(BOARD, "fpga_uart0")["clients"] == 1, what="one client")
    wait_for(lambda: broker.pty_info(BOARD, "fpga_uart0")["clients"] == 0, what="no client")
    assert open_fails_busy(info["path"])                           # what screen leaves behind


def test_output_nobody_reads_is_kept_as_recent_output_not_queued(broker, session):
    info = broker.pty(session, "fpga_uart0")
    gui = broker.subscribe(session, "fpga_uart0")
    read_until(gui, b"serial up")
    gui.write(b"x" * 60000 + b"END-MARK\r")                       # echoed; no PTY client
    read_until(gui, b"END-MARK", timeout=10)
    port = broker._ptys.get(BOARD, "fpga_uart0")
    wait_for(lambda: in_ring(port, b"END-MARK"), what="the ring")
    assert queued(port.slave) == 0 and port.flushed == 0           # nothing written for nobody
    with PtyClient(info["path"]) as client:
        got = client.read_until(b"END-MARK")
    assert len(got) <= 4096                                         # the recent output only


def test_negative_twin_always_live_output_nobody_reads_is_flushed(bus, root, session):
    b = ConsoleBroker(bus, pty_options=fast_pty_options(replay_on_attach=False))
    try:
        info = b.pty(session, "fpga_uart0")
        gui = b.subscribe(session, "fpga_uart0")
        read_until(gui, b"serial up")
        gui.write(b"x" * 60000 + b"END-MARK\r")
        read_until(gui, b"END-MARK", timeout=10)
        port = b._ptys.get(BOARD, "fpga_uart0")
        wait_for(lambda: port.flushed > 0, what="a flush")
        with PtyClient(info["path"]) as client:
            got = client.read_until(b"END-MARK")
        assert len(got) < 60000                                     # not the whole backlog
    finally:
        b.shutdown()


# -- serial baud ------------------------------------------------------------------------------------


def test_set_baud_reopens_a_serial_port_at_the_new_rate(broker, session, log):
    gui = broker.subscribe(session, "fpga_uart0")
    read_until(gui, b"serial up at 115200")
    mark = log.mark()
    assert broker.set_baud(session, "fpga_uart0", 57600) == {
        "name": "fpga_uart0", "baud": 57600, "source": "serial"}
    read_until(gui, b"serial up at 57600")
    assert LOOP.rates("lane0") == [115200, 57600]
    row = broker.baud(session, "fpga_uart0")
    assert (row["kind"], row["baud"], row["source"], row["settable"]) == ("serial", 57600,
                                                                          "serial", True)
    assert 57600 in row["choices"] and row["reason"] == ""
    ev = log.wait_for(lambda e: e.topic == "console.state" and e.data.get("baud") == 57600,
                      after=mark)
    assert ev.data["name"] == "fpga_uart0"
    # 0 goes back to the URL's rate.
    broker.set_baud(session, "fpga_uart0", 0)
    read_until(gui, b"serial up at 115200")
    assert broker.baud(session, "fpga_uart0")["baud"] == 115200


def test_negative_twin_a_rate_set_before_the_console_opens_is_used_when_it_does(broker, session):
    broker.set_baud(session, "fpga_uart0", 230400)
    assert LOOP.opens == []                                        # nothing reopened
    gui = broker.subscribe(session, "fpga_uart0")
    read_until(gui, b"serial up at 230400")
    assert LOOP.rates("lane0") == [230400]


@pytest.mark.parametrize("bad", [-1, 12_000_001, True, 9600.0, "9600"])
def test_a_bad_rate_is_a_usage_error(broker, session, bad):
    with pytest.raises(UsageError):
        broker.set_baud(session, "fpga_uart0", bad)
    assert LOOP.opens == []


def test_a_standard_speed_set_on_the_pty_is_forwarded_to_a_serial_console(broker, session):
    info = broker.pty(session, "fpga_uart0")
    with PtyClient(info["path"], speed=termios.B57600) as client:   # `screen <path> 57600`
        wait_for(lambda: LOOP.rates("lane0")[-1:] == [57600], what="the reopen at 57600")
        client.read_until(b"serial up at 57600")
    assert broker.baud(session, "fpga_uart0")["baud"] == 57600
    assert broker.pty_info(BOARD, "fpga_uart0")["command"].endswith(" 57600")


def test_negative_twin_screen_cannot_override_a_rate_the_gui_set_that_it_cannot_express(
        broker, session):
    broker.set_baud(session, "fpga_uart0", 76800)                  # not a termios speed
    info = broker.pty(session, "fpga_uart0")
    assert info["command"] == f"screen {info['path']}"              # no rate to pass
    with PtyClient(info["path"], speed=termios.B9600) as client:    # screen's own default
        client.read_until(b"serial up at 76800")
        port = broker._ptys.get(BOARD, "fpga_uart0")
        wait_for(lambda: port.seen_speed == termios.B9600, what="the watcher saw 9600")
    assert LOOP.rates("lane0") == [76800]
    assert broker.baud(session, "fpga_uart0")["baud"] == 76800


def test_a_tcp_console_with_no_pack_report_is_unknown_and_refuses_a_change(broker):
    s = BareSession(BOARD, consoles={"uart0": "tcp://127.0.0.1:9"})
    row = broker.baud(s, "uart0")
    assert (row["kind"], row["baud"], row["source"], row["settable"]) == ("ethernet", None,
                                                                          "unknown", False)
    assert "does not report" in row["reason"]
    with pytest.raises(UnavailableError) as err:
        broker.set_baud(s, "uart0", 115200)
    assert "does not report" in err.value.reason


def test_the_console_rows_carry_kind_rate_and_pty(broker, session):
    broker.pty(session, "fpga_uart0")
    rows = {r["name"]: r for r in broker.consoles(session)}
    assert set(rows) == {"fpga_uart0", "fpga_uart2", "shell"}
    assert rows["fpga_uart0"]["pty"].endswith("/fpga_uart0") and rows["fpga_uart2"]["pty"] is None
    assert rows["shell"]["alias_of"] == "fpga_uart2" and "alias_of" not in rows["fpga_uart0"]
    assert all(r["kind"] == "serial" and r["baud"] == 115200 for r in rows.values())


def test_a_console_the_pack_says_nothing_drives_is_marked_not_connected(broker):
    s = BareSession(BOARD, consoles={"uart0": "tcp://127.0.0.1:9", "uart1": "tcp://127.0.0.1:10"})
    s.consoles.console_baud_info = lambda: {
        "uart0": {"kind": "ethernet", "baud": 76800, "source": "design", "settable": False,
                  "reason": "fixed", "choices": []},
        "uart1": {"kind": "ethernet", "baud": None, "source": "design", "settable": False,
                  "reason": "nothing drives uart1", "choices": [], "connected": False,
                  "connected_reason": "DUT uart1: not connected in this shell"}}
    rows = {r["name"]: r for r in broker.consoles(s)}
    assert rows["uart1"]["connected"] is False
    assert rows["uart1"]["connected_reason"] == "DUT uart1: not connected in this shell"
    assert "connected" not in rows["uart0"] and "connected_reason" not in rows["uart0"]


def test_closing_the_board_closes_its_ptys_and_forgets_its_rates(broker, session, log):
    broker.set_baud(session, "fpga_uart0", 57600)
    info = broker.pty(session, "fpga_uart0")
    broker.close_all(BOARD)
    assert not os.path.lexists(info["path"])
    assert broker.pty_info(BOARD, "fpga_uart0") is None
    assert broker.baud(session, "fpga_uart0")["baud"] == 115200
    closed = [e for e in pty_events(log) if not e["open"]]
    assert closed and closed[-1]["path"] == info["path"]


def _plant(root: Path, board: str, name: str, pid: int, device: str = "/dev/pts/9999") -> Path:
    d = root / board
    d.mkdir(parents=True, mode=0o700)
    link = d / name
    link.symlink_to(device)
    ptymod.owner_file(link).write_text(f"{pid} {device}\n")
    return link


def test_a_link_left_by_a_dead_process_is_swept(broker, session, root):
    import subprocess
    import sys

    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True)
    stale = _plant(root, "mps3@10.0.0.9-6900", "uart0", int(dead.stdout))
    broker.pty(session, "fpga_uart0")                               # any new PTY sweeps
    assert not os.path.lexists(stale) and not ptymod.owner_file(stale).exists()


def test_negative_twin_a_link_of_a_live_process_is_left_alone(broker, session, root):
    import subprocess
    import sys

    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        link = _plant(root, "mps3@10.0.0.9-6900", "uart0", alive.pid)
        broker.pty(session, "fpga_uart0")
        assert os.path.islink(link) and ptymod.owner_file(link).exists()
    finally:
        alive.kill()
        alive.wait()


def _inotify_fds() -> int:
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{fd}") == "anon_inode:inotify":
                n += 1
        except OSError:
            continue
    return n


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd") or ptymod.client_counter() is None,
                    reason="needs Linux inotify")
def test_every_broker_shares_one_inotify_instance(bus, root):
    # inotify instances are a per-user limit (128 by default, shared with editors).
    brokers = [ConsoleBroker(bus, pty_options=fast_pty_options()) for _ in range(3)]
    try:
        for i, b in enumerate(brokers):
            b.pty(BareSession(f"test@lab{i}:6900", consoles={"fpga_uart0": f"l2loop://l{i}"}),
                  "fpga_uart0")
        assert {b._ptys.counting for b in brokers} == {"inotify"}
        assert _inotify_fds() == 1
        # Negative twin: the count really sees a second instance.
        extra = ptymod.Inotify.create()
        assert extra is not None and _inotify_fds() == 2
        extra.close()
    finally:
        for b in brokers:
            b.shutdown()


# -- what a new client sees: recent output, after it has set the line up ----------------------------
#
# The flake behind "screen shows nothing" (lead, 2026-09-24): test_pty_post_get_delete lost
# the boot banner about 1 run in 3. Something outside the process opened and closed the new
# PTY within ~1 ms of its link appearing; the count went 0 -> 1 -> 0 before the test's
# client, and the "last client left" reset (tty.setraw's default TCSAFLUSH) discarded the
# banner, queued on the line for the next reader. And GNU screen itself applies its modes
# with TCSAFLUSH ~200 ms after opening, so output queued before it attached never showed.
# Now nothing is written while no client is attached; a client gets the recent output once
# it has set the line up. These tests make both sequences happen on purpose.


GREETING = b"serial up at 115200"


def _greeting_held(broker, session) -> tuple[dict, object]:
    info = broker.pty(session, "fpga_uart0")
    port = broker._ptys.get(BOARD, "fpga_uart0")
    wait_for(lambda: in_ring(port, GREETING), what="the greeting in the PTY's recent output")
    return info, port


def _come_and_go(broker, path: str, *, exclusive: bool = False) -> None:
    """A client that opens the PTY, is counted, and leaves without reading a byte."""
    holder = PtyHolder(path, exclusive=exclusive)
    wait_for(lambda: broker.pty_info(BOARD, "fpga_uart0")["clients"] == 1, what="counted")
    holder.release()
    wait_for(lambda: broker.pty_info(BOARD, "fpga_uart0")["clients"] == 0, what="gone")


def _read_late(monkeypatch) -> threading.Event:
    """The counter thread reads its inotify queue only once the returned event is set (a
    loaded machine): what happens meanwhile stays queued, and the kernel coalesces it."""
    go = threading.Event()
    original = ptymod.Inotify.read

    def late(self):
        go.wait(10)
        return original(self)

    monkeypatch.setattr(ptymod.Inotify, "read", late)
    return go


def test_a_client_whose_open_the_kernel_coalesced_is_still_counted(broker, session, monkeypatch):
    # FLAKE 2026-09-24 ("timed out waiting for counted", 2 runs in 30 under load): another
    # opener came and went while the counter thread was behind. Its IN_OPEN and the
    # client's were ONE event, so opens minus closes read 0 with the client attached.
    info, port = _greeting_held(broker, session)
    go = _read_late(monkeypatch)
    holder = PtyHolder(info["path"])                                  # stays
    try:
        assert not open_fails_busy(info["path"])                      # comes and goes
        go.set()
        wait_for(lambda: broker.pty_info(BOARD, "fpga_uart0")["clients"] == 1,
                 what="the client that stayed")
        assert port.mode != ptymod.IDLE                               # it is written to
    finally:
        holder.release()
    wait_for(lambda: broker.pty_info(BOARD, "fpga_uart0")["clients"] == 0, what="gone")


def test_negative_twin_the_kernel_reads_two_back_to_back_opens_as_one():
    # Why a count of 0 is checked against /proc: opens minus closes alone says nobody
    # holds this PTY while a client does.
    ino = ptymod.Inotify.create()
    master, slave = os.openpty()
    try:
        device = os.ttyname(slave)
        wd = ino.add(device)
        stays = os.open(device, os.O_RDWR | os.O_NOCTTY)
        comes_and_goes = os.open(device, os.O_RDWR | os.O_NOCTTY)   # before anything is read
        os.close(comes_and_goes)
        deltas, _ = ino.read()
        assert deltas.get(wd, 0) < 1                                  # one IN_OPEN for two
        os.close(stays)
    finally:
        ino.close()
        os.close(master)
        os.close(slave)


def test_a_client_that_comes_and_goes_leaves_the_recent_output_for_the_next(broker, session):
    info, port = _greeting_held(broker, session)
    _come_and_go(broker, info["path"])
    with PtyClient(info["path"]) as client:
        client.read_until(GREETING, timeout=5)


def test_after_screen_quits_the_line_is_reset_and_the_next_client_sees_the_output(
        broker, session):
    info, port = _greeting_held(broker, session)
    _come_and_go(broker, info["path"], exclusive=True)            # TIOCEXCL left behind
    assert not open_fails_busy(info["path"])                      # the reset DID run
    with PtyClient(info["path"], exclusive=True) as client:
        client.read_until(GREETING, timeout=5)


def test_a_screen_like_client_that_flushes_its_line_still_sees_the_earlier_output(
        broker, session):
    info, port = _greeting_held(broker, session)
    with PtyClient(info["path"], setup_s=0.1) as client:          # TCSAFLUSH at 100 ms
        client.read_until(GREETING, timeout=5)
    assert port.replays == 1


def test_negative_twin_always_live_the_screen_like_flush_loses_the_earlier_output(
        bus, root, session):
    b = ConsoleBroker(bus, pty_options=fast_pty_options(replay_on_attach=False))
    try:
        info = b.pty(session, "fpga_uart0")
        port = b._ptys.get(BOARD, "fpga_uart0")
        wait_for(lambda: queued(port.slave) >= len(GREETING), what="the greeting queued")
        with PtyClient(info["path"], setup_s=0.1) as client, pytest.raises(AssertionError):
            client.read_until(GREETING, timeout=1)
    finally:
        b.shutdown()


# Always live (no inotify count, or replay_on_attach=False), output IS queued on the line:
# a reset must never discard it.


@pytest.fixture
def live_broker(bus, root) -> Iterator[ConsoleBroker]:
    b = ConsoleBroker(bus, backoff=(0.05, 0.2),
                      pty_options=fast_pty_options(replay_on_attach=False))
    yield b
    b.shutdown()


def _greeting_queued(broker, session) -> tuple[dict, object]:
    info = broker.pty(session, "fpga_uart0")
    port = broker._ptys.get(BOARD, "fpga_uart0")
    wait_for(lambda: queued(port.slave) >= len(GREETING), what="the greeting queued in the PTY")
    return info, port


def test_a_reset_itself_never_discards_queued_output(live_broker, session, monkeypatch):
    monkeypatch.setattr(ptymod.ConsolePty, "needs_reset", lambda self: True)
    info, port = _greeting_queued(live_broker, session)
    _come_and_go(live_broker, info["path"])
    assert queued(port.slave) >= len(GREETING)
    with PtyClient(info["path"]) as client:
        client.read_until(GREETING, timeout=5)


def test_negative_twin_a_flushing_reset_loses_the_queued_output(live_broker, session,
                                                                monkeypatch):
    info, port = _greeting_queued(live_broker, session)
    # The bug, put back AFTER the greeting is queued: reset on every 1 -> 0, with the
    # TCSAFLUSH tty.setraw() default. (Patched earlier, the PTY's own set-up flushed the
    # greeting before it was counted: the twin raced itself, ~1 run in 3.)
    monkeypatch.setattr(ptymod.ConsolePty, "needs_reset", lambda self: True)
    monkeypatch.setattr(ptymod, "RESET_WHEN", termios.TCSAFLUSH)
    _come_and_go(live_broker, info["path"])
    assert queued(port.slave) == 0                                # flushed
    with PtyClient(info["path"]) as client, pytest.raises(AssertionError):
        client.read_until(GREETING, timeout=1)


def test_a_come_and_go_with_nothing_left_behind_does_not_reset_the_line(live_broker, session,
                                                                        monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ptymod.ConsolePty, "reset_line", lambda self: calls.append(self.name))
    info, port = _greeting_queued(live_broker, session)
    _come_and_go(live_broker, info["path"])
    assert calls == []
    # Negative twin: a client that leaves TIOCEXCL behind does get a reset.
    _come_and_go(live_broker, info["path"], exclusive=True)
    assert calls == ["fpga_uart0"]
