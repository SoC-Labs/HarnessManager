"""XVC design spike (lane XVC, 2026-09-24): XVC through Harness Manager's own SSH tunnel.

Board-free, hub-free. Everything runs on 127.0.0.1:

    fake XVC server (tests/spikes/xvc_fake_server.py, the firmware's semantics)
        ^  127.0.0.1:<xvc>
    a PRIVATE sshd (throwaway keys, 127.0.0.1 only, this user, started and stopped here)
        ^  two host aliases in a private ssh config:
        |    xvc-hub    -> the sshd                        (today's hub-tunnel shape)
        |    xvc-board  -> the sshd, ProxyJump xvc-hub     (the Linux shape: ssh -J hub board,
        |                                                   forward to the board's 127.0.0.1)
    harness_manager_mps3.tunnel.SshTunnel  (the real class, real ssh processes)
        ^  127.0.0.1:<local>
    harness_manager.services.xvc_spike.XvcRelay (optional hop) and XvcClient / probe

It measures round-trip latency direct vs through each shape, then checks the
behaviours the design depends on: one client, the swap gate, the relay's kick/hold,
a tunnel drop and restart, and (``--hw-server``) a real hw_server against the fake.

Run (from the repo root; PYTHONDONTWRITEBYTECODE keeps other trees clean):

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. nice -n 10 python -m tests.spikes.xvc_tunnel_spike \
        [--hw-server /apps/Xilinx/Vivado/2024.1/bin/hw_server] [--json out.json]

Scratch lives in /tmp/xvc-spike-<pid>/ and is removed at the end (``--keep`` keeps it).
Never touches ~/.ssh, ~/.config/harness-manager, a hub or a board.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRATCH = Path("/tmp") / f"xvc-spike-{os.getpid()}"   # the lane rule: /tmp, xvc- prefix
os.environ["HARNESS_MANAGER_STATE_DIR"] = str(SCRATCH / "hm-state")   # never the user's

from harness_manager.services import xvc_spike as X  # noqa: E402
from harness_manager_mps3 import tunnel as T  # noqa: E402
from tests.spikes.xvc_fake_server import FakeXvcServer, Tap  # noqa: E402

SSHD = "/usr/sbin/sshd"
SSH = shutil.which("ssh") or "/usr/bin/ssh"
KEYGEN = shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listen(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if T.listening(port):
            return True
        time.sleep(0.05)
    return False


# --- the private sshd and ssh config -----------------------------------------------------


class PrivateSsh:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = free_port()
        self.user = getpass.getuser()
        self.proc: subprocess.Popen | None = None
        self.wrapper = root / "bin" / "ssh"

    def setup(self) -> PrivateSsh:
        r = self.root
        (r / "bin").mkdir(parents=True, exist_ok=True)
        os.chmod(r, 0o700)
        for key in ("host_ed25519", "id_ed25519"):
            subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", f"xvc-spike-{key}",
                            "-f", str(r / key)], check=True)
        (r / "authorized_keys").write_text((r / "id_ed25519.pub").read_text())
        hostpub = (r / "host_ed25519.pub").read_text().split()
        (r / "known_hosts").write_text(
            f"xvc-spike-hub {hostpub[0]} {hostpub[1]}\nxvc-spike-board {hostpub[0]} {hostpub[1]}\n")
        (r / "sshd_config").write_text(f"""\
Port {self.port}
ListenAddress 127.0.0.1
HostKey {r}/host_ed25519
PidFile {r}/sshd.pid
AuthorizedKeysFile {r}/authorized_keys
PasswordAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
UsePAM no
StrictModes no
AllowTcpForwarding yes
PermitTTY no
X11Forwarding no
AllowUsers {self.user}
LogLevel VERBOSE
""")
        common = f"""\
    HostName 127.0.0.1
    Port {self.port}
    User {self.user}
    IdentityFile {r}/id_ed25519
    IdentitiesOnly yes
    UserKnownHostsFile {r}/known_hosts
    GlobalKnownHostsFile /dev/null
    StrictHostKeyChecking yes
    ControlMaster no
    ControlPath none
