"""The HIL runbooks, unattended (lanes HIL-AUTO, HIL-GUI): the runner and its command line.

``python -m harness_manager.checks run`` (or ``python -m tools.hil run`` from a checkout), and
the service's run manager (``services/hil_runs.py``: the app's Checks section) run the same
``Runner``. It drives Harness Manager's own CLI (``harness-manager --json VERB``), one
subprocess per check, exactly as david would type the runbook, and saves each answer as
evidence. It is a CLIENT of the CLI's ``--json`` contract: it never imports the engine, so what
it proves is what a person at srv03335 gets. ``docs/HIL_AUTO.md`` says how to run it.

**Two routes** for those subprocesses (``SubprocessInvoker``):

- ``service`` (HIL-GUI): a Harness Manager service runs for the state dir. The children use the
  CLI's normal routing to it (``HARNESS_MANAGER_CLI_ENGINE=…require_service``: the service or
  UNREACHABLE, never a silent fall back to an in-process engine that its own lock would
  refuse), so they share the service's board session. The service's run manager keeps the
  board open for the run and its lease service heartbeats the lease (``Options.lease_kept``):
  the lease's first expiry is then not a deadline. ``main`` hands a run to a running service
  (``POST /boards/{bid}/checks``) and follows it;
- ``in-process`` (the fallback: no service runs): every child on the CLI's in-process engine
  (``HARNESS_MANAGER_NO_DAEMON=1``), one owner of the board per command, nothing heartbeats the
  lease, so its expiry is a deadline (``_lease_deadline``).

The safety rules are here, in code, the same on both routes:

1. **The lease.** ``gate_lease``: before anything touches the board, ``lease show`` must say
   ``lease.here`` (this Harness Manager holds the token). It is asked again before every
   section, before every ``safe`` check and before the final restore; lost = stop. The runner
   never acquires, requests, forces or releases a lease: ``allowed`` has no such argv.
2. **What may be sent.** ``allowed``: every command passes an exact allow-list of argv shapes
   for the ``--writes`` mode. ``none``: reads only (``board identity`` without a change flag
   among them). ``safe``: plus ``program <overlay> --yes``, ``restore``, ``mcc temp`` and
   ``identify`` (a blink of at most 5 s). Nothing else, ever: no ``--keep-on-card``, no
   ``--force``/``--consent``, no slot/card/sd verbs, no ``mcc reboot``/``mcc cmd``, no
   ``share start``, no ``board claim``. A refused argv stops the run (exit 2) unsent.
3. **Stops** (exit 2; ``Runner._verdict``): an unexpected identity (an ``Expect(stop=True)``,
   or exit 14); a refusal (exit 15: the claim lock, a safety rail); HELD (exit 4: another
   holder, a busy control channel, the reset guard's card job: never ``--force``); the lease
   lost; the board unreachable ``--max-unreachable`` times (reads back off 30 s doubling to
   10 min; a write is never retried). The MCC read's one-reader refusal is not a stop:
   "skipped (tty_00 busy)", never retried. **Our own service is not another holder**: a HELD
   whose ``error.data.reason`` is ``OWN_REQUEST`` (the service's control-port gate: this
   Harness Manager's own request held the port) is read again after the back-off (a read) or
   fails that check without stopping (a write); only a different process or client stops.
4. **The end state** (``Runner.end_state``, in a ``finally``): if the runner swapped the
   partition, it puts greybox back (while the lease is still here; a reset-guard refusal is
   waited out, never forced) and reads the identity to prove it.
5. **Pace** (QUIET-POLL: an explicit client, but polite): ``--gap`` seconds between commands,
   ``--interval`` of at least 60 s between iterations, no retry loop without a back-off.

Exit: 0 every automatic check passed; 1 a check failed; 2 stopped for safety (or refused to
start).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import plans as P

EXIT_PASS, EXIT_FAIL, EXIT_STOP = 0, 1, 2
#: HM's exit codes (harness_manager.core.errors.ExitCode; a client keeps its own copy)
HM_HELD, HM_UNREACHABLE, HM_INCOMPATIBLE, HM_REFUSED = 4, 7, 14, 15

PASS, FAIL, MANUAL, SKIPPED, STOPPED = "pass", "fail", "manual", "skipped", "stopped"
VERDICTS = (PASS, FAIL, MANUAL, SKIPPED, STOPPED)
#: ``error.data.reason`` of a HELD from this Harness Manager's own control-port gate
#: (``harness_manager_mps3.ctlgate.OWN_REQUEST``; a client keeps its own copy)
OWN_REQUEST = "OWN_REQUEST"

#: The subprocesses' routes (the module docstring)
ROUTE_SERVICE, ROUTE_IN_PROCESS = "service", "in-process"
ENV_NO_DAEMON = "HARNESS_MANAGER_NO_DAEMON"
ENV_ENGINE = "HARNESS_MANAGER_CLI_ENGINE"
ENV_STATE_DIR = "HARNESS_MANAGER_STATE_DIR"
#: The CLI's engine factory that is the running service or UNREACHABLE (``cli/engine.py``)
SERVICE_ENGINE = "harness_manager.cli.engine:require_service"

MIN_INTERVAL_S = 60.0
MIN_GAP_S = 1.0
BACKOFF_FIRST_S = 30.0          # QUIET-POLL's back-off
BACKOFF_MAX_S = 600.0
RESTORE_POLL_S = 60.0           # a reset-guard refusal of the final restore: ask again this often

#: ``allowed``'s argv shapes, per ``--writes`` mode. ``{B}`` is the board, ``{RM}`` an
#: overlay name. Exact matches: an extra flag (``--keep-on-card``, ``--force``) never passes.
READ_ARGV: tuple[tuple[str, ...], ...] = (
    ("version",),
    ("info", "{B}"),
    ("panel", "show", "{B}"),
    ("xvc", "status", "{B}"),
    ("board", "claim-status", "{B}"),
    ("board", "ssh", "{B}", "-c", "cat /run/mps3/persist.state"),
    ("slot", "status", "{B}"),
    ("card", "status", "{B}"),
    ("overlays", "{B}"),
    ("lease", "show", "{B}"),
    ("share", "list", "{B}"),
    ("debug", "detect", "{B}"),
    ("harness", "list", "{B}"),
    ("board", "identity", "{B}"),                  # A5: the read (no --from-hub, no --label)
)
SAFE_ARGV: tuple[tuple[str, ...], ...] = (
    ("program", "{B}", "{RM}", "--yes"),
    ("restore", "{B}"),
    ("mcc", "{B}", "temp"),
    ("identify", "{B}"),
    ("identify", "{B}", "--seconds", "5"),         # A6: a 5 s blink, nothing persistent
)
_RM = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


class SafetyStop(Exception):
    """The run must stop now (exit 2). ``check`` is the check it stopped at."""

    def __init__(self, reason: str, check: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.check = check
        self.result: CheckResult | None = None      # the check's own record, when it ran


def allowed(argv: Sequence[str], board: str, writes: str) -> bool:
    """Whether ``argv`` (without ``--json``) may be sent in this ``--writes`` mode."""
    shapes = READ_ARGV + (SAFE_ARGV if writes == "safe" else ())
    for shape in shapes:
        if len(shape) != len(argv):
            continue
        if all(want == got or (want == "{B}" and got == board)
               or (want == "{RM}" and _RM.fullmatch(got) is not None)
               for want, got in zip(shape, argv, strict=True)):
            return True
    return False


# --- running a command -----------------------------------------------------------------------


@dataclass
class Outcome:
    rc: int | None          # None: it timed out or could not run
    stdout: str
    stderr: str
    seconds: float


Invoker = Callable[[list[str], float], Outcome]


class SubprocessInvoker:
    """``harness-manager`` as a subprocess: stdin is /dev/null (a prompt fails, it never
    hangs), its own session (a Ctrl-C to the runner never reaches it mid-check), and a
    timeout that asks it to stop (SIGTERM, 10 s) before it is killed.

    ``route`` (the module docstring): ``in-process`` puts every command on the CLI's
    in-process engine, never a running service (one owner of the board per command, no
    background polling of it from this Harness Manager). ``service`` sends every command to
    the service running for ``state_dir`` through the CLI's normal routing, and only there:
    a service that does not answer is UNREACHABLE (exit 7), never the in-process engine, whose
    board lock the service holds."""

    def __init__(self, base: Sequence[str], *, route: str = ROUTE_IN_PROCESS,
                 state_dir: Path | str | None = None) -> None:
        if route not in (ROUTE_SERVICE, ROUTE_IN_PROCESS):
            raise ValueError(f"unknown route {route!r}")
        self.base = list(base)
        self.route = route
        env = dict(os.environ)
        if route == ROUTE_SERVICE:
            env.pop(ENV_NO_DAEMON, None)
            env[ENV_ENGINE] = SERVICE_ENGINE
            if state_dir is not None:
                env[ENV_STATE_DIR] = str(state_dir)       # the service's, whatever the caller's
        else:
            env[ENV_NO_DAEMON] = "1"
        self.env = env

    def __call__(self, argv: list[str], timeout: float) -> Outcome:
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(self.base + argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True, env=self.env)
        except OSError as exc:
            return Outcome(None, "", f"cannot run {self.base[0]}: {exc}", time.monotonic() - t0)
        try:
            out, err = proc.communicate(timeout=timeout)
            return Outcome(proc.returncode, out, err, time.monotonic() - t0)
        except subprocess.TimeoutExpired:
            for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    pass
                try:
                    out, err = proc.communicate(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    out, err = "", ""
            return Outcome(None, out or "", (err or "") + f"\n[hil: timed out after {timeout:.0f} s]",
                           time.monotonic() - t0)


def default_hm() -> list[str]:
    """This checkout's Harness Manager, run by the same Python (the venv's)."""
    return [sys.executable, "-m", "harness_manager.cli.main"]


# --- expectations ------------------------------------------------------------------------------

_MISSING = object()
_PICK = re.compile(r"^(?P<key>[^\[]+)\[(?P<k>[^=\]]+)=(?P<v>[^\]]*)\]$")


def resolve(data: Any, path: str) -> Any:
    """``a.b``; ``list[key=value]`` picks the first item whose ``key`` is ``value``."""
    cur = data
    for seg in path.split("."):
        m = _PICK.match(seg)
        key = m.group("key") if m else seg
        if not isinstance(cur, dict) or key not in cur:
            return _MISSING
        cur = cur[key]
        if m:
            if not isinstance(cur, list):
                return _MISSING
            cur = next((i for i in cur if isinstance(i, dict)
                        and str(i.get(m.group("k"))) == m.group("v")), _MISSING)
            if cur is _MISSING:
                return _MISSING
    return cur


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def evaluate(exp: P.Expect, data: Any, stdout: str, facts: dict[str, Any]) -> str:
    """"" when ``exp`` holds, else what was wrong (one line)."""
    got = stdout if exp.path == "$stdout" else resolve(data, exp.path)
    if exp.optional and (got is _MISSING or got in ("", None)):
        return ""
    v, op = exp.value, exp.op
    shown = "missing" if got is _MISSING else repr(got)
    if op == "present":
        ok = got is not _MISSING and got is not None
    elif op == "none":
        ok = got is None
    elif got is _MISSING:
        ok = False
    elif op == "eq":
        ok = _same(got, v)
    elif op == "ne":
        ok = not _same(got, v)
    elif op == "in":
        ok = any(_same(got, x) for x in v)
    elif op == "true":
        ok = got is True
    elif op == "false":
        ok = got is False
    elif op == "has":
        ok = isinstance(got, list) and any(_same(x, v) for x in got)
    elif op == "contains":
        ok = isinstance(got, str) and v in got
    elif op == "prefix":
        ok = isinstance(got, str) and got.startswith(v)
    elif op == "any_contains":
        ok = isinstance(got, list) and any(isinstance(x, str) and v in x for x in got)
    elif op == "range":
        ok = isinstance(got, (int, float)) and not isinstance(got, bool) and v[0] <= got <= v[1]
    elif op == "regex":
        ok = isinstance(got, str) and re.search(v, got) is not None
    elif op == "any_item":
        ok = isinstance(got, list) and any(
            isinstance(i, dict) and all(_same(i.get(k), x) for k, x in v.items()) for i in got)
    elif op == "no_item_in":
        key, names = v
        ok = isinstance(got, list) and not any(
            isinstance(i, dict) and i.get(key) in names for i in got)
    elif op == "no_item_endswith":
        key, suffix = v
        ok = isinstance(got, list) and not any(
            isinstance(i, dict) and str(i.get(key, "")).endswith(suffix) for i in got)
    elif op == "eq_fact":
        if v not in facts:
            return f"{exp.path}: the earlier value ({v}) was not recorded"
        ok = _same(got, facts[v])
        v = facts[v]
    else:
        raise ValueError(f"unknown expectation op {op!r}")
    if ok:
        return ""
    want = f"{op} {v!r}" if op not in ("present", "true", "false", "none") else op
    return f"{exp.path}: expected {want}, got {shown}" + (f" ({exp.said})" if exp.said else "")


_NOTE_FIELD = re.compile(r"\{(?P<path>[^{}]+)\}")


def note_text(template: str, data: Any, facts: dict[str, Any]) -> str:
    """A check's ``note`` with its fields filled: ``{a.b}`` from the JSON, ``{fact:x}`` from a
    fact an earlier check recorded; ``-`` for one that is missing or empty."""
    def fill(m: re.Match[str]) -> str:
        path = m.group("path")
        if path.startswith("fact:"):
            got = facts.get(path[5:], _MISSING)
        else:
            got = resolve(data, path) if isinstance(data, dict) else _MISSING
        if got is _MISSING or got in ("", None) or got == []:
            return "-"
        if isinstance(got, list):
            return ",".join(map(str, got))
        return str(got)
    return _NOTE_FIELD.sub(fill, template) if template else ""


# --- results -------------------------------------------------------------------------------------


@dataclass
class CheckResult:
    id: str
    section: str
    title: str
    tier: str
    verdict: str
    reason: str = ""
    command: str = ""
    exit: int | None = None
    seconds: float = 0.0
    started: str = ""
    failures: list[str] = field(default_factory=list)
    evidence: str = ""
    hint: str = ""
    attempts: int = 0


@dataclass
class Iteration:
    n: int
    dir: Path
    started: str = ""
    ended: str = ""
    results: list[CheckResult] = field(default_factory=list)
    stopped: dict[str, str] | None = None       # {reason, check}: a SAFETY stop
    ended_early: str = ""                       # the deadline or a signal (not a safety stop)
    facts: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.results:
            row = out.setdefault(r.section, {PASS: 0, FAIL: 0, MANUAL: 0, SKIPPED: 0, STOPPED: 0})
            row[r.verdict] += 1
        return out

    def first_failure(self) -> CheckResult | None:
        return next((r for r in self.results if r.verdict in (FAIL, STOPPED)), None)

    def exit_code(self) -> int:
        if self.stopped is not None:
            return EXIT_STOP
        return EXIT_FAIL if any(r.verdict == FAIL for r in self.results) else EXIT_PASS


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _json(text: str) -> Any:
    """The one JSON object HM prints on stdout (its last JSON line), else None."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _mcc_reading_busy(data: Any) -> str:
    """The reason of an MCC reading the one-reader scan refused (another process names or
    holds tty_00), else ""."""
    readings = data.get("readings") if isinstance(data, dict) else None
    for r in readings if isinstance(readings, list) else ():
        why = str(r.get("reason") or "") if isinstance(r, dict) else ""
        if isinstance(r, dict) and r.get("available") is False and "tty_00" in why \
                and ("another process" in why or "other processes" in why):
            return why
    return ""


