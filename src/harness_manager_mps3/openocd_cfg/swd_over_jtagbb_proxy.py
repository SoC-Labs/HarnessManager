#!/usr/bin/env python3
"""swd_over_jtagbb_proxy.py -- stock OpenOCD in SWD mode, against the shell's
JTAG bit-bang server, with no shell change at all.

WHY. The fielded shell's DUT-debug service is ``jtag_server`` (TCP 6921,
firmware/jtag_server/jtag_server.c), which accepts only the JTAG half of
OpenOCD's remote_bitbang protocol ('0'..'7' writes, 'R' reads) and fails
closed on the SWD half ('d'..'g', 'O'/'o', 'c'): jtag_server.c:165-171.
The SWD server that spoke the other half (swd_server.c, TCP 6920) is no longer
built into the image (firmware/platform/Makefile, LEGACY_SWD).

But the two servers poke the SAME register with the SAME bit layout:

    jtag_server  DRIVE = {tdi, tms, tck}          (jtag_server.c:79-86)
    swd_server   DRIVE = {swdio_oe, swdio_o, swclk} (swd_server.c:40-47)

so a JTAG write char '0' + (tck<<2 | tms<<1 | tdi) IS an SWD pin state
(swclk, swdio_o, swdio_oe), and 'R' IS the SWDIO sample. An RM that reads the
four jtag_* partition pins as SWD (fpga/rp/nanosoc_qs) is therefore reachable
by re-spelling OpenOCD's SWD bytes as JTAG bytes, one for one:

    SWD char                 state change            JTAG char sent
    'd'+(clk<<1|dio)         clk, dio := ...         '0'+(clk<<2|dio<<1|oe)
    'O' / 'o'                oe := 1 / 0             '0'+(clk<<2|dio<<1|oe)
    'c'                      (read SWDIO)            'R'  (reply passed back)
    'r'..'u', 'B', 'b', 'Q'  (reset / blink / quit)  unchanged (identical in both)

One byte in, one byte out, so the proxy adds no round trips; replies ('0'/'1')
flow back unchanged.

USAGE
    python3 swd_over_jtagbb_proxy.py --board 192.168.10.101 [--board-port 6921]
                                     [--listen 127.0.0.1:6920]
    openocd -f host/openocd/swd_remote_bitbang.cfg -c "set SHELL_HOST 127.0.0.1"
            ... (SHELL_SWD_PORT defaults to 6920, which is where this listens)

Stdlib only. The translation is ``Translator.feed`` and is unit-testable
without a socket; ``tests`` below the class exercise it.
"""
from __future__ import annotations

import argparse
import select
import socket
import sys

__all__ = ["Translator", "serve"]


class Translator:
    """Stateful SWD-remote_bitbang -> JTAG-remote_bitbang byte re-speller."""

    def __init__(self, mapping: str = "tck=clk,tms=dio,tdi=oe") -> None:
        # The mapping is fixed by the RTL (rp_nanosoc_qs_wrapper.sv); it is a
        # parameter only so a negative control can prove a wrong one fails.
        self.clk = 0
        self.dio = 0
        self.oe = 0
        self.swap_tms_tdi = mapping == "tck=clk,tms=oe,tdi=dio"
        if mapping not in ("tck=clk,tms=dio,tdi=oe", "tck=clk,tms=oe,tdi=dio"):
            raise ValueError(f"unknown mapping {mapping!r}")

    def _pins(self) -> bytes:
        tms, tdi = (self.oe, self.dio) if self.swap_tms_tdi else (self.dio, self.oe)
        return bytes([ord("0") + (self.clk << 2 | tms << 1 | tdi)])

    def feed_byte(self, c: int) -> bytes:
        if ord("d") <= c <= ord("g"):
            v = c - ord("d")
            self.clk, self.dio = (v >> 1) & 1, v & 1
            return self._pins()
        if c == ord("O"):
            self.oe = 1
            return self._pins()
        if c == ord("o"):
            self.oe = 0
            return self._pins()
        if c == ord("c"):
            return b"R"
        if c in b"rstuBbQ":
            return bytes([c])
        # Anything else is not an SWD-mode remote_bitbang byte. Refuse rather
        # than forward: jtag_server would drop the link on it anyway, and a
        # proxy that silently desyncs is worse than one that says so.
        raise ValueError(f"not an SWD remote_bitbang byte: {c!r}")

    def feed(self, data: bytes) -> bytes:
        return b"".join(self.feed_byte(c) for c in data)


def serve(listen: tuple[str, int], board: tuple[str, int]) -> None:
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(listen)
    ls.listen(1)
    print(f"swd_over_jtagbb_proxy: SWD on {listen[0]}:{listen[1]} -> JTAG bit-bang "
          f"{board[0]}:{board[1]}", flush=True)
    while True:
        cli, _ = ls.accept()
        up = socket.create_connection(board)
        for s in (cli, up):
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tr = Translator()
        try:
            while True:
                r, _, _ = select.select([cli, up], [], [])
                if cli in r:
                    d = cli.recv(4096)
                    if not d:
                        break
                    up.sendall(tr.feed(d))
                if up in r:
                    d = up.recv(4096)
                    if not d:
                        break
                    cli.sendall(d)
        except ValueError as e:
            print(f"swd_over_jtagbb_proxy: {e}; dropping the session", file=sys.stderr)
        finally:
            cli.close()
            up.close()


def _selftest() -> int:
    t = Translator()
    assert t.feed(b"O") == b"1"           # oe=1, clk=0, dio=0 -> tdi=1
    assert t.feed(b"g") == b"7"           # clk=1, dio=1, oe=1
    assert t.feed(b"e") == b"3"           # clk=0, dio=1
    assert t.feed(b"o") == b"2"           # oe=0 (dio stays 1)
    assert t.feed(b"c") == b"R"
    assert t.feed(b"rsQ") == b"rsQ"
    try:
        t.feed(b"5")                      # a JTAG byte is not SWD input
    except ValueError:
        pass
    else:
        raise AssertionError("JTAG byte accepted as SWD")
    print("swd_over_jtagbb_proxy selftest: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--board", help="shell IP (jtag_server host)")
    ap.add_argument("--board-port", type=int, default=6921)
    ap.add_argument("--listen", default="127.0.0.1:6920")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.board:
        ap.error("--board is required")
    host, port = a.listen.rsplit(":", 1)
    serve((host, int(port)), (a.board, a.board_port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
