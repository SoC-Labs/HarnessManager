"""``harness-manager config``: read and change Harness Manager's settings (lane SET-API).

Verbs::

    harness-manager config list [SECTION] [--all]     every setting, its value and where it came from
    harness-manager config get KEY                    one setting: value, source, lock, env shadowing
                                                      (the service's view AND this shell's)
    harness-manager config set KEY VALUE [KEY VALUE ...]   all or nothing
    harness-manager config unset KEY                  back to the next layer (the admin's, the default)
    harness-manager config set-secret KEY             the secret from stdin (a prompt in a terminal);
                                                      never from the command line
    harness-manager config unset-secret KEY           remove it from the store (clear-secret: the same)
    harness-manager config path                       every file in use, and where a new secret goes
    harness-manager config test SECTION [NAME]        prove a section works (the hub tester: SET-HUBS)

**One writer.** When the service runs, every verb goes through its API (``/settings``,
docs/API.md "Settings"), so the web UI's open tabs hear ``settings.changed``; otherwise the
verb reads and writes the files itself (``harness_manager.settings.ops``, the same code the
API runs). ``--json`` prints the API's own shapes either way.

**The service's environment and this shell's can differ** (the service keeps the one it was
started with). ``config get`` prints both when they give different answers, and says which
variable shadows your own value.

**Values** are typed as the command line gives them: ``90m``, ``true``, ``0x40``, ``a,b``
(a list is ONE argument). A secret never comes back: ``config`` shows whether it is set,
where, and whether this process can reach it.

Exit codes: 2 a bad key or value (nothing written); 15 a key the admin policy locks (the
file is named); 12 a section with no tester yet; 6 a test that ran and failed.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from typing import Any
from urllib.parse import quote, urlencode

from harness_manager.core.errors import ExitCode, UnavailableError, UsageError
from harness_manager.settings import ops

from .context import Ctx
from .output import TSV_COLUMNS, Result

#: How long ``config test`` waits for a test the service runs as a job.
TEST_WAIT_S = 120.0
_APPLY_NOTE = {"reopen": "takes effect the next time the board opens",
               "restart": "takes effect when the service restarts "
                          "(`harness-manager daemon stop`, then start it again)"}


def _parents() -> argparse.ArgumentParser:
    fmt = argparse.ArgumentParser(add_help=False)
    g = fmt.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="one JSON object on stdout (the API's shape)")
    g.add_argument("--tsv", action="store_true", default=argparse.SUPPRESS,
                   help="tab-separated rows, append-only columns")
    return fmt


def register(subparsers: Any) -> argparse.ArgumentParser:
    """Add ``config`` and its actions to the top-level subparsers. Returns the parser."""
    fmt = _parents()
    vp = subparsers.add_parser(
        "config", help="Harness Manager's settings: list, get, set, secrets, test",
        description="Harness Manager's settings (the Settings menu's, from the command line). "
                    "Through the service when it runs, so its open windows hear the change.",
        parents=[fmt])
    sub = vp.add_subparsers(dest="config_cmd", required=True, metavar="ACTION")

    def epilog(layout: str) -> str:
        return f"--tsv columns: {' '.join(TSV_COLUMNS[layout])}"

    ap = sub.add_parser("list", help="every setting, its value and where it came from",
                        parents=[fmt], epilog=epilog("config list"))
    ap.add_argument("section", nargs="?", default=None, metavar="SECTION",
                    help="one section: general, hubs, boards, tools, updates, harness-kits, "
                         "debug, consoles, advanced")
    ap.add_argument("--all", dest="include_dev", action="store_true",
                    help="the developer and test seams too (environment only)")

    ap = sub.add_parser("get", help="one setting: value, source, lock and env shadowing",
                        parents=[fmt], epilog=epilog("config get"))
    ap.add_argument("key", metavar="KEY", help="e.g. tools.openocd, hubs.lab.host")

    ap = sub.add_parser("set", help="change settings: KEY VALUE [KEY VALUE ...], all or nothing",
                        parents=[fmt], epilog=epilog("config set|unset"))
    ap.add_argument("pairs", nargs="+", metavar="KEY VALUE",
                    help="a key and its value, repeated; a list is one argument (a,b)")

    ap = sub.add_parser("unset", help="remove your value: back to the admin's or the default",
                        parents=[fmt], epilog=epilog("config set|unset"))
    ap.add_argument("key", metavar="KEY")

    for name, help_ in (("set-secret", "store a secret, read from stdin (never the command "
                                       "line)"),
                        ("unset-secret", "remove a secret from the store"),
                        ("clear-secret", "the same as unset-secret")):
        ap = sub.add_parser(name, help=help_, parents=[fmt],
                            epilog=epilog("config set-secret|unset-secret"))
        ap.add_argument("key", metavar="KEY", help="e.g. hubs.lab.token, updates.github_token")
        if name == "set-secret":
            # Caught so it can be refused WITHOUT repeating it (argparse would echo it).
            ap.add_argument("argv_value", nargs="*", help=argparse.SUPPRESS)

    sub.add_parser("path", help="every file the settings use, and where a new secret goes",
                   parents=[fmt], epilog=epilog("config path"))

    ap = sub.add_parser("test", help="prove a section's settings work (changes nothing)",
                        parents=[fmt], epilog=epilog("config test"))
    ap.add_argument("section", metavar="SECTION", help="e.g. hubs")
    ap.add_argument("name", nargs="?", default="", metavar="NAME", help="e.g. a hub's name")

    vp.set_defaults(fn=cmd_config)
    return vp


# --- where the settings are: the service, or the files ---------------------------------------


class _Local:
    """No service: this process reads and writes the files (``settings.ops``)."""

    remote = False

    def __init__(self, sctx: ops.SettingsContext) -> None:
        self.ctx = sctx

    def listing(self, **kw: Any) -> dict[str, Any]:
        return ops.listing(self.ctx, **kw)

    def set(self, changes: dict[str, str]) -> dict[str, Any]:
        return ops.set_values(self.ctx, changes)

    def unset(self, key: str) -> dict[str, Any]:
        return ops.unset(self.ctx, key)

    def set_secret(self, key: str, value: str) -> dict[str, Any]:
        return ops.set_secret(self.ctx, key, value)

    def delete_secret(self, key: str) -> dict[str, Any]:
        return ops.delete_secret(self.ctx, key)

    def test(self, section: str, name: str) -> dict[str, Any]:
        return ops.test(self.ctx, section, name)


class _Remote:
    """The running service's API (``/settings``)."""

    remote = True

    def __init__(self, http: Any) -> None:
        self.http = http

    @staticmethod
    def _body(reply: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in reply.items() if k != "ok"}

    def listing(self, *, section: str | None = None, key: str | None = None,
                include_dev: bool = False) -> dict[str, Any]:
        query = {k: v for k, v in (("section", section or ""), ("key", key or ""),
                                   ("all", "1" if include_dev else "")) if v}
        return self._body(self.http.get("/settings" + (f"?{urlencode(query)}" if query else "")))

    def set(self, changes: dict[str, str]) -> dict[str, Any]:
        return self._body(self.http.request("PUT", "/settings", changes))

    def unset(self, key: str) -> dict[str, Any]:
        return self._body(self.http.delete(f"/settings/{quote(key, safe='')}"))

    def set_secret(self, key: str, value: str) -> dict[str, Any]:
        return self._body(self.http.request("PUT", f"/settings/secrets/{quote(key, safe='')}",
                                            {"value": value}))

    def delete_secret(self, key: str) -> dict[str, Any]:
        return self._body(self.http.delete(f"/settings/secrets/{quote(key, safe='')}"))

    def test(self, section: str, name: str) -> dict[str, Any]:
        reply = self._body(self.http.post("/settings/test", {"section": section, "name": name}))
        job = reply.get("job")
        if not job or "testable" in reply:
            return reply
        deadline = time.monotonic() + TEST_WAIT_S
        while time.monotonic() < deadline:
            state = self.http.get(f"/jobs/{quote(str(job), safe='')}")
            if state.get("state") == "done":
                return dict(state.get("result") or {})
            if state.get("state") == "failed":
                from harness_manager.client.codec import error_from_json

                raise error_from_json(state.get("error") or {})
            time.sleep(0.2)
        raise UsageError(f"the test is still running in the service (job {job})",
                         hint=f"GET /api/v1/jobs/{job} shows it")


