"""OTA-C (P10, H5): a mirror serves assets by their signed sha256, before any URL.

The mirror layout is the release tool's (GitHub's URL layout + ``blobs/<sha256>``);
``mirror.write_mirror`` writes it from a verified channel. Every check has a twin:
a mirror without blobs, a damaged blob, Arm-IP kept out by default. 127.0.0.1 only.
"""

from __future__ import annotations

import logging
import shutil

import pytest

from harness_manager.core.errors import UnreachableError
from harness_manager.services.update.channel import ChannelClient, channel_url
from harness_manager.services.update.download import (
    Downloader,
    mirror_root_of,
    mirrors_from_env,
)
from harness_manager.services.update.mirror import write_mirror
from harness_manager.services.update.state import UpdateState
from tests.fakes.fake_channel import FakeChannelServer, TestKeys
from tests.spikes.harness_dist_publish import (
    ReleaseStore,
    build_release,
    channel_document,
    deterministic_zip,
    publish_channel,
)
from tests.spikes.harness_dist_spike import S_ILA, U_ILA, bare_metal_mint

KEYS = TestKeys()


def client(tmp_path, name: str = "c", **kw) -> ChannelClient:
    state = UpdateState(tmp_path / name / "update")
    kw.setdefault("mirrors", ())
    return ChannelClient(state, Downloader(state.cache, **kw), KEYS.trust())


def origin_release(tmp_path, srv: FakeChannelServer, *, with_aaa: bool = False) -> dict:
    """A channel whose assets have ABSOLUTE URLs on the origin, as GitHub Releases would."""
    mint = bare_metal_mint(tmp_path / "m", S_ILA, U_ILA, "0e12a0b0", ["windowed"], "0.12")
    if not with_aaa:
        mint.overlays_aaa = {}
    mint.kit_zip = deterministic_zip({"kit.json": b"{}"})
    store = ReleaseStore(srv.root, url_base=srv.base)
    rel = build_release(store, "1.1.1", mint, with_kit=True)
    doc = channel_document("stable", 1, KEYS.release, [rel], "1.1.1")
    doc["catalog"] = "mps3-harness"
    publish_channel(store, doc, KEYS.release)
    return rel


def fetch_all(c: ChannelClient, source: str) -> tuple[list[bytes], object]:
    v = c.fetch("stable", source, catalog="mps3-harness")
    rel = v.channel.harness_release()
    return [c.downloader.fetch(comp.asset, base_url=v.url).read_bytes()
            for comp in rel.components if comp.asset.access == "public"], v


def test_a_mirror_serves_absolute_url_assets_by_sha_with_the_origin_gone(tmp_path):
    with FakeChannelServer(tmp_path / "origin") as srv:
        origin_release(tmp_path, srv)
        writer = client(tmp_path, "w")
        v = writer.fetch("stable", srv.source(), catalog="mps3-harness")
        report = write_mirror(v, writer.downloader, tmp_path / "mirror")
    # the origin is gone; the mirror has the exact signed bytes and every blob
    assert (report.channel_dir / "channel.json").read_bytes() == v.data
    assert report.channel_dir.relative_to(tmp_path / "mirror").parts[:4] == \
        ("SoC-Labs", "HarnessManager", "releases", "download")
    got, v2 = fetch_all(client(tmp_path, "reader"), str(tmp_path / "mirror"))   # --source DIR
    assert len(got) == 3 and v2.channel.serial == 1                 # sd, overlays, kit
    assert mirror_root_of(v2.url) == str(tmp_path / "mirror")


def test_twin_a_plain_copy_without_blobs_still_goes_to_the_dead_origin(tmp_path):
    with FakeChannelServer(tmp_path / "origin") as srv:
        origin_release(tmp_path, srv)
    shutil.copytree(tmp_path / "origin", tmp_path / "copy")          # the spike's P10 mirror
    with pytest.raises(UnreachableError, match="cannot download"):
        fetch_all(client(tmp_path), str(tmp_path / "copy") + "/channel/{channel}/channel.json")


