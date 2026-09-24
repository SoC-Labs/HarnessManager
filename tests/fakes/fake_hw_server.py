"""A stand-in for Vivado's ``hw_server`` (lane XVC-CORE): no Vivado, no cable, 127.0.0.1 only.

It accepts the command line ``services.xvc.hw_server_argv`` builds and behaves the way
the design spike measured the real one (docs/assessment/xvc_spike_2026-09-24/):

- it listens on the ``-s TCP:127.0.0.1:H`` port after a short start-up;
- it opens the XVC target named by ``-e "set auto-open-servers xilinx-xvc:HOST:PORT"``
  LAZILY, when a client connects (and does a ``getinfo:``, a ``settck:`` and an
  IDCODE-sized ``shift:``, as a scan would);
- it lets go of the XVC target about ``FAKE_HW_SERVER_LINGER_S`` after its last client
  leaves;
- SIGTERM ends it at once;
- like the real one, ``-e "set jtag-port-filter F"`` hides every cable whose port name
  (``Xilinx/XVC/HOST:PORT``) does not contain ``F``: the XVC target is then never opened
  (lane XVC-UI, docs/assessment/xvc_ui_2026-09-25/hw_server_cable_filter.txt).

It refuses what Harness Manager must never pass: ``-d`` (daemon) and ``-I`` (idle exit)
exit with status 2. ``FAKE_HW_SERVER_ARGV_LOG``: append the argv as one JSON line.
``FAKE_HW_SERVER_FAIL=exit`` exits with status 1 before listening (a broken install).

Run through a shim script (tests make one): ``python -m tests.fakes.fake_hw_server ...``.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import sys
import threading
import time


def _parse(argv: list[str]) -> tuple[int, str, str | None]:
    port, xvc, port_filter = 0, "", None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-d" or a.startswith("-I"):
            print(f"fake hw_server: {a} is not expected (HM owns this hw_server)", flush=True)
            sys.exit(2)
        if a == "-s" and i + 1 < len(argv):
            port = int(argv[i + 1].rsplit(":", 1)[1])
            i += 2
            continue
        if a == "-e" and i + 1 < len(argv):
            words = argv[i + 1].split()
            if words[:2] == ["set", "auto-open-servers"] and len(words) == 3:
                xvc = words[2].split("xilinx-xvc:", 1)[1]
            if words[:2] == ["set", "jtag-port-filter"]:
                port_filter = " ".join(words[2:])
            i += 2
            continue
        i += 1
    return port, xvc, port_filter


class _Target:
    """The XVC target: open while clients are attached, released after a linger."""

    def __init__(self, xvc: str, linger_s: float) -> None:
        host, port = xvc.rsplit(":", 1)
        self.addr = (host, int(port))
        self.linger_s = linger_s
        self.mu = threading.Lock()
        self.sock: socket.socket | None = None
        self.clients = 0
        self.gen = 0
        self.hidden = False             # a jtag-port-filter that does not admit this cable

    def attach(self) -> None:
        with self.mu:
            self.clients += 1
            self.gen += 1
            if self.sock is not None or self.hidden:
                return
            try:
                s = socket.create_connection(self.addr, timeout=5)
                s.sendall(b"getinfo:")
                info = s.recv(64)
                s.sendall(b"settck:" + struct.pack("<I", 100))
                s.recv(4)
                # Test-Logic-Reset, then shift 32 bits of IDCODE out of DR.
                tms = bytes([0x1F | 0x20, 0x00, 0x00, 0x00, 0x00, 0x00])
                s.sendall(b"shift:" + struct.pack("<I", 48) + tms + bytes(6))
                got = 0
                while got < 6:
                    chunk = s.recv(6 - got)
                    if not chunk:
                        break
                    got += len(chunk)
                print(f"fake hw_server: opened xilinx-xvc:{self.addr[0]}:{self.addr[1]} "
                      f"({info.decode(errors='replace').strip()})", flush=True)
                self.sock = s
            except OSError as exc:
                print(f"fake hw_server: xvc open failed: {exc}", flush=True)

    def detach(self) -> None:
        with self.mu:
            self.clients -= 1
            gen = self.gen

        def later() -> None:
            time.sleep(self.linger_s)
            with self.mu:
                if self.clients == 0 and self.gen == gen and self.sock is not None:
                    self.sock.close()
                    self.sock = None
                    print("fake hw_server: released the xvc target", flush=True)

        threading.Thread(target=later, daemon=True).start()


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_HW_SERVER_ARGV_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "argv": argv}) + "\n")
    port, xvc, port_filter = _parse(argv)
    if os.environ.get("FAKE_HW_SERVER_FAIL") == "exit":
        print("fake hw_server: ERROR: cannot start (FAKE_HW_SERVER_FAIL)", flush=True)
        return 1
    if not port or not xvc:
        print("fake hw_server: need -s TCP:127.0.0.1:H and an auto-open-servers target",
              flush=True)
        return 2
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    time.sleep(float(os.environ.get("FAKE_HW_SERVER_START_S", "0.2")))
    target = _Target(xvc, float(os.environ.get("FAKE_HW_SERVER_LINGER_S", "0.3")))
    if port_filter is not None and port_filter not in f"Xilinx/XVC/{xvc}":
        print(f"fake hw_server: jtag-port-filter {port_filter!r} hides Xilinx/XVC/{xvc}",
              flush=True)
        target.hidden = True
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    print(f"fake hw_server: listening on 127.0.0.1:{port}", flush=True)

    def serve(conn: socket.socket) -> None:
        target.attach()
        try:
            while conn.recv(4096):
                pass
        except OSError:
            pass
        finally:
            conn.close()
            target.detach()

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
