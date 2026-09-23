"""Fetch and verify a channel: signature, schema, channel name, serial, expiry.

The order is fixed and every step refuses the whole channel:

1. **signature** over the exact bytes of ``channel.json``, by a key the app
   trusts *for this channel* (``trust.TrustStore``). If the signer is unknown,
   the channel's ``keys.json`` (root-signed rotation) is tried once;
2. **schema** (``schema.parse_channel``), strict;
3. **consistency**: the file names the channel asked for (a signed dev channel
   cannot be replayed as stable) and ``signing_key_id`` is the key that signed it;
4. **anti-rollback**: ``serial`` is not below the last one accepted, and a
   serial is never reused for different content (``state.SerialStore``);
5. **expiry**: past ``expires_at`` is a WARNING, never a block (labs run offline).

Only then is the serial recorded. Asset hashes and domain checks come later,
per component (``bundle.py``).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from socharness.core.errors import HarnessError, RefusedError

from .download import Downloader
from .schema import Channel, ChannelFormatError, parse_channel
from .state import SerialStore, UpdateState
from .trust import TrustStore, UntrustedKeyError, accept_keys_json

SOURCE_ENV = "SOCHARNESS_UPDATE_SOURCE"
DEFAULT_SOURCE = ("https://raw.githubusercontent.com/SoC-Labs/mps3-platform-dist/"
                  "channel/{channel}/channel.json")
DEFAULT_CHANNEL = "stable"
CHANNEL_ENV = "SOCHARNESS_UPDATE_CHANNEL"
MAX_CHANNEL_BYTES = 4 << 20
MAX_SIG_BYTES = 64 << 10


def channel_url(source: str | None, channel: str) -> str:
    """Where ``channel.json`` is: a URL template with ``{channel}``, a base URL ending in
    ``/``, a file URL, or a local directory/file (a mirror)."""
    src = (source or os.environ.get(SOURCE_ENV, "") or DEFAULT_SOURCE).strip()
    if "{channel}" in src:
        src = src.replace("{channel}", channel)
    if "://" not in src:
        path = Path(src).expanduser().resolve()
        if path.is_dir():
            path = path / "channel.json"
        return path.as_uri()
    if src.endswith("/"):
        src += "channel.json"
    return src


def default_channel() -> str:
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


class ChannelClient:
    def __init__(self, state: UpdateState, downloader: Downloader, trust: TrustStore, *,
                 now: Callable[[], float] = time.time) -> None:
        self.state = state
        self.downloader = downloader
        self.trust = trust
        self.serials = SerialStore(state)
        self.now = now

    def fetch(self, channel: str | None = None, source: str | None = None) -> VerifiedChannel:
        name = channel or default_channel()
        url = channel_url(source, name)
        data = self.downloader.fetch_bytes(url, max_bytes=MAX_CHANNEL_BYTES, what="channel.json")
        sig = self.downloader.fetch_bytes(url + ".minisig", max_bytes=MAX_SIG_BYTES,
                                          what="channel.json.minisig")
        try:
            return self.verify(data, sig, name, url)
        except UntrustedKeyError:
            # An unknown signer may be a rotated release key: try the root-signed keys.json once.
            if not self._try_rotation(url):
                raise
            return self.verify(data, sig, name, url)

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

    def verify(self, data: bytes, signature: bytes, channel: str, url: str = "") -> VerifiedChannel:
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
        # 4. anti-rollback
        sha = hashlib.sha256(data).hexdigest()
        self.serials.check(channel, parsed.serial, sha)
        # 5. expiry: warn only
        warnings = []
        if parsed.expired(self.now()):
            warnings.append(f"the {channel!r} channel expired at {parsed.expires_at}: it may be "
                            "stale (a mirror not updated, or an offline copy)")
        self.serials.accept(channel, parsed.serial, sha)
        return VerifiedChannel(channel=parsed, url=url, sha256=sha, key_id=key.id_hex,
                               key_role=key.role, trusted_comment=sig.trusted_comment,
                               warnings=tuple(warnings))