def _own_request(data: Any) -> bool:
    """A HELD from this Harness Manager's own control-port gate (the service's own request
    held the port): ``error.data.reason`` is ``OWN_REQUEST``. Never another holder."""
    return isinstance(data, dict) and resolve(data, "error.data.reason") == OWN_REQUEST


def _tty00_busy(data: Any, stderr: str) -> bool:
    msg = str(resolve(data, "error.message") if isinstance(data, dict) else "") + " " + stderr
    return "tty_00" in msg or "MCC console" in msg


# --- the runner -----------------------------------------------------------------------------------


@dataclass
class Options:
    board: str
    evidence: Path
    writes: str = "none"
    repeat: int = 1
    interval_s: float = 900.0
    stop_on_first_fail: bool = False
    stop_at: float | None = None          # epoch: no check starts after this
    until: float | None = None            # epoch: the planned end (--until)
    gap_s: float = 3.0
    max_unreachable: int = 3
    margin_s: float = 600.0               # no check starts this close to --until or the lease end
    restore_wait_s: float = 2400.0        # a card job reads back for up to ~35 min (B2)
    #: the subprocesses' route (the module docstring); the announcement says which
    route: str = ROUTE_IN_PROCESS
    #: the service keeps the board open and heartbeats the lease until the run ends (HIL-GUI):
    #: its expiry is then not a deadline. It must still be held HERE at every gate.
    lease_kept: bool = False
    #: the announcement's "lease" and "stop it" lines ("" = the command line's own words)
    lease_line: str = ""
    stop_line: str = ""
    where_line: str = ""