def _shell_context(ctx: Ctx) -> ops.SettingsContext:
    """This process's view: its own environment, the state dir the engine uses, and the
    installed board packs' rows (a service's engine is not this process's: a local one,
    which loads the packs and nothing else, stands in for it)."""
    engine = ctx.engine
    if not callable(getattr(engine, "settings_resolver", None)):
        from harness_manager.core.services import EngineConfig
        from harness_manager.engine import Engine

        engine = Engine(EngineConfig(state_dir=getattr(engine, "state_dir", None)))
    return ops.SettingsContext(state_dir=getattr(engine, "state_dir", None), engine=engine)


def _backend(ctx: Ctx) -> _Local | _Remote:
    http = getattr(ctx.engine, "http", None)
    if http is not None and callable(getattr(http, "request", None)):
        return _Remote(http)
    return _Local(_shell_context(ctx))


# --- text ---------------------------------------------------------------------------------------


def value_text(r: dict[str, Any]) -> str:
    """A row's value for a person: a secret only as set / not set, and where."""
    sec = r.get("secret")
    if sec is not None:
        if not sec.get("set"):
            return "not set"
        text = f"set ({sec.get('where') or sec.get('backend') or '?'})"
        if not sec.get("reachable", True):
            text += f", but this process cannot reach it: {sec.get('why') or '?'}"
        return text
    v = r.get("value")
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) if v else "(none)"
    if v is None:
        return "(none)"
    if v == "":
        return '""'
    return str(v)


