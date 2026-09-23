"""``socharness console`` as an interactive terminal (lead): keys to the board, Ctrl-] exits.

- unit: ``_terminal`` with fake keys and a fake stream (escape, write errors);
- end to end: the real CLI ``main()`` on a pseudo-terminal, against the virtual
  board's uart0 (FakeShell echoes every byte), through the real Engine and MPS3 pack.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types

import pytest

from socharness.cli import cmd_io
from socharness.cli.engine import ENV_NO_DAEMON, set_engine_factory
from socharness.core.errors import UnreachableError
from socharness.core.services import EngineConfig
from socharness.engine import Engine
from socharness_board_mps3.pack import Mps3Pack


class FakeKeys:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)
        self.entered = self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True

    def read(self, timeout: float) -> bytes:
        time.sleep(0.01)
        return self.chunks.pop(0) if self.chunks else b""


class FakeStream:
    def __init__(self, output: bytes = b"", fail: bool = False) -> None:
        self.output, self.fail, self.written, self.closed = output, fail, [], False

    def read(self, timeout=None) -> bytes:
        out, self.output = self.output, b""
        if not out:
            time.sleep(0.01)
        return out

    def write(self, data: bytes) -> None:
        if self.fail:
            raise UnreachableError("console 'uart0' is not connected (state down)")
        self.written.append(data)


def ctx_for(name: str = "uart0"):
    notes: list[str] = []
    return types.SimpleNamespace(args=types.SimpleNamespace(name=name), note=notes.append), notes


def test_keys_go_to_the_board_and_ctrl_bracket_exits(capsys):
    ctx, notes = ctx_for()
    keys = FakeKeys(b"print(1+1)\r", b"\x03", b"x\x1dnever sent")
    text = cmd_io._terminal(ctx, FakeStream(b">>> "), "mps3@b", keys)
    assert keys.entered and keys.exited                       # raw mode entered and left
    assert "Ctrl-] exits" in notes[0]
    assert text == ">>> " and ">>> " in capsys.readouterr().out


def test_every_byte_before_the_escape_is_sent_and_nothing_after(capsys):
    ctx, _ = ctx_for()
    stream = FakeStream()
    cmd_io._terminal(ctx, stream, "mps3@b", FakeKeys(b"ab", b"\x03", b"c\x1dd"))
    assert stream.written == [b"ab", b"\x03", b"c"]           # Ctrl-C reaches the board


def test_negative_twin_a_write_error_is_shown_and_the_terminal_stays(capsys):
    ctx, _ = ctx_for()
    stream = FakeStream(fail=True)
    cmd_io._terminal(ctx, stream, "mps3@b", FakeKeys(b"a", b"\x1d"))
    assert "[socharness: console 'uart0' is not connected" in capsys.readouterr().out


def test_json_tsv_for_and_read_only_are_never_interactive(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    base = dict(name="uart0", for_s=None, read_only=False)
    assert cmd_io._interactive(types.SimpleNamespace(fmt="human", args=types.SimpleNamespace(**base)))
    for fmt, extra in (("json", {}), ("tsv", {}), ("human", {"for_s": 1.0}),
                       ("human", {"read_only": True})):
        args = types.SimpleNamespace(**{**base, **extra})
        assert not cmd_io._interactive(types.SimpleNamespace(fmt=fmt, args=args))


@pytest.mark.skipif(os.name == "nt", reason="pseudo-terminals are POSIX")
def test_end_to_end_on_a_pty_against_the_virtual_uart0(vboard, monkeypatch):
    import pty

    from socharness.cli.main import main

    monkeypatch.setenv(ENV_NO_DAEMON, "1")
    eng = Engine(EngineConfig(), packs={"mps3": Mps3Pack(console_ports=vboard.console_ports)})
    previous = set_engine_factory(lambda _args: eng)
    master, slave = pty.openpty()
    tty_in = os.fdopen(os.dup(slave), "r", buffering=1)
    tty_out = os.fdopen(os.dup(slave), "w", buffering=1)
    monkeypatch.setattr(sys, "stdin", tty_in)
    monkeypatch.setattr(sys, "stdout", tty_out)
    import termios

    before = termios.tcgetattr(slave)
    result: dict = {}
    runner = threading.Thread(
        target=lambda: result.setdefault("rc", main(["console", vboard.shell_endpoint, "uart0"])),
        daemon=True)
    seen = bytearray()

    def drain(until: bytes, timeout: float = 10.0) -> None:
        end = time.monotonic() + timeout
        while until not in seen and time.monotonic() < end:
            import select
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                seen.extend(os.read(master, 4096))
        assert until in seen, bytes(seen)

    try:
        runner.start()
        drain(b"nanosoc boot")                          # FakeShell's uart0 banner
        os.write(master, b"hi\r")
        drain(b"hi\r")                                  # the echo: typed, paced, returned
        os.write(master, b"\x1d")                       # Ctrl-]
        runner.join(timeout=10)
        assert not runner.is_alive() and result["rc"] == 0
        assert termios.tcgetattr(slave) == before         # the terminal is given back cooked
    finally:
        set_engine_factory(previous)
        eng.close_all()
        for f in (tty_in, tty_out):
            f.close()
        os.close(master)
        os.close(slave)
