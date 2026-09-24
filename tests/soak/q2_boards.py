"""The boards side of the Q2 soak: N ``VirtualMps3`` boards in a process of their own.

``python -m tests.soak.q2_boards --n 3 --out DIR`` starts the boards (the ILA mint's
bake profile: the static on the lab board since 09-24), writes ``DIR/routes.json``
(``{"192.168.10.10N:PORT": "127.0.0.1:EPHEMERAL"}``, what the fake ssh on the
daemon's PATH forwards to) and ``DIR/boards.json`` (their ids), then serves until
SIGTERM. ``DIR/control`` takes commands one per line, so the soak can make a board
vanish and come back: ``stop N`` / ``start N`` (a board index).

Kept out of the daemon's process so the daemon's fds, threads and memory are its own.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

from tests.fakes.virtual_board import VirtualMps3, ila_mint_bake_profile

STATIC_ID = 0x72BB0A36
BOARD_IP = "192.168.10.{}"


def board_ip(i: int) -> str:
    return BOARD_IP.format(101 + i)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=3)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    profile = ila_mint_bake_profile(STATIC_ID)
    boards: list[VirtualMps3] = []
    with tempfile.TemporaryDirectory(prefix="hm-q2-boards-") as tmp:
        for i in range(a.n):
            vb = VirtualMps3(Path(tmp) / f"b{i}", profile).__enter__()
            vb.shell.console_echo = True
            boards.append(vb)
        routes: dict[str, str] = {}
        for i, vb in enumerate(boards):
            ip = board_ip(i)
            ports = {6900: vb.shell.control_port, 6910: vb.shell.raw_tcp_port,
                     **{6930 + k: vb.console_ports.get(n, 0)
                        for k, n in enumerate(("uart0", "uart1", "swo"))}}
            for remote, local in ports.items():
                if local:
                    routes[f"{ip}:{remote}"] = f"127.0.0.1:{local}"
        (out / "routes.json").write_text(json.dumps(routes, indent=1))
        (out / "boards.json").write_text(json.dumps(
            [{"ip": board_ip(i), "board_id": f"mps3@{board_ip(i)}:6900"}
             for i in range(len(boards))], indent=1))
        control = out / "control"
        control.write_text("")
        done = 0
        (out / "ready").write_text("ok\n")
        while not stop.wait(0.2):
            lines = control.read_text().splitlines()
            for line in lines[done:]:
                verb, _, idx = line.partition(" ")
                try:
                    vb = boards[int(idx)]
                except (ValueError, IndexError):
                    continue
                if verb == "stop":
                    vb.shell.stop()
                elif verb == "start":
                    vb.shell.start()
                print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
            done = len(lines)
        for vb in boards:
            vb.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
