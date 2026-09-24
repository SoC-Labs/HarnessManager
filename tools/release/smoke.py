"""The smoke: read the release back the way a client will, with HM's own code.

``verify`` (always; offline):

1. ``ChannelClient.fetch`` of the tree's ``channel.json`` over ``file://``: signature (by a
   key the given trust store holds for that channel), schema, channel name, key id,
   anti-rollback serial, expiry: the client's full order, in a throwaway state dir;
2. every asset the chosen release names is fetched by HM's ``Downloader``, which resolves
   the relative URL against the channel and checks size and sha256;
3. app: the wheel's METADATA names this version; the lock pins the ``dep`` wheel by its
   sha256 and does not pin harness-manager itself;
4. harness: ``bundle.prepare_release`` (the consumer's domain checks: SD tree, no .ebf, the
   ``.bit`` part and USERID, overlays keyed + CRC, ip_class) on every component.

Assets marked ``access: github-token`` are checked with the same code on a public view
of the entry: a ``file://`` tree never gets a token, and on a private host the client's
token handling is OTA-C's (channel fetch) and H5's (mirror lookup by sha).

``install`` (``--smoke install``; needs the package index): a throwaway venv, the lock with
pyverify rewritten to the verified ``dep`` file (what the client's stage step does, OTA
§4.1), ``pip install --require-hashes``, then the installed CLI must report the version.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from harness_manager.core.errors import HarnessError
from harness_manager.services.update import minisign
from harness_manager.services.update.bundle import PackOverlayHandler, prepare_release
from harness_manager.services.update.channel import ChannelClient, VerifiedChannel
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.schema import AppRelease, Asset, HarnessRelease
from harness_manager.services.update.state import UpdateState

from .app import PYVERIFY, lock_packages, wheel_version
from .channel_doc import catalog_of, section_for, trust_store
from .common import EXIT_ACTION_FAILED, EXIT_MISMATCH, Layout, ReleaseError, Runner, run


@dataclass
class SmokeReport:
    catalog: str
    channel: str
    serial: int = 0
    key_id: str = ""
    version: str = ""
    checks: list[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        self.checks.append(text)


def _public(asset: Asset) -> Asset:
    return dataclasses.replace(asset, access="public", repo="")


def fetch_verified(layout: Layout, catalog: str, channel: str,
                   public_keys: list[minisign.PublicKey], work: Path) -> VerifiedChannel:
    """The client's own fetch + verify, from the tree, in a throwaway state dir."""
    path = layout.channel_file(catalog, channel)
    if not path.is_file():
        raise ReleaseError(f"no channel at {path}")
    state = UpdateState(work / "state" / "update")
    dl = Downloader(work / "cache", token=None)
    client = ChannelClient(state, dl, trust_store(public_keys, with_pinned=False))
    try:
        return client.fetch(channel, source=path.as_uri())
    except HarnessError as exc:
        raise ReleaseError(f"HM's channel client refuses {path}: {exc.message}",
                           code=EXIT_MISMATCH) from None


def verify(layout: Layout, catalog: str, channel: str, public_keys: list[minisign.PublicKey],
           version: str = "", *, pack_part: str = "") -> SmokeReport:
    work = Path(tempfile.mkdtemp(prefix="otar-smoke-"))
    try:
        verified = fetch_verified(layout, catalog, channel, public_keys, work)
        ch = verified.channel
        rep = SmokeReport(catalog, channel, ch.serial, verified.key_id)
        rep.add(f"channel {catalog}/{channel} serial {ch.serial}: signature by {verified.key_id} "
                f"({verified.key_role}), schema, name, anti-rollback: OK")
        if catalog_of(ch) != catalog:
            raise ReleaseError(f"the signed channel says catalogue {catalog_of(ch)!r}, "
                               f"not {catalog!r}")
        dl = Downloader(work / "cache", token=None)
        if section_for(catalog) == "app":
            rel = ch.app_release(version or None)
            if rel is None:
                raise ReleaseError(f"no app release {version or '(current)'} in the channel")
            rep.version = rel.version
            _verify_app(rel, verified, dl, rep)
        else:
            rel = ch.harness_release(version or None)
            if rel is None:
                raise ReleaseError(f"no harness release {version or '(current)'} in the channel")
            rep.version = rel.version
            _verify_harness(rel, verified, dl, work, rep, pack_part or ch.board.part,
                            ch.board.pack or "mps3")
        return rep
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _fetch(dl: Downloader, asset: Asset, base: str) -> Path:
    try:
        return dl.fetch(_public(asset), base_url=base)
    except HarnessError as exc:
        raise ReleaseError(f"{asset.name}: {exc.message}", code=EXIT_MISMATCH) from None


