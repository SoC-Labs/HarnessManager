"""Test connection and discovery for a hub (lane SET-HUBS; ``docs/design/SETTINGS.md`` §6.2).

``test_hub`` proves, in order, and stops at the first failure with the step that failed
and what to do next: **config → reach → auth → group → targets → target**. It never takes,
joins or releases a lease, never starts a share, and never shows a token.

- **REST** (T8's ``RestHubClient``): exactly three reads, ``GET /health`` (anonymous: the
  fpgahub version), ``GET /whoami`` (the token's holder and role) and ``GET /groups`` (the
  targets on offer). There is no group step: the token carries a role.
- **SSH** (pyverify's ``SshHubRunner``, so the quoting is the one real use gets, plus
  ``ConnectTimeout=10``, which pyverify's options lack, and ``-J`` for a jump host): ONE
  round trip, ``echo HM-TEST:login; id -Gn; echo HM-TEST:ids; sg fpga -c 'fpgahub board list
  --json'``. The markers say how far it got: no login marker is reach or auth (ssh's own
  message says which), a login without the group is the group, a group without a board
  list is fpgahub. On the hub itself (``host = "local"``) the same line runs locally.
- **target** checks the targets the boards using this hub name (or the ones asked for)
  are on offer.

``target_details`` reads one target's facts for "Add board" (``board_ip``, description):
``GET /targets/{t}`` over REST, ``fpgahub target show T`` over SSH (v0.3.0 prints the same
JSON). Both are reads.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    ExitCode,
    HarnessError,
    UnreachableError,
    UsageError,
)

from .hubs import Hub, boards_using, check_target, hub_credential, load_resolver, resolve_hub
from .resolve import Resolver
from .schema import join_key

STEPS = ("config", "reach", "auth", "group", "targets", "target")
#: The exit code a failed step maps to (the CLI's and the API's ``code``).
STEP_CODES = {"config": ExitCode.USAGE, "reach": ExitCode.UNREACHABLE,
              "auth": ExitCode.UNREACHABLE, "group": ExitCode.UNREACHABLE,
              "targets": ExitCode.UNREACHABLE, "target": ExitCode.ABSENT}
SSH_CONNECT_TIMEOUT_S = 10
SSH_TIMEOUT_S = 20.0
REST_TIMEOUT_S = 5.0
LOGIN_MARK, IDS_MARK = "HM-TEST:login", "HM-TEST:ids"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass
class Step:
    step: str
    ok: bool
    detail: str = ""
    hint: str = ""
    ms: int = 0

    def view(self) -> dict[str, Any]:
        return {"step": self.step, "ok": self.ok, "detail": self.detail, "hint": self.hint,
                "ms": self.ms}


@dataclass
class Report:
    hub: str
    transport: str
    steps: list[Step] = field(default_factory=list)
    targets: list[dict[str, Any]] = field(default_factory=list)
    #: SET-UI: told each step as it starts (``(step, index, of)``), so a job shows progress
    progress: Callable[[str, int, int], None] | None = field(default=None, repr=False,
                                                              compare=False)

    def starting(self, step: str) -> None:
        if self.progress is not None:
            self.progress(step, STEPS.index(step), len(STEPS))

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    @property
    def failed(self) -> str:
        return next((s.step for s in self.steps if not s.ok), "")

    @property
    def code(self) -> int:
        return int(STEP_CODES.get(self.failed, ExitCode.OK)) if self.failed else 0

    def failure(self) -> Step | None:
        return next((s for s in self.steps if not s.ok), None)

    def view(self) -> dict[str, Any]:
        bad = self.failure()
        return {"ok": self.ok, "hub": self.hub, "transport": self.transport,
                "failed": self.failed, "code": self.code,
                "reason": bad.detail if bad else "", "hint": bad.hint if bad else "",
                "steps": [s.view() for s in self.steps], "targets": list(self.targets)}

    def error(self) -> HarnessError:
        """The first failure as the error the CLI exits with."""
        bad = self.failure()
        msg = f"hub {self.hub}: {bad.step}: {bad.detail}" if bad else f"hub {self.hub}: ok"
        hint = bad.hint if bad else ""
        code = STEP_CODES.get(self.failed, ExitCode.UNREACHABLE)
        if code == ExitCode.USAGE:
            return UsageError(msg, hint=hint)
        if code == ExitCode.ABSENT:
            return AbsentError(msg, hint=hint)
        return UnreachableError(msg, hint=hint)


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _run_step(report: Report, step: str, fn: Callable[[], tuple[str, Any]]) -> Any:
    report.starting(step)
    t0 = time.monotonic()
    try:
        detail, value = fn()
    except HarnessError as exc:
        report.steps.append(Step(step, False, exc.message, exc.hint or "", _ms(t0)))
        return None
    report.steps.append(Step(step, True, detail, "", _ms(t0)))
    return value if value is not None else True


def targets_from_groups(groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """fpgahub's ``GET /groups`` (and ``board list --json``) as ``[{board, target, role}]``."""
    return [{"board": g.get("board", ""), "target": m.get("name", ""), "role": m.get("role")}
            for g in groups for m in (g.get("members") or []) if isinstance(m, Mapping)]