"""
        (r / "ssh_config").write_text(
            f"Host xvc-hub\n    HostKeyAlias xvc-spike-hub\n{common}\n"
            f"Host xvc-board\n    HostKeyAlias xvc-spike-board\n    ProxyJump xvc-hub\n{common}")
        # ProxyJump runs a nested "ssh -W"; the wrapper is first on PATH, so it gets -F too.
        self.wrapper.write_text(f'#!/bin/sh\nexec {SSH} -F {r}/ssh_config "$@"\n')
        self.wrapper.chmod(0o755)
        os.environ["PATH"] = f"{r / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
        return self

    def start(self) -> PrivateSsh:
        self.log = open(self.root / "sshd.log", "w")  # noqa: SIM115
        self.proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(self.root / "sshd_config")],
                                     stdout=self.log, stderr=subprocess.STDOUT)
        if not wait_listen(self.port):
            raise RuntimeError(f"private sshd did not listen: {(self.root / 'sshd.log').read_text()}")
        return self

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(5)
            if self.proc.poll() is None:
                self.proc.kill()
        with contextlib.suppress(Exception):
            self.log.close()


def ssh_g(argv):
    return T.run_ssh_g(argv)


def tunnel(ssh: PrivateSsh, host: str, xvc_port: int) -> T.SshTunnel:
    t = T.SshTunnel(host, [T.Forward("xvc", "127.0.0.1", xvc_port)], ssh=str(ssh.wrapper),
                    ssh_g=ssh_g, ready_timeout_s=30, label=f"spike {host}",
                    user_config=ssh.root / "ssh_config", system_config=None)
    return t.start()


# --- measurements -------------------------------------------------------------------------


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def summary(xs: list[float]) -> dict[str, float]:
    return {"n": len(xs), "median_us": round(statistics.median(xs), 1),
            "p90_us": round(pct(xs, 0.90), 1), "p99_us": round(pct(xs, 0.99), 1),
            "min_us": round(min(xs), 1)}


def wait_free(fake: FakeXvcServer, timeout: float = 10.0) -> float:
    """Wait until the fake has noticed the last client left; return how long that took (s).

    The board's server is the same: a client that reconnects before the server has
    seen its previous close is accepted and closed (one client). Found by this spike.
    """
    t0 = time.monotonic()
    while fake.attached and time.monotonic() - t0 < timeout:
        time.sleep(0.005)
    return time.monotonic() - t0


def time_round_trips(paths: dict[str, int], fake: FakeXvcServer, rounds: int,
                     per_round: int) -> dict[str, Any]:
    """Steady-state round trips on ONE connection per path per round, rounds INTERLEAVED
    across the paths so load drift on a shared machine lands on every path alike. The
    first command after a connect is not timed (it pays thread start-up in the fake)."""
    kinds = {"getinfo": None, "shift_64": 64, "shift_2048": 2048}
    samples: dict[str, dict[str, list[float]]] = {p: {k: [] for k in kinds} for p in paths}
    for _ in range(rounds):
        for name, port in paths.items():
            wait_free(fake)
            with X.XvcClient("127.0.0.1", port, timeout=10) as c:
                c.getinfo()
                for kind, nbits in kinds.items():
                    zeros = bytes((nbits or 0) // 8)
                    for _ in range(per_round):
                        t0 = time.perf_counter()
                        if nbits is None:
                            c.getinfo()
                        else:
                            c.shift(nbits, zeros, zeros)
                        samples[name][kind].append((time.perf_counter() - t0) * 1e6)
    out: dict[str, Any] = {}
    for name in paths:
        out[name] = {k: summary(v) for k, v in samples[name].items()}
        med = out[name]["shift_2048"]["median_us"]
        out[name]["shift_2048"]["kbit_per_s_at_median"] = round(2048 / med * 1e3, 1)
    return out


def time_first_command(paths: dict[str, int], fake: FakeXvcServer, n: int) -> dict[str, Any]:
    """Connect + first getinfo, per path (what an attach or a probe costs)."""
    xs: dict[str, list[float]] = {p: [] for p in paths}
    for _ in range(n):
        for name, port in paths.items():
            wait_free(fake)
            t0 = time.perf_counter()
            with X.XvcClient("127.0.0.1", port, timeout=10) as c:
                c.getinfo()
            xs[name].append((time.perf_counter() - t0) * 1e6)
    return {p: summary(v) for p, v in xs.items()}


def tap_idcode(port: int) -> str:
    """Reset, then read the 32-bit DR after reset (IDCODE) through the path at ``port``."""
    def bits(seq: list[int]) -> bytes:
        out = bytearray((len(seq) + 7) // 8)
        for i, b in enumerate(seq):
            out[i >> 3] |= (b & 1) << (i & 7)
        return bytes(out)
    tms = [1, 1, 1, 1, 1, 0, 1, 0, 0] + [0] * 31 + [1]
    with X.XvcClient("127.0.0.1", port) as c:
        tdo = c.shift(len(tms), bits(tms), bits([0] * len(tms)))
    val = int.from_bytes(tdo, "little") >> 9
    return f"0x{val & 0xFFFFFFFF:08X}"


# --- the checks ------------------------------------------------------------------------------


def reconnect_race(fake: FakeXvcServer, paths: dict[str, int], tries: int = 20) -> dict[str, Any]:
    """Close, then reconnect AT ONCE: how often is the second connection refused?"""
    out: dict[str, Any] = {}
    for name, port in paths.items():
        refused = 0
        for _ in range(tries):
            wait_free(fake)
            with X.XvcClient("127.0.0.1", port) as c:
                c.getinfo()
            if X.probe("127.0.0.1", port)["state"] != "free":
                refused += 1
        out[name] = {"tries": tries, "refused_immediate_reconnect": refused}
    return out


def check_single_client(port: int, fake: FakeXvcServer) -> dict[str, Any]:
    before = fake.stats.refused_second
    a = X.XvcClient("127.0.0.1", port)
    a.getinfo()
    held = X.probe("127.0.0.1", port)
    time.sleep(0.3)
    a_still = a.getinfo()
    a.close()
    time.sleep(0.3)
    free = X.probe("127.0.0.1", port)
    return {"probe_while_held": held, "first_client_undisturbed": a_still.startswith("xvcServer"),
            "board_refusals_logged": fake.stats.refused_second - before,
            "probe_after_release": free}


def check_gate(port: int, fake: FakeXvcServer, hold_s: float = 1.0) -> dict[str, Any]:
    with X.XvcClient("127.0.0.1", port, timeout=10) as c:
        fake.gated.set()
        info_while_gated = c.getinfo()
        c.sock.sendall(b"shift:" + (32).to_bytes(4, "little") + bytes(4) + bytes(4))
        c.sock.settimeout(hold_s)
        stalled = False
        try:
            c.sock.recv(4)
        except TimeoutError:
            stalled = True
        t0 = time.perf_counter()
        fake.gated.clear()
        c.sock.settimeout(10)
        got = c._recv_exact(4)
        return {"getinfo_answered_while_gated": info_while_gated.startswith("xvcServer"),
                "shift_stalled_while_gated": stalled,
                "reply_after_ungate_ms": round((time.perf_counter() - t0) * 1e3, 1),
                "reply_len": len(got)}


def check_relay(relay: X.XvcRelay, fake: FakeXvcServer) -> dict[str, Any]:
    out: dict[str, Any] = {}
    c = X.XvcClient("127.0.0.1", relay.port)
    c.getinfo()
    time.sleep(0.2)
    st = relay.status()["attached"] or {}
    out["who_is_attached"] = {"pid": st.get("pid"), "is_this_process": st.get("pid") == os.getpid(),
                              "command": (st.get("command") or "")[:80]}
    second = X.probe("127.0.0.1", relay.port)
    out["second_client_via_relay"] = second["state"]
    n_disc = len(fake.events("disconnect"))
    t0 = time.monotonic()
    relay.hold("partition swap in progress")
    freed_ms = None
    while time.monotonic() - t0 < 10:
        if len(fake.events("disconnect")) > n_disc:
            freed_ms = round((fake.events("disconnect")[-1][0] - t0) * 1e3, 1)
            break
        time.sleep(0.01)
    out["kick_to_board_slot_free_ms"] = freed_ms
    try:
        c.getinfo()
        out["client_after_kick"] = "still answered (WRONG)"
    except (X.XvcClosed, OSError) as exc:
        out["client_after_kick"] = f"closed ({type(exc).__name__})"
    c.close()
    out["attach_while_held"] = X.probe("127.0.0.1", relay.port)["state"]
    relay.release()
    out["attach_after_release"] = X.probe("127.0.0.1", relay.port)["state"]
    return out


def check_tunnel_drop(t: T.SshTunnel, port: int, fake: FakeXvcServer) -> dict[str, Any]:
    c = X.XvcClient("127.0.0.1", port, timeout=10)
    c.getinfo()
    pid = t.status()["pid"]
    n_disc = len(fake.events("disconnect"))
    t0 = time.monotonic()
    os.kill(pid, signal.SIGKILL)                       # the hub connection dies
    try:
        c.getinfo()
        client = "still answered (WRONG)"
    except (X.XvcClosed, OSError) as exc:
        client = f"closed ({type(exc).__name__})"
    c.close()
    up_s = None
    while time.monotonic() - t0 < 60:
        st = t.status()
        if st["state"] == "up" and st["pid"] != pid:
            up_s = round(time.monotonic() - t0, 2)
            break
        time.sleep(0.05)
    slot_free_s = None
    for e in fake.events("disconnect")[n_disc:]:
        slot_free_s = round(e[0] - t0, 3)
        break
    after = X.probe("127.0.0.1", port)
    return {"client_on_drop": client, "board_slot_freed_after_s": slot_free_s,
            "tunnel_back_up_after_s": up_s, "same_local_port": t.local_port("xvc") == port,
            "probe_after_restart": after["state"]}


def check_hw_server(binary: str, xvc_port: int, fake: FakeXvcServer, root: Path) -> dict[str, Any]:
    """A real hw_server, owned by us, against the fake (127.0.0.1 only)."""
    out: dict[str, Any] = {}
    hw_port = free_port()
    argv = X.hw_server_argv(binary, hw_port, f"127.0.0.1:{xvc_port}", log_xvc=True)
    out["argv"] = argv
    n_conn = len(fake.events("connect"))
    log = open(root / "hw_server.log", "w")  # noqa: SIM115
    t0 = time.monotonic()
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out["listening_after_s"] = round(time.monotonic() - t0, 2) if wait_listen(hw_port, 60) else None
        # Does it open the XVC target on its own, with no client attached?
        connected = None
        while time.monotonic() - t0 < 30:
            if len(fake.events("connect")) > n_conn:
                connected = round(fake.events("connect")[n_conn][0] - t0, 2)
                break
            time.sleep(0.1)
        out["xvc_opened_with_no_client_after_s"] = connected
        time.sleep(3)
        out["fake_saw"] = {"getinfo": fake.stats.getinfo, "settck": fake.stats.settck,
                           "shifts": fake.stats.shifts, "shift_bits": fake.stats.shift_bits}
        out["slot_held_while_idle"] = bool(fake.attached)
        out["probe_while_hw_server_up"] = X.probe("127.0.0.1", xvc_port)["state"]
        # Drop the XVC connection under it (a harness restart / a relay kick):
        # does hw_server open it again by itself?
        if fake.attached:
            n_conn2 = len(fake.events("connect"))
            fake.kick()
            t1 = time.monotonic()
            again = None
            while time.monotonic() - t1 < 20:
                if len(fake.events("connect")) > n_conn2:
                    again = round(fake.events("connect")[n_conn2][0] - t1, 2)
                    break
                time.sleep(0.1)
            out["reconnects_after_drop_s"] = again
        n_disc = len(fake.events("disconnect"))
        t2 = time.monotonic()
        os.killpg(proc.pid, signal.SIGTERM)          # the wrapper script AND the binary
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(10)
        out["exit_after_sigterm_s"] = round(time.monotonic() - t2, 2) if proc.poll() is not None else None
        freed = None
        while time.monotonic() - t2 < 10:
            if len(fake.events("disconnect")) > n_disc or not fake.attached:
                freed = round(time.monotonic() - t2, 2)
                break
            time.sleep(0.05)
        out["board_slot_freed_after_stop_s"] = freed
    finally:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)       # never leave our hw_server behind
        log.close()
    txt = (root / "hw_server.log").read_text(errors="replace")
    out["log_tail"] = txt[-1500:]
    return out


# --- main -------------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="XVC through Harness Manager's SSH tunnel (spike)")
    ap.add_argument("--rounds", type=int, default=10, help="interleaved rounds per path")
    ap.add_argument("--per-round", type=int, default=50, help="commands of each kind per round")
    ap.add_argument("--connects", type=int, default=30, help="connect + getinfo samples per path")
    ap.add_argument("--hw-server", default="", help="path to a real hw_server to try (127.0.0.1 only)")
    ap.add_argument("--json", default="", help="write the full result here")
    ap.add_argument("--keep", action="store_true", help="keep /tmp/xvc-spike-<pid>")
    ns = ap.parse_args()

    SCRATCH.mkdir(parents=True, exist_ok=True)
    res: dict[str, Any] = {"when": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                           "host": socket.gethostname(), "loadavg": os.getloadavg(),
                           "ssh": subprocess.run([SSH, "-V"], capture_output=True, text=True).stderr.strip()}
    ssh = PrivateSsh(SCRATCH / "ssh").setup().start()
    tunnels: list[T.SshTunnel] = []
    relay = None
    fake = FakeXvcServer(echo=True).start()
    try:
        tap_fake = FakeXvcServer(tap=Tap()).start()
        res["tap_idcode_direct"] = tap_idcode(tap_fake.port)
        tap_fake.close()

        hub = tunnel(ssh, "xvc-hub", fake.port)
        tunnels.append(hub)
        board = tunnel(ssh, "xvc-board", fake.port)
        tunnels.append(board)
        res["tunnel_argv_board_shape"] = board.argv
        relay = X.XvcRelay(("127.0.0.1", board.local_port("xvc"))).start()

        paths = {"direct": fake.port, "hub_shape_1hop": hub.local_port("xvc"),
                 "board_shape_2hop_proxyjump": board.local_port("xvc"),
                 "relay_plus_2hop": relay.port}
        res["latency"] = {}
        res["reconnect_race"] = reconnect_race(fake, paths)
        res["latency"] = time_round_trips(paths, fake, ns.rounds, ns.per_round)
        res["connect_plus_first_getinfo"] = time_first_command(paths, fake, ns.connects)
        base = res["latency"]["direct"]
        res["overhead_median_us"] = {
            k: {m: round(v[m]["median_us"] - base[m]["median_us"], 1) for m in v}
            for k, v in res["latency"].items() if k != "direct"}

        wait_free(fake)
        res["single_client_via_2hop"] = check_single_client(board.local_port("xvc"), fake)
        wait_free(fake)
        res["swap_gate_via_2hop"] = check_gate(board.local_port("xvc"), fake)
        wait_free(fake)
        res["relay"] = check_relay(relay, fake)
        wait_free(fake)
        res["tunnel_drop_2hop"] = check_tunnel_drop(board, board.local_port("xvc"), fake)
        if ns.hw_server:
            hw_fake = FakeXvcServer(tap=Tap()).start()
            try:
                res["hw_server"] = check_hw_server(ns.hw_server, hw_fake.port, hw_fake, SCRATCH)
                res["hw_server"]["fake_events"] = [(round(t, 3), k, d) for t, k, d in hw_fake.events()
                                                   if k != "getinfo"][:40]
            finally:
                hw_fake.close()
        res["fake_totals"] = {k: v for k, v in vars(fake.stats).items() if k != "events"}
    finally:
        if relay is not None:
            relay.close()
        for t in tunnels:
            t.close()
        fake.close()
        ssh.stop()
        res["leftover_ssh_pids"] = [
            int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()
            and str(SCRATCH) in _cmdline(p)]
        if not ns.keep:
            shutil.rmtree(SCRATCH, ignore_errors=True)
    text = json.dumps(res, indent=2, default=str)
    if ns.json:
        Path(ns.json).write_text(text + "\n")
    print(text)
    return 0


def _cmdline(p: Path) -> str:
    try:
        return (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


if __name__ == "__main__":
    sys.exit(main())
