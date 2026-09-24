"""Publishing: the ``gh`` plan (run only with ``--publish``) and the hub-mirror layout.

A dry run WRITES the plan (``publish-plan.json``, plus a readable ``publish-plan.txt``) and
runs nothing. ``--publish`` runs the same steps, in order, and stops at the first failure.
GitHub hosting (david's U1: private GitHub Releases fetched with a token):

- one release per version: ``v<V>`` (the app, the git tag of the source commit) or
  ``<catalog>-v<V>`` (a harness); Arm-IP assets go to the AAA repo under the same tag;
- one ROLLING release per (catalog, channel), ``channel-<catalog>-<channel>``, holding
  ``channel.json`` + ``channel.json.minisig`` (``gh release upload --clobber``);
- the live channel is read back with ``gh release download`` (read-only) and is the base
  of a published channel, so the serial always follows the live one.

The mirror (``--mirror DIR``): the same GitHub URL layout under DIR, so the relative
asset URLs resolve there too, plus ``blobs/<sha256>`` for a lookup by hash (H5). An asset
is never overwritten with other bytes; a channel file only by a higher serial.
"""

from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .common import (
    EXIT_ACTION_FAILED,
    Layout,
    ReleaseError,
    Runner,
    run,
    sha256_file,
    write_once,
)


@dataclass
class Step:
    what: str
    argv: list[str]
    unless: list[str] | None = None       # run this first; exit 0 means skip ``argv``


def _gh(gh: str, *args: str) -> list[str]:
    return [gh, *args]


def channel_steps(layout: Layout, catalog: str, channel: str, gh: str = "gh") -> list[Step]:
    tag = layout.channel_tag(catalog, channel)
    d = layout.channel_dir(catalog, channel)
    return [
        Step(f"rolling release {tag} exists (create it once)",
             _gh(gh, "release", "create", tag, "--repo", layout.repo, "--prerelease",
                 "--title", f"{catalog} {channel} channel",
                 "--notes", "Rolling channel release: channel.json + its signature. "
                            "Managed by tools/release; do not edit by hand."),
             unless=_gh(gh, "release", "view", tag, "--repo", layout.repo, "--json", "tagName")),
        Step(f"upload the signed channel to {tag}",
             _gh(gh, "release", "upload", tag, str(d / "channel.json"),
                 str(d / "channel.json.minisig"), "--repo", layout.repo, "--clobber")),
    ]


def version_steps(layout: Layout, tag: str, assets: list[Path], *, title: str, notes: str,
                  prerelease: bool, repo: str = "", git_repo: Path | None = None,
                  gh: str = "gh") -> list[Step]:
    """``git_repo``: the checkout whose tag ``tag`` is pushed first (an app release)."""
    repo = repo or layout.repo
    git_tag = git_repo is not None
    steps: list[Step] = []
    if git_repo is not None:
        steps.append(Step(f"push the source tag {tag}",
                          ["git", "-C", str(git_repo), "push", "origin", f"refs/tags/{tag}"]))
    argv = _gh(gh, "release", "create", tag, "--repo", repo, "--title", title,
               "--notes", notes or title)
    if git_tag:
        argv.append("--verify-tag")
    if prerelease:
        argv.append("--prerelease")
    steps.append(Step(f"create release {tag} in {repo} with {len(assets)} assets",
                      argv + [str(a) for a in assets]))
    return steps


def write_plan(out: Path, steps: list[Step], *, header: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "publish-plan.json").write_text(json.dumps([asdict(s) for s in steps], indent=1) + "\n",
                                           encoding="utf-8")
    lines = [f"# {header}", "# DRY RUN: nothing below was run. --publish runs it in order.", ""]
    for i, s in enumerate(steps, 1):
        lines.append(f"# {i}. {s.what}")
        if s.unless:
            lines.append(f"{shlex.join(s.unless)} >/dev/null 2>&1 || \\")
        lines.append(shlex.join(s.argv))
        lines.append("")
    path = out / "publish-plan.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def execute(steps: list[Step], runner: Runner = run) -> list[str]:
    done = []
    for s in steps:
        if s.unless is not None and runner(s.unless).returncode == 0:
            done.append(f"skip: {s.what} (already there)")
            continue
        res = runner(s.argv)
        if res.returncode != 0:
            raise ReleaseError(f"publish step failed: {s.what}: "
                               f"{(res.stderr or res.stdout or '').strip()[-300:]}",
                               hint=f"steps done: {len(done)}; fix and re-run: the plan is "
                                    "idempotent up to the failed step", code=EXIT_ACTION_FAILED)
        done.append(f"ok: {s.what}")
    return done


def fetch_live_channel(layout: Layout, catalog: str, channel: str, dest: Path, *,
                       gh: str = "gh", runner: Runner = run) -> Path | None:
    """The live ``channel.json`` (+ .minisig) from GitHub, read-only. None: no channel yet."""
    tag = layout.channel_tag(catalog, channel)
    view = runner(_gh(gh, "release", "view", tag, "--repo", layout.repo, "--json", "tagName"))
    if view.returncode != 0:
        err = (view.stderr or "").lower()
        if "not found" in err or "could not find" in err:
            return None
        raise ReleaseError(f"cannot read the live {tag} release: {(view.stderr or '').strip()}",
                           hint="gh auth status; the repo must be readable", code=EXIT_ACTION_FAILED)
    dest.mkdir(parents=True, exist_ok=True)
    res = runner(_gh(gh, "release", "download", tag, "--repo", layout.repo, "--pattern",
                     "channel.json*", "--dir", str(dest), "--clobber"))
    if res.returncode != 0 or not (dest / "channel.json").is_file():
        raise ReleaseError(f"cannot download the live channel from {tag}",
                           code=EXIT_ACTION_FAILED)
    return dest / "channel.json"


# --- the mirror ----------------------------------------------------------------------------


def mirror(src: Layout, dest_root: Path, files: list[Path], channel_files: list[Path]) -> list[str]:
    """Copy ``files`` (assets) and ``channel_files`` from the tree into a mirror at
    ``dest_root``: the same layout, plus ``blobs/<sha256>``."""
    from harness_manager.services.update.schema import parse_channel

    done = []
    for f in files:
        rel = f.relative_to(src.root)
        data = f.read_bytes()
        write_once(dest_root / rel, data)
        write_once(dest_root / "blobs" / sha256_file(f), data)
        done.append(str(rel))
    for f in channel_files:
        rel = f.relative_to(src.root)
        dst = dest_root / rel
        if f.name == "channel.json" and dst.is_file():
            old = parse_channel(json.loads(dst.read_bytes())).serial
            new = parse_channel(json.loads(f.read_bytes())).serial
            if new <= old and dst.read_bytes() != f.read_bytes():
                raise ReleaseError(f"the mirror's {rel} has serial {old}; refusing to replace it "
                                   f"with serial {new}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dst)
        done.append(str(rel))
    return done