def _check_targets(report: Report, wanted: Sequence[str], ms: int = 0) -> None:
    if not wanted:
        return
    offered = {t["target"] for t in report.targets}
    missing = [t for t in wanted if t not in offered]
    if missing:
        report.steps.append(Step(
            "target", False, f"this hub has no target {', '.join(map(repr, missing))}",
            f"it offers {', '.join(sorted(offered)) or 'none'}; set hub.target in "
            "boards.toml to one of them", ms))
    else:
        report.steps.append(Step("target", True, ", ".join(wanted), "", ms))


def wanted_targets(hub: Hub, resolver: Resolver) -> list[str]:
    """The targets of the boards that use this hub (``hub.target``, default mps3_01_pl)."""
    out = []
    for board in boards_using(resolver, hub.name):
        t = resolver.layer.values.get(join_key(("boards", board, "hub", "target")),
                                      "mps3_01_pl")
        if isinstance(t, str) and t not in out:
            out.append(t)
    return out


# --- the entry point ---------------------------------------------------------------------------


def test_hub(hub: Hub | str, *, resolver: Resolver | None = None,
             targets: Sequence[str] | None = None, token: str | None = None,
             run: Runner | None = None, ssh_timeout_s: float = SSH_TIMEOUT_S,
             rest_timeout_s: float = REST_TIMEOUT_S,
             root: Path | str | None = None, env: Mapping[str, str] | None = None,
             progress: Callable[[str, int, int], None] | None = None) -> Report:
    """Test connection for a hub, by name or as a ``Hub`` (an unsaved one: ``token`` then
    stands in for its stored token). ``targets``: the ones to look for (default: those of
    the boards that use the hub). Never raises for a hub that fails: the report says where."""
    r = resolver or load_resolver(root, env)
    if isinstance(hub, str):
        try:
            hub = resolve_hub(hub, r)
        except HarnessError as exc:
            rep = Report(str(hub), "")
            rep.steps.append(Step("config", False, exc.message, exc.hint))
            return rep
    wanted = list(targets) if targets is not None else wanted_targets(hub, r)
    bad = hub.config_problems()
    if hub.transport == "rest":
        return _test_rest(hub, r, wanted, bad, token=token, timeout_s=rest_timeout_s,
                          progress=progress)
    return _test_ssh(hub, wanted, bad, run=run, timeout_s=ssh_timeout_s, progress=progress)


# --- REST ----------------------------------------------------------------------------------------


def rest_config(hub: Hub, *, timeout_s: float | None = None, target: str | None = None,
                root: Path | str | None = None) -> Any:
    """The hub as T8's ``RestHubConfig`` (its token comes from its name: SET-HUB-2)."""
    from harness_manager.transports import hub_rest

    table = hub.table()
    if timeout_s is not None:
        table["timeout_s"] = timeout_s
    cfg = hub_rest.parse_rest_table(table, where=f"hubs.{hub.name}",
                                    target=target or hub_rest.DEFAULT_TARGET)
    assert cfg is not None
    return replace(cfg, hub_name=hub.name, settings_root=str(root) if root else "")