def raw_deps(verified: VerifiedChannel, version: str) -> list[Asset]:
    """The ``dep`` artifacts of an app release, read from the signed document itself, so it
    works whether or not this tree's parser knows the kind (OTA-C adds it)."""
    from harness_manager.services.update.version import parse_version

    want = parse_version(version)
    for r in (verified.channel.raw.get("app") or {}).get("releases", []):
        if parse_version(r["version"]) != want:
            continue
        return [Asset(name=a["name"], url=a["url"], sha256=a["sha256"].lower(), size=a["size"],
                      access=a.get("access", "public"), repo=a.get("repo", ""))
                for a in r.get("artifacts", []) if a.get("kind") == "dep"]
    return []


def _verify_app(rel: AppRelease, verified: VerifiedChannel, dl: Downloader,
                rep: SmokeReport) -> None:
    wheel = _fetch(dl, rel.wheel, verified.url)
    rep.add(f"wheel {rel.wheel.name}: size + sha256 as signed")
    named = wheel.with_name(rel.wheel.name)
    shutil.copyfile(wheel, named)
    got = wheel_version(named)
    if got != rel.version:
        raise ReleaseError(f"the wheel's METADATA says {got}, the channel says {rel.version}",
                           code=EXIT_MISMATCH)
    rep.add(f"wheel METADATA: harness-manager {got}")
    if rel.lock is None:
        raise ReleaseError("the release has no lock: the client cannot stage it (spike step 3)")
    lock = _fetch(dl, rel.lock, verified.url).read_text(encoding="utf-8")
    rep.add(f"lock {rel.lock.name}: size + sha256 as signed")
    pkgs = lock_packages(lock)
    if "harness-manager" in pkgs:
        raise ReleaseError("the lock pins harness-manager itself")
    deps = raw_deps(verified, rel.version)
    if not deps:
        raise ReleaseError("the release names no dep artifact: pyverify has no source")
    for dep in deps:
        _fetch(dl, dep, verified.url)
        pin = next((ln for ln in lock.splitlines() if ln.lower().startswith(PYVERIFY + "==")), "")
        if f"--hash=sha256:{dep.sha256}" not in pin:
            raise ReleaseError(f"the lock does not pin {dep.name} by its sha256 "
                               f"({pin or 'no ' + PYVERIFY + ' line'})", code=EXIT_MISMATCH)
        rep.add(f"dep {dep.name}: fetched, and the lock pins it by sha256")
    rep.add(f"lock: {len(pkgs)} packages, every line hashed")


def _verify_harness(rel: HarnessRelease, verified: VerifiedChannel, dl: Downloader, work: Path,
                    rep: SmokeReport, part: str, pack: str) -> None:
    public = dataclasses.replace(rel, components=tuple(
        dataclasses.replace(c, asset=_public(c.asset)) for c in rel.components))
    names = [c.name for c in public.components]
    try:
        prepared = prepare_release(public, names, downloader=dl, base_url=verified.url,
                                   work=work / "work", part=part,
                                   overlay_handler=PackOverlayHandler(pack))
    except HarnessError as exc:
        raise ReleaseError(f"HM's bundle checks refuse harness {rel.version}: {exc.message}",
                           code=EXIT_MISMATCH) from None
    for c in public.components:
        rep.add(f"component {c.name} ({c.target}, {c.kind}): fetched, size + sha256 as signed")
    for item in prepared.checks:
        rep.add(f"  {item.check.value}: {item.name}: {item.detail}")


