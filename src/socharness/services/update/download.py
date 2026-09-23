"""A resumable, hash-checked downloader with a content-addressed cache.

Rules:

- **The hash is the name.** A finished download lives at
  ``cache/blobs/<sha256>`` and only once its size and sha256 match what the
  signed channel says. A cached blob is re-hashed before reuse; a damaged one
  is fetched again.
- **Interrupted is not failed.** Bytes land in ``cache/partial/<sha256>.part``.
  The next attempt asks for the rest with ``Range: bytes=N-``; a server that
  answers 200 instead of 206 starts the file over. A server that sends more
  bytes than the channel declared is cut off and refused.
- **The GitHub token is for GitHub only.** Private (``access: github-token``)
  assets are fetched with ``Authorization: Bearer <token>``, sent only to the
  hosts in ``token_hosts`` and added as an *unredirected* header, so the
  redirect GitHub answers with (to a signed storage URL) never carries it.
  The token is never logged, never put in a URL, never in an error message.
- **Sources:** ``https://``, ``http://`` and ``file://`` (a local or hub mirror).
  A URL with credentials in it is refused.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import logging
import os
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from socharness import __version__
from socharness.core.errors import RefusedError, UnavailableError, UnreachableError

from .schema import ACCESS_TOKEN, Asset

log = logging.getLogger(__name__)

CHUNK = 1 << 16
GITHUB_TOKEN_HOSTS = frozenset({"api.github.com", "github.com", "uploads.github.com"})
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_ENV = "SOCHARNESS_GITHUB_TOKEN"

Progress = Callable[[str, int, int], None]


def resolve_url(base: str, url: str) -> str:
    """An asset URL relative to the channel's own URL, made absolute."""
    return urljoin(base, url) if base else url


def _split(url: str):
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise RefusedError("refusing a URL with credentials in it",
                           hint="give a GitHub token through SOCHARNESS_GITHUB_TOKEN instead")
    if parts.scheme not in ("https", "http", "file"):
        raise RefusedError(f"unsupported URL scheme {parts.scheme!r}",
                           hint="use https://, http:// or file://")
    return parts


def _redact(url: str) -> str:
    """The URL without its query string (signed storage URLs carry tokens there)."""
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}" if p.scheme != "file" else url


def token_from_env() -> str | None:
    tok = os.environ.get(TOKEN_ENV, "").strip()
    return tok or None


@dataclass
class DownloadStats:
    requests: list[dict] = field(default_factory=list)   # {url, range_from, status} per request


