"""T7: the resumable, hash-checked downloader, against the fake server."""

from __future__ import annotations

import hashlib
import logging

import pytest

from socharness.core.errors import RefusedError, UnavailableError, UnreachableError
from socharness.services.update.download import Downloader
from socharness.services.update.schema import Asset
from tests.fakes.fake_channel import FakeChannelServer

DATA = bytes(range(256)) * 1024          # 256 KiB, several chunks


@pytest.fixture
def server(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        (srv.root / "assets").mkdir(parents=True)
        (srv.root / "private").mkdir(parents=True)
        (srv.root / "assets" / "blob.bin").write_bytes(DATA)
        (srv.root / "private" / "arm.zip").write_bytes(DATA[:1000])
        (srv.root / "assets" / "arm-redirected.zip").write_bytes(DATA[:1000])
        yield srv


def asset(name="blob.bin", data=DATA, url="assets/blob.bin", **kw) -> Asset:
    return Asset(name=name, url=url, sha256=hashlib.sha256(data).hexdigest(), size=len(data), **kw)


def dl(tmp_path, **kw) -> Downloader:
    return Downloader(tmp_path / "cache", **kw)


def test_download_verifies_and_caches(server, tmp_path):
    d = dl(tmp_path)
    path = d.fetch(asset(), base_url=server.base)
    assert path.read_bytes() == DATA and path.name == asset().sha256
    n = len(server.requests)
    assert d.fetch(asset(), base_url=server.base) == path
    assert len(server.requests) == n                     # a verified cache hit asks nothing


def test_a_damaged_cache_entry_is_fetched_again(server, tmp_path):
    d = dl(tmp_path)
    path = d.fetch(asset(), base_url=server.base)
    path.write_bytes(b"x" * len(DATA))
    assert d.fetch(asset(), base_url=server.base).read_bytes() == DATA


def test_a_tampered_asset_is_refused_and_discarded(server, tmp_path):
    (server.root / "assets" / "blob.bin").write_bytes(DATA[:-1] + b"\x00")
    d = dl(tmp_path)
    with pytest.raises(RefusedError, match="fails its sha256 check"):
        d.fetch(asset(), base_url=server.base)
    assert not list((tmp_path / "cache" / "partial").iterdir())
    assert not list((tmp_path / "cache" / "blobs").iterdir())


def test_more_bytes_than_signed_are_refused(server, tmp_path):
    (server.root / "assets" / "blob.bin").write_bytes(DATA + b"extra")
    with pytest.raises(RefusedError, match="more than"):
        dl(tmp_path).fetch(asset(), base_url=server.base)


def test_an_interrupted_download_resumes_with_a_range_request(server, tmp_path):
    server.truncate_once["assets/blob.bin"] = 100_000
    d = dl(tmp_path)
    with pytest.raises(UnreachableError, match="resumes"):
        d.fetch(asset(), base_url=server.base)
    part = tmp_path / "cache" / "partial" / f"{asset().sha256}.part"
    assert part.stat().st_size == 100_000
    assert d.fetch(asset(), base_url=server.base).read_bytes() == DATA
    assert server.requests[-1]["range"] == "bytes=100000-"


def test_a_server_that_ignores_range_restarts_cleanly(server, tmp_path):
    server.truncate_once["assets/blob.bin"] = 100_000
    d = dl(tmp_path)
    with pytest.raises(UnreachableError):
        d.fetch(asset(), base_url=server.base)
    server.ignore_range = True
    assert d.fetch(asset(), base_url=server.base).read_bytes() == DATA


def private_asset() -> Asset:
    return asset(name="overlays-aaa", data=DATA[:1000], url="private/arm.zip",
                 access="github-token", repo="SoC-Labs/mps3-platform-dist-aaa")


def test_a_private_asset_without_a_token_is_unavailable(server, tmp_path):
    with pytest.raises(UnavailableError, match="GitHub token"):
        dl(tmp_path).fetch(private_asset(), base_url=server.base)
    assert not server.requests                           # nothing was even asked


def test_the_token_goes_only_to_allowed_hosts(server, tmp_path):
    d = dl(tmp_path, token=server.token)                 # default hosts: GitHub only
    with pytest.raises(RefusedError, match="refusing to send the GitHub token"):
        d.fetch(private_asset(), base_url=server.base)
    assert not server.requests


def test_a_private_asset_with_a_token(server, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    d = dl(tmp_path, token=server.token, token_hosts=frozenset({"127.0.0.1"}))
    assert d.fetch(private_asset(), base_url=server.base).read_bytes() == DATA[:1000]
    assert server.requests[-1]["auth_value_ok"]
    assert server.token not in caplog.text and server.token not in repr(d)


def test_a_public_asset_never_carries_the_token(server, tmp_path):
    d = dl(tmp_path, token=server.token, token_hosts=frozenset({"127.0.0.1"}))
    d.fetch(asset(), base_url=server.base)
    assert not server.requests[-1]["auth"]


def test_the_token_is_not_forwarded_across_a_redirect(server, tmp_path):
    server.redirect["private/arm.zip"] = server.url("assets/arm-redirected.zip")
    d = dl(tmp_path, token=server.token, token_hosts=frozenset({"127.0.0.1"}))
    d.fetch(private_asset(), base_url=server.base)
    first, second = server.requests[-2:]
    assert first["path"] == "private/arm.zip" and first["auth"]
    assert second["path"] == "assets/arm-redirected.zip" and not second["auth"]


def test_a_wrong_token_is_reported_without_echoing_it(server, tmp_path):
    d = dl(tmp_path, token="ghp_wrong_token_value", token_hosts=frozenset({"127.0.0.1"}))
    with pytest.raises(UnavailableError) as exc:
        d.fetch(private_asset(), base_url=server.base)
    assert "HTTP 401" in str(exc.value) and "ghp_wrong_token_value" not in str(exc.value)


def test_credentials_in_a_url_are_refused(tmp_path):
    with pytest.raises(RefusedError, match="credentials"):
        dl(tmp_path).fetch(asset(url="https://user:pw@example.invalid/x"), base_url="")


def test_a_missing_asset_is_unreachable(server, tmp_path):
    with pytest.raises(UnreachableError, match="HTTP 404"):
        dl(tmp_path).fetch(asset(url="assets/nope.bin"), base_url=server.base)
