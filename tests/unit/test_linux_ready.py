"""LINUX-READY: the host side of the Linux harness's B1 v4 findings (silicon, 2026-09-25).

- the vendored pyverify (platform 3f7cea2; 3bfda65 when written): every pyverify name HM imports
  still exists;
- the push rule: the Linux harness gets plain tcp with pyverify's 30 s push inactivity
  limit (``choose_push``), bare metal keeps its rule and the 2 s limit;
- 6900 after a failed push: the harness turns new clients away until the swap's 30 s
  idle timeout. Within 35 s of a push this process ran that failed, that is "busy" with
  the swap hint, never "offline" or "another client".

Each claim has its negative twin. The 6900 cases use real sockets on 127.0.0.1.
"""

from __future__ import annotations

import ast
import importlib
import json
import socket
import threading
from pathlib import Path

import pytest
from pyverify import pusher as pv_pusher

from harness_manager.core.errors import HeldError
from harness_manager_mps3 import shell as sh
from harness_manager_mps3.capabilities import HARNESS_STATES
from harness_manager_mps3.deploy import (
    TRANSPORT_TCP,
    TRANSPORT_TFTP,
    TRANSPORT_WINDOWED,
    _check_transport,
    _Live,
    push_timeout_s,
)
from harness_manager_mps3.shell import (
    Mps3Shell,
    ShellProbes,
    ShellRefusedError,
    SwapSettlingError,
)

SRC = Path(__file__).resolve().parents[2] / "src"

V011 = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed", "dut_egress", "jtag_server",
        "xvc_dbgbr", "stats", "log", "reboot", "touch_cal")
LINUX = tuple(f for f in V011 if f != "windowed") + ("usd",)   # B1 v4's harnessd list
NON_WINDOWED = ("clcd", "clcd_kvm", "touch", "hwicap_fifo")


# -- the vendored pyverify ------------------------------------------------------------


def _pyverify_imports(tree: ast.AST) -> list[tuple[str, str]]:
    return [(node.module, alias.name) for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            and node.module.split(".")[0] == "pyverify" for alias in node.names]