def tsv_value(r: dict[str, Any]) -> Any:
    sec = r.get("secret")
    if sec is not None:
        return "set" if sec.get("set") else "not set"
    return r.get("value")


def source_text(r: dict[str, Any], env_owner: str = "") -> str:
    src, where = r.get("source", ""), r.get("where", "")
    text = {"lock": f"locked by the administrator in {where}",
            "env": f"from {where}" + (f" in {env_owner} environment" if env_owner else ""),
            "user": f"yours, in {where}" if where else "yours",
            "machine": f"this machine's default, in {where}",
            "pack": f"from {where}",
            "default": "default"}.get(src, src)
    if r.get("shadowed"):
        text += f"; it hides your own value: unset {r['shadowed']} to use it"
    if r.get("capped"):
        text += f"; lowered: {r['capped']}"
    return text


def _apply_notes(result: dict[str, Any]) -> list[str]:
    out = []
    for apply, note in _APPLY_NOTE.items():
        keys = result.get("applies", {}).get(apply) or []
        if keys:
            out.append(f"{', '.join(keys)}: {note}")
    return out


def _problems(listing: dict[str, Any]) -> list[str]:
    return [f"note: {p}" for p in listing.get("problems", [])]


# --- the verbs ----------------------------------------------------------------------------------


def cmd_config(ctx: Ctx) -> int:
    action = ctx.args.config_cmd
    return {"list": _list, "get": _get, "set": _set, "unset": _unset,
            "set-secret": _set_secret, "unset-secret": _unset_secret,
            "clear-secret": _unset_secret, "path": _path, "test": _test}[action](ctx)


def _list(ctx: Ctx) -> int:
    a = ctx.args
    be = _backend(ctx)
    got = be.listing(section=a.section, include_dev=a.include_dev)
    rows = got["rows"]
    human: list[str] = []
    width = max((len(r["key"]) for r in rows), default=10)
    names = {s["id"]: s["name"] for s in got.get("sections", [])}
    section = None
    for r in rows:
        if r.get("section_id") != section:
            section = r.get("section_id")
            human.append(names.get(section, section or ""))
        lock = "  [locked]" if r.get("locked") else ""
        human.append(f"  {r['key']:<{width}}  {value_text(r)}  ({source_text(r)}){lock}")
        human += [f"      problem: {p}" for p in r.get("problems", [])]
    if not rows:
        human.append("no settings" + (f" in {a.section}" if a.section else ""))
    inst = got.get("instances", {})
    if a.section and not inst.get("hubs") and ops.testers.section_id(a.section) == "hubs":
        human.append("  No hub: Harness Manager talks to boards directly. Add one with "
                     "`harness-manager config set hubs.NAME.host HOST`.")
    human += _problems(got)
    if be.remote:
        human.append("(the service's view; `harness-manager config get KEY` also shows this "
                     "shell's)")
    tsv = [[r["key"], tsv_value(r), r.get("source"), r.get("where"), r.get("locked"),
            r.get("shadowed"), r.get("apply"), r.get("section_id"), r.get("type")]
           for r in rows]
    ctx.emit(Result("config list", got, rows=tsv, human=human))
    return ExitCode.OK


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    keys = ("value", "source", "where", "locked", "shadowed", "capped", "secret")
    return all(a.get(k) == b.get(k) for k in keys)


