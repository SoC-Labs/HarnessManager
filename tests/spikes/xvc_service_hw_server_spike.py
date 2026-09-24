"""XVC-CORE spike (never collected): the SHIPPED ``XvcService`` with a REAL Vivado hw_server
against the fake XVC server, 127.0.0.1 only (no cable, no board, no hub).

The unit tests run the service against ``tests/fakes/fake_hw_server.py``. This checks the
one thing they cannot: that a real hw_server's XVC traffic passes through the relay's
command framing (whole ``getinfo:``/``settck:``/``shift:`` commands only), before and
after a swap (``deploy.started`` stops hw_server and frees the slot; ``deploy.done``
starts a fresh one on the same port).

Run (from the repo root):

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. nice -n 10 python -m tests.spikes.xvc_service_hw_server_spike \\
        --vivado-bin /apps/Xilinx/Vivado/2024.1/bin

It starts and stops only its own processes; scratch in /tmp/xvcc-spike-<pid>/ (removed).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from harness_manager.core.events import Event, EventBus
from harness_manager.services import xvc as X
from tests.fakes.xvc_server import FakeXvcServer
from tests.unit.test_xvc_service import FakeAdapter, FakeSession, free_pair


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vivado-bin", default="/apps/Xilinx/Vivado/2024.1/bin")
    ns = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix=f"xvcc-spike-{os.getpid()}-", dir="/tmp"))
    t0 = time.monotonic()

    def say(text: str) -> None:
        print(f"[{time.monotonic() - t0:6.1f}s] {text}", flush=True)

    def xsdb(label: str, port: int) -> str:
        tcl = root / f"{label}.tcl"
        tcl.write_text("\n".join([
            f"connect -url TCP:127.0.0.1:{port}", "after 1000",
            'if {[catch {jtag targets -open 1} e]} {puts "OPEN-ERR $e"}', "after 3000",
            'puts "TARGETS"', "puts [jtag targets]", "disconnect", "exit"]) + "\n")
        t = time.monotonic()
        r = subprocess.run([str(Path(ns.vivado_bin) / "xsdb"), "-quiet", str(tcl)],
                           capture_output=True, text=True, timeout=300)
        out = (r.stdout + r.stderr).strip()
        say(f"xsdb {label}: rc={r.returncode} in {time.monotonic() - t:.1f}s")
        for line in out.splitlines():
            say(f"    {line}")
        return out

    class Eng:
        pass

    bus = EventBus()
    eng = Eng()
    eng.bus = bus
    eng.state_dir = root / "state"
    states: list[str] = []
    bus.subscribe(X.TOPIC, lambda ev: states.append(ev.data["state"]))
    svc = X.XvcService(eng, hw_server=str(Path(ns.vivado_bin) / "hw_server"),
                       port_base=free_pair(), start_timeout=120)
    say(f"load average {os.getloadavg()}")
    try:
        with FakeXvcServer() as fake:
            s = FakeSession(FakeAdapter(fake))
            st = svc.open(s)
            say(f"open: {st.state} relay 127.0.0.1:{st.relay_port} hw_server {st.url} "
                f"pid {st.hw_server_pid} slot {st.board_slot}")
            a = xsdb("A_before_swap", st.hw_server_port)
            live = svc.status(s)
            say(f"after A: state {live.state}, fake shifts {fake.stats.shifts}, "
                f"dropped {fake.stats.dropped}, board connections {fake.stats.connections}")
            bus.publish(Event("deploy.started", s.candidate.board_id, {"overlay": "nanosoc"}))
            say(f"deploy.started: state {svc.status(s).state}, slot held by "
                f"{fake.attached or 'nobody'}")
            bus.publish(Event("deploy.done", s.candidate.board_id,
                              {"rm_id": "0x01000001", "verified": True}))
            for t in svc.threads:
                t.join(180)
            st2 = svc.status(s)
            say(f"deploy.done: {st2.state}, hw_server pid {st2.hw_server_pid} (was "
                f"{st.hw_server_pid}), same port {st2.hw_server_port == st.hw_server_port}")
            b = xsdb("B_after_swap", st2.hw_server_port)
            say(f"after B: fake shifts {fake.stats.shifts}, dropped {fake.stats.dropped}, "
                f"board connections {fake.stats.connections}")
            svc.close(s)
            say(f"closed; slot {'free' if not fake.attached else 'HELD'}; events {states}")
            ok = ("0a003093" in a and "0a003093" in b and fake.stats.dropped == 0)
            say("RESULT " + ("PASS" if ok else "FAIL"))
            return 0 if ok else 1
    finally:
        svc.shutdown()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
