"""Fetch and verify a channel: signature, schema, channel name, serial, expiry.

The order is fixed and every step refuses the whole channel:

1. **signature** over the exact bytes of ``channel.json``, by a key the app
   trusts *for this channel* (``trust.TrustStore``). If the signer is unknown,
   the channel's ``keys.json`` (root-signed rotation) is tried once;
2. **schema** (``schema.parse_channel``), strict;
3. **consistency**: the file names the channel asked for (a signed dev channel
   cannot be replayed as stable), the catalogue asked for when the caller names one
   (a harness catalogue cannot be served as the app's), and ``signing_key_id`` is the
   key that signed it;
4. **anti-rollback**: ``serial`` is not below the last one accepted *for this
   catalogue and channel*, and a serial is never reused for different content
   (``state.SerialStore``);
5. **expiry**: past ``expires_at`` is a WARNING, never a block (labs run offline).

Only then is the serial recorded. Asset hashes and domain checks come later,
per component (``bundle.py``).

**Sources** (``channel_url``): a URL template (``{channel}``, ``{catalog}``), a base URL
ending in ``/``, a ``file://`` URL, a local file or directory (a hub mirror), or
``github:OWNER/REPO`` (private GitHub Releases; ``github.py``). The default is
``github:SoC-Labs/HarnessManager`` (david U1). A mirror directory (the release tool's
``--mirror``, or ``mirror.write_mirror``) uses GitHub's own URL layout, so relative asset
URLs resolve in it too: ``<root>/<owner>/<repo>/releases/download/<tag>/channel.json`` with
``<tag>`` = ``channel-<catalog>-<channel>``, plus ``<root>/blobs/<sha256>``. ``--source
<root>`` finds the channel in it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from harness_manager.core.errors import HarnessError, RefusedError

from . import github
from .download import Downloader
from .schema import Channel, ChannelFormatError, parse_channel
from .state import SerialStore, UpdateState
from .trust import TrustStore, UntrustedKeyError, accept_keys_json

SOURCE_ENV = "HARNESS_MANAGER_UPDATE_SOURCE"
#: david U1: private GitHub Releases on the HarnessManager repo (``github.py``).
DEFAULT_SOURCE = github.DEFAULT_SOURCE
DEFAULT_CHANNEL = "stable"
CHANNEL_ENV = "HARNESS_MANAGER_UPDATE_CHANNEL"
MAX_CHANNEL_BYTES = 4 << 20
MAX_SIG_BYTES = 64 << 10


def mirror_channel_path(root: Path, channel: str, catalog: str | None = None) -> Path | None:
    """Inside a mirror (GitHub's layout): ``<owner>/<repo>/releases/download/<tag>/channel.json``
    for the channel's rolling release. With no catalogue, ``channel-<channel>``, else the one
    ``channel-<catalog>-<channel>`` there is. The default repo wins a tie between repos."""
    def find(tag_glob: str) -> list[Path]:
        return sorted(root.glob(f"*/*/releases/download/{tag_glob}/channel.json"))

    hits = find(github.release_tag(channel, catalog))
    if not hits and not catalog:
        hits = find(f"channel-*-{channel}")
        if len({h.parent.name for h in hits}) > 1:
            return None                      # several catalogues: the caller must name one
    if len(hits) > 1:
        hits = [h for h in hits if h.parts[-6:-4] == tuple(github.DEFAULT_REPO.split("/"))]
    return hits[0] if len(hits) == 1 else None


def channel_url(source: str | None, channel: str, catalog: str | None = None) -> str:
    """Where ``channel.json`` is: a URL template with ``{channel}`` (and ``{catalog}``), a
    base URL ending in ``/``, a file URL, a local directory/file (a mirror), or a
    ``github:OWNER/REPO`` source (the rolling ``channel-<catalog>-<channel>`` release)."""
    src = (source or _source_setting() or DEFAULT_SOURCE).strip()
    gh = github.source_url(src, channel, catalog)
    if gh is not None:
        return gh
    if "{channel}" in src:
        src = src.replace("{channel}", channel)
    if "{catalog}" in src:
        if not catalog:
            raise RefusedError(f"the update source {src!r} needs a catalogue",
                               hint="name one (hm-app, mps3-harness, ...)")
        src = src.replace("{catalog}", catalog)
    if "://" not in src:
        path = Path(src).expanduser().resolve()
        if path.is_dir():
            inner = path / "channel.json"
            if not inner.is_file():
                inner = mirror_channel_path(path, channel, catalog) or inner
            path = inner
        return path.as_uri()
    if src.endswith("/"):
        src += "channel.json"
    return src


def _source_setting() -> str:
    """The setting ``updates.source``: ``SOURCE_ENV``, then the Settings menu /
    ``settings.toml``, then the admin's ``[default]`` (lane SET-WIRE)."""
    from harness_manager.settings import runtime

    return str(runtime.value("updates.source") or "")


def default_channel() -> str:
    """The channel when a caller names none (a harness catalogue; the app's checker passes
    the user's own, ``selfupdate.effective``). Deliberately the variable only, not the whole
    ``updates.channel`` row: that row's file value is the APP's channel (the Updates card),
    and reading it here would move every harness check onto it (lane SET-WIRE)."""
    return os.environ.get(CHANNEL_ENV, "").strip() or DEFAULT_CHANNEL


@dataclass(frozen=True)
class VerifiedChannel:
    channel: Channel
    url: str                        # where it came from: the base for relative asset URLs
    sha256: str                     # of the exact signed bytes
    key_id: str
    key_role: str
    trusted_comment: str
    warnings: tuple[str, ...] = ()
    catalog: str = ""               # the catalogue it belongs to (stated or derived)
    data: bytes = field(default=b"", repr=False, compare=False)       # the signed bytes
    signature: bytes = field(default=b"", repr=False, compare=False)  # their .minisig


class ChannelClient:
    def __init__(self, state: UpdateState, downloader: Downloader, trust: TrustStore, *,
                 now: Callable[[], float] = time.time) -> None:
        self.state = state
        self.downloader = downloader
        self.trust = trust
        self.serials = SerialStore(state)
        self.now = now

    def fetch(self, channel: str | None = None, source: str | None = None, *,
              catalog: str | None = None) -> VerifiedChannel:
        """Fetch and verify a channel. ``catalog``: the catalogue the caller wants; it picks
        the rolling release of a ``github:`` source and must match the signed document."""
        name = channel or default_channel()
        url = channel_url(source, name, catalog)
        data = self.downloader.fetch_bytes(url, max_bytes=MAX_CHANNEL_BYTES, what="channel.json")
        sig = self.downloader.fetch_bytes(url + ".minisig", max_bytes=MAX_SIG_BYTES,
                                          what="channel.json.minisig")
        try:
            return self.verify(data, sig, name, url, catalog=catalog)
        except UntrustedKeyError:
            # An unknown signer may be a rotated release key: try the root-signed keys.json once.
            if not self._try_rotation(url):
                raise
            return self.verify(data, sig, name, url, catalog=catalog)

    def _try_rotation(self, url: str) -> bool:
        keys_url = urljoin(url, "keys.json")
        try:
            data = self.downloader.fetch_bytes(keys_url, max_bytes=MAX_CHANNEL_BYTES,
                                               what="keys.json")
            sig = self.downloader.fetch_bytes(keys_url + ".minisig", max_bytes=MAX_SIG_BYTES,
                                              what="keys.json.minisig")
        except HarnessError:
            return False
        before = {k.id_hex for k in self.trust.all_keys()}
        try:
            accept_keys_json(self.state, self.trust, data, sig)
        except HarnessError:
            return False
        return {k.id_hex for k in self.trust.all_keys()} != before

    def verify(self, data: bytes, signature: bytes, channel: str, url: str = "", *,
               catalog: str | None = None) -> VerifiedChannel:
        # 1. signature, by a key allowed for this channel
        key, sig = self.trust.verify_for_channel(data, signature, channel)
        # 2. schema
        try:
            doc = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChannelFormatError(f"channel.json is signed but is not JSON: {exc}") from None
        parsed = parse_channel(doc)
        # 3. consistency
        if parsed.channel != channel:
            raise RefusedError(
                f"asked for the {channel!r} channel but the signed file is the "
                f"{parsed.channel!r} channel",
                hint="a channel file is being replayed under another name; refusing it")
        if parsed.signing_key_id != key.id_hex:
            raise RefusedError(
                f"channel.json says it is signed by {parsed.signing_key_id} but key "
                f"{key.id_hex} signed it", hint="the channel is misconfigured; refusing it")
        cat = parsed.catalog_id
        if catalog and cat != catalog:
            raise RefusedError(
                f"asked for the {catalog!r} catalogue but the signed file belongs to {cat!r}",
                hint="a channel of another catalogue is being served here; refusing it")
        # 4. anti-rollback, per (catalogue, channel)
        sha = hashlib.sha256(data).hexdigest()
        self.serials.check(channel, parsed.serial, sha, catalog=cat)
        # 5. expiry: warn only
        warnings = []
        if parsed.expired(self.now()):
            warnings.append(f"the {channel!r} channel expired at {parsed.expires_at}: it may be "
                            "stale (a mirror not updated, or an offline copy)")
        self.serials.accept(channel, parsed.serial, sha, catalog=cat)
        return VerifiedChannel(channel=parsed, url=url, sha256=sha, key_id=key.id_hex,
                               key_role=key.role, trusted_comment=sig.trusted_comment,
                               warnings=tuple(warnings), catalog=cat, data=bytes(data),
                               signature=bytes(signature))
