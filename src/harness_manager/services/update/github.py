"""GitHub Releases as an update source (david U1: private GitHub Releases, with a token).

**The layout** (HM_SELF_UPDATE §4.2 A, HARNESS_DISTRIBUTION §4.1):

- one **rolling release per (catalogue, channel)**, tag ``channel-<catalog>-<channel>``
  (``channel-hm-app-stable``, ``channel-mps3-harness-beta``), holding ``channel.json``,
  its ``.minisig`` and (optionally) ``keys.json(.minisig)``;
- one **release per version** holding the assets (wheel, lock, deps, SD zip, overlays,
  kit). The signed ``channel.json`` names them by their release-download URL
  (``https://github.com/O/R/releases/download/<tag>/<name>``), or by a URL relative to
  ``channel.json`` (``../v0.2.0/<name>``), which resolves to one.

**How a download works.** A private repo does not serve release-download URLs to a
token, so HM never fetches them directly: it looks the asset up through the REST API,
``GET /repos/O/R/releases/tags/<tag>`` (with the token), and downloads the asset's API
URL with ``Accept: application/octet-stream``. GitHub answers with a redirect to signed
storage; the token is an *unredirected* header, so it never goes there. An API asset URL
(``https://api.github.com/repos/O/R/releases/assets/<id>``) in the channel is used as is.

**The token** (``token_provider``): ``$HARNESS_MANAGER_GITHUB_TOKEN``, else
``gh auth token`` when the GitHub CLI is logged in. It is resolved lazily (only when a
GitHub host is about to be asked), sent only to the API host, and never logged, put in a
URL, or echoed in an error.

**A source** is written ``github:OWNER/REPO`` (``--source``, ``$HARNESS_MANAGER_UPDATE_SOURCE``).
Tests point ``api`` at a fake on 127.0.0.1 (``tests/fakes/fake_github.py``); nothing
here reaches github.com in a test.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from harness_manager.core.errors import UsageError

log = logging.getLogger(__name__)

WEB_HOST = "github.com"
DEFAULT_API = "https://api.github.com"
API_ENV = "HARNESS_MANAGER_GITHUB_API"
TOKEN_ENV = "HARNESS_MANAGER_GITHUB_TOKEN"
SOURCE_PREFIX = "github:"
#: david U1: the private HarnessManager repo hosts both catalogues' channels.
DEFAULT_REPO = "SoC-Labs/HarnessManager"
DEFAULT_SOURCE = SOURCE_PREFIX + DEFAULT_REPO

_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_DOWNLOAD_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/"
                          r"(?P<tag>[^/]+)/(?P<name>[^/]+)$")
_API_ASSET_RE = re.compile(r"^(?:/api/v3)?/repos/[^/]+/[^/]+/releases/assets/\d+$")


def default_api() -> str:
    return (os.environ.get(API_ENV, "").strip() or DEFAULT_API).rstrip("/")


def api_host(api: str) -> str:
    return (urlsplit(api).hostname or "").lower()


def release_tag(channel: str, catalog: str | None = None) -> str:
    """The rolling release that holds a channel: ``channel-<catalog>-<channel>``.

    With no catalogue, ``channel-<channel>``: the spelling of a T7-era combined channel.
    """
    return f"channel-{catalog}-{channel}" if catalog else f"channel-{channel}"


@dataclass(frozen=True)
class ReleaseDownload:
    """A ``https://github.com/O/R/releases/download/<tag>/<name>`` URL, taken apart."""

    owner: str
    repo: str
    tag: str
    name: str

    @property
    def url(self) -> str:
        return (f"https://{WEB_HOST}/{self.owner}/{self.repo}/releases/download/"
                f"{quote(self.tag, safe='')}/{quote(self.name, safe='')}")


def parse_release_download(url: str) -> ReleaseDownload | None:
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != WEB_HOST or parts.query:
        return None
    m = _DOWNLOAD_RE.match(parts.path)
    if not m:
        return None
    return ReleaseDownload(m["owner"], m["repo"], unquote(m["tag"]), unquote(m["name"]))


def is_api_asset_url(url: str, api: str) -> bool:
    """Is ``url`` an asset of the REST API ``api`` (``…/repos/O/R/releases/assets/<id>``)?"""
    parts, base = urlsplit(url), urlsplit(api)
    return (parts.scheme, (parts.hostname or "").lower(), parts.port) == \
        (base.scheme, (base.hostname or "").lower(), base.port) and \
        bool(_API_ASSET_RE.match(parts.path))


def parse_source(src: str) -> tuple[str, str] | None:
    """``github:OWNER/REPO`` -> (OWNER/REPO, tag template or ""). None if not that form.

    ``github:OWNER/REPO/TEMPLATE`` names the rolling release another way; ``{catalog}``
    and ``{channel}`` are filled in (``github:o/r/dist-{channel}``).
    """
    if not src.startswith(SOURCE_PREFIX):
        return None
    rest = src[len(SOURCE_PREFIX):].strip().strip("/")
    owner, _, repo_and_more = rest.partition("/")
    repo, _, template = repo_and_more.partition("/")
    owner_repo = f"{owner}/{repo}"
    if not _OWNER_REPO_RE.match(owner_repo):
        raise UsageError(f"{src!r} is not a GitHub source",
                         hint="write it github:OWNER/REPO (e.g. github:SoC-Labs/HarnessManager)")
    return owner_repo, template


def source_url(src: str, channel: str, catalog: str | None = None) -> str | None:
    """Where ``channel.json`` is for a ``github:`` source (a release-download URL), else None."""
    parsed = parse_source(src)
    if parsed is None:
        return None
    owner_repo, template = parsed
    if template:
        tag = template.replace("{channel}", channel).replace("{catalog}", catalog or "")
    else:
        tag = release_tag(channel, catalog)
    owner, repo = owner_repo.split("/", 1)
    return ReleaseDownload(owner, repo, tag, "channel.json").url


# --- the token ---------------------------------------------------------------------------


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
    return subprocess.run(list(argv), capture_output=True, text=True, check=False,
                          timeout=15, stdin=subprocess.DEVNULL, env=env)


def gh_cli_token(*, runner: Runner | None = None, gh: str | None = None) -> str | None:
    """``gh auth token`` (the GitHub CLI's stored credential), or None. Reads local config
    only (no network). Its output is never logged; a failure logs only the exit code."""
    exe = gh or shutil.which("gh")
    if not exe:
        return None
    try:
        res = (runner or _run)([exe, "auth", "token", "--hostname", WEB_HOST])
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("gh auth token did not run (%s)", type(exc).__name__)
        return None
    if res.returncode != 0:
        log.info("gh auth token: exit %s (not logged in?)", res.returncode)
        return None
    tok = (res.stdout or "").strip().splitlines()
    return tok[0].strip() if tok and tok[0].strip() else None


def resolve_token(env: Mapping[str, str] | None = None, *, runner: Runner | None = None,
                  gh: str | None = None) -> str | None:
    """The GitHub token: ``$HARNESS_MANAGER_GITHUB_TOKEN``, else ``gh auth token``."""
    tok = (env if env is not None else os.environ).get(TOKEN_ENV, "").strip()
    return tok or gh_cli_token(runner=runner, gh=gh)