@dataclass
class Downloader:
    cache: Path
    token: str | None = None
    token_hosts: frozenset[str] = GITHUB_TOKEN_HOSTS
    timeout_s: float = 30.0
    stats: DownloadStats = field(default_factory=DownloadStats)

    def __post_init__(self) -> None:
        self.cache = Path(self.cache)
        (self.cache / "blobs").mkdir(parents=True, exist_ok=True)
        (self.cache / "partial").mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:                    # never show the token
        return f"Downloader(cache={self.cache!s}, token={'set' if self.token else 'unset'})"

    # -- small files: the channel, its signature, keys.json --

    def fetch_bytes(self, url: str, *, max_bytes: int = 4 << 20, what: str = "") -> bytes:
        """A small file whose hash is not known in advance (it is signature-checked)."""
        parts = _split(url)
        what = what or url
        if parts.scheme == "file":
            path = Path(unquote(parts.path))
            try:
                size = path.stat().st_size
                if size > max_bytes:
                    raise RefusedError(f"{what} is {size} bytes, more than the {max_bytes} allowed")
                return path.read_bytes()
            except FileNotFoundError:
                raise UnreachableError(f"{what} not found at {path}",
                                       hint="check the channel source") from None
        req = urllib.request.Request(url, headers={"User-Agent": f"socharness/{__version__}"})
        try:
            with self._opener(parts.hostname or "").open(req, timeout=self.timeout_s) as resp:
                self.stats.requests.append({"url": _redact(url), "range_from": 0,
                                            "status": resp.status})
                data = resp.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            self.stats.requests.append({"url": _redact(url), "range_from": 0, "status": exc.code})
            if exc.code == 404:
                raise UnreachableError(f"{what} not found ({exc.code}) at {_redact(url)}",
                                       hint="check the channel name and source") from None
            raise UnreachableError(f"fetching {what} failed: HTTP {exc.code}",
                                   hint="retry later") from None
        except (urllib.error.URLError, OSError) as exc:
            raise UnreachableError(f"cannot fetch {what} from {_redact(url)}: "
                                   f"{getattr(exc, 'reason', exc)}",
                                   hint="check the network, or use a mirror (--source)") from None
        if len(data) > max_bytes:
            raise RefusedError(f"{what} is larger than {max_bytes} bytes; not a channel file")
        return data

    # -- assets: hash known in advance --

    def cached(self, asset: Asset) -> Path | None:
        blob = self.cache / "blobs" / asset.sha256
        if blob.is_file() and blob.stat().st_size == asset.size and _sha256(blob) == asset.sha256:
            return blob
        if blob.exists():
            log.warning("cached %s failed its hash check; fetching it again", asset.name)
            with contextlib.suppress(OSError):
                blob.unlink()
        return None

    def fetch(self, asset: Asset, *, base_url: str = "", progress: Progress | None = None) -> Path:
        """Download ``asset`` (resuming if interrupted), verify it, return the cached path."""
        hit = self.cached(asset)
        if hit is not None:
            if progress:
                progress(f"download:{asset.name}", asset.size, asset.size)
            return hit
        url = resolve_url(base_url, asset.url)
        parts = _split(url)
        headers = {"User-Agent": f"socharness/{__version__}", "Accept": "application/octet-stream"}
        token = None
        if asset.access == ACCESS_TOKEN:
            if not self.token:
                raise UnavailableError(
                    f"component {asset.name}",
                    f"it is private (Arm Academic Access IP{f' in {asset.repo}' if asset.repo else ''}); "
                    f"it needs a GitHub token with read access, in ${TOKEN_ENV}")
            host = (parts.hostname or "").lower()
            if host not in self.token_hosts:
                raise RefusedError(
                    f"refusing to send the GitHub token to {host or 'a file URL'} for {asset.name}",
                    hint="a private asset must be served by GitHub (api.github.com)")
            token = self.token
        part = self.cache / "partial" / f"{asset.sha256}.part"
        if parts.scheme == "file":
            self._copy_file(Path(unquote(parts.path)), part, asset, progress)
        else:
            self._http(url, parts.hostname or "", headers, token, part, asset, progress)
        return self._finish(part, asset)

    # -- internals --

    def _opener(self, host: str) -> urllib.request.OpenerDirector:
        handlers: list = []
        if host.lower() in LOOPBACK:
            handlers.append(urllib.request.ProxyHandler({}))   # never proxy the loopback
        return urllib.request.build_opener(*handlers)

    def _http(self, url: str, host: str, headers: dict[str, str], token: str | None,
              part: Path, asset: Asset, progress: Progress | None) -> None:
        for attempt in range(2):
            have = part.stat().st_size if part.is_file() else 0
            if have > asset.size:
                part.unlink()
                have = 0
            if have == asset.size:
                return                                  # complete; _finish checks it
            req = urllib.request.Request(url, headers=headers)
            if have:
                req.add_header("Range", f"bytes={have}-")
            if token:
                req.add_unredirected_header("Authorization", f"Bearer {token}")
            try:
                resp = self._opener(host).open(req, timeout=self.timeout_s)
            except urllib.error.HTTPError as exc:
                self.stats.requests.append({"url": _redact(url), "range_from": have,
                                            "status": exc.code})
                if exc.code == 416 and attempt == 0:
                    part.unlink(missing_ok=True)        # our partial is not a prefix: start over
                    continue
                if exc.code in (401, 403, 404) and token:
                    raise UnavailableError(
                        f"component {asset.name}",
                        f"GitHub refused the token (HTTP {exc.code}); it needs read access to "
                        f"{asset.repo or 'the private release repo'}") from None
                raise UnreachableError(f"downloading {asset.name} failed: HTTP {exc.code} "
                                       f"from {_redact(url)}", hint="retry later") from None
            except (urllib.error.URLError, OSError) as exc:
                raise UnreachableError(
                    f"cannot download {asset.name} from {_redact(url)}: "
                    f"{getattr(exc, 'reason', exc)}",
                    hint="check the network; a retry resumes where it stopped") from None
            with resp:
                status = resp.status
                self.stats.requests.append({"url": _redact(url), "range_from": have,
                                            "status": status})
                if status == 206:
                    start = _content_range_start(resp.headers.get("Content-Range", ""))
                    if start != have:
                        part.unlink(missing_ok=True)
                        if attempt == 0:
                            continue
                        raise UnreachableError(f"{asset.name}: the server resumed at the wrong "
                                               "offset", hint="retry")
                    mode = "ab"
                elif status == 200:
                    have, mode = 0, "wb"
                else:
                    raise UnreachableError(f"downloading {asset.name}: unexpected HTTP {status}")
                self._stream(resp, part, mode, have, asset, progress)
            return

    def _stream(self, resp, part: Path, mode: str, have: int, asset: Asset,
                progress: Progress | None) -> None:
        done = have
        label = f"download:{asset.name}"
        try:
            with open(part, mode) as out:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    if done + len(chunk) > asset.size:
                        out.close()
                        part.unlink(missing_ok=True)
                        raise RefusedError(
                            f"{asset.name}: the server sent more than the {asset.size} bytes "
                            "the signed channel declares", hint="the asset is not the one signed")
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(label, done, asset.size)
                out.flush()
                os.fsync(out.fileno())
        except (OSError, http.client.HTTPException) as exc:
            raise UnreachableError(
                f"download of {asset.name} interrupted at {done}/{asset.size} bytes: {exc}",
                hint="run the same command again: it resumes where it stopped") from None

    def _copy_file(self, src: Path, part: Path, asset: Asset, progress: Progress | None) -> None:
        if not src.is_file():
            raise UnreachableError(f"{asset.name} not found at {src}",
                                   hint="check the mirror directory")
        if src.stat().st_size != asset.size:
            raise RefusedError(f"{asset.name} is {src.stat().st_size} bytes, the signed channel "
                               f"says {asset.size}", hint="the mirror copy is not the one signed")
        shutil.copyfile(src, part)
        if progress:
            progress(f"download:{asset.name}", asset.size, asset.size)

    def _finish(self, part: Path, asset: Asset) -> Path:
        size = part.stat().st_size if part.is_file() else 0
        if size != asset.size:
            raise UnreachableError(f"download of {asset.name} is incomplete ({size}/{asset.size})",
                                   hint="run the same command again: it resumes")
        actual = _sha256(part)
        if actual != asset.sha256:
            part.unlink(missing_ok=True)
            raise RefusedError(
                f"{asset.name} fails its sha256 check (expected {asset.sha256[:12]}…, "
                f"got {actual[:12]}…): it is not the file the channel signed",
                hint="nothing was installed; the damaged download was deleted")
        blob = self.cache / "blobs" / asset.sha256
        os.replace(part, blob)
        return blob


def _content_range_start(value: str) -> int:
    # "bytes 100-199/200"
    try:
        unit, rng = value.split(" ", 1)
        return int(rng.split("-", 1)[0]) if unit == "bytes" else -1
    except (ValueError, IndexError):
        return -1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


sha256_file = _sha256