class Runner:
    def __init__(self, plan: P.Plan, opts: Options, invoker: Invoker, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] | None = None,
                 log: Callable[[str], None] | None = None,
                 notify: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.plan, self.o, self.invoke_raw = plan, opts, invoker
        #: progress for a watcher (the service's run manager): ``notify(kind, data)``, kind
        #: ``iteration``, ``check``, ``result``, ``iteration_end``, ``waiting``, ``end_state``,
        #: ``end``. It never changes the run; an exception in it is logged and ignored.
        self._notify = notify
        self.clock = clock
        self.halt = threading.Event()           # a signal: finish the check, restore, exit
        self._sleep = sleep if sleep is not None else (lambda s: self.halt.wait(s))
        self.log = log or (lambda text: print(f"hil: {text}", file=sys.stderr, flush=True))
        self.changed = False                    # the runner swapped the partition
        self.last_rm: str = ""                  # the last rm_id an identity read reported
        self.end: dict[str, Any] = {}
        self.iterations: list[Iteration] = []
        self.sent: list[list[str]] = []         # every argv sent (evidence and tests)
        self._last_end = 0.0
        self._lease_ok_at = -1                  # len(self.sent) when the lease was last held here
        self._lease_rc: int | None = 0          # the last `lease show`'s exit
        self.stop_why = ""                      # why stop_at is what it is, when not --until
        self.ended_early = ""                   # the run ended between iterations: why
        self.refused_start = ""

    def notify(self, kind: str, **data: Any) -> None:
        if self._notify is None:
            return
        try:
            self._notify(kind, data)
        except Exception as exc:  # noqa: BLE001 - a watcher never changes the run
            self.log(f"progress watcher failed ({type(exc).__name__}: {exc}); the run goes on")

    def _add(self, it: Iteration, res: CheckResult) -> None:
        it.results.append(res)
        self.notify("result", iteration=it.n, result=asdict(res))

    # -- sending --

    def _pace(self) -> None:
        wait = self._last_end + self.o.gap_s - time.monotonic()
        if self._last_end and wait > 0:
            self._sleep(wait)

    def invoke(self, argv: Sequence[str], *, text: bool = False, timeout: float = 180.0,
               check: str = "") -> Outcome:
        """The ONE way a command reaches Harness Manager: allow-listed, paced, recorded."""
        argv = list(argv)
        if not allowed(argv, self.o.board, self.o.writes):
            raise SafetyStop(f"refused to send `harness-manager {shlex.join(argv)}`: not allowed "
                             f"with --writes {self.o.writes} (the runner's allow-list)", check)
        full = argv if text else ["--json", *argv]
        self._pace()
        self.sent.append(full)
        try:
            return self.invoke_raw(full, timeout)
        finally:
            self._last_end = time.monotonic()

    def lease_view(self, check: str = "lease") -> tuple[bool, str, Any]:
        """(held here, why not, the lease JSON)."""
        out = self.invoke(("lease", "show", self.o.board), timeout=120.0, check=check)
        self._lease_rc = out.rc
        data = _json(out.stdout)
        if out.rc != 0 or not isinstance(data, dict):
            return False, f"`lease show` answered exit {out.rc}: {_err_text(data, out)}", data
        lease = data.get("lease") or None
        if not lease:
            return False, "the board is not leased (take it with `harness-manager lease acquire`)", data
        if lease.get("here") is not True:
            who = lease.get("holder") or "?"
            return False, (f"the lease is held by {who}, not by this Harness Manager "
                           "(lease.here is false: another session or machine has the token)"), data
        self._lease_ok_at = len(self.sent)
        return True, "", data

    # -- the run --

    def run(self) -> int:
        o = self.o
        o.evidence.mkdir(parents=True, exist_ok=True)
        started = self.clock()
        code = EXIT_STOP
        try:
            ok, why, data = self.lease_view("gate")
            _write_json(o.evidence / "0_lease_gate.json",
                        {"command": f"harness-manager --json lease show {o.board}",
                         "here": ok, "why": why, "stdout_json": data})
            if not ok:
                self.refused_start = f"refused to start: {why}"
                self.log(self.refused_start)
                return EXIT_STOP
            refusal = self._lease_deadline(data)
            if refusal:
                self.refused_start = f"refused to start: {refusal}"
                self.log(self.refused_start)
                return EXIT_STOP
            for n in range(1, o.repeat + 1):
                if self._time_up():
                    self.ended_early = self._why_up()
                    break
                folder = o.evidence if o.repeat == 1 else o.evidence / f"iter-{n:03d}"
                it = Iteration(n, folder)
                self.iterations.append(it)
                self.log(f"iteration {n}/{o.repeat}: {self.plan.name}, writes {o.writes}")
                self.notify("iteration", n=n, repeat=o.repeat, dir=str(folder))
                self.run_iteration(it)
                self.notify("iteration_end", n=n, exit=it.exit_code(), counts=_totals(it),
                            first_failure=_ff(it), stopped=it.stopped,
                            ended_early=it.ended_early or None)
                if it.stopped or it.ended_early or (o.stop_on_first_fail and it.exit_code()):
                    break
                if n < o.repeat:
                    self._wait_interval()
            code = self.exit_code()
        except SafetyStop as exc:           # a stop outside an iteration (the gate's own argv)
            self.refused_start = self.refused_start or exc.reason
            code = EXIT_STOP
        finally:
            try:
                self.notify("end_state", changed=self.changed)
                self.end = self.end_state()
            finally:
                if self.end.get("stop"):
                    code = EXIT_STOP
                self.write_reports(started, code)
                self.notify("end", exit=code, end_state=self.end,
                            refused_start=self.refused_start or None,
                            ended_early=self.ended_early or None)
        return code

    def exit_code(self) -> int:
        if self.refused_start:
            return EXIT_STOP
        codes = [it.exit_code() for it in self.iterations]
        if self.halt.is_set():
            codes.append(EXIT_STOP)
        return max(codes, default=EXIT_PASS)

    def _time_up(self) -> bool:
        return self.halt.is_set() or (self.o.stop_at is not None and self.clock() >= self.o.stop_at)

    def _why_up(self) -> str:
        if self.halt.is_set():
            return ("interrupted by a signal" if self.o.route == ROUTE_IN_PROCESS
                    else "interrupted by a signal (Stop in the app)")
        return self.stop_why or f"the deadline ({_iso(self.o.stop_at or 0)})"

    def _wait_interval(self) -> None:
        wake = self.clock() + self.o.interval_s
        if self.o.stop_at is not None:
            wake = min(wake, self.o.stop_at)
        self.notify("waiting", next_at=wake)
        while not self.halt.is_set():
            left = wake - self.clock()
            if left <= 0:
                return
            self._sleep(min(left, 30.0))

    def _lease_deadline(self, data: Any) -> str:
        """The lease's expiry is a deadline too: nothing heartbeats it here (the runner's
        commands run in-process, not in the service), and a lease that lapses mid-swap would
        leave the board swapped with nobody allowed to put it back. So no check starts in the
        last ``--margin`` minutes before it expires; less than that left refuses the start."""
        exp = resolve(data, "lease.expires_at") if isinstance(data, dict) else None
        if self.o.lease_kept:
            # HIL-GUI: the service holds the board open and heartbeats the lease until the run
            # ends, so today's expiry moves on. Held HERE is still asked at every gate: a lease
            # that stops being ours (lost, taken, a heartbeat that failed for 2/3 of its TTL)
            # stops the run there.
            self.log(f"the lease{f' (now until {exp})' if isinstance(exp, str) and exp else ''} "
                     "is heartbeated by the Harness Manager service until the run ends")
            return ""
        if not isinstance(exp, str) or not exp:
            return ""
        try:
            at = datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return ""
        latest = at - self.o.margin_s
        if latest <= self.clock():
            return (f"the lease expires at {exp}, within --margin ({self.o.margin_s / 60:g} min): "
                    "take it with a --ttl that outlasts the run")
        if self.o.stop_at is None or latest < self.o.stop_at:
            self.o.stop_at = latest
            self.stop_why = (f"the lease expires at {exp} (the runner stops "
                             f"{self.o.margin_s / 60:g} min before it; take it with a longer --ttl)")
            if self.o.until is not None:
                self.log(f"the lease expires at {exp}, before the planned end "
                         f"{_iso(self.o.until)}: the run ends {self.o.margin_s / 60:g} min "
                         "before the lease does")
        return ""

    def run_iteration(self, it: Iteration) -> None:
        it.dir.mkdir(parents=True, exist_ok=True)
        it.started = _iso(self.clock())
        stop: SafetyStop | None = None
        for section in self.plan.sections:
            runs = any(self._classify(c, it) is None for c in section.checks)
            for c in section.checks:
                if stop is not None or it.ended_early:
                    reason = (f"the run stopped at {stop.check or 'the lease check'}"
                              if stop is not None else it.ended_early)
                    self._add(it, self._static(c, SKIPPED, reason))
                    continue
                if self._time_up():
                    it.ended_early = self._why_up()
                    self._add(it, self._static(c, SKIPPED, it.ended_early))
                    continue
                pre = self._classify(c, it)
                if pre is not None:
                    self._add(it, pre)
                    continue
                try:
                    if runs:
                        runs = False            # once per section, before its first command
                        self._require_lease(c.id)
                    if c.tier == P.SAFE:
                        self._require_lease(c.id)
                    self.notify("check", iteration=it.n, id=c.id, section=c.section,
                                title=c.title, tier=c.tier, started=_iso(self.clock()))
                    res = self.run_check(c, it)
                except SafetyStop as exc:
                    stop = exc
                    res = exc.result or self._static(c, STOPPED, exc.reason)
                    res.verdict, res.reason, res.hint = STOPPED, exc.reason, c.hint
                    it.stopped = {"reason": exc.reason, "check": exc.check or c.id}
                self._add(it, res)
                if res.verdict == FAIL and self.o.stop_on_first_fail:
                    it.ended_early = f"--stop-on-first-fail: {c.id} failed"
        it.ended = _iso(self.clock())
        self._write_iteration(it)

    def _require_lease(self, check: str) -> None:
        if self._lease_ok_at == len(self.sent):
            return                  # nothing was sent since the lease was last seen held here
        backoff = BACKOFF_FIRST_S
        for attempt in range(1, self.o.max_unreachable + 1):
            ok, why, _ = self.lease_view(check)
            if ok:
                return
            if self._lease_rc not in (None, HM_UNREACHABLE):
                raise SafetyStop(f"the lease was lost: {why}", check)
            if attempt < self.o.max_unreachable and not self.halt.is_set():
                self.log(f"{check}: the hub did not answer the lease check; again in "
                         f"{backoff:.0f} s ({attempt}/{self.o.max_unreachable})")
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
        raise SafetyStop(f"the hub did not answer the lease check {self.o.max_unreachable} "
                         f"times: {why}", check)

    def _static(self, c: P.Check, verdict: str, reason: str) -> CheckResult:
        return CheckResult(c.id, c.section, c.title, c.tier, verdict, reason)

    def _classify(self, c: P.Check, it: Iteration) -> CheckResult | None:
        """A check that does not run: manual, skipped in this plan or mode, or needing a fact."""
        if c.skip:
            return self._static(c, SKIPPED, c.skip)
        if c.tier == P.MANUAL:
            return self._static(c, MANUAL, c.why)
        if c.tier == P.SAFE and self.o.writes != "safe":
            return self._static(c, SKIPPED, "--writes none (it needs --writes safe)")
        if c.needs is not None:
            fact, want = c.needs
            have = self.changed if fact == "changed" else it.facts.get(fact)
            if have != want:
                return self._static(c, SKIPPED, c.needs_why or f"needs {fact} = {want}")
        return None

    def run_check(self, c: P.Check, it: Iteration) -> CheckResult:
        argv = [a.replace("{B}", self.o.board).replace("{static}", self.plan.static)
                for a in c.argv]
        res = CheckResult(c.id, c.section, c.title, c.tier, FAIL, started=_iso(self.clock()),
                          command="harness-manager " + shlex.join(argv if c.text
                                                                    else ["--json", *argv]),
                          evidence=f"{c.evidence or c.id}.json", hint=c.hint)
        backoff = BACKOFF_FIRST_S
        attempts: list[dict[str, Any]] = []
        is_program = argv[:1] == ["program"]
        while True:
            if is_program:
                self.changed = True             # attempted: the finally puts greybox back
            out = self.invoke(argv, text=c.text, timeout=c.timeout_s, check=c.id)
            data = None if c.text else _json(out.stdout)
            attempts.append({"exit": out.rc, "seconds": round(out.seconds, 3),
                             "stderr_tail": out.stderr[-2000:]})
            res.exit, res.seconds, res.attempts = out.rc, round(out.seconds, 3), len(attempts)
            unreachable = out.rc in (None, HM_UNREACHABLE) and HM_UNREACHABLE not in c.exits
            own = out.rc == HM_HELD and HM_HELD not in c.exits and _own_request(data)
            if (unreachable or own) and c.tier == P.READ and not self._time_up() \
                    and len(attempts) < self.o.max_unreachable:
                what = ("busy with this Harness Manager's own request" if own
                        else f"unreachable (exit {out.rc})")
                self.log(f"{c.id}: {what}; again in {backoff:.0f} s "
                         f"({len(attempts)}/{self.o.max_unreachable})")
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue
            break
        try:
            self._verdict(c, it, res, out, data, attempts)
        except SafetyStop as exc:
            res.verdict, res.reason = STOPPED, exc.reason
            exc.result = res
            raise
        finally:
            _write_json(it.dir / res.evidence,
                        {**{k: v for k, v in asdict(res).items() if k != "evidence"},
                         "argv": argv, "attempts": attempts,
                         "stdout_json" if not c.text else "stdout":
                         data if not c.text else out.stdout,
                         **({"stdout_raw": out.stdout} if not c.text and data is None else {}),
                         "stderr": out.stderr[-20000:]})
        self.log(f"{c.id} {res.verdict}" + (f": {res.reason}" if res.reason else ""))
        return res

    def _verdict(self, c: P.Check, it: Iteration, res: CheckResult, out: Outcome, data: Any,
                 attempts: list[dict[str, Any]]) -> None:
        """Pass, fail, skip; or raise ``SafetyStop``. The rules in the module docstring (3)."""
        rc = out.rc
        err = _err_text(data, out)
        exits = c.exits
        alt = next((a for a in c.answers if a.exit == rc), None)
        if rc is None or (rc == HM_UNREACHABLE and rc not in exits):
            what = "timed out" if rc is None else "unreachable (exit 7)"
            if c.tier == P.SAFE:
                raise SafetyStop(f"{c.id} {what}: a write is never retried ({err})", c.id)
            raise SafetyStop(f"the board was unreachable {len(attempts)} times at {c.id} "
                             f"({err})", c.id)
        if rc == HM_HELD and rc not in exits:
            if c.mcc and _tty00_busy(data, out.stderr):
                res.verdict, res.reason = SKIPPED, f"tty_00 busy: {err} (not retried)"
                return
            if _own_request(data):
                # Our own service's session held the control port (its gate waited 10 s for
                # this Harness Manager's own request): not another holder, so no stop. A read
                # was asked again after the back-off above; a write is not retried.
                res.failures.append(f"busy with this Harness Manager's own request ({err}); "
                                    "not another holder, so the run goes on")
                res.verdict, res.reason = FAIL, res.failures[0]
                return
            raise SafetyStop(f"{c.id}: HELD (exit 4): {err}. Another holder, or the reset "
                             "guard's card job: the runner never forces", c.id)
        if rc == HM_REFUSED and rc not in exits:
            raise SafetyStop(f"{c.id}: refused (exit 15): {err}", c.id)
        if rc == HM_INCOMPATIBLE and rc not in exits:
            raise SafetyStop(f"{c.id}: an unexpected identity (exit 14): {err}", c.id)
        busy = _mcc_reading_busy(data) if c.mcc and rc == 0 else ""
        if busy:
            # The CLI answers a refused MCC read as an unavailable reading, with the scan's
            # words (hub_mcc.other_readers): the same "tty_00 busy", not a failure.
            res.verdict, res.reason = SKIPPED, f"tty_00 busy: {busy} (not retried)"
            return
        if rc not in exits:
            res.failures.append(f"exit {rc}, expected {' or '.join(map(str, exits))}: {err}")
        elif not c.text and not isinstance(data, dict):
            res.failures.append("no JSON object on stdout")
        else:
            stops: list[str] = []
            for exp in (alt.expects if alt is not None else c.expects):
                why = evaluate(exp, data, out.stdout, it.facts)
                if why:
                    (stops if exp.stop else res.failures).append(why)
            if stops:
                res.failures[:0] = stops
                res.reason = "; ".join(res.failures)
                self._learn(c, it, data, passed=False)
                raise SafetyStop(f"{c.id}: an unexpected identity: {'; '.join(stops)}", c.id)
        self._learn(c, it, data, passed=not res.failures)
        if res.failures:
            res.verdict, res.reason = FAIL, "; ".join(res.failures)
        else:
            res.verdict = PASS
            res.reason = alt.note if alt is not None else note_text(c.note, data, it.facts)

    def _learn(self, c: P.Check, it: Iteration, data: Any, *, passed: bool) -> None:
        if isinstance(data, dict):
            for fact, path in c.record.items():
                got = resolve(data, path)
                if got is not _MISSING:
                    it.facts[fact] = got
                    if fact == "rm_id":
                        self.last_rm = str(got)
        if passed and c.argv[:1] == ("program",):
            it.facts["swapped"] = c.argv[2]
            rm = resolve(data, "result.rm_id") if isinstance(data, dict) else _MISSING
            self.last_rm = str(rm) if rm is not _MISSING else ""
        if passed and c.argv[:1] == ("restore",):
            self.last_rm = P.GREYBOX_RM

    # -- the end state (the finally) --

    def end_state(self) -> dict[str, Any]:
        """Greybox if the runner changed the partition (rule 4), and the claim as found."""
        end: dict[str, Any] = {"changed": self.changed}
        try:
            end.update(self._restore_if_changed())
        except SafetyStop as exc:
            end.update(greybox=False, restore=f"not restored: {exc.reason}", stop=True)
        if self.plan.impl == "linux" and self.iterations and not end.get("stop"):
            end["claim"] = self._claim_end()
        return end

    def _restore_if_changed(self) -> dict[str, Any]:
        if not self.changed:
            return {"greybox": None, "restore": "not needed: the runner did not swap the partition"}
        ok, why, _ = self.lease_view("end")
        if not ok:
            return {"greybox": False, "stop": True,
                    "restore": f"NOT attempted: {why}. The board may still run "
                               f"{self.last_rm or 'the overlay the runner loaded'}: someone "
                               "else holds it now, so the runner leaves it alone"}
        rm = self._read_rm("end")
        if rm and rm.lower() == P.GREYBOX_RM:
            return {"greybox": True, "restore": "on greybox at the end (read back)"}
        deadline = self.clock() + self.o.restore_wait_s
        while True:
            out = self.invoke(("restore", self.o.board), timeout=600.0, check="end")
            data = _json(out.stdout)
            _write_json(self.o.evidence / "end_restore.json",
                        {"command": f"harness-manager --json restore {self.o.board}",
                         "exit": out.rc, "stdout_json": data, "stderr": out.stderr[-20000:]})
            if out.rc == 0:
                break
            if out.rc == HM_HELD and self.clock() < deadline:
                self.log(f"the final restore was refused (exit 4: {_err_text(data, out)}); "
                         f"asking again in {RESTORE_POLL_S:.0f} s, never forced")
                self._sleep(RESTORE_POLL_S)
                ok, why, _ = self.lease_view("end")
                if not ok:
                    return {"greybox": False, "stop": True,
                            "restore": f"NOT restored: the lease was lost while waiting ({why})"}
                continue
            return {"greybox": False, "stop": True,
                    "restore": f"the restore failed (exit {out.rc}): {_err_text(data, out)}. The "
                               f"board may still run {self.last_rm or 'the overlay'}: "
                               "`harness-manager restore` by hand"}
        rm = self._read_rm("end")
        if rm.lower() == P.GREYBOX_RM:
            return {"greybox": True, "restore": "restored greybox and read it back"}
        return {"greybox": False, "stop": True,
                "restore": f"restore said done, but the board reports rm_id {rm or '?'}"}

    def _read_rm(self, check: str) -> str:
        out = self.invoke(("info", self.o.board), timeout=180.0, check=check)
        data = _json(out.stdout)
        rm = resolve(data, "identity.rm_id") if isinstance(data, dict) else _MISSING
        return "" if rm is _MISSING or rm is None else str(rm)

    def _claim_end(self) -> dict[str, Any]:
        """The runner never claims, and HM has no unclaim verb (``mps3-unclaim`` is on the
        board's serial console): the end state is the claim as B1 found it. Read it again."""
        first = next((it.facts.get("claim") for it in self.iterations if "claim" in it.facts), None)
        ok, _, _ = self.lease_view("end")
        if not ok:
            return {"start": first, "end": None, "note": "not read: the lease is not held here"}
        out = self.invoke(("board", "claim-status", self.o.board), timeout=180.0, check="end")
        data = _json(out.stdout)
        now = resolve(data, "claim.state") if isinstance(data, dict) else _MISSING
        now = None if now is _MISSING else now
        return {"start": first, "end": now, "unchanged": first == now,
                "note": "the runner never runs `board claim`; Harness Manager has no verb to "
                        "unclaim (mps3-unclaim on the board's serial console is the only way)"}

    # -- the evidence --

    def _write_iteration(self, it: Iteration) -> None:
        summary = iteration_summary(self, it)
        _write_json(it.dir / "summary.json", summary)
        (it.dir / "REPORT.md").write_text(iteration_report(self, it, summary), encoding="utf-8")

    def write_reports(self, started: float, code: int) -> None:
        o = self.o
        agg = {"plan": self.plan.name, "runbook": self.plan.runbook, "board": o.board,
               "writes": o.writes, "static": self.plan.static, "started": _iso(started),
               "ended": _iso(self.clock()), "exit": code,
               "refused_start": self.refused_start or None, "end_state": self.end,
               "ended_early": self.ended_early or None,
               "repeat": o.repeat,
               "iterations": [{"n": it.n, "dir": str(it.dir), "exit": it.exit_code(),
                               "stopped": it.stopped, "ended_early": it.ended_early or None,
                               "counts": _totals(it),
                               "first_failure": _ff(it)} for it in self.iterations],
               "sent": [" ".join(a) for a in self.sent]}
        flaky: dict[str, list[int]] = {}
        for it in self.iterations:
            for r in it.results:
                if r.verdict == FAIL:
                    flaky.setdefault(r.id, []).append(it.n)
        agg["failed_in"] = flaky
        if o.repeat == 1 and self.iterations:
            # One iteration: its own summary is the top one, with the run's end state added.
            it = self.iterations[0]
            summary = iteration_summary(self, it)
            summary.update(exit=code, end_state=self.end, sent=agg["sent"])
            _write_json(o.evidence / "summary.json", summary)
            (o.evidence / "REPORT.md").write_text(
                iteration_report(self, it, summary, final=True), encoding="utf-8")
            return
        _write_json(o.evidence / "summary.json", agg)
        (o.evidence / "REPORT.md").write_text(aggregate_report(self, agg), encoding="utf-8")


