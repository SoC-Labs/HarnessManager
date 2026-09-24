"""Team T4 debug test rig: point the debug service at ``stub_openocd`` and fake MPS3 configs.

``use_stub(monkeypatch, tmp_path)`` sets:

- ``$HARNESS_MANAGER_OPENOCD`` -> a launcher for tests/fakes/stub_openocd.py;
- ``$STUB_OPENOCD_LOG`` -> a JSON-lines log of every stub run and event;
- ``$HARNESS_MANAGER_MPS3_OPENOCD_DIR`` -> a directory holding empty files with the
  real config names, so the tests never depend on the platform repo checkout.
"""

from __future__ import annotations

import random
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fakes.stub_openocd import make_wrapper, read_log

STUB = Path(__file__).with_name("stub_openocd.py")
CFG_NAMES = ("nanosoc_mps3_jtag.cfg", "nanosoc_iice_chain.cfg", "nanosoc_ops.tcl")


@dataclass
class StubRig:
    binary: Path
    log: Path
    cfg_dir: Path

    def runs(self) -> list[list[str]]:
        return [e["argv"] for e in read_log(self.log) if "argv" in e]

    def events(self, name: str) -> list[dict]:
        return [e for e in read_log(self.log) if e.get("event") == name]


def make_cfg_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name in CFG_NAMES:
        (path / name).write_text(f"# {name}: test stand-in\n")
    return path


def use_stub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StubRig:
    rig = StubRig(binary=make_wrapper(tmp_path / "bin"), log=tmp_path / "stub_openocd.jsonl",
                  cfg_dir=make_cfg_dir(tmp_path / "cfg"))
    monkeypatch.setenv("HARNESS_MANAGER_OPENOCD", str(rig.binary))
    monkeypatch.setenv("STUB_OPENOCD_LOG", str(rig.log))
    monkeypatch.setenv("HARNESS_MANAGER_MPS3_OPENOCD_DIR", str(rig.cfg_dir))
    monkeypatch.delenv("HARNESS_MANAGER_DEBUG_PORT_BASE", raising=False)
    for var in ("STUB_OPENOCD_IDCODE", "STUB_OPENOCD_INIT_DELAY", "STUB_OPENOCD_NO_ADAPTER"):
        monkeypatch.delenv(var, raising=False)
    return rig


def held_gdb_block() -> tuple[socket.socket, int]:
    """``(listener, base)``: a listener on a port block's gdb port, the rest of the block free.

    The base is drawn below every OS's ephemeral range (Linux 32768+, Windows and macOS
    49152+) and below the debug service's own 23300 range. A block next to a ``bind(0)``
    port is in the ephemeral range, where this host's outgoing connections take the
    telnet or tcl port between the check and the stub's bind: 2 runs in 30 failed with
    "local tcl port <base+3> is already in use" instead of the gdb port (Q1, 2026-09-24).
    """
    from harness_manager.services.debug import DebugPorts, port_in_use

    pick = random.SystemRandom()          # not `random`: pytest-randomly reseeds it per test
    for _ in range(200):
        base = pick.randrange(20000, 23000)
        if any(port_in_use(p) for p in DebugPorts.block(base).reserved()):
            continue
        blocker = socket.socket()
        try:
            blocker.bind(("127.0.0.1", base))
        except OSError:
            blocker.close()
            continue
        blocker.listen(1)
        return blocker, base
    raise AssertionError("no free debug port block in 20000-23000 on this host")


def run_stub(argv: list[str], env: dict[str, str] | None = None,
             timeout: float = 10.0) -> subprocess.CompletedProcess:
    """Run the stub directly (one-shot command lines that end in ``shutdown``)."""
    return subprocess.run([sys.executable, str(STUB), *argv], capture_output=True, text=True,
                          timeout=timeout, env=env)


def argv_positions(argv: list[str]) -> dict[str, int]:
    """Index of the first ``-f``, the first probe override, and the port line."""
    first_f = argv.index("-f")
    probe = next(i for i, a in enumerate(argv) if a.startswith("set RBB_HOST"))
    ports = next(i for i, a in enumerate(argv) if a.startswith("gdb_port"))
    return {"first_f": first_f, "probe": probe, "ports": ports}


class StaticDebugAdapter:
    """A ``core.pack.DebugAdapter`` with fixed answers (no board needed)."""

    def __init__(self, cfg_dir: Path, rbb_port: int, *,
                 configs: tuple[str, ...] = ("nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl")) -> None:
        self.cfg_dir = cfg_dir
        self.rbb_port = rbb_port
        self.configs = configs

    def openocd_probe_args(self) -> tuple[str, ...]:
        return ("set RBB_HOST 127.0.0.1", f"set RBB_PORT {self.rbb_port}")

    def openocd_search_paths(self) -> tuple[Path, ...]:
        return (self.cfg_dir,)

    def openocd_config(self) -> tuple[str, ...]:
        return self.configs


# A separate "engine" process that starts a session and then waits to be killed:
# killing it with SIGKILL leaves exactly the orphan the HAPS work met.
ORPHAN_MAKER = '''
import json, sys, time
from pathlib import Path
from harness_manager.services.debug import DebugService
from tests.fakes.t4_console_rig import BareSession
from tests.fakes.t4_debug_rig import StaticDebugAdapter
cfg_dir, rbb_port, board = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
session = BareSession(board, debug=StaticDebugAdapter(cfg_dir, rbb_port))
st = DebugService(None, start_timeout=15).up(session)
print(json.dumps({"pid": st.pid, "gdb": st.gdb_port, "telnet": st.telnet_port,
                  "tcl": st.tcl_port}), flush=True)
time.sleep(120)
'''


def start_owner_process(repo: Path, cfg_dir: Path, rbb_port: int, board: str,
                        env: dict[str, str]) -> tuple[subprocess.Popen, dict]:
    """Run ORPHAN_MAKER; return (the owner process, the session it reported)."""
    import json

    proc = subprocess.Popen([sys.executable, "-c", ORPHAN_MAKER, str(cfg_dir), str(rbb_port),
                             board], cwd=str(repo), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    line = proc.stdout.readline() if proc.stdout else ""
    if not line:
        err = proc.stderr.read() if proc.stderr else ""
        proc.kill()
        raise AssertionError(f"owner process failed to start a session: {err}")
    return proc, json.loads(line)
