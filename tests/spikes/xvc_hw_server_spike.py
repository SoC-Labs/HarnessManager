"""XVC design spike, part 2: a REAL Vivado hw_server that Harness Manager would own,
against the fake XVC server, 127.0.0.1 only (no cable, no board, no hub).

Questions it answers for docs/design/XVC_DEBUG.md:

1. Does an HM-owned hw_server (``xvc_spike.hw_server_argv``: private port, ``-p0``, no
   ``-d``/``-I``, XVC target pre-opened with ``-e "set auto-open-servers ..."``) see the
   device the way Vivado saw it on silicon (``debug_bridge``, IDCODE 0x0A003093)?
2. Does it hold the board's ONE XVC slot while no client is attached?
3. A partition swap under an OPEN session: the firmware gate stalls shifts (here 12 s)
   and the chain behind the bridge changes. Does hw_server drop, hang, or re-detect?
4. After the swap, does the SAME hw_server serve a new client correctly, and a fresh one?

The chain change is modelled as an IDCODE change (the fake has one TAP). On silicon the
IDCODE stays 0x0A003093 and what changes is the debug hub behind the BSCAN master, which
the fake does not model; see the design doc for what that leaves open.

Run (from the repo root):

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. nice -n 10 python -m tests.spikes.xvc_hw_server_spike \
        --vivado-bin /apps/Xilinx/Vivado/2024.1/bin

It starts and stops only its own hw_server processes (their own process group) and
writes scratch to /tmp/xvc-hwspike-<pid>/ (removed unless --keep).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from harness_manager.services import xvc_spike as X
from harness_manager_mps3.tunnel import listening
from tests.spikes.xvc_fake_server import FakeXvcServer, Tap

NEW_RM_IDCODE = 0x13631093


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Rig:
    def __init__(self, vivado_bin: Path, root: Path) -> None:
        self.hw = str(vivado_bin / "hw_server")
        self.xsdb_bin = str(vivado_bin / "xsdb")
        self.root = root
        self.fake = FakeXvcServer(tap=Tap()).start()
        self.t0 = time.monotonic()
        self.lines: list[str] = []

    def say(self, text: str) -> None:
        line = f"[{time.monotonic() - self.t0:6.1f}s] {text}"
        self.lines.append(line)
        print(line, flush=True)

    def state(self, tag: str) -> None:
        s = self.fake.stats
        self.say(f"{tag}: slot={'held by ' + self.fake.attached if self.fake.attached else 'free'}"
                 f" connections={s.connections} shifts={s.shifts} stalled_polls={s.stalled_shifts}"
                 f" dropped={s.dropped}")

    def start_hw(self) -> tuple[subprocess.Popen, int]:
        port = free_port()
        log = open(self.root / f"hw_server_{port}.log", "w")  # noqa: SIM115
        argv = X.hw_server_argv(self.hw, port, f"127.0.0.1:{self.fake.port}", log_xvc=True)
        t = time.monotonic()
        p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        while not listening(port) and time.monotonic() - t < 60:
            time.sleep(0.1)
        self.say(f"hw_server up on 127.0.0.1:{port} in {time.monotonic() - t:.1f}s: {' '.join(argv[1:])}")
        return p, port

    def stop_hw(self, p: subprocess.Popen) -> None:
        t = time.monotonic()
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(10)
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGKILL)
        while self.fake.attached and time.monotonic() - t < 10:
            time.sleep(0.01)
        self.say(f"hw_server stopped; slot free {time.monotonic() - t:.2f}s after SIGTERM")

    def xsdb(self, label: str, port: int, hold_ms: int = 0) -> str:
        body = [f"connect -url TCP:127.0.0.1:{port}", "after 1000",
                'if {[catch {jtag targets -open 1} e]} {puts "OPEN-ERR $e"}', "after 4000",
                f'puts "TARGETS {label} (a)"', "puts [jtag targets]"]
        if hold_ms:
            body += [f"after {hold_ms}", f'puts "TARGETS {label} (b, same session, after the swap)"',
                     "puts [jtag targets]"]
        body += ["disconnect", "exit"]
        tcl = self.root / f"{label}.tcl"
        tcl.write_text("\n".join(body) + "\n")
        t = time.monotonic()
        r = subprocess.run([self.xsdb_bin, "-quiet", str(tcl)], capture_output=True, text=True, timeout=300)
        out = (r.stdout + r.stderr).strip()
        self.say(f"xsdb {label}: rc={r.returncode} in {time.monotonic() - t:.1f}s")
        for line in out.splitlines():
            self.say(f"    {line}")
        return out

    def swap_when_open(self, delay: float, stall: float) -> None:
        deadline = time.monotonic() + 240
        while self.fake.stats.shifts < 200:
            if time.monotonic() > deadline:
                self.say("SWAP skipped: the cable was never opened and scanned")
                return
            time.sleep(0.05)
        time.sleep(delay)
        self.state("SWAP begins (gate on: shifts stall; the chain changes)")
        self.fake.gated.set()
        self.fake.tap.idcode = NEW_RM_IDCODE
        time.sleep(stall)
        self.fake.gated.clear()
        self.state(f"SWAP ends after {stall:.0f}s (gate off)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vivado-bin", default="/apps/Xilinx/Vivado/2024.1/bin")
    ap.add_argument("--stall-s", type=float, default=12.0)
    ap.add_argument("--keep", action="store_true")
    ns = ap.parse_args()
    root = Path("/tmp") / f"xvc-hwspike-{os.getpid()}"
    root.mkdir(parents=True, exist_ok=True)
    rig = Rig(Path(ns.vivado_bin), root)
    rig.say(f"load average {os.getloadavg()}; fake XVC on 127.0.0.1:{rig.fake.port}")
    try:
        hw, port = rig.start_hw()
        try:
            time.sleep(5)
            rig.state("Q2 hw_server idle 5 s, no client")
            swap = threading.Thread(target=rig.swap_when_open, args=(2.0, ns.stall_s), daemon=True)
            swap.start()
            rig.xsdb("A_open_across_swap", port, hold_ms=int((ns.stall_s + 8) * 1000))
            swap.join()
            rig.state("after client A left")
            time.sleep(6)
            rig.state("Q2 6 s after the last client left")
            rig.xsdb("B_same_hw_server_after_swap", port)
        finally:
            rig.stop_hw(hw)
        hw2, port2 = rig.start_hw()
        try:
            rig.xsdb("C_fresh_hw_server", port2)
        finally:
            rig.stop_hw(hw2)
        for log in sorted(root.glob("hw_server_*.log")):
            for line in log.read_text(errors="replace").splitlines():
                rig.say(f"{log.name}: {line}")
        for t, kind, detail in rig.fake.events():
            if kind != "getinfo":
                rig.say(f"fake {t - rig.t0:7.2f}s {kind} {detail}")
    finally:
        rig.fake.close()
        if not ns.keep:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
