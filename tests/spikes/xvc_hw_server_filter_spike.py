"""XVC-UI spike (never collected): which JTAG cables does a REAL Vivado hw_server open, per argv?

David's scope rule: XVC is the reconfigurable partition's debug chain, never whole-device
JTAG. A default hw_server opens every local cable type (``auto-open-servers *``), so a
USB JTAG cable on the Harness Manager host would be offered next to the XVC target. This
runs a real hw_server on 127.0.0.1 per configuration, against two fake XVC servers
(``tests/fakes/xvc_server.py``), and asks it through ``xsdb`` (Vivado's own TCF client):

- the cable settings (``configparams auto-open-servers``, ``jtag-port-filter``, ...);
- the open cable servers (``jtag servers``) and targets (``jtag targets``);
- then, as a client could, it opens a SECOND XVC server and the ``digilent-ftdi`` type
  (``jtag servers -open``) and lists again: the filter must hide the second cable.

Configurations: ``baseline`` (no -e), ``old`` (HM before XVC-UI: auto-open-servers only),
``hm`` (``services.xvc.hw_server_argv``, the shipped argv) and ``nomatch`` (a filter that
admits nothing: proves the parameter is live).

Run (from the repo root; no board, no cable, no hub; only its own processes are stopped):

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. nice -n 10 python -m tests.spikes.xvc_hw_server_filter_spike \\
        --vivado-bin /apps/Xilinx/Vivado/2024.1/bin

Scratch in /tmp/xvcui-filter-<pid>/ (removed).
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from harness_manager.services import xvc as X
from tests.fakes.xvc_server import FakeXvcServer

PARAMS = ("auto-open-servers", "jtag-port-filter", "always-open-jtag", "auto-open-ports")


def _free() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vivado-bin", default="/apps/Xilinx/Vivado/2024.1/bin")
    ap.add_argument("configs", nargs="*", default=["baseline", "old", "hm", "nomatch"])
    ns = ap.parse_args()
    vbin = Path(ns.vivado_bin)
    root = Path(tempfile.mkdtemp(prefix=f"xvcui-filter-{os.getpid()}-", dir="/tmp"))
    print(f"# hw_server {vbin / 'hw_server'}; 127.0.0.1 only; scratch {root}")
    try:
        with FakeXvcServer() as board, FakeXvcServer() as other:
            print(f"# fake XVC 'board' 127.0.0.1:{board.port}, second fake XVC 127.0.0.1:{other.port}")
            xvc = f"127.0.0.1:{board.port}"
            configs = {
                "baseline": [],
                "old": ["-e", f"set auto-open-servers xilinx-xvc:{xvc}"],
                "hm": X.hw_server_argv("hw_server", 0, xvc)[5:],
                "nomatch": ["-e", f"set auto-open-servers xilinx-xvc:{xvc}",
                            "-e", "set jtag-port-filter Xilinx/nomatch"],
            }
            for name in ns.configs:
                run(vbin, root, name, configs[name], other.port)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


def run(vbin: Path, root: Path, name: str, settings: list[str], other: int) -> None:
    port = _free()
    argv = [str(vbin / "hw_server"), "-q", "-p0", "-s", f"TCP:127.0.0.1:{port}", *settings]
    work = root / name
    work.mkdir()
    print(f"\n=== {name}\nargv: {' '.join(argv[1:])}", flush=True)
    with open(work / "hw_server.log", "wb") as log:
        proc = subprocess.Popen(argv, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while not _listening(port):
            if proc.poll() is not None or time.monotonic() > deadline:
                print(f"hw_server did not listen (rc {proc.poll()})")
                return
            time.sleep(0.2)
        tcl = [f"connect -url TCP:127.0.0.1:{port}", "after 1500"]
        tcl += [f'if {{[catch {{configparams {p}}} v]}} {{puts "{p}: ERR $v"}} else {{puts "{p} = <$v>"}}'
                for p in PARAMS]
        tcl += ['puts "SERVERS"', "puts [jtag servers]",
                'if {[catch {jtag targets -open 1} e]} {puts "OPEN-ERR $e"}', "after 3000",
                'puts "TARGETS"', "puts [jtag targets]",
                f'if {{[catch {{jtag servers -open xilinx-xvc:127.0.0.1:{other}}} e]}} {{puts "OPEN2-ERR $e"}}',
                'if {[catch {jtag servers -open digilent-ftdi} e]} {puts "OPENFTDI-ERR $e"}',
                "after 4000", 'puts "SERVERS after a client opens a 2nd XVC and digilent-ftdi"',
                "puts [jtag servers]", 'puts "TARGETS after"', "puts [jtag targets]",
                "disconnect", "exit"]
        (work / "q.tcl").write_text("\n".join(tcl) + "\n")
        r = subprocess.run([str(vbin / "xsdb"), "-quiet", str(work / "q.tcl")], cwd=work,
                           capture_output=True, text=True, timeout=300)
        print((r.stdout + r.stderr).strip(), flush=True)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(2)


if __name__ == "__main__":
    raise SystemExit(main())
