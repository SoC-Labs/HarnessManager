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
- **The index is fetched with the token too** (OTA-C, P9): ``channel.json``, its
  signature and ``keys.json`` on a GitHub host carry it, so a private repo can host
  the channel. Everywhere else (a mirror, 127.0.0.1) they go without it.
- **GitHub release downloads go through the API** (``github.py``): a
  ``https://github.com/O/R/releases/download/<tag>/<name>`` URL is looked up with
  ``GET /repos/O/R/releases/tags/<tag>`` and fetched from the asset's API URL, which is
  what a private repo serves to a token. An API asset URL carries the token even for an
  asset the channel marks public (a private repo's API needs it for every asset).
- **Mirrors by hash** (H5, P10): before any URL, every mirror is asked for
  ``blobs/<sha256>``: the directories and URLs in ``$HARNESS_MANAGER_UPDATE_MIRRORS``
  (separated by spaces, ``,`` or ``;``), plus the mirror a local channel was read from
  (``--source DIR``). A mirror never has to rewrite a signed URL, and a mirror copy
  that fails its hash is skipped, never used.
- **The token itself** is ``token``, else ``token_provider()`` resolved on first need
  (``github.resolve_token``: ``$HARNESS_MANAGER_GITHUB_TOKEN``, else ``gh auth token``).
- **Sources:** ``https://``, ``http://`` and ``file://`` (a local or hub mirror).
  A URL with credentials in it is refused.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from harness_manager import __version__
from harness_manager.core.errors import (
    HarnessError,
    RefusedError,
    UnavailableError,
    UnreachableError,
)

from . import github
from .schema import ACCESS_TOKEN, Asset


def _file_path(url_path: str) -> Path:
    """A ``file://`` URL's path as a local path. ``url2pathname`` unquotes it and, on
    Windows, turns ``/C:/x`` into ``C:\\x`` (``Path("/C:/x")`` is not a valid path there)."""
    return Path(urllib.request.url2pathname(url_path))

log = logging.getLogger(__name__)

CHUNK = 1 << 16
GITHUB_TOKEN_HOSTS = frozenset({"api.github.com", "github.com", "uploads.github.com"})
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_ENV = github.TOKEN_ENV
MIRRORS_ENV = "HARNESS_MANAGER_UPDATE_MIRRORS"
MAX_API_BYTES = 4 << 20
#: How far above a local ``channel.json`` a mirror root (the dir holding ``blobs/``) may be:
#: ``<root>/<owner>/<repo>/releases/download/<tag>/channel.json`` is six levels down.
MIRROR_ROOT_DEPTH = 6

Progress = Callable[[str, int, int], None]


def resolve_url(base: str, url: str) -> str:
    """An asset URL relative to the channel's own URL, made absolute."""
    return urljoin(base, url) if base else url


def _split(url: str):
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise RefusedError("refusing a URL with credentials in it",
                           hint="give a GitHub token through HARNESS_MANAGER_GITHUB_TOKEN instead")
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


def mirrors_from_env() -> tuple[str, ...]:
    """``$HARNESS_MANAGER_UPDATE_MIRRORS``: directories or URLs, split on spaces, ``,``, ``;``."""
    raw = os.environ.get(MIRRORS_ENV, "")
    return tuple(m for m in re.split(r"[\s,;]+", raw) if m)


def mirror_root_of(channel_url: str) -> str | None:
    """The mirror a local ``channel.json`` sits in: the nearest parent holding ``blobs/``."""
    if not channel_url.startswith("file:"):
        return None
    d = _file_path(urlsplit(channel_url).path).parent
    for _ in range(MIRROR_ROOT_DEPTH):
        if (d / "blobs").is_dir():
            return str(d)
        if d.parent == d:
            break
        d = d.parent
    return None


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
    #: Resolves the token on first need when ``token`` is not given (``github.resolve_token``).
    token_provider: Callable[[], str | None] | None = None
    #: The GitHub REST API base (``github.default_api()``); its host may receive the token.
    github_api: str = field(default_factory=github.default_api)
    #: Mirror roots holding ``blobs/<sha256>`` (dirs, ``file://`` or ``http(s)://``).
    mirrors: tuple[str, ...] = field(default_factory=mirrors_from_env)
    _resolved: str | None = field(default=None, init=False, repr=False)
    _provider_done: bool = field(default=False, init=False, repr=False)
    _releases: dict[tuple[str, str, str], dict[str, str]] = field(
        default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.cache = Path(self.cache)
        (self.cache / "blobs").mkdir(parents=True, exist_ok=True)
        (self.cache / "partial").mkdir(parents=True, exist_ok=True)
        self.github_api = self.github_api.rstrip("/")
        api = github.api_host(self.github_api)
        if api and api not in self.token_hosts:
            self.token_hosts = frozenset(self.token_hosts | {api})

    def __repr__(self) -> str:                    # never show the token
        state = "set" if self.token else ("on demand" if self.token_provider else "unset")
        return f"Downloader(cache={self.cache!s}, token={state})"

    # -- the token --

    def _tok(self) -> str | None:
        """The token, resolving the provider once, on first need. Never logged."""
        if self.token:
            return self.token
        if self.token_provider is not None and not self._provider_done:
            self._provider_done = True
            try:
                self._resolved = self.token_provider() or None
            except Exception as exc:  # noqa: BLE001 - a provider failure means "no token"
                log.info("the GitHub token provider failed (%s)", type(exc).__name__)
                self._resolved = None
        return self._resolved

    def has_token(self) -> bool:
        return bool(self._tok())

    def _token_for(self, host: str) -> str | None:
        """The token for a request to ``host``: only a token host ever gets it."""
        return self._tok() if host.lower() in self.token_hosts else None

    # -- small files: the channel, its signature, keys.json --

    def fetch_bytes(self, url: str, *, max_bytes: int = 4 << 20, what: str = "") -> bytes:
        """A small file whose hash is not known in advance (it is signature-checked).

        On a GitHub host it carries the token (a private repo's channel, P9); a GitHub
        release download is fetched through the API.
        """
        parts = _split(url)
        what = what or _redact(url)
        if parts.scheme == "file":
            path = _file_path(parts.path)
            try:
                size = path.stat().st_size
                if size > max_bytes:
                    raise RefusedError(f"{what} is {size} bytes, more than the {max_bytes} allowed")
                return path.read_bytes()
            except FileNotFoundError:
                raise UnreachableError(f"{what} not found at {path}",
                                       hint="check the channel source") from None
        rd = github.parse_release_download(url)
        if rd is not None:
            url = self._github_asset_url(rd, what)
            parts = _split(url)
        accept = "application/octet-stream" if github.is_api_asset_url(url, self.github_api) \
            else "*/*"
        data = self._get(url, what=what, max_bytes=max_bytes, accept=accept)
        if len(data) > max_bytes:
            raise RefusedError(f"{what} is larger than {max_bytes} bytes; not a channel file")
        return data

    def _get(self, url: str, *, what: str, max_bytes: int, accept: str,
             extra_headers: dict[str, str] | None = None) -> bytes:
        """One small GET: the token only to a token host, unredirected; errors never echo it."""
        parts = _split(url)
        host = parts.hostname or ""
        token = self._token_for(host)
        req = urllib.request.Request(url, headers={"User-Agent": f"harness-manager/{__version__}",
                                                   "Accept": accept, **(extra_headers or {})})
        if token:
            req.add_unredirected_header("Authorization", f"Bearer {token}")
        try:
            with self._opener(host).open(req, timeout=self.timeout_s) as resp:
                self.stats.requests.append({"url": _redact(url), "range_from": 0,
                                            "status": resp.status})
                return resp.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            self.stats.requests.append({"url": _redact(url), "range_from": 0, "status": exc.code})
            raise self._http_error(exc, url, what, token_sent=bool(token)) from None
        except (urllib.error.URLError, OSError) as exc:
            raise UnreachableError(f"cannot fetch {what} from {_redact(url)}: "
                                   f"{getattr(exc, 'reason', exc)}",
                                   hint="check the network, or use a mirror (--source)") from None

    def _http_error(self, exc: urllib.error.HTTPError, url: str, what: str, *,
                    token_sent: bool) -> HarnessError:
        code = exc.code
        host = (urlsplit(url).hostname or "").lower()
        github_host = host in self.token_hosts
        if github_host and code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            return UnreachableError(f"fetching {what}: the GitHub API rate limit is used up",
                                    hint=f"retry later, or set ${TOKEN_ENV} (a token has a "
                                         "higher limit)")
        if github_host and code in (401, 403):
            if token_sent:
                return UnavailableError(what, f"GitHub refused the token (HTTP {code}); it "
                                              "needs read access to the release repo")
            return UnavailableError(what, f"it is private (HTTP {code}); it needs a GitHub "
                                          f"token: set ${TOKEN_ENV}, or log in with `gh auth "
                                          "login`")
        if code == 404:
            hint = "check the channel name and source"
            if github_host:
                hint += ("; a private repo answers 404 to a token without read access" if token_sent
                         else f"; a private repo needs a token (${TOKEN_ENV} or `gh auth login`)")
            return UnreachableError(f"{what} not found ({code}) at {_redact(url)}", hint=hint)
        return UnreachableError(f"fetching {what} failed: HTTP {code}", hint="retry later")

    # -- GitHub releases, through the API --

    def _github_asset_url(self, rd: github.ReleaseDownload, what: str) -> str:
        """The API URL of the asset a release-download URL names (one lookup per release)."""
        key = (rd.owner.lower(), rd.repo.lower(), rd.tag)
        assets = self._releases.get(key)
        if assets is None:
            api = (f"{self.github_api}/repos/{quote(rd.owner, safe='')}/"
                   f"{quote(rd.repo, safe='')}/releases/tags/{quote(rd.tag, safe='')}")
            doc = self._get_json(api, what=f"GitHub release {rd.tag} of {rd.owner}/{rd.repo}")
            assets = _asset_urls(doc.get("assets"))
            more = doc.get("assets_url")
            if isinstance(more, str) and len(assets) >= 30:
                for page in range(1, 11):          # every asset, not only the first 30
                    got = self._get_json(f"{more}?per_page=100&page={page}",
                                         what=f"the assets of GitHub release {rd.tag}")
                    page_assets = _asset_urls(got if isinstance(got, list) else [])
                    assets.update(page_assets)
                    if len(page_assets) < 100:
                        break
            self._releases[key] = assets
        url = assets.get(rd.name)
        if url is None:
            raise UnreachableError(f"{what}: GitHub release {rd.tag} of {rd.owner}/{rd.repo} has "
                                   f"no asset {rd.name}",
                                   hint="check the release was published completely")
        if not github.is_api_asset_url(url, self.github_api):
            # Never send the token where a JSON reply points: only to the API itself.
            raise RefusedError(f"{what}: GitHub named an asset URL outside its API "
                               f"({_redact(url)})", hint="refusing it; report the release")
        return url

    def _get_json(self, url: str, *, what: str) -> Any:
        data = self._get(url, what=what, max_bytes=MAX_API_BYTES,
                         accept="application/vnd.github+json",
                         extra_headers={"X-GitHub-Api-Version": "2022-11-28"})
        try:
            return json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise UnreachableError(f"{what}: the GitHub API answered something that is not JSON",
                                   hint="retry later") from None

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
        """Download ``asset`` (resuming if interrupted), verify it, return the cached path.

        Tried in order: the cache, every mirror by sha256, then the asset's URL.
        """
        hit = self.cached(asset)
        if hit is not None:
            if progress:
                progress(f"download:{asset.name}", asset.size, asset.size)
            return hit
        for root in self.mirror_roots(base_url):
            blob = self._from_mirror(root, asset, progress)
            if blob is not None:
                return blob
        url = resolve_url(base_url, asset.url)
        parts = _split(url)
        headers = {"User-Agent": f"harness-manager/{__version__}", "Accept": "application/octet-stream"}
        # A private asset needs the token from a host; a local copy (file://: a dry-run tree,
        # a mirror) is read as it is, since nothing leaves the machine.
        private = asset.access == ACCESS_TOKEN and parts.scheme != "file"
        if private and not self._tok():
            raise UnavailableError(
                f"component {asset.name}",
                f"it is private{f' (in {asset.repo})' if asset.repo else ''}; it needs a GitHub "
                f"token with read access, in ${TOKEN_ENV} (or `gh auth login`)")
        rd = github.parse_release_download(url) if parts.scheme != "file" else None
        if rd is not None:
            url = self._github_asset_url(rd, asset.name)
            parts = _split(url)
        host = (parts.hostname or "").lower()
        token = None
        if private:
            if host not in self.token_hosts:
                raise RefusedError(
                    f"refusing to send the GitHub token to {host or 'a file URL'} for {asset.name}",
                    hint="a private asset must be served by GitHub (api.github.com)")
            token = self._tok()
        elif host in self.token_hosts and github.is_api_asset_url(url, self.github_api):
            token = self._tok()               # a private repo's API wants it for every asset
        part = self.cache / "partial" / f"{asset.sha256}.part"
        if parts.scheme == "file":
            self._copy_file(_file_path(parts.path), part, asset, progress)
        else:
            self._http(url, parts.hostname or "", headers, token, part, asset, progress)
        return self._finish(part, asset)

    # -- mirrors (H5): by sha256, before any URL --

    def mirror_roots(self, base_url: str = "") -> list[str]:
        """The mirror a local channel was read from, then ``mirrors`` (the environment)."""
        roots: list[str] = []
        derived = mirror_root_of(base_url) if base_url else None
        for r in ((derived,) if derived else ()) + tuple(self.mirrors):
            if r and r not in roots:
                roots.append(r)
        return roots

    def _from_mirror(self, root: str, asset: Asset, progress: Progress | None) -> Path | None:
        """``<root>/blobs/<sha256>``, verified, into the cache; None when the mirror lacks it
        (or holds a copy that is not the signed one: that copy is skipped, never used)."""
        part = self.cache / "partial" / f"{asset.sha256}.part"
        where = _redact(root)
        try:
            if "://" in root and not root.startswith("file:"):
                parts = _split(root)
                url = root.rstrip("/") + "/blobs/" + asset.sha256
                headers = {"User-Agent": f"harness-manager/{__version__}",
                           "Accept": "application/octet-stream"}
                self._http(url, parts.hostname or "", headers, None, part, asset, progress)
            else:
                base = _file_path(urlsplit(root).path) if root.startswith("file:") else Path(root)
                src = base.expanduser() / "blobs" / asset.sha256
                if not src.is_file():
                    return None
                self._copy_file(src, part, asset, progress)
            blob = self._finish(part, asset)
        except HarnessError as exc:
            log.info("mirror %s has no usable copy of %s: %s", where, asset.name, exc.message)
            if isinstance(exc, RefusedError):
                part.unlink(missing_ok=True)
            return None
        log.info("%s came from the mirror %s", asset.name, where)
        return blob

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


def _asset_urls(assets: Any) -> dict[str, str]:
    """name -> API url, from a GitHub release's ``assets`` list."""
    out: dict[str, str] = {}
    for a in assets if isinstance(assets, list) else []:
        if isinstance(a, dict) and isinstance(a.get("name"), str) and isinstance(a.get("url"), str):
            out[a["name"]] = a["url"]
    return out


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