def _err_text(data: Any, out: Outcome) -> str:
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        e = data["error"]
        return f"{e.get('message', '')}" + (f" — {e['hint']}" if e.get("hint") else "")
    tail = [ln for ln in out.stderr.strip().splitlines() if ln.strip()]
    return tail[-1] if tail else "(no message)"


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def _totals(it: Iteration) -> dict[str, int]:
    out = {PASS: 0, FAIL: 0, MANUAL: 0, SKIPPED: 0, STOPPED: 0}
    for r in it.results:
        out[r.verdict] += 1
    return out


def _ff(it: Iteration) -> dict[str, Any] | None:
    r = it.first_failure()
    return None if r is None else {"id": r.id, "verdict": r.verdict, "reason": r.reason,
                                   "hint": r.hint, "evidence": r.evidence}


# --- reports -------------------------------------------------------------------------------------


def iteration_summary(runner: Runner, it: Iteration) -> dict[str, Any]:
    return {"plan": runner.plan.name, "runbook": runner.plan.runbook, "board": runner.o.board,
            "writes": runner.o.writes, "static": runner.plan.static, "iteration": it.n,
            "started": it.started, "ended": it.ended, "exit": it.exit_code(),
            "stopped": it.stopped, "ended_early": it.ended_early or None,
            "sections": it.counts(), "totals": _totals(it), "first_failure": _ff(it),
            "facts": it.facts, "checks": [asdict(r) for r in it.results]}


