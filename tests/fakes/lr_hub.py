"""fpgahub 0.3.0 for the lease-request lanes (LR-A..D): principals, the queue, revoke,
lease-history, whoami, and Harness Manager's note directory. Nothing here reaches a hub.

``LrFakeHub`` extends L1's ``FakeHub`` (shares, ``add_tty``, ``routes``, ``close`` are its)
and replaces the lease model with the one fpgahub 0.3.0 actually has. Every text below is
what the v0.3.0 source prints (tag v0.3.0, 22aa362; ``/home/dam1n19/SoCLabs/fpgahub-v030-readonly``):

- **Leases belong to the caller.** A lease or queue entry is recorded under the caller's
  principal, ``name@<hub hostname>`` (api/principal.py ``Principal.holder``); ``--holder`` is
  ignored (kept in ``ignored_holders``). Each caller is a runner: ``hub`` itself is the
  default user, ``hub.as_user("alice")`` is alice (admin, as on the hub's unix socket;
  ``role="write"`` models a token caller, who may not revoke).
- ``whoami [--json]`` (cli.py ``whoami``): ``json.dumps(GET /whoami, indent=2)``.
- ``lease acquire``: free -> granted; the holder again -> refreshed with the SAME token;
  anyone else -> queued (FCFS, idempotent per holder), ``queued position=N queue=interactive``.
- ``lease release``/``heartbeat``: 403 ``holder or token does not match current lease`` /
  ``no current lease for board`` (lease.py); a release or revoke promotes the queue head
  (fresh token, ``lease.promoted``); the promoted waiter's next acquire returns that token.
- ``lease show`` (cli.py ``lease_show``): ``held by H (user U, expires E)`` or ``not leased``,
  then rich's ``Queue`` table (HEAVY_HEAD box; ``ascii_box=True`` draws rich's ASCII fallback).
- ``lease cancel``: ``cancelled board=T`` / ``no matching wait to cancel board=T``. An admin
  (every unix-socket caller) may name anyone's holder; a write-role caller cancels its own.
- ``board list --json`` / ``board lease show B --json`` (``GET /groups``, ``GET /boards/B/lease``).
- ``board lease revoke B --reason R --yes`` (``_do_revoke``): admin only; kicks every member
  of B, emits ``lease.revoked`` (with ``reason``), ``lease.promoted``, ``lease.released`` and
  ``lease.admin_revoked`` (``by``, ``reason``, ``prior_holder``, ``chassis``, NO ``board``) into
  ``audit`` (the JSONL audit log), prints ``revoked T (by unix:alice)`` or ``no lease to revoke``.
  Without ``--yes`` it aborts, as click.confirm does with no terminal.
- ``target lease-history T --limit N --json``: the audit records whose ``board`` is T,
  projected onto LeaseEventRecord's ten fields (so no ``reason``, no ``by``, and never the
  ``admin_revoked`` record), printed as rich ``print_json`` does.
- ``sh -c NOTE_SCRIPT hm-lease OP ROOT TARGET GROUP ...``: the note directory, modelled in
  Python op for op on ``harness_manager_mps3.hub.NOTE_SCRIPT`` (exit codes and messages
  included; tests/unit/test_lra_notes.py runs the real script beside it). Flags:
  ``notes_unwritable``, ``notes_unreadable``, ``notes_symlink``; ``age_notes(s)`` makes
  every note ``s`` seconds older.

One-shot canned replies for any verb: ``hub.override(("lease", "show"), stdout="garbage")``.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from pyverify.lease import RunResult

from harness_manager_mps3 import hub as hubmod
from tests.fakes.l1_fake_hub import FakeHub, FakeShareServer

HUB_HOSTNAME = "mapstone-dev"
BOARD = "mps3_01"
TARGET = "mps3_01_pl"
#: LeaseEventRecord's fields, in order (api/schemas.py): all that lease-history keeps.
EVENT_FIELDS = ("ts", "event", "board", "holder", "user", "position", "ttl_s", "expires_at",
                "source", "error")
CLICK_NO_SUCH = ("Usage: fpgahub [OPTIONS] COMMAND [ARGS]...\nTry 'fpgahub --help' for help.\n\n"
                 "Error: No such command '{}'.\n")


def iso_z(dt: datetime) -> str:
    """How pydantic's JSON mode prints a UTC datetime: ``2026-09-24T12:00:00Z``."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Principal:
    name: str
    hostname: str = HUB_HOSTNAME
    role: str = "admin"
    kind: str = "unix"

    @property
    def holder(self) -> str:
        return f"{self.name}@{self.hostname}"

    @property
    def audit_id(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass
class _Note:
    body: str
    mtime: float


@dataclass
class _Override:
    prefix: tuple[str, ...]
    result: RunResult
    times: int = 1


class Caller:
    """A runner bound to one principal (``LrFakeHub.as_user``)."""

    def __init__(self, hub: LrFakeHub, principal: Principal) -> None:
        self.hub = hub
        self.principal = principal

    def __call__(self, argv: Sequence[str], timeout: float | None = None) -> RunResult:
        return self.hub.run_as(self.principal, argv)


def _heavy_table(title: str, head: Sequence[str], rows: list[Sequence[str]], *, ascii_box: bool) -> str:
    widths = [max(len(head[i]), *(len(r[i]) for r in rows)) for i in range(len(head))]

    def line(cells: Sequence[str], sep: str) -> str:
        return sep + sep.join(f" {c.ljust(w)} " for c, w in zip(cells, widths, strict=True)) + sep

    if ascii_box:                                   # rich box.ASCII (the safe_box fallback)
        top = "+" + "-".join("-" * (w + 2) for w in widths) + "+"
        mid = "|" + "+".join("-" * (w + 2) for w in widths) + "|"
        out = [title.center(len(top)), top, line(head, "|"), mid, *(line(r, "|") for r in rows), top]
    else:                                           # rich box.HEAVY_HEAD (Table's default)
        top = "┏" + "┳".join("━" * (w + 2) for w in widths) + "┓"
        mid = "┡" + "╇".join("━" * (w + 2) for w in widths) + "┩"
        bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"
        out = [title.center(len(top)), top, line(head, "┃"), mid, *(line(r, "│") for r in rows), bot]
    return "\n".join(out) + "\n"


class LrFakeHub(FakeHub):
    def __init__(self, target: str = TARGET, *, user: str = "dam1n19", hostname: str = HUB_HOSTNAME,
                 board: str = BOARD, members: Sequence[str] | None = None, ascii_box: bool = False,
                 extra_groups: Sequence[tuple[str, Sequence[str]]] = (("kr260_01", ("kr260_01_ps",)),),
                 clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(target, user=user)
        self.hostname = hostname
        self.board = board
        self.members = list(members or [target])
        if target not in self.members:
            self.members.insert(0, target)
        self.extra_groups = [(b, list(m)) for b, m in extra_groups]
        self.ascii_box = ascii_box
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.default = Principal(user)
        #: other members' leases (the target's is ``current``): member -> lease dict or None
        self.others: dict[str, dict[str, Any] | None] = {m: None for m in self.members if m != target}
        #: queue entries: {"holder", "user", "ttl"}; FakeHub's ``queue`` stays a list of holders
        self.waiters: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []          # every lease.* record, as the JSONL log has it
        self.revocations: list[dict[str, Any]] = []
        self.ignored_holders: list[str] = []
        self.calls_by: list[tuple[str, list[str]]] = []
        self.overrides: list[_Override] = []
        self._last_ts: datetime | None = None
        # the note directory (/tmp/harness-manager-lease/<target>/ on the hub)
        self.note_dirs: dict[str, dict[str, _Note]] = {}
        self.note_ops: list[list[str]] = []
        self.notes_unwritable = False
        self.notes_unreadable = False
        self.notes_symlink = False
        self.note_clock: Callable[[], float] = time.time
        self._mu = threading.RLock()                 # one hub: callers on several threads take turns

    # -- helpers for tests -------------------------------------------------------------------

    def as_user(self, name: str, *, role: str = "admin", kind: str = "unix") -> Caller:
        return Caller(self, Principal(name, self.hostname, role, kind))

    def holder_of(self, name: str) -> str:
        return f"{name}@{self.hostname}"

    def override(self, prefix: Sequence[str], *, stdout: str = "", stderr: str = "", rc: int = 0,
                 times: int = 1) -> None:
        """The next ``times`` calls whose argv starts with ``fpgahub *prefix`` (or ``sh -c …
        hm-lease OP`` for ``("note", OP)``) get this reply instead."""
        self.overrides.append(_Override(tuple(prefix), RunResult(rc, stdout, stderr), times))

    def grant(self, name: str, *, ttl: int = 3600) -> str:
        """Put ``name`` in as the holder (as an acquire would); returns the token."""
        who = Principal(name, self.hostname)
        lease = self._grant(who, ttl)
        self._emit("lease.acquired", board=self.target, holder=who.holder, user=who.name,
                   tier="interactive")
        return lease["token"]

    def expire(self) -> None:
        """The lease's TTL ran out (``expire_stale``): lease.expired, then the head is promoted."""
        if self.current is not None:
            c, self.current = self.current, None
            self._emit("lease.expired", board=self.target, holder=c["holder"], user=c["user"])
            self._promote()

    def steal(self, holder: str = "someone-else") -> None:
        self.current = {"holder": holder, "user": holder.split("@")[0], "token": "tok-other-0001",
                        "expires_at": self._expires(3600), "ttl": 3600, "tier": "interactive"}

    def notes(self, root: str = hubmod.NOTE_ROOT, target: str | None = None) -> dict[str, str]:
        """The note directory's files: name -> content."""
        d = self.note_dirs.get(f"{root}/{target or self.target}", {})
        return {n: v.body for n, v in d.items()}

    def plant_note(self, name: str, body: str, *, root: str = hubmod.NOTE_ROOT, age_s: float = 0.0,
                   target: str | None = None) -> None:
        """A file somebody else left (no validation: it may be junk or hostile)."""
        self.note_dirs.setdefault(f"{root}/{target or self.target}", {})[name] = _Note(
            body, self.note_clock() - age_s)

    def age_notes(self, seconds: float) -> None:
        for d in self.note_dirs.values():
            for n in d.values():
                n.mtime -= seconds

    # -- dispatch ------------------------------------------------------------------------------

    def __call__(self, argv: Sequence[str], timeout: float | None = None) -> RunResult:
        return self.run_as(self.default, argv)

    def run_as(self, who: Principal, argv: Sequence[str]) -> RunResult:
        with self._mu:
            return self._run_as(who, list(argv))

    def _run_as(self, who: Principal, argv: list[str]) -> RunResult:
        self.calls.append(argv)
        self.calls_by.append((who.holder, argv))
        if self.fail_with:
            text, self.fail_with = self.fail_with, ""
            return RunResult(1, "", text)
        canned = self._take_override(argv)
        if canned is not None:
            return canned
        if argv[:2] == ["sh", "-c"]:
            return self._note_op(argv)
        if argv[:1] != ["fpgahub"] or len(argv) < 2:
            return RunResult(127, "", f"sh: {argv[0] if argv else ''}: command not found\n")
        cmd = argv[1:]
        if cmd[0] == "whoami":
            return self._whoami(who, "--json" in cmd)
        if cmd[0] == "lease" and len(cmd) >= 3:
            name = cmd[2]
            if name != self.target:
                return self._no_board("GET", f"/targets/{name}/lease", name)
            verb = cmd[1]
            opts = self._opts(cmd[3:])
            handler = getattr(self, f"_lr_lease_{verb}", None)
            if handler is None:
                return RunResult(2, "", CLICK_NO_SUCH.format(verb))
            return handler(who, opts)
        if cmd[0] == "board":
            return self._board(who, cmd[1:])
        if cmd[0] == "target" and len(cmd) >= 3 and cmd[1] == "lease-history":
            return self._history(cmd[2], self._opts(cmd[3:]), "--json" in cmd)
        if cmd[0] == "share" and len(cmd) >= 3:
            if cmd[2] != self.target:
                return self._no_board("GET", f"/targets/{cmd[2]}/tty", cmd[2])
            handler = getattr(self, f"_share_{cmd[1]}", None)
            if handler is None:
                return RunResult(2, "", CLICK_NO_SUCH.format(cmd[1]))
            return handler(argv, cmd[3:], self._opts(cmd[3:]))
        return RunResult(2, "", CLICK_NO_SUCH.format(cmd[0]))

    def _take_override(self, argv: list[str]) -> RunResult | None:
        key = argv[1:] if argv[:1] == ["fpgahub"] else (
            ["note", argv[4]] if argv[:2] == ["sh", "-c"] and len(argv) > 4 else [])
        for o in self.overrides:
            if tuple(key[:len(o.prefix)]) == o.prefix and o.times > 0:
                o.times -= 1
                if o.times == 0:
                    self.overrides.remove(o)
                return o.result
        return None

    @staticmethod
    def _opts(words: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        i = 0
        while i < len(words):
            w = words[i]
            if w.startswith("--"):
                if i + 1 < len(words) and not words[i + 1].startswith("--"):
                    out[w[2:]] = words[i + 1]
                    i += 2
                    continue
                out[w[2:]] = ""
            i += 1
        return out

    def _no_board(self, method: str, path: str, name: str) -> RunResult:
        configured = ", ".join([*self.members, *(m for _, ms in self.extra_groups for m in ms)])
        return RunResult(1, "", f"{method} {path} → HTTP 404: no such board: {name!r}; "
                                f"configured: {configured}\n")

    # -- the audit log -------------------------------------------------------------------------

    def _now(self) -> datetime:
        """The audit log's next time: never equal to the last one, and never ahead of the
        clock. A coarse clock (Windows: ~16 ms) is waited out, so a note a client stamps
        with the same clock can never land before an event that happened before it. A
        clock that does not move (a test's) gets 1 µs steps instead."""
        now = self.clock()
        if self._last_ts is not None and now <= self._last_ts:
            deadline = time.monotonic() + 0.1
            while now <= self._last_ts and time.monotonic() < deadline:
                time.sleep(0.001)
                now = self.clock()
            if now <= self._last_ts:
                now = self._last_ts + timedelta(microseconds=1)
        self._last_ts = now
        return now

    def _emit(self, event: str, **data: Any) -> None:
        self.audit.append({"ts": self._now().isoformat(), "event": event,
                           **{k: v for k, v in data.items() if k != "token"}})

    def _expires(self, ttl: int) -> str:
        return iso_z(self.clock() + timedelta(seconds=ttl))

    # -- leases (0.3.0 semantics) ----------------------------------------------------------------

    def _grant(self, who: Principal, ttl: int, token: str | None = None) -> dict[str, Any]:
        self._tokens += 1
        self.current = {"holder": who.holder, "user": who.name,
                        "token": token or f"tok-{self._tokens:04d}-{secrets.token_hex(6)}",
                        "expires_at": self._expires(ttl), "ttl": ttl, "tier": "interactive"}
        return self.current

    def _promote(self) -> None:
        if self.current is not None or not self.waiters:
            return
        w = self.waiters.pop(0)
        self.queue = [x["holder"] for x in self.waiters]
        lease = self._grant(Principal(w["user"], self.hostname), w["ttl"])
        lease["holder"] = w["holder"]
        self._emit("lease.promoted", board=self.target, holder=w["holder"], user=w["user"],
                   tier="interactive", expires_at=lease["expires_at"])

    def _lr_lease_acquire(self, who: Principal, o: dict[str, str]) -> RunResult:
        if o.get("holder") and o["holder"] != who.holder:
            self.ignored_holders.append(o["holder"])
        ttl = int(o.get("ttl") or 3600)
        c = self.current
        if c is None or c["holder"] == who.holder:
            lease = self._grant(who, ttl, token=c["token"] if c else None)
            self._emit("lease.acquired", board=self.target, holder=who.holder, user=who.name,
                       tier="interactive")
            return RunResult(0, f"granted token={lease['token']} expires={lease['expires_at']} "
                                f"tier=interactive\n", "")
        pos = next((i + 1 for i, w in enumerate(self.waiters) if w["holder"] == who.holder), 0)
        if not pos:
            self.waiters.append({"holder": who.holder, "user": who.name, "ttl": ttl})
            pos = len(self.waiters)
        self.queue = [x["holder"] for x in self.waiters]
        self._emit("lease.queued", board=self.target, holder=who.holder, user=who.name,
                   position=pos, tier="interactive")
        return RunResult(0, f"queued position={pos} queue=interactive\n", "")

    def _lr_lease_release(self, who: Principal, o: dict[str, str]) -> RunResult:
        c = self.current
        if c is None:
            return RunResult(0, "no lease to release\n", "")
        if c["holder"] != who.holder or c["token"] != o.get("token"):
            return RunResult(1, "", f"DELETE /targets/{self.target}/lease → HTTP 403: "
                                    "holder or token does not match current lease\n")
        self.current = None
        self._emit("lease.released", board=self.target, holder=who.holder, actor=who.audit_id)
        self._promote()
        return RunResult(0, "released\n", "")

    def _lr_lease_heartbeat(self, who: Principal, o: dict[str, str]) -> RunResult:
        c = self.current
        path = f"POST /targets/{self.target}/lease/heartbeat → HTTP 403: "
        if c is None:
            return RunResult(1, "", path + "no current lease for board\n")
        if c["holder"] != who.holder or c["token"] != o.get("token"):
            return RunResult(1, "", path + "holder or token does not match current lease\n")
        self.heartbeats += 1
        c["expires_at"] = self._expires(int(o.get("ttl") or c["ttl"]))
        self._emit("lease.heartbeat", board=self.target, holder=who.holder, user=who.name)
        return RunResult(0, f"extended expires={c['expires_at']}\n", "")

    def _lr_lease_cancel(self, who: Principal, o: dict[str, str]) -> RunResult:
        wanted = o.get("holder") or ""
        holder = wanted if (wanted and who.role == "admin") else who.holder
        before = len(self.waiters)
        self.waiters = [w for w in self.waiters if w["holder"] != holder]
        self.queue = [x["holder"] for x in self.waiters]
        if len(self.waiters) < before:
            self._emit("lease.queue.cancelled", board=self.target, holder=holder, actor=who.audit_id)
            return RunResult(0, f"cancelled board={self.target}\n", "")
        return RunResult(0, f"no matching wait to cancel board={self.target}\n", "")

    def _lr_lease_show(self, who: Principal, _o: dict[str, str]) -> RunResult:
        c = self.current
        out = (f"held by {c['holder']} (user {c['user']}, expires {c['expires_at']})\n" if c
               else "not leased\n")
        if self.waiters:
            rows = [(str(i + 1), w["holder"], w["user"]) for i, w in enumerate(self.waiters)]
            out += _heavy_table("Queue", ("Pos", "Holder", "User"), rows, ascii_box=self.ascii_box)
        return RunResult(0, out, "")

    # -- whoami / board ----------------------------------------------------------------------------

    def _whoami(self, who: Principal, as_json: bool) -> RunResult:
        data = {"kind": who.kind, "name": who.name, "role": who.role, "host": who.hostname,
                "holder": who.holder, "audit_id": who.audit_id, "token_name": None, "groups": [],
                "limits": {"max_ttl_s": None, "max_boards": None, "tier": None, "rule": None}}
        if as_json:
            return RunResult(0, json.dumps(data, indent=2) + "\n", "")
        return RunResult(0, "".join(f"{k:<8}{data[k]}\n" for k in ("name", "kind", "role", "holder")), "")

    def _groups(self) -> list[tuple[str, list[str]]]:
        return [(self.board, self.members), *self.extra_groups]

    def _lease_of(self, member: str) -> dict[str, Any] | None:
        return self.current if member == self.target else self.others.get(member)

    def _board(self, who: Principal, cmd: list[str]) -> RunResult:
        if cmd[:1] == ["list"]:
            groups = []
            for board, members in self._groups():
                roles = [next((r for s, r in (("_ps", "ps"), ("_pl", "pl"), ("_mcc", "mcc"))
                               if m.endswith(s)), None) for m in members]
                groups.append({"board": board, "size": len(members), "is_paired": len(members) > 1,
                               "members": [{"name": m, "role": r} for m, r in zip(members, roles, strict=True)]})
            if "--json" in cmd:
                return RunResult(0, json.dumps({"groups": groups}, indent=2) + "\n", "")
            return RunResult(0, "".join(f"{g['board']}\n" for g in groups), "")
        if cmd[:2] == ["lease", "show"] and len(cmd) >= 3:
            board = cmd[2]
            if board != self.board:
                return self._no_board("GET", f"/boards/{board}/lease", board)
            per = [{"board": m, "current": None if (c := self._lease_of(m)) is None else {
                "board": m, "holder": c["holder"], "user": c["user"], "expires_at": c["expires_at"],
                "tier": c.get("tier", "interactive")}} for m in self.members]
            holders = {p["current"]["holder"] for p in per if p["current"]}
            state = ("free" if not holders else
                     "held" if len(holders) == 1 and all(p["current"] for p in per) else "partial")
            # A single-member board's chassis queue IS the target's queue (lease.py: one store).
            queue = [{"board": board, "holder": w["holder"], "user": w["user"], "position": i + 1,
                      "tier": "interactive"} for i, w in enumerate(self.waiters)] \
                if len(self.members) == 1 else []
            data = {"board": board, "state": state, "description": None, "capabilities": [],
                    "tags": [], "members": per, "queue": queue, "queue_length": len(queue),
                    "background_queue": [], "background_queue_length": 0,
                    "current_tier": "interactive" if holders else None}
            if "--json" in cmd:
                return RunResult(0, json.dumps(data, indent=2) + "\n", "")
            return RunResult(0, f"{state}  board={board}\n", "")
        if cmd[:2] == ["lease", "revoke"] and len(cmd) >= 3:
            return self._revoke(who, cmd[2], self._opts(cmd[3:]), "--yes" in cmd)
        return RunResult(2, "", CLICK_NO_SUCH.format(" ".join(cmd[:2])))

    def _revoke(self, who: Principal, board: str, o: dict[str, str], yes: bool) -> RunResult:
        prompt = f"Force-release board {board!r}, kicking the current holder? [y/N]: "
        if not yes:                                   # click.confirm with no terminal: EOF
            return RunResult(1, prompt, "Aborted!\n")
        if board != self.board:
            return self._no_board("POST", f"/boards/{board}/lease/revoke", board)
        if who.role != "admin":
            return RunResult(1, "", f"POST /boards/{board}/lease/revoke → HTTP 403: "
                                    f"role={who.role!r}; admin required to revoke the lease on {board!r}\n")
        full = (o.get("reason") or "admin force-release") + f" (by {who.audit_id})"
        kicked: list[tuple[str, str, str]] = []
        for m in self.members:
            c = self._lease_of(m)
            if c is None:
                continue
            self._emit("lease.revoked", board=m, holder=c["holder"], user=c["user"], reason=full,
                       tier=c.get("tier", "interactive"))
            if m == self.target:
                self.current = None
                self._promote()
            else:
                self.others[m] = None
            kicked.append((m, c["holder"], c["user"]))
        for m, prior, _user in kicked:
            self._emit("lease.released", board=m, holder=prior, chassis=board)
        if kicked:
            self._emit("lease.admin_revoked", by=who.audit_id, reason=full,
                       members=[m for m, _, _ in kicked], prior_holder=kicked[0][1],
                       prior_user=kicked[0][2], chassis=board)
            self.revocations.append({"board": board, "by": who.audit_id, "reason": full,
                                     "kicked": kicked, "reason_arg": o.get("reason")})
            return RunResult(0, f"revoked {', '.join(m for m, _, _ in kicked)} (by {who.audit_id})\n", "")
        return RunResult(0, "no lease to revoke\n", "")

    def _history(self, name: str, o: dict[str, str], as_json: bool) -> RunResult:
        if name != self.target and name not in self.members:
            return self._no_board("GET", f"/targets/{name}/lease/history", name)
        limit = int(o.get("limit") or 50)
        records = [r for r in self.audit if r.get("board") == name][-limit:] if limit > 0 else []
        events = [{k: r.get(k) for k in EVENT_FIELDS} for r in records]
        if as_json:        # rich print_json: json.dumps(indent=2, ensure_ascii=False)
            return RunResult(0, json.dumps({"board": name, "events": events}, indent=2,
                                           ensure_ascii=False) + "\n", "")
        if not events:
            return RunResult(0, f"no lease events for {name} in the tail buffer\n", "")
        return RunResult(0, "".join(f"{e['ts']} {e['event']} {e['holder'] or '-'}\n" for e in events), "")

    # -- the note directory: hub.NOTE_SCRIPT, op for op ---------------------------------------------

    @staticmethod
    def _fail(code: int, message: str) -> RunResult:
        return RunResult(code, "", f"hm-lease: {message}\n")

    @staticmethod
    def _note_name_ok(name: str) -> bool:
        return (any(name.startswith(p) and len(name) > len(p) + len(".json") for p in ("req-", "ans-", "rev-"))
                and name.endswith(".json") and all(c.isalnum() and c.isascii() or c in "_.-" for c in name))

    @staticmethod
    def _emit_body(body: str) -> str:
        return "".join(c for c in body.encode("utf-8", "replace")[:4097].decode("latin-1")
                       if " " <= c <= "~")

    def _prune(self, d: dict[str, _Note]) -> None:
        now = self.note_clock()
        for name in [n for n, v in d.items() if now - v.mtime > hubmod.NOTE_MAX_AGE_S
                     and (n.startswith(("req-", "ans-", "rev-", ".tmp.")))]:
            del d[name]

    def _note_op(self, argv: list[str]) -> RunResult:
        """One op of ``hub.NOTE_SCRIPT``, check for check in the script's order."""
        if len(argv) < 8 or argv[2] != hubmod.NOTE_SCRIPT or argv[3] != "hm-lease":
            return RunResult(2, "", "fake hub: not harness_manager_mps3.hub.NOTE_SCRIPT\n")
        self.note_ops.append(argv[4:])
        op, root, target, _grp, args = argv[4], argv[5], argv[6], argv[7], argv[8:]
        path = f"{root}/{target}"
        d = self.note_dirs.get(path)
        symlink = self._fail(68, f"{path} is a symbolic link; refusing to use it")
        if op in ("put", "get", "del") and not (args and self._note_name_ok(args[0])):
            return self._fail(64, f"not a note name: {args[0] if args else ''}")
        if op == "put":                         # checkname, size, ensure, prune, mktemp + mv
            name, body = args[0], args[1] if len(args) > 1 else ""
            n = len(body.encode("utf-8"))
            if n > hubmod.NOTE_MAX_BYTES:
                return self._fail(65, f"a note is at most 4096 bytes; this one is {n}")
            if self.notes_symlink:
                return symlink
            if self.notes_unwritable:
                return self._fail(66, f"{path} is not writable")
            d = self.note_dirs.setdefault(path, {})
            self._prune(d)
            d[name] = _Note(body, self.note_clock())
            return RunResult(0, "", "")
        if op in ("list", "get"):               # (kind), readable, prune, emit
            prefix = args[0] if args else ""
            if op == "list" and prefix not in ("req-", "ans-", "rev-"):
                return self._fail(64, f"not a note kind: {prefix}")
            if self.notes_symlink:
                return symlink
            if d is None:
                return RunResult(0, "", "")
            if self.notes_unreadable:
                return self._fail(67, f"{path} is not readable")
            if op == "get":
                note = d.get(args[0])
                return RunResult(0, self._emit_body(note.body) if note else "", "")
            self._prune(d)
            lines = [f"{name}\t{self._emit_body(v.body)}\n" for name, v in sorted(d.items())
                     if name.startswith(prefix) and name.endswith(".json")
                     and all(c.isalnum() and c.isascii() or c in "_.-" for c in name)]
            return RunResult(0, "".join(lines), "")
        if op == "del":                         # checkname, plain, missing is fine, rm
            if self.notes_symlink:
                return symlink
            if d is None or args[0] not in d:
                return RunResult(0, "", "")
            if self.notes_unwritable:
                return self._fail(66, f"cannot delete {path}/{args[0]}")
            del d[args[0]]
            return RunResult(0, "", "")
        return self._fail(64, f"unknown op: {op}")

@dataclass
class TwoSessions:
    """The usual cast: ``holder`` (alice) holds the board, ``requester`` (bob) wants it.
    Each is a real ``HubClient`` over the one fake hub, as its own principal."""

    hub: LrFakeHub
    holder: hubmod.HubClient
    requester: hubmod.HubClient
    tokens: dict[str, str] = field(default_factory=dict)


def two_sessions(hub: LrFakeHub | None = None, *, host: str = "mapstone-dev.ecs.soton.ac.uk") -> TwoSessions:
    hub = hub or LrFakeHub()
    alice = hubmod.HubClient(host, hub.target, runner=hub.as_user("alice"))
    bob = hubmod.HubClient(host, hub.target, runner=hub.as_user("bob"))
    return TwoSessions(hub, alice, bob, {"alice": hub.grant("alice")})


class LaggingShareServer(FakeShareServer):
    """``FakeShareServer`` whose hub notices a client's EOF ``leave_after_s`` late, as the
    real hub does through the ssh forward: until then the gone client is still counted
    (``share list`` readers) and still holds the write slot (first writer wins), so a
    client that connects meanwhile has its writes dropped (``dropped_writes``)."""

    def __init__(self, port_like: Any, tty: str, *, leave_after_s: float = 0.5) -> None:
        self.leave_after_s = leave_after_s
        super().__init__(port_like, tty)

    def _client(self, c: Any) -> None:                     # FakeShareServer._client, lagged
        try:
            while not self._stop.is_set():
                data = c.recv(4096)
                if not data:
                    self._stop.wait(self.leave_after_s)   # the EOF crosses the forward
                    break
                with self._cmu:
                    is_writer = bool(self._clients) and self._clients[0] is c
                if not is_writer:
                    self.dropped_writes += len(data)
                    continue
                with self._lock:
                    self.written += data
                    self._port.write(data)
        except OSError:
            pass
        finally:
            with self._cmu:
                if c in self._clients:
                    self._clients.remove(c)
            c.close()
