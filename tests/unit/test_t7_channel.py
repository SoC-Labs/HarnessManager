"""T7: fetching and verifying a channel from the fake server. Every refusal has its twin."""

from __future__ import annotations

import json

import pytest

from socharness.core.errors import RefusedError, UnreachableError
from socharness.services.update.channel import ChannelClient, channel_url
from socharness.services.update.download import Downloader
from socharness.services.update.state import UpdateState
from tests.fakes.fake_channel import ChannelBuilder, FakeChannelServer, TestKeys
from tests.fakes.t7_bundles import Release

KEYS = TestKeys()


@pytest.fixture
def server(tmp_path):
    with FakeChannelServer(tmp_path / "www") as srv:
        yield srv


@pytest.fixture
def builder(server, tmp_path):
    b = ChannelBuilder(server.root, KEYS)
    Release("1.1.0").add_to(b, tmp_path / "art")
    return b


def client(tmp_path, trust=None, now=None) -> ChannelClient:
    state = UpdateState(tmp_path / "state" / "update")
    kw = {"now": now} if now else {}
    return ChannelClient(state, Downloader(state.cache), trust or KEYS.trust(), **kw)


def test_a_signed_channel_verifies(server, builder, tmp_path):
    builder.publish(serial=7)
    v = client(tmp_path).fetch("stable", server.source())
    assert v.channel.serial == 7 and v.key_id == KEYS.release.public.id_hex
    assert v.channel.harness_release().version == "1.1.0" and not v.warnings


def test_a_tampered_channel_is_refused(server, builder, tmp_path):
    path = builder.publish(serial=7)
    path.write_bytes(path.read_bytes().replace(b'"serial": 7', b'"serial": 8'))
    with pytest.raises(RefusedError, match="fails its signature check"):
        client(tmp_path).fetch("stable", server.source())


def test_a_channel_signed_by_an_untrusted_key_is_refused(server, builder, tmp_path):
    doc = builder.document(channel="stable", serial=7, key=KEYS.rogue)
    builder.publish(doc=doc, key=KEYS.rogue)
    with pytest.raises(RefusedError, match="does not trust"):
        client(tmp_path).fetch("stable", server.source())


def test_a_missing_signature_is_unreachable(server, builder, tmp_path):
    path = builder.publish(serial=7)
    path.with_name("channel.json.minisig").unlink()
    with pytest.raises(UnreachableError, match="minisig"):
        client(tmp_path).fetch("stable", server.source())


def test_a_rollback_to_a_lower_serial_is_refused(server, builder, tmp_path):
    c = client(tmp_path)
    builder.publish(serial=7)
    c.fetch("stable", server.source())
    builder.publish(serial=6)
    with pytest.raises(RefusedError, match="older than serial 7"):
        c.fetch("stable", server.source())


def test_a_newer_serial_is_accepted(server, builder, tmp_path):
    c = client(tmp_path)
    builder.publish(serial=7)
    c.fetch("stable", server.source())
    builder.publish(serial=8)
    assert c.fetch("stable", server.source()).channel.serial == 8


def test_the_same_serial_with_other_content_is_refused(server, builder, tmp_path):
    c = client(tmp_path)
    builder.publish(serial=7)
    c.fetch("stable", server.source())
    builder.board["part"] = "xcvu9p"
    builder.publish(serial=7)
    with pytest.raises(RefusedError, match="reuses serial 7"):
        c.fetch("stable", server.source())


def test_the_same_file_again_is_fine(server, builder, tmp_path):
    c = client(tmp_path)
    builder.publish(serial=7)
    c.fetch("stable", server.source())
    assert c.fetch("stable", server.source()).channel.serial == 7


def test_a_channel_replayed_under_another_name_is_refused(server, builder, tmp_path):
    # A genuine, signed dev channel served at the stable URL.
    doc = builder.document(channel="dev", serial=7, key=KEYS.release)
    builder.publish(channel="stable", doc=doc)
    with pytest.raises(RefusedError, match="is the 'dev' channel"):
        client(tmp_path).fetch("stable", server.source())


def test_signing_key_id_must_name_the_signer(server, builder, tmp_path):
    doc = builder.document(channel="stable", serial=7, key=KEYS.release)
    doc["signing_key_id"] = KEYS.app_ci.public.id_hex
    builder.publish(doc=doc)
    with pytest.raises(RefusedError, match="says it is signed by"):
        client(tmp_path).fetch("stable", server.source())


def test_an_expired_channel_warns_but_does_not_block(server, builder, tmp_path):
    builder.publish(serial=7, expires_at="2026-01-01T00:00:00Z")
    v = client(tmp_path).fetch("stable", server.source())
    assert v.channel.serial == 7 and "expired" in v.warnings[0]


def test_signed_but_malformed_is_refused(server, builder, tmp_path):
    builder.publish(raw=json.dumps({"schema": "socharness-channel", "schema_version": 1}).encode())
    with pytest.raises(RefusedError, match="channel.json"):
        client(tmp_path).fetch("stable", server.source())


def test_a_rotated_release_key_is_accepted_through_a_root_signed_keys_json(server, builder,
                                                                           tmp_path):
    doc = builder.document(channel="stable", serial=7, key=KEYS.rotated)
    builder.publish(doc=doc, key=KEYS.rotated)
    builder.publish_keys([{"public_key": KEYS.rotated.public.to_base64(),
                           "role": "harness-release", "channels": ["stable"]}])
    v = client(tmp_path).fetch("stable", server.source())
    assert v.key_id == KEYS.rotated.public.id_hex


def test_a_rotation_not_signed_by_root_does_not_help(server, builder, tmp_path):
    doc = builder.document(channel="stable", serial=7, key=KEYS.rotated)
    builder.publish(doc=doc, key=KEYS.rotated)
    builder.publish_keys([{"public_key": KEYS.rotated.public.to_base64(),
                           "role": "harness-release", "channels": ["stable"]}], key=KEYS.rogue)
    with pytest.raises(RefusedError, match="does not trust"):
        client(tmp_path).fetch("stable", server.source())


def test_a_local_mirror_directory_works_offline(builder, tmp_path):
    builder.publish(serial=7)
    mirror = builder.root / "channel" / "stable"
    v = client(tmp_path).fetch("stable", str(mirror))
    assert v.url.startswith("file://") and v.channel.serial == 7


def test_channel_url_forms(tmp_path):
    assert channel_url("https://h/x/{channel}/channel.json", "beta") == "https://h/x/beta/channel.json"
    assert channel_url("https://h/mirror/", "beta") == "https://h/mirror/channel.json"
    assert channel_url(str(tmp_path), "beta") == (tmp_path / "channel.json").as_uri()
    assert "raw.githubusercontent.com" in channel_url(None, "stable")
