"""``python -m tools.hil run``: the HIL runbooks, unattended (lane HIL-AUTO).

It drives Harness Manager's own CLI (``harness-manager --json VERB``), one subprocess per
check, exactly as david would type the runbook, and saves each answer as evidence. It is a
CLIENT of the CLI's ``--json`` contract: it never imports the engine, so what it proves is
what a person at srv03335 gets. ``docs/HIL_AUTO.md`` says how to run it.

The safety rules are here, in code:

1. **The lease.** ``gate_lease``: before anything touches the board, ``lease show`` must say
   ``lease.here`` (this Harness Manager holds the token). It is asked again before every
   section, before every ``safe`` check and before the final restore; lost = stop. The runner
   never acquires, requests, forces or releases a lease: ``allowed`` has no such argv.
2. **What may be sent.** ``allowed``: every command passes an exact allow-list of argv shapes
   for the ``--writes`` mode. ``none``: reads only. ``safe``: plus ``program <overlay> --yes``,
   ``restore``, ``mcc temp`` and ``identify``. Nothing else, ever: no ``--keep-on-card``, no
   ``--force``/``--consent``, no slot/card/sd verbs, no ``mcc reboot``/``mcc cmd``, no
   ``share start``, no ``board claim``. A refused argv stops the run (exit 2) unsent.
3. **Stops** (exit 2; ``Runner._verdict``): an unexpected identity (an ``Expect(stop=True)``,
   or exit 14); a refusal (exit 15: the claim lock, a safety rail); HELD (exit 4: another
   holder, a busy control channel, the reset guard's card job: never ``--force``); the lease
   lost; the board unreachable ``--max-unreachable`` times (reads back off 30 s doubling to
   10 min; a write is never retried). The MCC read's one-reader refusal is not a stop:
   "skipped (tty_00 busy)", never retried.
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
)
SAFE_ARGV: tuple[tuple[str, ...], ...] = (
    ("program", "{B}", "{RM}", "--yes"),
    ("restore", "{B}"),
    ("mcc", "{B}", "temp"),
    ("identify", "{B}"),
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
    timeout that asks it to stop (SIGTERM, 10 s) before it is killed."""

    def __init__(self, base: Sequence[str]) -> None:
        self.base = list(base)
        # Every command on the CLI's in-process engine, never a running service: one owner of
        # the board per command (opened, used, closed), no background polling of it from this
        # Harness Manager, and the slot/card verbs are in-process anyway (cli/engine.py).
        self.env = {**os.environ, "HARNESS_MANAGER_NO_DAEMON": "1"}

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