_RESULT_WORDS = {EXIT_PASS: "PASS", EXIT_FAIL: "FAIL", EXIT_STOP: "STOPPED"}


def _headline(code: int, stopped: str, refused: str, halted: bool = False) -> str:
    if refused:
        return f"**STOPPED: {refused}** (exit 2; nothing touched the board)"
    if code == EXIT_STOP and halted and not stopped:
        return ("**STOPPED: asked to stop** (Stop in the app, or a signal: it finished the check "
                "and put the end state back; exit 2)")
    if code == EXIT_STOP:
        return f"**STOPPED for safety: {stopped or 'see below'}** (exit 2)"
    return f"**{_RESULT_WORDS[code]}** (exit {code})"


def _end_lines(end: dict[str, Any]) -> list[str]:
    if not end:
        return []
    lines = [f"- partition: {end.get('restore', '?')}"]
    claim = end.get("claim")
    if claim:
        lines.append(f"- SSH claim: {claim.get('start')} at the start, {claim.get('end')} at the "
                     f"end ({claim.get('note')})")
    return lines


def iteration_report(runner: Runner, it: Iteration, summary: dict[str, Any], *,
                     final: bool = False) -> str:
    plan, o = runner.plan, runner.o
    code = summary.get("exit", it.exit_code())
    stopped = (it.stopped or {}).get("reason", "") or (runner.end.get("restore", "")
                                                       if runner.end.get("stop") else "")
    lines = [f"# HIL-AUTO: {plan.name} on {o.board}", "",
             f"- {_headline(code, stopped, runner.refused_start if final else '', runner.halt.is_set())}",
             f"- plan `{plan.name}` ({plan.runbook}), `--writes {o.writes}`, static "
             f"`{plan.static}`, iteration {it.n} of {o.repeat}",
             f"- ran {it.started} → {it.ended}"]
    if it.ended_early:
        lines.append(f"- ended early: {it.ended_early}")
    if final:
        lines += _end_lines(runner.end)
    ff = it.first_failure()
    if ff is not None:
        reason = ff.reason[len(ff.id) + 2:] if ff.reason.startswith(f"{ff.id}: ") else ff.reason
        lines += ["", "## First failure", "",
                  f"**{ff.id}** {ff.title}: {reason}"]
        if ff.hint:
            lines.append(f"\nHint: {ff.hint}")
        if ff.evidence:
            lines.append(f"\nEvidence: `{ff.evidence}`")
    lines += ["", "## Sections", "", "| § | section | pass | fail | stopped | manual | skipped |",
              "|---|---|---|---|---|---|---|"]
    counts = it.counts()
    for s in plan.sections:
        c = counts.get(s.id, {})
        lines.append(f"| {s.id} | {s.title} | {c.get(PASS, 0)} | {c.get(FAIL, 0)} | "
                     f"{c.get(STOPPED, 0)} | {c.get(MANUAL, 0)} | {c.get(SKIPPED, 0)} |")
    lines += ["", "## Checks", "", "| id | verdict | s | why |", "|---|---|---|---|"]
    for r in it.results:
        if r.verdict == MANUAL:
            continue
        why = (r.reason or "").replace("|", "/")
        lines.append(f"| {r.id} | {r.verdict} | {r.seconds:g} | {why} |")
    manual = [r for r in it.results if r.verdict == MANUAL]
    if manual:
        lines += ["", "## Manual (never unattended)", ""]
        lines += [f"- **{r.id}** {r.title}: {r.reason}" for r in manual]
    return "\n".join(lines) + "\n"


