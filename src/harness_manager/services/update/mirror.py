"""Write an offline/lab mirror of a verified channel (H5; the ``harness mirror`` verb's core).

The layout is the release tool's (``tools/release/publish.py``), which is GitHub's own URL
space, so one signed channel works on GitHub, on the hub and on a USB stick::

    <root>/<owner>/<repo>/releases/download/<channel tag>/channel.json(.minisig)
    <root>/<owner>/<repo>/releases/download/<channel tag>/keys.json(.minisig)   (if published)
    <root>/blobs/<sha256>                                                      every asset

A client reads it with ``--source <root>`` (``channel.mirror_channel_path``) or lists
``<root>`` in ``$HARNESS_MANAGER_UPDATE_MIRRORS``; the downloader then finds every asset
by its signed sha256 in ``blobs/`` before it tries any URL. Nothing in the channel is
rewritten: the mirror holds the exact signed bytes, so it verifies with the same keys.

**Arm-IP stays out by default.** A component with ``access: github-token`` (the Arm
Academic Access overlays, a private dist) is copied only with ``include_private=True``:
a mirror is often readable by more people than the private repo is.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from harness_manager.core.errors import HarnessError, RefusedError

from . import github
from .channel import VerifiedChannel
from .download import Downloader, Progress
from .schema import ACCESS_TOKEN, Asset
from .state import atomic_write_bytes
from .version import parse_version


@dataclass
class MirrorReport:
    root: Path
    channel_dir: Path
    blobs: list[str] = field(default_factory=list)         # sha256s written or already there
    skipped: dict[str, str] = field(default_factory=dict)  # asset name -> why not mirrored

    def summary(self) -> dict[str, object]:
        return {"root": str(self.root), "channel_dir": str(self.channel_dir),
                "blobs": list(self.blobs), "skipped": dict(self.skipped)}


def channel_dir(root: Path, verified: VerifiedChannel) -> Path:
    """Where the channel files go: the same place as on GitHub (or the default repo's)."""
    rd = github.parse_release_download(verified.url)
    if rd is not None:
        owner, repo, tag = rd.owner, rd.repo, rd.tag
    else:
        owner, repo = github.DEFAULT_REPO.split("/", 1)
        tag = github.release_tag(verified.channel.channel, verified.catalog or None)
    return Path(root) / owner / repo / "releases" / "download" / tag


def assets_of(verified: VerifiedChannel, versions: Iterable[str] | None = None) -> list[Asset]:
    """Every asset of the chosen releases (all when ``versions`` is None): harness
    components (kits included), app wheels, locks and deps."""
    want = {parse_version(v) for v in versions} if versions is not None else None
    ch = verified.channel
    out: list[Asset] = []
    for rel in ch.harness:
        if want is None or parse_version(rel.version) in want:
            out += [c.asset for c in rel.components]
    for app in ch.app:
        if want is None or parse_version(app.version) in want:
            out += app.assets()
    seen: set[str] = set()
    return [a for a in out if not (a.sha256 in seen or seen.add(a.sha256))]


def write_mirror(verified: VerifiedChannel, downloader: Downloader, root: Path, *,
                 versions: Iterable[str] | None = None, include_private: bool = False,
                 progress: Progress | None = None) -> MirrorReport:
    """Copy a verified channel (its exact signed bytes) and its assets (by sha256) into
    ``root``. Every asset is downloaded and hash-checked first; a blob already in the
    mirror is re-hashed and replaced if it is damaged."""
    if not verified.data or not verified.signature:
        raise RefusedError("this verified channel does not carry its signed bytes",
                           hint="fetch it with ChannelClient.fetch, then mirror it")
    root = Path(root)
    cdir = channel_dir(root, verified)
    report = MirrorReport(root=root, channel_dir=cdir)
    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    for asset in assets_of(verified, versions):
        if asset.access == ACCESS_TOKEN and not include_private:
            report.skipped[asset.name] = "private (access: github-token); pass include_private"
            continue
        dest = blobs / asset.sha256
        if dest.is_file() and dest.stat().st_size == asset.size and _sha256(dest) == asset.sha256:
            report.blobs.append(asset.sha256)
            continue
        try:
            src = downloader.fetch(asset, base_url=verified.url, progress=progress)
        except HarnessError as exc:
            report.skipped[asset.name] = exc.message
            continue
        tmp = dest.with_name(f".{asset.sha256}.tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dest)
        report.blobs.append(asset.sha256)
    # The channel last, so a mirror that lists a release holds its blobs.
    atomic_write_bytes(cdir / "channel.json", verified.data)
    atomic_write_bytes(cdir / "channel.json.minisig", verified.signature)
    for name in ("keys.json", "keys.json.minisig"):
        try:
            data = downloader.fetch_bytes(urljoin(verified.url, name), what=name)
        except HarnessError:
            continue                            # no rotation published: nothing to copy
        atomic_write_bytes(cdir / name, data)
    return report


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
