"""The board's ``mps3-debug`` launcher (``mps3-debug/1``), behind the claim's one-shot ssh
(lane DEBUG-ONBOARD).

``FakeLauncher`` stands in for ``harness_manager_mps3.claim.DEFAULT_RUN`` (``(argv, timeout) ->
RunResult``), the way ``lc_fake_board_ssh.FakeBoardSsh`` does for the claim. It checks each argv
is the claim's pinned, key-only one-shot to the board (``-J HUB -l root BOARD``, strict host
key, batch mode), then plays the launcher as the Linux lead's contract has it:

- ``up [--rm auto|NAME] --json``: 0 and ``up`` (``already`` when it was), 4 ``busy`` when 6921
  is held by another client, 12 ``no_openocd``, 13 ``no_dap`` (greybox, led), 14 ``no_cfg``
  (an unknown NAME, or ``auto`` when its identify gets no answer), 6 ``openocd_exit`` with a
  log tail (``fail_start``);
- ``down``: 0 and ``down`` (also when nothing ran);
- ``status``: the state, or the watchdog's stop after a swap (``swap_stop``);
- ``installed=False``: exit 127 (``sh: mps3-debug: not found``); ``malformed`` (True, or the
  verbs it applies to): exit 0 and text that is not its JSON.

``serve=True``: each board gdb port (3333, 3334) gets a tiny listener on 127.0.0.1 (a random
port: ``servers``, which the test's fake ssh routes as the board's 127.0.0.1:3333/3334), so a
connection through the claim forward reaches "the board's gdb server". Nothing touches ~/.ssh
and nothing leaves 127.0.0.1.
"""

from __future__ import annotations

import json
import shlex
import socket
import threading
from collections.abc import Sequence
from typing import Any

from harness_manager_mps3.claim import RunResult

#: design number -> (name, cores); None cores: no debug port (the launcher says no_dap).
DESIGNS: dict[int, tuple[str, tuple[str, ...] | None]] = {
    0x0000: ("greybox", None), 0x0001: ("nanosoc", ("cpu0",)),
    0x0003: ("nanosoc_multicore", ("cpu0", "cpu1")), 0x0005: ("nanosoc_upy", ("cpu0",)),
    0x0008: ("nanosoc_iice", ("cpu0",)), 0x001E: ("led", None),
}
GDB_PORTS = (3333, 3334)
IDCODE = "0x6ba00477"


class _GdbServer:
    """A listener standing for one of the board's gdb servers: it greets and closes."""

    def __init__(self, core: str) -> None:
        self.core = core
        self.hits = 0
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.hits += 1
            try:
                conn.sendall(f"board-gdb {self.core}\n".encode())
            except OSError:
                pass
            conn.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