class Runner:
    def __init__(self, plan: P.Plan, opts: Options, invoker: Invoker, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        self.plan, self.o, self.invoke_raw = plan, opts, invoker
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
                self.run_iteration(it)
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
                self.end = self.end_state()
            finally:
                if self.end.get("stop"):
                    code = EXIT_STOP
                self.write_reports(started, code)
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
            return "interrupted by a signal"
        return self.stop_why or f"the deadline ({_iso(self.o.stop_at or 0)})"

    def _wait_interval(self) -> None:
        wake = self.clock() + self.o.interval_s
        if self.o.stop_at is not None:
            wake = min(wake, self.o.stop_at)
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
                    it.results.append(self._static(c, SKIPPED, reason))
                    continue
                if self._time_up():
                    it.ended_early = self._why_up()
                    it.results.append(self._static(c, SKIPPED, it.ended_early))
                    continue
                pre = self._classify(c, it)
                if pre is not None:
                    it.results.append(pre)
                    continue
                try:
                    if runs:
                        runs = False            # once per section, before its first command
                        self._require_lease(c.id)
                    if c.tier == P.SAFE:
                        self._require_lease(c.id)
                    res = self.run_check(c, it)
                except SafetyStop as exc:
                    stop = exc
                    res = exc.result or self._static(c, STOPPED, exc.reason)
                    res.verdict, res.reason, res.hint = STOPPED, exc.reason, c.hint
                    it.stopped = {"reason": exc.reason, "check": exc.check or c.id}
                it.results.append(res)
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
            unreachable = out.rc in (None, HM_UNREACHABLE) and HM_UNREACHABLE not in c.exit_ok
            if unreachable and c.tier == P.READ and not self._time_up() \
                    and len(attempts) < self.o.max_unreachable:
                self.log(f"{c.id}: unreachable (exit {out.rc}); again in {backoff:.0f} s "
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
        if rc is None or (rc == HM_UNREACHABLE and rc not in c.exit_ok):
            what = "timed out" if rc is None else "unreachable (exit 7)"
            if c.tier == P.SAFE:
                raise SafetyStop(f"{c.id} {what}: a write is never retried ({err})", c.id)
            raise SafetyStop(f"the board was unreachable {len(attempts)} times at {c.id} "
                             f"({err})", c.id)
        if rc == HM_HELD and rc not in c.exit_ok:
            if c.mcc and _tty00_busy(data, out.stderr):
                res.verdict, res.reason = SKIPPED, f"tty_00 busy: {err} (not retried)"
                return
            raise SafetyStop(f"{c.id}: HELD (exit 4): {err}. Another holder, or the reset "
                             "guard's card job: the runner never forces", c.id)
        if rc == HM_REFUSED and rc not in c.exit_ok:
            raise SafetyStop(f"{c.id}: refused (exit 15): {err}", c.id)
        if rc == HM_INCOMPATIBLE and rc not in c.exit_ok:
            raise SafetyStop(f"{c.id}: an unexpected identity (exit 14): {err}", c.id)
        busy = _mcc_reading_busy(data) if c.mcc and rc == 0 else ""
        if busy:
            # The CLI answers a refused MCC read as an unavailable reading, with the scan's
            # words (hub_mcc.other_readers): the same "tty_00 busy", not a failure.
            res.verdict, res.reason = SKIPPED, f"tty_00 busy: {busy} (not retried)"
            return
        if rc not in c.exit_ok:
            res.failures.append(f"exit {rc}, expected {' or '.join(map(str, c.exit_ok))}: {err}")
        elif not c.text and not isinstance(data, dict):
            res.failures.append("no JSON object on stdout")
        else:
            stops: list[str] = []
            for exp in c.expects:
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


def _headline(code: int, stopped: str, refused: str) -> str:
    if refused:
        return f"**STOPPED: {refused}** (exit 2; nothing touched the board)"
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
             f"- {_headline(code, stopped, runner.refused_start if final else '')}",
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
             f"- {_headline(agg['exit'], stopped, runner.refused_start)}",
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
    return "\n".join([
        f"HM HIL-AUTO on {o.board} ({plan.runbook}, plan {plan.name})",
        f"start        {_iso(started)} on {socket.gethostname()} (pid {os.getpid()})",
        f"planned end  {until}",
        f"runs         {reps}; {len(auto)} automatic checks each; writes {o.writes}",
        f"expects      static {plan.static} ({plan.impl} harness); any other identity stops it",
        "lease        taken by david beforehand, with a --ttl that outlasts the run; the runner "
        "only checks it is held here (and stops --margin before it expires), and never "
        "acquires, requests, forces or releases one",
        "changes      " + ("nothing on the board (read only)" if not changes
                             else "each iteration:"),
        *changes,
        "never        no card or slot writes, no SD writes, no MCC REBOOT, no fpgahub share, "
        "no lease or SSH-claim change, never --force",
        "end state    greybox (restored in a finally if the runner swapped the partition); the "
        "SSH claim as it was",
        f"evidence     {o.evidence.resolve()}",
        f"stop it      kill -INT {os.getpid()} (or Ctrl-C in its tmux): it finishes the check, "
        "restores greybox, writes REPORT.md",
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
                                            "(docs/HIL_AUTO.md).")
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


def _halt(runner: Runner) -> None:
    if not runner.halt.is_set():
        runner.log("signal: finishing the current check, then restoring and writing the report")
    runner.halt.set()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