def _test_rest(hub: Hub, resolver: Resolver, wanted: Sequence[str], problems: list[str], *,
               token: str | None, timeout_s: float,
               progress: Callable[[str, int, int], None] | None = None) -> Report:
    from harness_manager.transports.hub_rest import Credential, RestHubClient

    report = Report(hub.name, "rest", progress=progress)
    root = resolver.files.root if resolver.files is not None else None
    state: dict[str, Any] = {}

    def config() -> tuple[str, Any]:
        if problems:
            raise UsageError(problems[0], hint=f"fix hubs.{hub.name} in settings.toml")
        cfg = rest_config(hub, timeout_s=timeout_s, root=root)
        try:
            anon = RestHubClient(cfg, credential=Credential(None, "none"), retry_backoff_s=())
        except (OSError, ValueError) as exc:      # ca_file / cert_file / key_file unreadable
            raise UsageError(f"hubs.{hub.name}: the TLS files cannot be used: {exc}",
                             hint="check ca_file, cert_file and key_file") from None
        state["cfg"], state["anon"] = cfg, anon
        return cfg.url, cfg

    if not _run_step(report, "config", config):
        return report

    def reach() -> tuple[str, Any]:
        h = state["anon"].health()
        return f"fpgahub {h.get('version', '?')} at {state['cfg'].addr}", h

    if not _run_step(report, "reach", reach):
        return report

    def auth() -> tuple[str, Any]:
        cfg = state["cfg"]
        if token is not None:
            cred = Credential(token, "the token given")
        else:
            cred = hub_credential(hub.name, cfg.host, cfg.port, resolver=resolver)
        if not cred.present:
            raise UsageError(f"no token is set for the hub {hub.name}",
                             hint=f"`harness-manager hub token {hub.name} --stdin` stores the "
                                  "token an fpgahub admin made (`fpgahub token create`)")
        client = RestHubClient(cfg, credential=cred, retry_backoff_s=())
        who = client.whoami(fresh=True)
        state["client"] = client
        role = who.get("role") or "?"
        note = "" if role in ("write", "admin") else " (a read token can see a lease, not take it)"
        return f"{who.get('holder')} (role {role}), token from {cred.source}{note}", who

    if not _run_step(report, "auth", auth):
        return report

    def listing() -> tuple[str, Any]:
        found = targets_from_groups(state["client"].groups())
        return (f"{len(found)} targets on {len({t['board'] for t in found})} boards",
                found)

    found = _run_step(report, "targets", listing)
    if found is None:
        return report
    report.targets = list(found) if isinstance(found, list) else []
    _check_targets(report, wanted)
    return report


# --- SSH -----------------------------------------------------------------------------------------