def _missing(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    missing = []
    for module, name in pairs:
        try:
            mod = importlib.import_module(module)
        except ImportError:
            missing.append((module, name))
            continue
        if not hasattr(mod, name):
            try:
                importlib.import_module(f"{module}.{name}")
            except ImportError:
                missing.append((module, name))
    return missing


def test_every_pyverify_name_harness_manager_imports_exists():
    pairs = sorted({pair for path in SRC.rglob("*.py")
                    for pair in _pyverify_imports(ast.parse(path.read_text()))})
    assert ("pyverify.pusher", "LINUX_PUSH_TIMEOUT_S") in pairs     # the scan sees the new use
    assert _missing(pairs) == []


def test_negative_twin_the_scan_flags_a_removed_name():
    tree = ast.parse("from pyverify.pusher import choose_push, no_such_name\n"
                     "from pyverify.no_such_module import thing\n")
    assert _missing(_pyverify_imports(tree)) == [("pyverify.pusher", "no_such_name"),
                                                 ("pyverify.no_such_module", "thing")]


def test_the_vendored_pyverify_has_the_b1_v4_push_rule():
    assert callable(pv_pusher.choose_push)
    assert pv_pusher.LINUX_PUSH_TIMEOUT_S == 30.0
    assert pv_pusher.DEFAULT_PUSH_TIMEOUT_S == 2.0


# -- the push rule ----------------------------------------------------------------------


def live(features=(), impl="bare-metal", version_ok=True) -> _Live:
    return _Live("0x1a102610", "0x0", version_ok, tuple(features), impl=impl)


def test_linux_gets_plain_tcp_with_a_30_s_inactivity_limit():
    for tunnelled in (False, True):
        _, transport = _check_transport(live(LINUX, impl="linux"), tunnelled)
        assert transport == TRANSPORT_TCP
        assert push_timeout_s("linux", transport) == 30.0


def test_negative_twin_bare_metal_keeps_windowed_and_2_s():
    _, transport = _check_transport(live(V011))
    assert transport == TRANSPORT_WINDOWED
    assert push_timeout_s("bare-metal", transport) == 2.0
    _, transport = _check_transport(live(NON_WINDOWED), tunnelled=True)   # tcp, not Linux
    assert (transport, push_timeout_s("bare-metal", transport)) == (TRANSPORT_TCP, 2.0)


def test_a_shell_with_no_version_keeps_todays_rule():
    _, transport = _check_transport(live(version_ok=False, impl=""))
    assert (transport, push_timeout_s("", transport)) == (TRANSPORT_TFTP, 2.0)
    _, transport = _check_transport(live(version_ok=False, impl=""), tunnelled=True)
    assert (transport, push_timeout_s("", transport)) == (TRANSPORT_TCP, 2.0)


class _Version:
    def __init__(self, features, impl):
        self.ok, self.features, self.impl = True, tuple(features), impl


class _Client:
    def __init__(self, version=None):
        self._version = version

    def version(self):
        if self._version is None:
            raise ValueError("unknown op 'version'")
        return self._version


HM_TO_PYVERIFY = {TRANSPORT_WINDOWED: ("tcp", True), TRANSPORT_TCP: ("tcp", False),
                  TRANSPORT_TFTP: ("tftp", False)}


@pytest.mark.parametrize(("features", "impl", "version_ok"), [
    (LINUX, "linux", True),
    (V011, None, True),
    (NON_WINDOWED, None, True),
    ((), None, False),
], ids=["linux", "bare-metal-windowed", "bare-metal-plain", "no-version"])
def test_harness_manager_and_pyverify_choose_the_same_push(features, impl, version_ok):
    hm = live(features, impl=(impl or "bare-metal") if version_ok else "", version_ok=version_ok)
    _, transport = _check_transport(hm)
    choice = pv_pusher.choose_push(_Client(_Version(features, impl) if version_ok else None))
    assert (choice.transport, choice.windowed) == HM_TO_PYVERIFY[transport]
    assert choice.timeout_s == push_timeout_s(hm.impl, transport)


def test_negative_twin_a_tunnel_is_the_one_deliberate_difference():
    # TFTP cannot cross an SSH forward, so Harness Manager takes tcp where pyverify
    # (which never runs through one) keeps tftp. The limit stays pyverify's: 2 s.
    _, transport = _check_transport(live(NON_WINDOWED), tunnelled=True)
    choice = pv_pusher.choose_push(_Client(_Version(NON_WINDOWED, None)))
    assert HM_TO_PYVERIFY[transport] != (choice.transport, choice.windowed)
    assert push_timeout_s("bare-metal", transport) == choice.timeout_s == 2.0


# -- 6900 turned away while the harness finishes a failed swap --------------------------

NO_PROBES = ShellProbes(identify=lambda host, timeout: None,
                        tcp=lambda host, port, timeout: False,
                        icmp=lambda host, timeout: False)


@pytest.fixture(autouse=True)
def _no_failed_pushes():
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()
    yield
    with sh._failed_pushes_lock:
        sh._failed_pushes.clear()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """A one-line 6900: ``reply`` answers every request; None = accept, then close."""

    def __init__(self, reply: dict | None) -> None:
        self.reply = reply
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                if self.reply is None:
                    continue                           # accept, then close
                conn.settimeout(2)
                buf = b""
                try:
                    while not buf.endswith(b"\n"):
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    if buf:
                        conn.sendall(json.dumps(self.reply).encode() + b"\n")
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.sock.close()


@pytest.fixture
def server():
    made: list[_Server] = []

    def make(reply: dict | None) -> _Server:
        made.append(_Server(reply))
        return made[-1]

    yield make
    for s in made:
        s.close()


def shell_at(port: int) -> Mps3Shell:
    return Mps3Shell("127.0.0.1", port, timeout=1.0, probes=NO_PROBES)


def ping(shell: Mps3Shell):
    return shell.call(lambda c: c.ping())


SETTLING = HARNESS_STATES["harness.swap_settling"]


def test_a_refusal_after_our_failed_push_is_busy_with_the_hint():
    shell = shell_at(_closed_port())
    shell.note_failed_push()
    with pytest.raises(SwapSettlingError) as err:
        ping(shell)
    assert isinstance(err.value, HeldError) and err.value.code == HeldError.code
    assert err.value.hint == SETTLING and "within 30 s" in SETTLING
    assert 0.0 < err.value.remaining_s <= sh.FAILED_PUSH_WINDOW_S
    health = shell.health()
    assert (health.reachable, health.control_channel) == (True, "busy")
    assert health.notes[0] == SETTLING


def test_negative_twin_a_refusal_with_no_recent_push_stays_offline():
    shell = shell_at(_closed_port())
    with pytest.raises(ShellRefusedError):
        ping(shell)
    health = shell.health()
    assert (health.reachable, health.control_channel) == (False, "offline")
    assert SETTLING not in health.notes


def test_negative_twin_the_window_closes_after_35_s(monkeypatch):
    shell = shell_at(_closed_port())
    t = [1000.0]
    monkeypatch.setattr(sh, "clock", lambda: t[0])
    shell.note_failed_push()
    t[0] += sh.FAILED_PUSH_WINDOW_S - 1.0
    with pytest.raises(SwapSettlingError):
        ping(shell)
    t[0] += 2.0
    with pytest.raises(ShellRefusedError):
        ping(shell)
    assert shell.health().control_channel == "offline"


def test_negative_twin_a_push_to_another_board_does_not_count():
    shell = shell_at(_closed_port())
    sh.note_failed_push("127.0.0.1", _closed_port())
    with pytest.raises(ShellRefusedError):
        ping(shell)


def test_accept_then_close_after_our_failed_push_is_the_swap_not_another_client(server):
    srv = server(None)
    shell = shell_at(srv.port)
    shell.note_failed_push()
    with pytest.raises(SwapSettlingError) as err:
        ping(shell)
    assert err.value.hint == SETTLING
    assert shell.health().notes[0] == SETTLING


def test_negative_twin_accept_then_close_with_no_recent_push_is_another_client(server):
    shell = shell_at(server(None).port)
    with pytest.raises(HeldError) as err:
        ping(shell)
    assert not isinstance(err.value, SwapSettlingError)
    health = shell.health()
    assert health.control_channel == "busy" and health.notes[0] == HARNESS_STATES["harness.busy"]


def test_the_harness_saying_so_is_busy_settling_with_no_push_of_ours(server):
    srv = server({"ok": False, "err": "EBUSY", "since_ms": 1200, "swap": "await_partial"})
    with pytest.raises(SwapSettlingError) as err:
        ping(shell_at(srv.port))
    assert err.value.hint == SETTLING


def test_negative_twin_a_plain_ebusy_names_another_client_even_in_the_window(server):
    srv = server({"ok": False, "err": "EBUSY", "since_ms": 1200, "holder": "10.0.0.7:5123"})
    shell = shell_at(srv.port)
    shell.note_failed_push()
    with pytest.raises(HeldError) as err:
        ping(shell)
    assert not isinstance(err.value, SwapSettlingError)
    assert err.value.holder == "10.0.0.7:5123"


def test_an_answer_closes_the_window(server):
    srv = server({"ok": True, "shell_id": "0x1a102610", "rm_id": "0x00000000"})
    shell = shell_at(srv.port)
    shell.note_failed_push()
    assert shell.settling_remaining_s() > 0.0
    assert ping(shell).ok
    assert shell.settling_remaining_s() == 0.0