def test_a_damaged_blob_is_skipped_never_used(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    with FakeChannelServer(tmp_path / "origin") as srv:
        origin_release(tmp_path, srv)
        w = client(tmp_path, "w")
        v = w.fetch("stable", srv.source(), catalog="mps3-harness")
        write_mirror(v, w.downloader, tmp_path / "mirror")
        sd = v.channel.harness_release().components[0].asset
        blob = tmp_path / "mirror" / "blobs" / sd.sha256
        data = blob.read_bytes()
        blob.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))          # same size, other bytes
        r = client(tmp_path, "r", mirrors=(str(tmp_path / "mirror"),))
        before = len(srv.requests)
        got = r.downloader.fetch(sd, base_url=v.url)                  # twin: the origin serves it
        assert got.read_bytes() == data
        assert [q["path"] for q in srv.requests[before:]] == [sd.url.split("/", 3)[3]]
    assert "no usable copy" in caplog.text


def test_mirrors_from_the_environment_serve_a_github_channels_assets(tmp_path, monkeypatch):
    with FakeChannelServer(tmp_path / "origin") as srv:
        origin_release(tmp_path, srv)
        w = client(tmp_path, "w")
        v = w.fetch("stable", srv.source(), catalog="mps3-harness")
        write_mirror(v, w.downloader, tmp_path / "hub")
        for p in (tmp_path / "origin" / "assets").rglob("*"):
            if p.is_file():
                p.unlink()                                        # the origin lost its assets
        monkeypatch.setenv("HARNESS_MANAGER_UPDATE_MIRRORS", f"{tmp_path / 'nope'}, {tmp_path / 'hub'}")
        before = len(srv.requests)
        assert mirrors_from_env() == (str(tmp_path / "nope"), str(tmp_path / "hub"))
        state = UpdateState(tmp_path / "r" / "update")
        reader = ChannelClient(state, Downloader(state.cache), KEYS.trust())  # env default
        got, _ = fetch_all(reader, srv.source())
        assert len(got) == 3
        assert all(r["path"].endswith(("channel.json", ".minisig")) for r in srv.requests[before:])


def test_an_http_mirror_is_asked_for_blobs(tmp_path):
    with FakeChannelServer(tmp_path / "origin") as srv:
        origin_release(tmp_path, srv)
        w = client(tmp_path, "w")
        v = w.fetch("stable", srv.source(), catalog="mps3-harness")
        write_mirror(v, w.downloader, tmp_path / "hub")
    with FakeChannelServer(tmp_path / "hub") as hub:                  # the hub serves its dir
        r = client(tmp_path, "r", mirrors=(hub.base,))
        rel = v.channel.harness_release()
        assert r.downloader.fetch(rel.components[0].asset, base_url=v.url).is_file()
        assert hub.paths() == [f"blobs/{rel.components[0].asset.sha256}"]


def test_arm_ip_stays_out_of_a_mirror_unless_asked(tmp_path):
    with FakeChannelServer(tmp_path / "origin") as srv:
        origin_release(tmp_path, srv, with_aaa=True)
        w = client(tmp_path, "w", token=srv.token, token_hosts=frozenset({"127.0.0.1"}))
        v = w.fetch("stable", srv.source(), catalog="mps3-harness")
        aaa = next(c.asset for c in v.channel.harness_release().components if c.ip_class == "arm-aaa")
        rep = write_mirror(v, w.downloader, tmp_path / "open")
        assert aaa.name in rep.skipped and not (tmp_path / "open" / "blobs" / aaa.sha256).exists()
        rep2 = write_mirror(v, w.downloader, tmp_path / "lab", include_private=True)   # twin
        assert aaa.sha256 in rep2.blobs


def test_source_dir_finds_the_channel_in_the_github_layout(tmp_path):
    root = tmp_path / "m"
    d = root / "SoC-Labs" / "HarnessManager" / "releases" / "download"
    for tag in ("channel-hm-app-beta", "channel-mps3-harness-beta"):
        (d / tag).mkdir(parents=True)
        (d / tag / "channel.json").write_text("{}")
    assert channel_url(str(root), "beta", "hm-app").endswith("channel-hm-app-beta/channel.json")
    assert channel_url(str(root), "beta", "mps3-harness").endswith(
        "channel-mps3-harness-beta/channel.json")
    # twin: two catalogues and none named -> not guessed (the plain dir form, which is absent)
    assert channel_url(str(root), "beta") == (root / "channel.json").as_uri()