def aggregate_report(runner: Runner, agg: dict[str, Any]) -> str:
    plan, o = runner.plan, runner.o
    stopped = next((it.stopped["reason"] for it in runner.iterations if it.stopped), "") or (
        runner.end.get("restore", "") if runner.end.get("stop") else "")
    lines = [f"# HIL-AUTO: {plan.name} on {o.board}, {len(runner.iterations)} of {o.repeat} "
             "iterations", "",
             f"- {_headline(agg['exit'], stopped, runner.refused_start, runner.halt.is_set())}",
             f"- plan `{plan.name}` ({plan.runbook}), `--writes {o.writes}`, every "
             f"{o.interval_s:g} s",
             f"- ran {agg['started']} → {agg['ended']}", *_end_lines(runner.end)]
    if agg["ended_early"]:
        lines.append(f"- ended early: {agg['ended_early']}")
    if agg["failed_in"]:
        lines += ["", "## Failed checks, by iteration", ""]
        lines += [f"- **{cid}**: iteration {', '.join(map(str, ns))}"
                  for cid, ns in sorted(agg["failed_in"].items())]
    lines += ["", "## Iterations", "", "| # | result | pass | fail | skipped | first failure |",
              "|---|---|---|---|---|---|"]
    for row in agg["iterations"]:
        c = row["counts"]
        ff = row["first_failure"]
        first = f"{ff['id']}: {ff['reason']}".replace("|", "/") if ff else ""
        if row["ended_early"]:
            first = (first + "; " if first else "") + f"ended early: {row['ended_early']}"
        lines.append(f"| [{row['n']}]({Path(row['dir']).name}/REPORT.md) | "
                     f"{_RESULT_WORDS[row['exit']]} | {c[PASS]} | {c[FAIL] + c[STOPPED]} | "
                     f"{c[SKIPPED]} | {first} |")
    return "\n".join(lines) + "\n"