_SSH_FAIL = (
    (re.compile(r"Could not resolve hostname|Name or service not known", re.I), "reach",
     "check the host name, and that you are on the campus network or VPN"),
    (re.compile(r"timed out|No route to host|Network is unreachable|Connection refused|"
                r"Connection closed by|kex_exchange", re.I),
     "reach", "check the host name, and that you are on the campus network or VPN"),
    (re.compile(r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED", re.I),
     "auth", "the hub's host key is not in known_hosts (or it changed): run `ssh {host}` "
             "once in a terminal and check the fingerprint with the hub admin"),
    (re.compile(r"Permission denied \(", re.I), "auth",
     "your SSH key is not accepted: load it into your agent (ssh-add), or ask the hub "
     "admin to add it"),
)


def _remote(argv: Sequence[str], group: str | None) -> str:
    """pyverify's remote command for ``argv`` (its render settings and its ``sg`` quoting)."""
    from pyverify.lease import SshHubRunner

    return SshHubRunner("hub", group=group or None).build(list(argv))[-1]


def ssh_argv(host: str, group: str | None, remote_argv: Sequence[str], *, jump: str = "",
             connect_timeout_s: int = SSH_CONNECT_TIMEOUT_S, markers: bool = False) -> list[str]:
    """``ssh`` with pyverify's options, a connect timeout and the jump, running
    ``remote_argv`` on the hub under ``sg GROUP`` (``markers``: the test's markers first)."""
    from pyverify.lease import SshHubRunner

    base = SshHubRunner(host, group=group or None).build(list(remote_argv))
    remote = base[-1]
    opts = base[1:-2]
    extra = ["-o", f"ConnectTimeout={connect_timeout_s}"] + (["-J", jump] if jump else [])
    if markers:
        remote = f"echo {LOGIN_MARK}; id -Gn; echo {IDS_MARK}; {remote}"
    return ["ssh", *opts, *extra, host, remote]


def local_argv(group: str | None, remote_argv: Sequence[str], *, markers: bool = False) -> list[str]:
    """The same line for a hub this process runs on (``host = "local"``)."""
    remote = _remote(remote_argv, group)
    if markers:
        remote = f"echo {LOGIN_MARK}; id -Gn; echo {IDS_MARK}; {remote}"
    return ["sh", "-c", remote]


def _default_run(timeout_s: float) -> Runner:
    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_s,
                              stdin=subprocess.DEVNULL, check=False)
    return run


