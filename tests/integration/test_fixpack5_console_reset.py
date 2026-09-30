"""FIX-PACK-5 item 1: resetting the DUT while ``harness-manager console`` is open.

The guide walk (board 2, 2026-09-30 14:34) ran ``console B uart0 --read-only --for 25 &``
and then ``reset B`` from a second terminal, which exited 4: "your own `harness-manager
console uart0` (pid ...) holds it". The cause is Harness Manager's own session lock (one
process owns a board; ``Engine.open`` takes ``<state_dir>/locks/<board>``), never 6900: the
MPS3 shell opens, asks and closes 6900 per call, and the DUT consoles are on 6930/6931.

The fix, and each check's twin:

- the interactive console resets the DUT itself: Ctrl-] then r arms, r or y confirms
  (twin: any other key cancels, nothing is reset and nothing is sent to the board);
- Ctrl-] then another key still exits at once, and Ctrl-] alone exits after the wait;
- a reset refused by the board is shown and the console stays;
- end to end on a pseudo-terminal against the virtual board (FakeShell counts the resets);
- in-process, a second terminal's ``reset`` is still refused, now with the escape in the
  hint; through the running service the two share its session and the reset is done.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import types

import pytest

from harness_manager.cli import cmd_io
from harness_manager.cli.engine import ENV_NO_DAEMON, set_engine_factory
from harness_manager.cli.main import main
from harness_manager.core.errors import ActionFailedError, ExitCode
from tests.fakes.t13_daemon import LiveDaemon, engine_for, wait_for


class FakeKeys:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self, timeout: float) -> bytes:
        time.sleep(0.01)
        return self.chunks.pop(0) if self.chunks else b""


class FakeStream:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def read(self, timeout=None) -> bytes:
        time.sleep(0.01)
        return b""

    def write(self, data: bytes) -> None:
        self.written.append(data)


def ctx_for(name: str = "uart0"):
    notes: list[str] = []
    return types.SimpleNamespace(args=types.SimpleNamespace(name=name), note=notes.append), notes


class Resetter:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls, self.fail = 0, fail

    def __call__(self) -> None:
        self.calls += 1
        if self.fail is not None:
            raise self.fail


# -- the escape, unit -----------------------------------------------------------------------------


def test_ctrl_bracket_r_r_resets_the_dut_and_the_console_stays(capsys):
    ctx, notes = ctx_for()
    stream, reset = FakeStream(), Resetter()
    keys = FakeKeys(b"a", b"\x1d", b"r", b"r", b"b", b"\x1d", b"q")
    cmd_io._terminal(ctx, stream, "mps3@b", keys, reset=reset)
    out = capsys.readouterr().out
    assert reset.calls == 1
    assert stream.written == [b"a", b"b"]               # the escape keys never reach the board
    assert "reset the DUT of mps3@b? r or y resets it" in out
    assert "reset the DUT of mps3@b: done" in out
    assert "Ctrl-] then r, r resets the DUT" in notes[0] and "Ctrl-] exits" in notes[0]


def test_y_confirms_too_and_keys_in_one_chunk_work():
    ctx, _ = ctx_for()
    stream, reset = FakeStream(), Resetter()
    cmd_io._terminal(ctx, stream, "mps3@b", FakeKeys(b"\x1dRyz", b"\x1dx"), reset=reset)
    assert reset.calls == 1 and stream.written == [b"z"]


def test_negative_twin_any_other_confirm_key_cancels(capsys):
    ctx, _ = ctx_for()
    stream, reset = FakeStream(), Resetter()
    cmd_io._terminal(ctx, stream, "mps3@b", FakeKeys(b"\x1d", b"r", b"n", b"c", b"\x1d", b"q"),
                     reset=reset)
    assert reset.calls == 0
    assert stream.written == [b"c"]                      # "n" was the answer, not typing
    assert "not reset; back to the console" in capsys.readouterr().out


def test_negative_twin_ctrl_bracket_then_another_key_exits_at_once():
    ctx, _ = ctx_for()
    stream, reset = FakeStream(), Resetter()
    started = time.monotonic()
    cmd_io._terminal(ctx, stream, "mps3@b", FakeKeys(b"x\x1dnever sent"), reset=reset)
    assert time.monotonic() - started < cmd_io.ESCAPE_WAIT_S
    assert reset.calls == 0 and stream.written == [b"x"]


def test_ctrl_bracket_alone_exits_after_the_wait(monkeypatch):
    monkeypatch.setattr(cmd_io, "ESCAPE_WAIT_S", 0.3)
    ctx, _ = ctx_for()
    reset = Resetter()
    cmd_io._terminal(ctx, FakeStream(), "mps3@b", FakeKeys(b"\x1d"), reset=reset)
    assert reset.calls == 0


def test_without_a_reset_adapter_ctrl_bracket_exits_at_once_as_before():
    ctx, notes = ctx_for()
    stream = FakeStream()
    started = time.monotonic()
    cmd_io._terminal(ctx, stream, "mps3@b", FakeKeys(b"ab\x1dr"))
    assert time.monotonic() - started < cmd_io.ESCAPE_WAIT_S
    assert stream.written == [b"ab"] and "resets the DUT" not in notes[0]


def test_negative_twin_a_refused_reset_is_shown_and_the_console_stays(capsys):
    ctx, _ = ctx_for()
    stream = FakeStream()
    reset = Resetter(ActionFailedError("shell refused reset target 'dut'", hint="try info"))
    cmd_io._terminal(ctx, stream, "mps3@b", FakeKeys(b"\x1d", b"r", b"r", b"k", b"\x1d", b"q"),
                     reset=reset)
    out = capsys.readouterr().out
    assert reset.calls == 1 and stream.written == [b"k"]
    assert "the DUT was not reset: shell refused reset target 'dut'; try info" in out


def test_the_console_offers_the_escape_only_with_a_reset_adapter():
    ctx = types.SimpleNamespace(args=types.SimpleNamespace(name="uart0"))
    assert cmd_io._dut_reset(ctx, types.SimpleNamespace(resets=None), "b") is None
    assert callable(cmd_io._dut_reset(ctx, types.SimpleNamespace(resets=object()), "b"))


# -- end to end on a pseudo-terminal, against the virtual board --------------------------------


def _pty_console(vboard, monkeypatch, keys: list[bytes], expect: list[bytes]) -> dict:
    """The real CLI ``console`` on a PTY: send each of ``keys`` after its ``expect``."""
    import pty
    import select

    monkeypatch.setenv(ENV_NO_DAEMON, "1")
    eng = engine_for(vboard)
    previous = set_engine_factory(lambda _args: eng)
    master, slave = pty.openpty()
    tty_in = os.fdopen(os.dup(slave), "r", buffering=1)
    tty_out = os.fdopen(os.dup(slave), "w", buffering=1)
    monkeypatch.setattr(sys, "stdin", tty_in)
    monkeypatch.setattr(sys, "stdout", tty_out)
    result: dict = {"seen": bytearray()}
    runner = threading.Thread(
        target=lambda: result.setdefault("rc", main(["console", vboard.shell_endpoint, "uart0"])),
        daemon=True)

    def drain(until: bytes, timeout: float = 10.0) -> None:
        seen = result["seen"]
        end = time.monotonic() + timeout
        while until not in seen and time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                seen.extend(os.read(master, 4096))
        assert until in seen, bytes(seen)

    try:
        runner.start()
        drain(b"nanosoc boot")
        for key, want in zip(keys, expect, strict=True):
            os.write(master, key)
            drain(want)
        runner.join(timeout=10)
        assert not runner.is_alive()
    finally:
        set_engine_factory(previous)
        eng.close_all()
        for f in (tty_in, tty_out):
            f.close()
        os.close(master)
        os.close(slave)
    return result


@pytest.mark.skipif(os.name == "nt", reason="pseudo-terminals are POSIX")
def test_end_to_end_the_console_resets_the_virtual_dut(vboard, monkeypatch):
    result = _pty_console(vboard, monkeypatch,
                          [b"\x1d", b"r", b"r", b"\x1d", b"q"],
                          [b"r resets the DUT", b"r or y resets it", b": done]",
                           b"any other key exits", b""])
    assert result["rc"] == 0
    assert vboard.shell.resets == ["dut"]                 # FakeShell saw exactly one reset


@pytest.mark.skipif(os.name == "nt", reason="pseudo-terminals are POSIX")
def test_end_to_end_negative_twin_cancel_resets_nothing(vboard, monkeypatch):
    result = _pty_console(vboard, monkeypatch,
                          [b"\x1d", b"r", b"n", b"\x1d", b"q"],
                          [b"r resets the DUT", b"r or y resets it", b"not reset",
                           b"any other key exits", b""])
    assert result["rc"] == 0 and vboard.shell.resets == []


# -- a second terminal: in-process vs through the service -------------------------------------------


def _console_in_thread(ep: str, seconds: float) -> tuple[threading.Thread, dict]:
    result: dict = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "rc", main(["console", ep, "uart0", "--read-only", "--for", str(seconds)])), daemon=True)
    t.start()
    return t, result


def test_negative_twin_in_process_a_second_reset_is_refused_naming_the_escape(
        vboard, monkeypatch, capsys):
    monkeypatch.setenv(ENV_NO_DAEMON, "1")
    # A new Engine per verb: two terminals, each with its own in-process engine.
    previous = set_engine_factory(lambda _args: engine_for(vboard))
    ep = vboard.shell_endpoint
    probe = engine_for(vboard)
    bid = probe.candidate_for(ep).board_id
    try:
        t, result = _console_in_thread(ep, 4)
        try:
            wait_for(lambda: probe.lock_owner(bid), what="the console's lock")
            rc = main(["--json", "reset", ep])
            out, _err = capsys.readouterr()
        finally:
            t.join(timeout=15)
    finally:
        set_engine_factory(previous)
        probe.close_all()
    err = json.loads(out.strip().splitlines()[-1])["error"]    # after the console's own output
    assert rc == ExitCode.HELD and "console uart0" in err["hint"]
    assert "Ctrl-] then r, r" in err["hint"] and "harness-manager daemon start" in err["hint"]
    assert vboard.shell.resets == []                      # refused before the board
    assert result["rc"] == 0


def test_through_the_service_a_second_reset_shares_the_console_session(vboard, capsys):
    ep = vboard.shell_endpoint
    eng = engine_for(vboard)
    try:
        with LiveDaemon(eng):
            t, result = _console_in_thread(ep, 4)
            try:
                wait_for(lambda: eng.open_boards(), what="the console's session in the service")
                rc = main(["--json", "reset", ep])
                out, _err = capsys.readouterr()
            finally:
                t.join(timeout=15)
    finally:
        eng.close_all()
    body = json.loads(out.strip().splitlines()[-1])
    assert rc == ExitCode.OK and body["target"] == "dut" and body["result"] == "done"
    assert vboard.shell.resets == ["dut"]
    assert result["rc"] == 0