# --- the announcement ------------------------------------------------------------------------------


def announce_text(plan: P.Plan, o: Options, started: float) -> str:
    auto = [c for c in plan.checks() if not c.skip and c.tier != P.MANUAL
            and (c.tier == P.READ or o.writes == "safe")]
    changes = [f"  - {c.id} {c.title}: {c.writes}" for c in auto if c.writes]
    mcc = [c.id for c in auto if c.mcc]
    if mcc:
        changes.append(f"  - {', '.join(mcc)}: one paced MCC read on the hub's tty_00 per "
                       "iteration; skipped (never retried) while anything else names or holds "
                       "tty_00")
    until = "none (runs until the plan ends)" if o.until is None else (
        f"{_iso(o.until)}; no check starts after {_iso(o.stop_at or o.until)}")
    reps = (f"{o.repeat} iteration(s), every {o.interval_s:g} s" if o.repeat > 1
            else "one pass")
    where = o.where_line or f"on {socket.gethostname()} (pid {os.getpid()})"
    lease = o.lease_line or (
        "taken by david beforehand, with a --ttl that outlasts the run; the runner only checks "
        "it is held here (and stops --margin before it expires), and never acquires, requests, "
        "forces or releases one")
    stop = o.stop_line or (f"kill -INT {os.getpid()} (or Ctrl-C in its tmux): it finishes the "
                           "check, restores greybox, writes REPORT.md")
    return "\n".join([
        f"HM HIL-AUTO on {o.board} ({plan.runbook}, plan {plan.name})",
        f"start        {_iso(started)} {where}",
        f"planned end  {until}",
        f"runs         {reps}; {len(auto)} automatic checks each; writes {o.writes}",
        f"expects      static {plan.static} ({plan.impl} harness); any other identity stops it",
        f"lease        {lease}",
        "changes      " + ("nothing on the board (read only)" if not changes
                             else "each iteration:"),
        *changes,
        "never        no card or slot writes, no SD writes, no MCC REBOOT, no fpgahub share, "
        "no SSH-claim change, never --force; the runner itself never takes, forces or "
        "releases a lease",
        "end state    greybox (restored in a finally if the runner swapped the partition); the "
        "SSH claim as it was",
        f"evidence     {o.evidence.resolve()}",
        f"stop it      {stop}",
        ""])


# --- the command line ------------------------------------------------------------------------------


def parse_until(text: str, now: datetime) -> datetime:
    """``HH:MM``: the next such local time after ``now``; else an ISO 8601 date-time."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if m:
        at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)
    at = datetime.fromisoformat(text.strip())
    return at if at.tzinfo else at.astimezone()


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.hil",
                                description="Harness Manager's HIL runbooks, unattended "
                                            "(docs/HIL_AUTO.md). With a Harness Manager "
                                            "service running, the run goes through it (the "
                                            "app's Checks section shows it).")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a plan against the board")
    r.add_argument("--plan", required=True, choices=sorted(P.PLANS))
    r.add_argument("--board", required=True, metavar="ADDR", help="the board (192.168.10.101 board 1, 192.168.11.101 board 2)")
    r.add_argument("--evidence", required=True, type=Path, metavar="DIR")
    r.add_argument("--writes", choices=("none", "safe"), default="none",
                   help="none (default): read-only checks. safe: also swap-and-restore and "
                        "the MCC read")
    r.add_argument("--repeat", type=int, default=1, metavar="N")
    r.add_argument("--interval", type=float, default=900.0, metavar="S",
                   help=f"seconds between iterations (at least {MIN_INTERVAL_S:g})")
    r.add_argument("--stop-on-first-fail", action="store_true")
    r.add_argument("--until", "--deadline", dest="until", default=None, metavar="HH:MM|ISO",
                   help="finish the check, restore and exit by then (HH:MM: the next one)")
    r.add_argument("--margin", type=float, default=10.0, metavar="MIN",
                   help="no check starts in the last MIN minutes before --until or before the "
                        "lease expires (default 10)")
    r.add_argument("--expect-static", default=None, metavar="HEX",
                   help="the static the board must run (default: the runbook's)")
    r.add_argument("--gap", type=float, default=3.0, metavar="S",
                   help="seconds between two commands (default 3)")
    r.add_argument("--max-unreachable", type=int, default=3, metavar="N",
                   help="tries of a read check before an unreachable board stops the run")
    r.add_argument("--hm", default=None, metavar="CMD",
                   help="the harness-manager command (default: this Python's)")
    r.add_argument("--announce-only", action="store_true",
                   help="write ANNOUNCE.txt and exit: nothing is sent")
    r.add_argument("--take-lease", action="store_true",
                   help="through a service: when the lease is free, the service takes it for "
                        "the run and releases it at the end (never someone else's)")
    r.add_argument("--in-process", action="store_true",
                   help="never hand the run to a running service: every command on the CLI's "
                        "in-process engine, as before HIL-GUI (the service must not hold the "
                        "board)")
    s = sub.add_parser("plans", help="print a plan's checks (id, section, tier, command)")
    s.add_argument("--plan", choices=sorted(P.PLANS), default=None)
    s.add_argument("--expect-static", default=None, metavar="HEX")
    return p


def print_plans(names: list[str], static: str | None) -> None:
    for name in names:
        plan = P.build(name, static=static)
        print(f"# {name} ({plan.runbook}, static {plan.static})")
        for c in plan.checks():
            state = c.tier if not c.skip else "skip"
            cmd = " ".join(c.argv) if c.argv else (c.why or c.skip)
            print(f"{c.id:5} §{c.section:2} {state:7} {c.title[:44]:44} {cmd}")


def main(argv: Sequence[str] | None = None, *, invoker: Invoker | None = None,
         clock: Callable[[], float] = time.time,
         sleep: Callable[[float], None] | None = None) -> int:
    a = make_parser().parse_args(argv)
    if a.cmd == "plans":
        print_plans([a.plan] if a.plan else sorted(P.PLANS), a.expect_static)
        return EXIT_PASS
    if a.repeat < 1 or a.interval < MIN_INTERVAL_S or a.gap < MIN_GAP_S or a.max_unreachable < 1:
        print(f"hil: --repeat >= 1, --interval >= {MIN_INTERVAL_S:g}, --gap >= {MIN_GAP_S:g} "
              "and --max-unreachable >= 1 (no tight loops)", file=sys.stderr)
        return EXIT_STOP
    if a.expect_static and not re.fullmatch(r"0x[0-9a-fA-F]{8}", a.expect_static):
        print("hil: --expect-static is 0x plus 8 hex digits", file=sys.stderr)
        return EXIT_STOP
    ev: Path = a.evidence
    if ev.exists() and any(p.name != "ANNOUNCE.txt" for p in ev.iterdir()):
        print(f"hil: {ev} already holds evidence: give a new folder (evidence is never "
              "overwritten)", file=sys.stderr)
        return EXIT_STOP
    if invoker is None and not a.hm and not a.in_process:
        service = running_service()
        # --announce-only goes to the service only for a board open there: it never opens one
        if service is not None and (not a.announce_only or open_in(service, a.board)):
            return through_service(service, a, sleep=sleep)
    plan = P.build(a.plan, static=a.expect_static)
    now = datetime.fromtimestamp(clock()).astimezone()
    until = stop_at = None
    if a.until:
        try:
            end = parse_until(a.until, now)
        except ValueError:
            print(f"hil: --until {a.until!r} is not HH:MM or an ISO 8601 time", file=sys.stderr)
            return EXIT_STOP
        until = end.timestamp()
        stop_at = until - 60.0 * max(a.margin, 0.0)
    opts = Options(board=a.board, evidence=ev, writes=a.writes, repeat=a.repeat,
                   interval_s=a.interval, stop_on_first_fail=a.stop_on_first_fail,
                   stop_at=stop_at, until=until, gap_s=a.gap, max_unreachable=a.max_unreachable,
                   margin_s=60.0 * max(a.margin, 0.0))
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "ANNOUNCE.txt").write_text(announce_text(plan, opts, clock()), encoding="utf-8")
    if a.announce_only:
        print((ev / "ANNOUNCE.txt").read_text(encoding="utf-8"), end="")
        return EXIT_PASS
    inv = invoker or SubprocessInvoker(shlex.split(a.hm) if a.hm else default_hm())
    runner = Runner(plan, opts, inv, clock=clock, sleep=sleep)
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: _halt(runner))
    try:
        code = runner.run()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f"hil: exit {code}; {ev / 'REPORT.md'}", file=sys.stderr)
    return code


# --- the command line, through a running service (HIL-GUI) -------------------------------------------


def running_service() -> Any:
    """The Harness Manager service running for this state dir (a ``RemoteEngine``), or None:
    none runs, it does not answer, or ``HARNESS_MANAGER_NO_DAEMON`` is set."""
    from harness_manager.cli.engine import daemon_disabled

    if daemon_disabled():
        return None
    try:
        from harness_manager.client import RemoteEngine

        return RemoteEngine.discover(timeout=5.0)
    except Exception:  # noqa: BLE001 - no service, or a broken one: run it here
        return None


def open_in(remote: Any, target: str) -> bool:
    """Whether the service has ``target`` open (its own state: no board contact)."""
    try:
        bid = remote.candidate_for(target).board_id
        rows = remote.http.get("/boards").get("boards") or []
    except Exception:  # noqa: BLE001 - not known: the announcement is written here
        return False
    return any(r.get("board_id") == bid and r.get("open") for r in rows if isinstance(r, dict))


def service_body(a: argparse.Namespace) -> dict[str, Any]:
    """The command line's options as ``POST /boards/{bid}/checks`` takes them."""
    body: dict[str, Any] = {
        "plan": a.plan, "writes": a.writes, "repeat": a.repeat, "interval_s": a.interval,
        "margin_min": a.margin, "gap_s": a.gap, "max_unreachable": a.max_unreachable,
        "stop_on_first_fail": bool(a.stop_on_first_fail), "take_lease": bool(a.take_lease),
        "evidence": str(Path(a.evidence).resolve()), "until": a.until or ""}
    if a.expect_static:
        body["expect_static"] = a.expect_static
    return body