def _last(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def _test_ssh(hub: Hub, wanted: Sequence[str], problems: list[str], *, run: Runner | None,
              timeout_s: float, progress: Callable[[str, int, int], None] | None = None,
              ) -> Report:
    report = Report(hub.name, "ssh", progress=progress)
    host = hub.host
    group = hub.group or None
    if problems or not host or any(c.isspace() for c in host) or host.startswith("-"):
        report.steps.append(Step("config", False,
                                 problems[0] if problems else f"{host!r} is not a host name",
                                 f"fix hubs.{hub.name} in settings.toml"))
        return report
    report.steps.append(Step("config", True, f"ssh {host}" + (f" via {hub.jump}"
                                                                 if hub.jump else "")))
    listing = ["fpgahub", "board", "list", "--json"]
    argv = local_argv(group, listing, markers=True) if hub.local else \
        ssh_argv(host, group, listing, jump=hub.jump, markers=True)
    run = run or _default_run(timeout_s)
    report.starting("reach")                       # one round trip: reach, auth, group, targets
    t0 = time.monotonic()
    try:
        res = run(argv)
    except subprocess.TimeoutExpired:
        report.steps.append(Step("reach", False, f"no answer from {host} in {timeout_s:g} s",
                                 "check the host name and your network", _ms(t0)))
        return report
    except FileNotFoundError:
        report.steps.append(Step("reach", False, "ssh is not installed",
                                 "install the OpenSSH client", _ms(t0)))
        return report
    ms = _ms(t0)
    out, err = res.stdout or "", (res.stderr or "").strip()
    if LOGIN_MARK not in out:
        step, hint = "reach", "ssh said: " + (_last(err) or f"nothing (exit {res.returncode})")
        for pat, s, h in _SSH_FAIL:
            if pat.search(err):
                step, hint = s, h.format(host=host)
                break
        if step == "auth":
            report.steps.append(Step("reach", True, f"{host} answered", ms=ms))
        report.steps.append(Step(step, False, _last(err) or f"ssh exited {res.returncode}",
                                 hint, ms))
        return report
    report.steps.append(Step("reach", True, f"{host} answered", ms=ms))
    report.steps.append(Step("auth", True, "on this machine" if hub.local else
                             "logged in with your SSH key", ms=ms))
    ids = out.split(LOGIN_MARK, 1)[1].split(IDS_MARK, 1)[0].split()
    rest = out.split(IDS_MARK, 1)[1] if IDS_MARK in out else ""
    if group and group not in ids:
        report.steps.append(Step("group", False, f"your account on {host} is not in {group!r}",
                                 f"ask the hub admin: `usermod -aG {group} <you>`, then log in "
                                 "again (a new group needs a new login)", ms))
        return report
    if res.returncode != 0:
        if re.search(r"command not found|No such file", err):
            report.steps.append(Step("group", True, f"in {group!r}" if group else
                                     "no sg wrapper", ms=ms))
            report.steps.append(Step("targets", False, f"fpgahub is not installed on {host}",
                                     "is this the hub? (fpgahub runs there)", ms))
        elif re.search(r"Permission denied|Errno 13|Invalid password|failed to crypt", err,
                       re.I):
            report.steps.append(Step("group", False, _last(err),
                                     f"the fpgahub socket needs the group {group!r}: log in "
                                     "again after being added (`usermod -aG`)", ms))
        else:
            report.steps.append(Step("targets", False, _last(err) or
                                     f"exit {res.returncode}", "", ms))
        return report
    report.steps.append(Step("group", True, f"in {group!r}" if group else "no sg wrapper",
                             ms=ms))
    try:
        data = json.loads(rest)
        groups = data.get("groups", []) if isinstance(data, dict) else []
    except ValueError:
        report.steps.append(Step("targets", False, "fpgahub's board list was not JSON",
                                 "the hub needs fpgahub 0.3.0 or newer", ms))
        return report
    report.targets = targets_from_groups(groups)
    report.steps.append(Step("targets", True, f"{len(report.targets)} targets on "
                             f"{len({t['board'] for t in report.targets})} boards", ms=ms))
    _check_targets(report, wanted)
    return report


# --- discovery: one target's facts --------------------------------------------------------------


def target_details(hub: Hub | str, target: str, *, resolver: Resolver | None = None,
                   run: Runner | None = None, timeout_s: float = SSH_TIMEOUT_S,
                   token: str | None = None) -> dict[str, Any]:
    """``GET /targets/{t}`` (REST) or ``fpgahub target show T`` (SSH): the target's
    ``network`` (``board_ip``, ``hostname``) and ``description``. A read, never a lease.
    ``HarnessError`` when the hub will not say; ``UsageError`` for a target that is not a
    target name (it is an argument on the hub: never option-like)."""
    from harness_manager.transports import hub_rest

    check_target(target)
    r = resolver or load_resolver()
    if isinstance(hub, str):
        hub = resolve_hub(hub, r)
    if hub.transport == "rest":
        root = r.files.root if r.files is not None else None
        cfg = rest_config(hub, target=target, root=root)
        cred = hub_rest.Credential(token, "the token given") if token is not None else \
            hub_credential(hub.name, cfg.host, cfg.port, resolver=r)
        client = hub_rest.RestHubClient(cfg, credential=cred, retry_backoff_s=())
        return client.target_info()
    show = ["fpgahub", "target", "show", target]
    argv = local_argv(hub.group or None, show) if hub.local else \
        ssh_argv(hub.host, hub.group or None, show, jump=hub.jump)
    run = run or _default_run(timeout_s)
    try:
        res = run(argv)
    except subprocess.TimeoutExpired:
        raise UnreachableError(f"no answer from {hub.host} in {timeout_s:g} s") from None
    if res.returncode != 0:
        said = _last(res.stderr or "") or _last(res.stdout or "") or f"exit {res.returncode}"
        if re.search(r"no such board|404", said, re.I):
            raise AbsentError(f"the hub {hub.name} has no target {target!r} ({said})",
                              hint=f"`harness-manager hub targets {hub.name}` lists them")
        raise UnreachableError(f"`fpgahub target show {target}` on {hub.host} failed: {said}",
                               hint=f"`harness-manager hub test {hub.name}` shows where it "
                                    "stops")
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        raise UnreachableError(f"`fpgahub target show {target}` on {hub.host} did not print "
                               f"JSON: {_last(res.stderr or res.stdout or '')[:200]}",
                               hint=f"`harness-manager hub test {hub.name}` shows where it "
                                    "stops") from None
    if not isinstance(data, dict):
        raise UnreachableError(f"`fpgahub target show {target}` printed no target")
    return data
