"""``python -m tools.release``: the command line (docs/RELEASING.md is the runbook).

    app       an app release: preconditions, wheel, hashed lock, pyverify dep, signed channel
    harness   a harness release from a mint's bundle dir (the H13 front-end)
    promote   copy a release from one channel to another (beta -> stable) and re-sign
    withdraw  mark a release withdrawn (never deleted) and re-sign
    verify    run the smoke on a release tree (HM's own channel client + checks)
    keygen    a THROWAWAY key pair under the temp dir (tests and dry runs only)

Every command that changes a channel is a DRY RUN into ``--out`` (default ``dist/release``)
unless ``--publish`` is given: the tree is written, signed and verified, and the ``gh``
steps are written to ``publish-plan.txt`` but not run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness_manager.services.update import minisign, trust

from . import smoke
from .app import build_app_release, build_wheel, check_preconditions, find_lock_tool
from .channel_doc import (
    CATALOG_APP,
    CATALOG_MPS3,
    ChannelDoc,
    pinned_role,
    schema_allows_private_app,
)
from .common import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    Layout,
    ReleaseError,
    Runner,
    run,
)
from .harness import ingest
from .publish import (
    Step,
    channel_steps,
    execute,
    fetch_live_channel,
    mirror,
    version_steps,
    write_plan,
)
from .signer import from_options, keygen_throwaway, read_public_key

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNELS_NEW = ("beta", "dev")          # a new release goes here; stable only by promote


def _out(args: argparse.Namespace) -> Path:
    return Path(args.out).resolve()


def _layout(args: argparse.Namespace) -> Layout:
    return Layout(_out(args), repo=args.repo, aaa_repo=getattr(args, "aaa_repo", Layout.aaa_repo))


def _signer(args: argparse.Namespace, runner: Runner):
    return from_options(args.signer, args.secret_key, args.public_key, args.minisign,
                        runner=runner)


def _trust_keys(args: argparse.Namespace, signer) -> list[minisign.PublicKey]:
    return [signer.public, *(read_public_key(Path(p)) for p in (args.trust_key or []))]


class Ctx:
    """One run: the output lines, the runner, and whether it publishes."""

    def __init__(self, args: argparse.Namespace, runner: Runner = run,
                 printer: Callable[[str], None] = print) -> None:
        self.args = args
        self.runner = runner
        self.say = printer
        self.publish = bool(getattr(args, "publish", False))
        self.report: dict[str, Any] = {"publish": self.publish, "warnings": [], "steps": []}

    def warn(self, text: str) -> None:
        self.report["warnings"].append(text)
        self.say(f"WARNING: {text}")


# --- shared: base, sign, verify, plan ----------------------------------------------------


def _base_path(ctx: Ctx, layout: Layout, catalog: str, channel: str, tmp: Path) -> Path | None:
    a = ctx.args
    if getattr(a, "base", None):
        return Path(a.base) / "channel.json" if Path(a.base).is_dir() else Path(a.base)
    if ctx.publish:
        return fetch_live_channel(layout, catalog, channel, tmp / f"live-{channel}",
                                  gh=a.gh, runner=ctx.runner)
    p = layout.channel_file(catalog, channel)
    return p if p.is_file() else None


def _publish_rails(ctx: Ctx, signer, channel: str) -> None:
    a = ctx.args
    role = pinned_role(signer.public)
    if not role:
        msg = (f"key {signer.public.id_hex} is not pinned in this tree's trust.PINNED_KEYS "
               f"({len(trust.PINNED_KEYS)} pinned; pending david's U2): no client built from "
               "it would trust the channel")
        if ctx.publish and not a.allow_unpinned_key:
            raise ReleaseError(msg, hint="pin the release key first (docs/KEYS.md)")
        ctx.warn(msg)
    elif role == trust.ROLE_APP_CI and channel != "dev":
        raise ReleaseError(f"key {signer.public.id_hex} is the app-ci key: it signs dev only")
    if ctx.publish and signer.kind != "minisign":
        raise ReleaseError("--publish signs with the minisign CLI only (--signer minisign): "
                           "HM's Python signer is for throwaway keys")


def _sign_and_verify(ctx: Ctx, doc: ChannelDoc, layout: Layout, signer, trust_keys,
                     version: str) -> None:
    path = layout.channel_file(doc.catalog, doc.channel)
    parsed = doc.write_signed(path, signer)
    ctx.say(f"signed {doc.catalog}/{doc.channel} serial {parsed.serial} with "
            f"{signer.kind} key {signer.public.id_hex} -> {path}")
    rep = smoke.verify(layout, doc.catalog, doc.channel, trust_keys, version)
    ctx.say(f"verified with HM's own channel client: {len(rep.checks)} checks")
    for c in rep.checks:
        ctx.say(f"  {c}")
    ctx.report.update({"catalog": doc.catalog, "channel": doc.channel, "serial": parsed.serial,
                       "key_id": signer.public.id_hex, "signer": signer.kind,
                       "channel_file": str(path), "verify": rep.checks, "changes": doc.notes})


def _finish(ctx: Ctx, layout: Layout, steps: list[Step], header: str, catalog: str,
            channel: str, assets: list[Path]) -> int:
    a = ctx.args
    out = _out(a)
    plan = write_plan(out / "plans" / f"{catalog}-{channel}", steps, header=header)
    ctx.report["plan"] = str(plan)
    if a.mirror:
        d = layout.channel_dir(catalog, channel)
        done = mirror(layout, Path(a.mirror).resolve(), assets,
                      [d / "channel.json", d / "channel.json.minisig"])
        ctx.say(f"mirror: {len(done)} files into {a.mirror} (+ blobs/<sha256>)")
        ctx.report["mirror"] = done
    if ctx.publish:
        done = execute(steps, ctx.runner)
        ctx.report["steps"] = done
        for d in done:
            ctx.say(f"published: {d}")
    else:
        ctx.say(f"DRY RUN: nothing published. The gh steps are in {plan}")
    rpath = out / "plans" / f"{catalog}-{channel}" / "report.json"
    rpath.write_text(json.dumps(ctx.report, indent=1) + "\n", encoding="utf-8")
    ctx.say(f"source for clients: --source {layout.source_template(catalog)}")
    ctx.say(f"dry-run source:     --source {layout.channel_file(catalog, '{channel}')}")
    return EXIT_OK


# --- commands ------------------------------------------------------------------------------


def cmd_app(ctx: Ctx) -> int:
    a = ctx.args
    if a.channel not in CHANNELS_NEW:
        raise ReleaseError(f"a new release goes to {' or '.join(CHANNELS_NEW)}, not {a.channel!r}",
                           hint="publish to beta, then `promote --to stable`", code=EXIT_USAGE)
    repo = Path(a.repo_root).resolve()
    signer = _signer(a, ctx.runner)
    _publish_rails(ctx, signer, a.channel)
    if ctx.publish and a.access != "public" and not schema_allows_private_app():
        raise ReleaseError("this tree's schema cannot mark the app wheel private (access "
                           "github-token): a private-repo release needs OTA-C first",
                           hint="or publish to a public host with --access public")
    pre = check_preconditions(repo, a.version, allow_dirty=a.allow_dirty, publish=ctx.publish,
                              runner=ctx.runner)
    for w in pre.warnings:
        ctx.warn(w)
    ctx.say(f"preconditions: {pre.version} at {pre.commit[:12]}, tag {pre.tag} {pre.tag_state}, "
            f"tree {'DIRTY' if pre.dirty else 'clean'}")
    layout = _layout(a)
    trust_keys = _trust_keys(a, signer)
    with tempfile.TemporaryDirectory(prefix="otar-run-") as tmp:
        base = _base_path(ctx, layout, CATALOG_APP, a.channel, Path(tmp))
        doc = ChannelDoc.load_or_new(base, CATALOG_APP, a.channel, trust_keys)
        tool = None
        if not a.lock:
            tool = find_lock_tool(a.lock_tool, uv=a.uv or "", pip_compile=a.pip_compile or "",
                                  runner=ctx.runner)
            ctx.say(f"lock tool: {tool.name} {tool.version} "
                    f"({'universal' if tool.universal else 'THIS MACHINE ONLY'})")
        build = build_app_release(
            repo, layout, pre, access=a.access,
            wheel_builder=build_wheel(ctx.runner, a.python) if not a.wheel else None,
            prebuilt_wheel=Path(a.wheel) if a.wheel else None, lock_tool=tool,
            prebuilt_lock=Path(a.lock) if a.lock else None,
            prebuilt_lock_universal=a.lock_universal, runner=ctx.runner)
        for w in build.warnings[len(pre.warnings):]:
            ctx.warn(w)
        if ctx.publish and not build.lock_tool.universal and not a.allow_non_universal_lock:
            raise ReleaseError("the lock is not universal: other OSes and Pythons could not "
                               "stage this release", hint="install uv (pip install uv) and re-run")
        doc.add(build.entry)
        build.write()
        ctx.say(f"assets: {build.wheel.name}, {build.lock.name}, {build.dep.name} "
                f"({build.lock_tool.name} lock, {build.entry['lock_info']['packages']} packages)")
        _sign_and_verify(ctx, doc, layout, signer, trust_keys, build.version)
        if a.smoke == "install":
            rep = smoke.install(layout, CATALOG_APP, a.channel, trust_keys, build.version,
                                python=a.python, runner=ctx.runner)
            for c in rep.checks:
                ctx.say(f"install smoke: {c}")
            ctx.report["install_smoke"] = rep.checks
        elif ctx.publish and not a.skip_install_smoke:
            raise ReleaseError("--publish needs --smoke install (a real install from the tree)",
                               hint="or --skip-install-smoke REASON")
        ctx.report.update({"version": build.version, "tag": build.tag,
                           "lock_info": build.entry["lock_info"]})
        assets = [build.wheel, build.lock, build.dep]
        steps = version_steps(layout, build.tag, assets, title=f"Harness Manager {build.version}",
                              notes=build.entry["notes"], prerelease=a.channel != "stable",
                              git_repo=repo, gh=a.gh)
        steps += channel_steps(layout, CATALOG_APP, a.channel, gh=a.gh)
        return _finish(ctx, layout, steps, f"app {build.version} -> {a.channel}", CATALOG_APP,
                       a.channel, assets)


def cmd_harness(ctx: Ctx) -> int:
    a = ctx.args
    if a.channel not in CHANNELS_NEW:
        raise ReleaseError(f"a new release goes to {' or '.join(CHANNELS_NEW)}, not {a.channel!r}",
                           hint="publish to beta, then `promote --to stable`", code=EXIT_USAGE)
    layout = _layout(a)
    hb = ingest(Path(a.bundle), a.version, catalog=a.catalog, layout=layout, access=a.access,
                allow_dirty=a.allow_dirty or "", channel=a.channel, min_app=a.min_app,
                pack=a.pack)
    for f in hb.findings:
        ctx.say(f"  ok [{f.code}] {f.detail}")
    for w in hb.warnings:
        ctx.warn(w)
    ctx.say(f"harness {hb.version} ({hb.impl}): {len(hb.entry['components'])} components, "
            f"{len(hb.findings)} checks passed")
    if a.check_only:
        ctx.say(json.dumps(hb.entry, indent=1, sort_keys=True))
        return EXIT_OK
    signer = _signer(a, ctx.runner)
    _publish_rails(ctx, signer, a.channel)
    trust_keys = _trust_keys(a, signer)
    with tempfile.TemporaryDirectory(prefix="otar-run-") as tmp:
        base = _base_path(ctx, layout, a.catalog, a.channel, Path(tmp))
        doc = ChannelDoc.load_or_new(base, a.catalog, a.channel, trust_keys, board=hb.board)
        doc.add(hb.entry)
        hb.write()
        _sign_and_verify(ctx, doc, layout, signer, trust_keys, hb.version)
        ctx.report.update({"version": hb.version, "tag": hb.tag,
                           "findings": [f.__dict__ for f in hb.findings]})
        notes = hb.entry.get("notes", "") or f"{a.catalog} {hb.version}"
        steps = []
        if hb.assets:
            steps += version_steps(layout, hb.tag, hb.assets, title=f"{a.catalog} {hb.version}",
                                   notes=notes, prerelease=True, gh=a.gh)
        if hb.aaa_assets:
            steps += version_steps(layout, hb.tag, hb.aaa_assets, repo=layout.aaa_repo,
                                   title=f"{a.catalog} {hb.version} (Arm IP)", notes=notes,
                                   prerelease=True, gh=a.gh)
        steps += channel_steps(layout, a.catalog, a.channel, gh=a.gh)
        return _finish(ctx, layout, steps, f"harness {hb.version} -> {a.channel}", a.catalog,
                       a.channel, [*hb.assets, *hb.aaa_assets])


def cmd_promote(ctx: Ctx) -> int:
    a = ctx.args
    if a.source == a.to:
        raise ReleaseError("--from and --to are the same channel", code=EXIT_USAGE)
    signer = _signer(a, ctx.runner)
    _publish_rails(ctx, signer, a.to)
    layout = _layout(a)
    trust_keys = _trust_keys(a, signer)
    with tempfile.TemporaryDirectory(prefix="otar-run-") as tmp:
        if ctx.publish:
            src_path = fetch_live_channel(layout, a.catalog, a.source, Path(tmp) / "src",
                                          gh=a.gh, runner=ctx.runner)
        else:
            src_path = layout.channel_file(a.catalog, a.source)
        if src_path is None or not Path(src_path).is_file():
            raise ReleaseError(f"no {a.catalog}/{a.source} channel to promote from")
        src = ChannelDoc.load(Path(src_path), a.catalog, a.source, trust_keys)
        base = _base_path(ctx, layout, a.catalog, a.to, Path(tmp))
        board = src.doc.get("board")
        doc = ChannelDoc.load_or_new(base, a.catalog, a.to, trust_keys, board=board)
        doc.promote_from(src, a.version)
        _sign_and_verify(ctx, doc, layout, signer, trust_keys, a.version)
        ctx.report.update({"version": a.version, "from": a.source})
        steps = channel_steps(layout, a.catalog, a.to, gh=a.gh)
        return _finish(ctx, layout, steps, f"promote {a.catalog} {a.version} {a.source} -> {a.to}",
                       a.catalog, a.to, [])


def cmd_withdraw(ctx: Ctx) -> int:
    a = ctx.args
    signer = _signer(a, ctx.runner)
    _publish_rails(ctx, signer, a.channel)
    layout = _layout(a)
    trust_keys = _trust_keys(a, signer)
    with tempfile.TemporaryDirectory(prefix="otar-run-") as tmp:
        base = _base_path(ctx, layout, a.catalog, a.channel, Path(tmp))
        if base is None:
            raise ReleaseError(f"no {a.catalog}/{a.channel} channel to withdraw from")
        doc = ChannelDoc.load(base, a.catalog, a.channel, trust_keys)
        doc.withdraw(a.version, a.reason, new_current=a.current or "")
        path = layout.channel_file(a.catalog, a.channel)
        parsed = doc.write_signed(path, signer)
        rep = smoke.fetch_verified(layout, a.catalog, a.channel, trust_keys,
                                   Path(tmp) / "verify")
        ctx.say(f"withdrew {a.version}: {a.catalog}/{a.channel} serial {parsed.serial}, "
                f"re-verified by HM's channel client (serial {rep.channel.serial})")
        ctx.report.update({"catalog": a.catalog, "channel": a.channel, "serial": parsed.serial,
                           "changes": doc.notes})
        steps = channel_steps(layout, a.catalog, a.channel, gh=a.gh)
        return _finish(ctx, layout, steps, f"withdraw {a.catalog} {a.version}", a.catalog,
                       a.channel, [])


def cmd_verify(ctx: Ctx) -> int:
    a = ctx.args
    keys = [read_public_key(Path(p)) for p in a.public_key_verify]
    layout = _layout(a)
    rep = smoke.verify(layout, a.catalog, a.channel, keys, a.version or "")
    for c in rep.checks:
        ctx.say(c)
    if a.smoke == "install":
        for c in smoke.install(layout, a.catalog, a.channel, keys, a.version or "",
                               python=a.python, runner=ctx.runner).checks:
            ctx.say(f"install smoke: {c}")
    ctx.say(f"VERIFIED {a.catalog}/{a.channel} {rep.version} serial {rep.serial}")
    return EXIT_OK


def cmd_keygen(ctx: Ctx) -> int:
    sk, pk = keygen_throwaway(Path(ctx.args.throwaway), ctx.args.name)
    ctx.say(f"THROWAWAY key pair (unencrypted, tests and dry runs only): {sk} {pk}")
    ctx.say(f"key id {minisign.PublicKey.from_text(pk.read_text()).id_hex}")
    return EXIT_OK


# --- argparse ------------------------------------------------------------------------------


def _common(p: argparse.ArgumentParser, *, signs: bool = True) -> None:
    p.add_argument("--out", default=str(REPO_ROOT / "dist" / "release"),
                   help="the release tree (default dist/release)")
    p.add_argument("--repo", default=Layout.repo, help="the GitHub repo hosting the releases")
    p.add_argument("--gh", default=os.environ.get("HM_RELEASE_GH", "gh"), help="the gh binary")
    p.add_argument("--python", default=sys.executable, help="python for build and install smoke")
    if signs:
        g = p.add_argument_group("key source (U2 pending: docs/KEYS.md)")
        g.add_argument("--signer", choices=("minisign", "python"),
                       help="minisign CLI (default; env HM_RELEASE_SIGNER) or HM's own signer "
                            "(throwaway keys)")
        g.add_argument("--secret-key", help="minisign secret key (env HM_RELEASE_SECRET_KEY)")
        g.add_argument("--public-key", help="its public key (env HM_RELEASE_PUBLIC_KEY)")
        g.add_argument("--minisign", help="the minisign binary (env HM_MINISIGN)")
        g.add_argument("--trust-key", action="append",
                       help="another public key to accept on the BASE channel (rotation)")
        p.add_argument("--base", help="the current channel.json (file or dir) to add to; default: "
                                      "the one in --out, or the live one with --publish")
        p.add_argument("--mirror", help="also write a hub-mirror layout into this directory")
        p.add_argument("--publish", action="store_true",
                       help="run the gh steps (default: dry run, nothing published)")
        p.add_argument("--allow-unpinned-key", action="store_true",
                       help="publish with a key no client build pins (never for real users)")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m tools.release",
                                 description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("app", help="an app release (wheel, lock, dep, signed channel)")
    _common(p)
    p.add_argument("--version", help="default: harness_manager.__version__ (the one source)")
    p.add_argument("--channel", default="beta", help="beta (default) or dev")
    p.add_argument("--repo-root", default=str(REPO_ROOT), help="the HM checkout to release")
    p.add_argument("--access", default="github-token", choices=("github-token", "public"),
                   help="asset access (U1: private GitHub Releases -> github-token)")
    p.add_argument("--wheel", help="use this wheel instead of building one")
    p.add_argument("--lock", help="use this hashed lock instead of compiling one")
    p.add_argument("--lock-universal", action="store_true",
                   help="the --lock file is universal (uv --universal)")
    p.add_argument("--lock-tool", default="auto", choices=("auto", "uv", "pip-tools"))
    p.add_argument("--uv", help="the uv binary (else $HARNESS_MANAGER_UV or PATH)")
    p.add_argument("--pip-compile", help="the pip-compile binary (pip-tools; the fallback)")
    p.add_argument("--smoke", default="verify", choices=("verify", "install"),
                   help="verify (offline, always) or also install into a throwaway venv")
    p.add_argument("--skip-install-smoke", metavar="REASON",
                   help="publish without the install smoke (recorded)")
    p.add_argument("--allow-dirty", action="store_true", help="dry run only: a dirty tree")
    p.add_argument("--allow-non-universal-lock", action="store_true",
                   help="publish a pip-tools lock (this machine's OS and Python only)")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("harness", help="a harness release from a mint's bundle dir (H13)")
    _common(p)
    p.add_argument("--bundle", required=True, help="the bundle dir (see tools/release/harness.py)")
    p.add_argument("--version", required=True, help="the harness release version, e.g. 1.2.0")
    p.add_argument("--catalog", default=CATALOG_MPS3)
    p.add_argument("--channel", default="beta")
    p.add_argument("--pack", default="mps3")
    p.add_argument("--aaa-repo", default=Layout.aaa_repo, help="the private repo for Arm IP")
    p.add_argument("--access", default="github-token", choices=("github-token", "public"),
                   help="access of the OPEN assets (Arm IP is always github-token)")
    p.add_argument("--min-app", default="0.1.0")
    p.add_argument("--allow-dirty", metavar="REASON",
                   help="beta/dev only: release a dirty image, with the reason signed in")
    p.add_argument("--check-only", action="store_true",
                   help="validate and print the catalogue entry; write nothing")
    p.set_defaults(func=cmd_harness)

    p = sub.add_parser("promote", help="beta -> stable (re-sign; no re-upload)")
    _common(p)
    p.add_argument("--catalog", default=CATALOG_APP)
    p.add_argument("--version", required=True)
    p.add_argument("--from", dest="source", default="beta")
    p.add_argument("--to", default="stable")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("withdraw", help="withdraw a release (never deleted)")
    _common(p)
    p.add_argument("--catalog", default=CATALOG_APP)
    p.add_argument("--channel", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--current", help="the release to make current instead (default: the newest "
                                     "older one)")
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("verify", help="the smoke on a release tree")
    _common(p, signs=False)
    p.add_argument("--catalog", default=CATALOG_APP)
    p.add_argument("--channel", default="beta")
    p.add_argument("--version")
    p.add_argument("--public-key", dest="public_key_verify", action="append", required=True)
    p.add_argument("--smoke", default="verify", choices=("verify", "install"))
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("keygen", help="a THROWAWAY key pair (temp dir only)")
    p.add_argument("--throwaway", required=True, metavar="DIR")
    p.add_argument("--name", default="release")
    p.set_defaults(func=cmd_keygen)
    return ap


def main(argv: list[str] | None = None, *, runner: Runner = run,
         printer: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    ctx = Ctx(args, runner, printer)
    try:
        return args.func(ctx)
    except ReleaseError as exc:
        printer(f"REFUSED: {exc}" if exc.code == EXIT_REFUSED else f"ERROR: {exc}")
        return exc.code