FOLLOW_S = 5.0


def through_service(remote: Any, a: argparse.Namespace, *,
                    sleep: Callable[[float], None] | None = None) -> int:
    """Hand the run to the running service and follow it until it ends (Ctrl-C: Stop, as the
    app's button; the run then finishes its check and restores greybox). The service holds
    the board open and the lease for the run's length (``services/hil_runs.py``)."""
    from harness_manager.client.http import q
    from harness_manager.core.errors import AlreadyError, HarnessError

    http = remote.http

    def say(text: str) -> None:
        print(f"hil: {text}", file=sys.stderr, flush=True)

    bid = remote.candidate_for(a.board).board_id
    opened_here = False
    if not a.announce_only:               # the announcement alone never opens the board
        try:
            opened = http.post("/boards", {"target": a.board, "note": "hil-auto (checks run)"})
            bid = str(opened.get("board_id") or bid)
            opened_here = True
        except AlreadyError:
            pass                              # open in the service already (the app): shared
        except HarnessError as exc:
            say(f"refused to start: the service could not open {a.board}: {exc.message}")
            return EXIT_STOP
    path = f"/boards/{q(bid)}/checks"
    body = service_body(a)
    try:
        if a.announce_only:
            out = http.post(path, {**body, "announce_only": True})
            ev = Path(body["evidence"])
            ev.mkdir(parents=True, exist_ok=True)
            (ev / "ANNOUNCE.txt").write_text(out.get("announce", ""), encoding="utf-8")
            print(out.get("announce", ""), end="")
            refusal = out.get("refusal")
            if refusal:
                say(f"the run would be refused now: {refusal.get('message')}")
            return EXIT_PASS
        run = http.post(path, body)["run"]
    except HarnessError as exc:
        say(f"refused to start: {exc.message}" + (f" ({exc.hint})" if exc.hint else ""))
        return EXIT_STOP
    say(f"the Harness Manager service runs it (run {run['id']}, plan {run['plan']}); "
        f"evidence {run['evidence']}. Ctrl-C stops it (finishes the check, restores greybox)")
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: stop.set())
    asked, seen = False, 0
    wait = sleep or (lambda s: stop.wait(s))
    try:
        while True:
            if stop.is_set() and not asked:
                asked = True
                say("stopping: the run finishes its check, restores greybox, writes REPORT.md")
                try:
                    http.delete(path)
                except HarnessError as exc:
                    say(f"stop: {exc.message}")
            try:
                status = http.get(path)
            except HarnessError as exc:
                say(f"the service did not answer ({exc.message}); asking again")
                wait(FOLLOW_S)
                continue
            now = status.get("run")
            if now is None or now.get("id") != run["id"]:
                last = status.get("last") or next(
                    (r for r in status.get("runs") or [] if r.get("id") == run["id"]), {})
                code = last.get("exit")
                say(f"ended: {last.get('state')}, {last.get('result') or '?'}"
                    + (f" ({last.get('reason')})" if last.get("reason") else "")
                    + f"; {Path(str(last.get('evidence') or run['evidence'])) / 'REPORT.md'}")
                if opened_here:
                    # We opened the board in the service for this run: close it again (the
                    # service then stops heartbeating the lease; the lease stays yours).
                    with contextlib.suppress(HarnessError):
                        http.delete(f"/boards/{q(bid)}")
                return int(code) if isinstance(code, int) else EXIT_STOP
            lines = now.get("log") or []
            total = int(now.get("log_total") or len(lines))
            first = total - len(lines)           # the service keeps the last lines only
            for i, line in enumerate(lines):
                if first + i >= seen:
                    print(f"hil: {line}", file=sys.stderr, flush=True)
            seen = total
            wait(FOLLOW_S)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _halt(runner: Runner) -> None:
    if not runner.halt.is_set():
        runner.log("signal: finishing the current check, then restoring and writing the report")
    runner.halt.set()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