def _get(ctx: Ctx) -> int:
    key = ctx.args.key
    be = _backend(ctx)
    service = be.listing(key=key)["rows"][0] if be.remote else None
    try:
        shell = ops.listing(_shell_context(ctx), key=key)["rows"][0]
    except UsageError:
        if service is None:
            raise
        shell = service                  # a row only the service's packs declare
    main = service or shell
    differs = service is not None and not _same(service, shell)
    owner = "the service's" if service is not None else "this shell's"
    human = [f"{main['key']} = {value_text(main)}",
             f"  source: {source_text(main, owner)}",
             f"  apply:  {main.get('apply', 'live')}"
             + (f" ({_APPLY_NOTE[main['apply']]})" if main.get("apply") in _APPLY_NOTE else "")]
    if main.get("doc"):
        human.append(f"  about:  {main['doc']}")
    human += [f"  problem: {p}" for p in main.get("problems", [])]
    if service is None:
        human.append("  (no service is running: this is this shell's view)")
    elif differs:
        where = source_text(shell, "this shell's")
        human.append(f"  this shell would use: {value_text(shell)}, {where}")
    else:
        human.append("  this shell sees the same")
    views = [("service", service)] if service is not None else []
    views.append(("shell", shell))
    tsv = [[r["key"], tsv_value(r), r.get("source"), r.get("where"), r.get("locked"),
            r.get("shadowed"), r.get("apply"), who] for who, r in views]
    data = {"key": main["key"], "row": main, "service": service, "shell": shell,
            "differs": differs}
    ctx.emit(Result("config get", data, rows=tsv, human=human))
    return ExitCode.OK


def _changed_output(ctx: Ctx, result: dict[str, Any], verb: str) -> None:
    human = []
    for r in result.get("rows", []):
        human.append(f"{r['key']} = {value_text(r)}  ({source_text(r)})")
        human += [f"  problem: {p}" for p in r.get("problems", [])]
        if r.get("locked"):
            human.append("  note: the administrator's policy fixes it; your value is kept "
                         "for when the lock goes")
    human += [f"note: {n}" for n in _apply_notes(result)]
    if verb == "unset" and not result.get("changed", True):
        human.append("note: your settings did not set it; nothing was written")
    tsv = [[r["key"], tsv_value(r), r.get("source"), r.get("where"), r.get("shadowed"),
            r.get("apply")] for r in result.get("rows", [])]
    ctx.emit(Result("config set|unset", result, rows=tsv, human=human))


def _set(ctx: Ctx) -> int:
    pairs = list(ctx.args.pairs)
    if len(pairs) % 2:
        raise UsageError("config set takes KEY VALUE pairs; one is missing its value",
                         hint="a list is ONE argument: config set updates.mirrors '/a,/b'")
    changes: dict[str, str] = {}
    for k, v in zip(pairs[::2], pairs[1::2], strict=True):
        if k in changes:
            raise UsageError(f"{k} is given twice")
        changes[k] = v
    _changed_output(ctx, _backend(ctx).set(changes), "set")
    return ExitCode.OK


def _unset(ctx: Ctx) -> int:
    _changed_output(ctx, _backend(ctx).unset(ctx.args.key), "unset")
    return ExitCode.OK