# --- the install smoke -------------------------------------------------------------------


def install(layout: Layout, catalog: str, channel: str, public_keys: list[minisign.PublicKey],
            version: str = "", *, python: str = sys.executable, runner: Runner = run,
            keep: Path | None = None) -> SmokeReport:
    """Install the release into a throwaway venv from the tree + the package index."""
    work = Path(tempfile.mkdtemp(prefix="otar-install-", dir=keep))
    try:
        verified = fetch_verified(layout, catalog, channel, public_keys, work)
        rel = verified.channel.app_release(version or None)
        if rel is None or rel.lock is None:
            raise ReleaseError("no app release with a lock to install")
        dl = Downloader(work / "cache", token=None)
        wheel = _fetch(dl, rel.wheel, verified.url)
        wdir = work / "wheels"
        wdir.mkdir()
        named = wdir / rel.wheel.name
        shutil.copyfile(wheel, named)
        deps = {}
        for dep in raw_deps(verified, rel.version):
            p = wdir / dep.name
            shutil.copyfile(_fetch(dl, dep, verified.url), p)
            deps[dep.name.split("-")[0].replace("_", "-").lower()] = (p, dep.sha256)
        lock = _fetch(dl, rel.lock, verified.url).read_text(encoding="utf-8")
        lines = []
        for ln in lock.splitlines():
            name = ln.split("==", 1)[0].strip().lower() if "==" in ln else ""
            if name in deps:
                p, sha = deps[name]
                lines.append(f"{name} @ {p.resolve().as_uri()} --hash=sha256:{sha}")
            else:
                lines.append(ln)
        lines.append(f"harness-manager @ {named.resolve().as_uri()} "
                     f"--hash=sha256:{rel.wheel.sha256}")
        reqs = work / "reqs.txt"
        reqs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        venv = work / "venv"
        steps = [[python, "-m", "venv", str(venv)],
                 [str(venv / "bin" / "python"), "-m", "pip", "install", "-q",
                  "--disable-pip-version-check", "--require-hashes", "-r", str(reqs)]]
        for argv in steps:
            res = runner(argv)
            if res.returncode != 0:
                tail = ((res.stderr or res.stdout or "").strip().splitlines() or [""])[-3:]
                raise ReleaseError(f"install smoke failed at `{' '.join(argv[:4])} …`: "
                                   + " | ".join(tail), code=EXIT_ACTION_FAILED)
        exe = venv / "bin" / "harness-manager"
        res = runner([str(exe), "version"])
        got = (res.stdout or "").strip().split()[0] if res.returncode == 0 and res.stdout else ""
        if got != rel.version:
            raise ReleaseError(f"the installed CLI reports {got or '(nothing)'}; expected "
                               f"{rel.version}: {(res.stderr or '').strip()[-200:]}",
                               code=EXIT_ACTION_FAILED)
        freeze = runner([str(venv / "bin" / "python"), "-m", "pip", "list", "--format=json",
                         "--disable-pip-version-check"])
        installed = {p["name"].lower(): p["version"] for p in json.loads(freeze.stdout or "[]")}
        rep = SmokeReport(catalog, channel, verified.channel.serial, verified.key_id, rel.version)
        rep.add(f"installed into a throwaway venv with --require-hashes: {len(installed)} packages")
        rep.add(f"`harness-manager version` -> {got}; mps3-pyverify "
                f"{installed.get('mps3-pyverify', '?')} from the dep wheel")
        return rep
    finally:
        if keep is None:
            shutil.rmtree(work, ignore_errors=True)