def _opts(argv: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, a in enumerate(argv[:-1]):
        if a == "-o":
            k, _, v = argv[i + 1].partition("=")
            out.setdefault(k.lower(), v)
    return out


class FakeLauncher:
    def __init__(self, *, rm_id: int = 0x01000001, board_ip: str = "", hub: str = "",
                 installed: bool = True, openocd: bool = True,
                 malformed: bool | tuple[str, ...] = False,
                 serve: bool = False, identify_answers: bool = True) -> None:
        self.rm_id = rm_id
        self.board_ip = board_ip
        self.hub = hub
        self.installed = installed
        self.openocd = openocd
        self.malformed = malformed
        self.identify_answers = identify_answers
        #: ssh itself fails (255): the board or the hub does not answer
        self.ssh_fails = False
        #: 6921 held by another client (the launcher's exit 4); {"by":..,"peer":..}
        self.busy: dict[str, str] | None = None
        #: the next ``up`` fails with openocd_exit and this log tail
        self.fail_start: list[str] | None = None
        #: the watchdog stopped OpenOCD for a swap (status reports it)
        self.swap_stop = False
        #: the watchdog stopped OpenOCD for another reason (status: openocd_exit, no "swap")
        self.died: str = ""
        self.state = "down"
        self.design = ""
        self.cores: tuple[str, ...] = ()
        self.pid = 0
        self._next_pid = 811
        self.calls: list[list[str]] = []          # the remote words of each call
        self.argvs: list[list[str]] = []          # the whole ssh argv of each call
        #: board gdb port -> its stand-in (``serve``): route ("127.0.0.1", port) to it
        self.servers: dict[int, _GdbServer] = (
            {port: _GdbServer(f"cpu{i}") for i, port in enumerate(GDB_PORTS)} if serve else {})

    # -- the argv ---------------------------------------------------------------------------

    def _check(self, argv: list[str]) -> None:
        o = _opts(argv)
        assert argv[0] == "ssh", argv
        assert o.get("stricthostkeychecking") == "yes", "not the pinned host key"
        assert o.get("batchmode") == "yes" and o.get("passwordauthentication") == "no"
        assert o.get("clearallforwardings") == "yes", "a one-shot never forwards"
        assert "-L" not in argv and "-t" not in argv
        assert argv[argv.index("-l") + 1] == "root"
        if self.board_ip:
            assert argv[-2] == self.board_ip, argv
        if self.hub:
            assert self.hub in argv[argv.index("-J") + 1]

    # -- the launcher -------------------------------------------------------------------------

    def words(self) -> list[str]:
        """The verbs asked, in order (``["status", "up", ...]``)."""
        return [c[1] for c in self.calls if len(c) > 1]

    def __call__(self, argv: Sequence[str], timeout: float) -> RunResult:
        argv = list(argv)
        self._check(argv)
        self.argvs.append(argv)
        words = shlex.split(argv[-1])
        self.calls.append(words)
        if self.ssh_fails:
            return RunResult(255, "", f"ssh: connect to host {self.board_ip or 'board'} port 22: "
                                      "Connection timed out\r\n")
        if not words or words[0] != "mps3-debug":
            return RunResult(0, "", "")
        if not self.installed:
            return RunResult(127, "", "sh: mps3-debug: not found\n")
        verb = words[1] if len(words) > 1 else ""
        if self.malformed is True or (self.malformed and verb in self.malformed):
            return RunResult(0, "mps3-debug: usage: mps3-debug up|down|status\n{not json", "")
        rm = words[words.index("--rm") + 1] if "--rm" in words else "auto"
        return getattr(self, f"_{verb}", self._unknown)(rm)

    def _reply(self, rc: int, **kw: Any) -> RunResult:
        body: dict[str, Any] = {
            "schema": "mps3-debug/1", "state": self.state, "already": False,
            "rm_id": f"0x{self.rm_id:08x}", "design": self.design, "cfg": [], "pid": self.pid,
            "started_at": "2026-10-01T09:12:03Z", "bind": "127.0.0.1",
            "openocd": {"version": "0.12.0+dev.21f88d7-soclabs.1", "adapter": "mps3_jtagbb"},
            "tap": {"idcode": IDCODE if self.state == "up" else ""},
            "cores": [{"name": c, "gdb_port": GDB_PORTS[i]} for i, c in enumerate(self.cores)]
            if self.state == "up" else [],
            "telnet_port": 4444, "tcl_port": 6666, "busy": None, "error": None, "log_tail": [],
            "watchdog": {"idle_s": 7200},                 # an extra key: ignored by HM
        }
        if self.state == "up":
            body["cfg"] = ["interface/mps3_jtagbb.cfg", f"target/{self.design}.cfg"]
        body.update(kw)
        return RunResult(rc, "Linux mps3 6.6.0 (a banner line)\n" + json.dumps(body) + "\n", "")

    def _failed(self, rc: int, code: str, message: str, hint: str = "", **kw: Any) -> RunResult:
        state, self.state = self.state, "failed"
        try:
            return self._reply(rc, error={"code": code, "message": message, "hint": hint}, **kw)
        finally:
            self.state = state if state == "up" else "down"

    def _up(self, rm: str) -> RunResult:
        if self.state == "up":
            return self._reply(0, already=True)
        if not self.openocd:
            return self._failed(12, "no_openocd", "openocd is not in this image")
        if self.busy:
            return self._failed(4, "busy", "6921 is held by another JTAG client", busy=self.busy)
        if rm == "auto":
            if not self.identify_answers:
                return self._failed(14, "no_cfg", "identify on 127.0.0.1:6899 did not answer",
                                    hint="pass --rm NAME")
            name, cores = DESIGNS.get(self.rm_id & 0xFFFF, ("", None))
        else:
            known = {n: c for n, c in DESIGNS.values()}
            if rm not in known:
                return self._failed(14, "no_cfg", f"no config for --rm {rm}",
                                    hint="pass --rm NAME")
            name, cores = rm, known[rm]
        if cores is None:
            return self._failed(13, "no_dap", f"{name} has no debug port")
        if self.fail_start is not None:
            tail, self.fail_start = self.fail_start, None
            return self._failed(6, "openocd_exit", "openocd exited during init (exit 1)",
                                log_tail=tail)
        self._next_pid += 1
        self.state, self.design, self.cores, self.pid = "up", name, cores, self._next_pid
        self.swap_stop, self.died = False, ""
        return self._reply(0)

    def _down(self, _rm: str) -> RunResult:
        was = self.state
        self.state, self.cores, self.pid = "down", (), 0
        self.swap_stop, self.died = False, ""
        return self._reply(0, already=was != "up")

    def _status(self, _rm: str) -> RunResult:
        if self.swap_stop:
            self.state, self.cores, self.pid = "down", (), 0
            return self._reply(0, error={"code": "openocd_exit",
                                         "message": "stopped: the rm_id changed (a swap)",
                                         "hint": ""})
        if self.died:
            self.state, self.cores, self.pid = "failed", (), 0
            return self._reply(0, error={"code": "openocd_exit", "message": self.died,
                                         "hint": ""}, log_tail=["Error: target lost"])
        return self._reply(0)

    def _version(self, _rm: str) -> RunResult:
        return self._reply(0)

    def _unknown(self, _rm: str) -> RunResult:
        return RunResult(2, "", "mps3-debug: unknown verb\n")

    # -- a swap on the board --------------------------------------------------------------------

    def swapped(self, rm_id: int) -> None:
        """The partition was swapped: the watchdog stops a running OpenOCD."""
        self.rm_id = rm_id
        if self.state == "up":
            self.swap_stop = True

    def close(self) -> None:
        for srv in self.servers.values():
            srv.close()
        self.servers.clear()