def _read_secret(key: str) -> str:
    """One line from stdin; a prompt without echo when stdin is a terminal."""
    stdin = sys.stdin
    if stdin is not None and stdin.isatty():
        value = getpass.getpass(f"{key} (not shown): ")
    else:
        value = stdin.readline() if stdin is not None else ""
    value = value.rstrip("\r\n")
    if not value.strip():
        raise UsageError(f"no secret on stdin for {key}",
                         hint=f"pipe it in: `pass show x | harness-manager config set-secret "
                              f"{key}`, or run it in a terminal to be asked")
    return value


def _secret_output(ctx: Ctx, result: dict[str, Any]) -> None:
    st = result.get("secret") or {}
    rows = result.get("rows") or [{}]
    human = [f"{result.get('key')}: " + (f"stored in {st.get('where')}" if st.get("set")
                                         else "not set")]
    if st.get("why"):
        human.append(f"  note: {st['why']}")
    main = rows[0]
    if main.get("source") == "env":
        human.append(f"  note: {main.get('where')} is set, and it wins over the store")
    human += [f"note: {n}" for n in _apply_notes(result)]
    tsv = [[result.get("key"), st.get("set"), st.get("backend"), st.get("where"),
            st.get("reachable"), st.get("why")]]
    ctx.emit(Result("config set-secret|unset-secret", result, rows=tsv, human=human))


def _set_secret(ctx: Ctx) -> int:
    if ctx.args.argv_value:
        raise UsageError("config set-secret never takes the secret on the command line "
                         "(ps and your shell history would keep it); it was not stored",
                         hint=f"pipe it: `... | harness-manager config set-secret "
                              f"{ctx.args.key}`, or run it in a terminal to be asked")
    be = _backend(ctx)
    value = _read_secret(ctx.args.key)
    _secret_output(ctx, be.set_secret(ctx.args.key, value))
    return ExitCode.OK


def _unset_secret(ctx: Ctx) -> int:
    _secret_output(ctx, _backend(ctx).delete_secret(ctx.args.key))
    return ExitCode.OK


def _path(ctx: Ctx) -> int:
    be = _backend(ctx)
    shell = ops.paths(_shell_context(ctx))
    # the service's files block (any one row brings it)
    service = be.listing(key="general.theme") if be.remote else None
    human = [f"{f['what']:<15} {f['path']}" + ("" if f["exists"] else "  (not there)")
             for f in shell["files"]]
    b = shell["secrets_backend"]
    human.append(f"new secrets go to {b['where']}" + (f" ({b['why']})" if b["why"] else "")
                 + (" from this shell" if be.remote else ""))
    tsv = [[f["what"], f["path"], f["exists"], "shell"] for f in shell["files"]]
    data = {**shell, "service": None}
    if service is not None:
        sb = service["files"]["secrets_backend"]
        human.append(f"new secrets go to {sb['where']}" + (f" ({sb['why']})" if sb["why"]
                                                           else "") + " from the service")
        if service["files"]["config_dir"] != shell["config_dir"]:
            human.append(f"note: the service's settings are in {service['files']['config_dir']}")
        tsv += [[k, service["files"][k], "", "service"]
                for k in ("config_dir", "settings", "boards", "policy", "secrets")]
        data["service"] = {"files": service["files"], "policy": service["policy"]}
    ctx.emit(Result("config path", data, rows=tsv, human=human))
    return ExitCode.OK


def _test(ctx: Ctx) -> int:
    a = ctx.args
    report = _backend(ctx).test(a.section, a.name)
    if not report.get("testable"):
        raise UnavailableError("config test", report.get("why") or "not testable yet")
    label = f"{report.get('section')}{' ' + report['name'] if report.get('name') else ''}"
    human = [f"{label}: {'PASS' if report.get('passed') else 'FAIL'}"]
    for s in report.get("steps", []):
        human.append(f"  {'ok  ' if s.get('ok') else 'FAIL'}  {s.get('step', '?'):<8} "
                     f"{s.get('detail', '')}")
        if s.get("hint") and not s.get("ok"):
            human.append(f"        hint: {s['hint']}")
    tsv = [[report.get("section"), report.get("name"), s.get("step"), s.get("ok"),
            s.get("detail"), s.get("hint")] for s in report.get("steps", [])]
    ctx.emit(Result("config test", report, rows=tsv, human=human))
    return ExitCode.OK if report.get("passed") else ExitCode.ACTION_FAILED
