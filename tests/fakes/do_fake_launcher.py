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
- ``down``: 0 and ``down`` (also when nothing ran); ``fail_down = (rc, message)``: that exit
  and ``failed`` with ``openocd_exit`` (FIX-PACK-7: the down that refuses a swap);
- ``version``: the status-shaped object, plus ``capabilities`` when set (FIX-PACK-7: v2.1's
  ``harnessd-lock``);
- ``status``: the state, or the watchdog's stop after a swap (``swap_stop``);
- ``up --via hostio`` (HM 1.2.1; contract ``hio_debug_json.md``): ``via``/``capabilities``/
  ``gdb_init`` appended to every reply (``legacy=True``: an older board, none of them), one
  core, cfg ``nanosoc_mps3_hostio4.cfg``, adapter ``hostio4``, ``tap`` null; the refusals in
  the contract (``hostio_refusal = (rc, code, message, hint, busy)`` plays any of them;
  ``no_hostio`` for a design without it, ``busy`` for the other via);
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
    0x0042: ("jtag_only", ("cpu0",)),               # a DAP but no hostio4 link (HM 1.2.1)
}
#: designs whose hostio4 link reaches an ADP (``hostio=yes`` in the board's designs.conf)
HOSTIO_DESIGNS = frozenset({"nanosoc", "nanosoc_multicore", "nanosoc_upy", "nanosoc_iice"})
JTAG_CAPS = ["mem_read", "mem_write", "load", "halt", "step", "breakpoints", "registers",
             "reset"]
HOSTIO_CAPS = ["mem_read", "mem_write", "load"]
HOSTIO_INIT = ["set remote p-packet off", "set remote P-packet off"]
GDB_PORTS = (3333, 3334)
IDCODE = "0x6ba00477"
DESIGNS_BY_NAME = {n: c for n, c in DESIGNS.values()}


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
        #: FIX-PACK-7: ``down`` fails with (exit code, message); OpenOCD keeps running
        self.fail_down: tuple[int, str] | None = None
        #: FIX-PACK-7: ``version --json``'s ``capabilities`` (None: the field is absent)
        self.capabilities: list[str] | None = None
        #: the launcher's state when each verb arrived ([(verb, state)]): what a swap met
        self.states: list[tuple[str, str]] = []
        #: HM 1.2.1: the next hostio ``up`` refuses with (rc, code, message, hint, busy)
        self.hostio_refusal: tuple[int, str, str, str, dict[str, str] | None] | None = None
        #: an older board: no ``via`` / ``capabilities`` / ``gdb_init`` in any reply
        self.legacy = False
        self.via = "jtag"
        self.hostio_designs = set(HOSTIO_DESIGNS)       # designs with hostio=yes
        #: called as ``during(words)`` while each launcher call runs (a test's probe)
        self.during: Any = None
        self.asked_via = "jtag"
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
        if self.during is not None:
            self.during(words)
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
        self.asked_via = words[words.index("--via") + 1] if "--via" in words else "jtag"
        self.states.append((verb, self.state))
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
            if self.via == "hostio":
                body["cfg"] = ["nanosoc_mps3_hostio4.cfg"]
                body["openocd"] = {"version": "0.12.0+mps3 (test)", "adapter": "hostio4"}
                body["tap"] = None
                body["cores"] = [{"name": "cpu0", "gdb_port": GDB_PORTS[0]}]
        if not self.legacy:                              # the three appended keys, last
            hostio = self.via == "hostio"
            body["via"] = self.via
            body["capabilities"] = HOSTIO_CAPS if hostio else JTAG_CAPS
            body["gdb_init"] = HOSTIO_INIT if hostio else []
        body.update(kw)
        return RunResult(rc, "Linux mps3 6.6.0 (a banner line)\n" + json.dumps(body) + "\n", "")

    def _failed(self, rc: int, code: str, message: str, hint: str = "", **kw: Any) -> RunResult:
        state, self.state = self.state, "failed"
        try:
            return self._reply(rc, error={"code": code, "message": message, "hint": hint}, **kw)
        finally:
            self.state = state if state == "up" else "down"

    def _refuse_hostio(self, rc: int, code: str, message: str, hint: str = "",
                       busy: dict[str, str] | None = None) -> RunResult:
        """A refusal of ``up --via hostio``: ``via`` is the one asked for; down."""
        keep, self.via = self.via, "hostio"
        try:
            return self._failed(rc, code, message, hint, **({"busy": busy} if busy else {}))
        finally:
            self.via = keep

    def _up(self, rm: str) -> RunResult:
        want = self.asked_via
        if self.state == "up" and want != self.via:
            other = "JTAG" if self.via == "jtag" else "hostio"
            keep = self.state
            res = self._reply(4, error={"code": "busy", "hint": "mps3-debug down, then up again",
                                        "message": f"a {other} debug session is up (via "
                                                   f"{self.via}): one via at a time"},
                              busy={"by": f"mps3-debug via {self.via}", "peer": None})
            self.state = keep
            return res
        if want == "hostio" and self.state != "up":
            if self.hostio_refusal is not None:
                return self._refuse_hostio(*self.hostio_refusal)
            name = (DESIGNS.get(self.rm_id & 0xFFFF) or ("", None))[0] if rm == "auto" else rm
            if name and name not in self.hostio_designs and DESIGNS_BY_NAME.get(name, ((),))[0]:
                self.design = name
                return self._refuse_hostio(
                    13, "no_hostio", f"the design {name} has no hostio4 debug path (no ADP on "
                                     "the hostio4 link)",
                    "use the JTAG path (mps3-debug up), or load a nanoSoC-family design")
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
        self.via = want
        self.state, self.design, self.cores, self.pid = "up", name, cores, self._next_pid
        self.swap_stop, self.died = False, ""
        return self._reply(0)

    def _down(self, _rm: str) -> RunResult:
        if self.fail_down is not None:
            rc, message = self.fail_down
            return self._failed(rc, "openocd_exit", message)
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
        extra: dict[str, Any] = {}
        if not self.legacy:                      # HM 1.2.1: each design says if it has hostio
            extra["designs"] = [{"name": n, "hostio": n in self.hostio_designs}
                                for n, _c in DESIGNS.values()]
        if self.capabilities is not None:
            return self._reply(0, launcher="2.1.0", capabilities=list(self.capabilities),
                               **extra)
        return self._reply(0, **extra)

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
