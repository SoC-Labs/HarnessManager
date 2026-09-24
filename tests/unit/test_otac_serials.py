"""OTA-C (P8): anti-rollback serials keyed by (catalogue, channel), migrated from T7's
channel-only keys. Every check has a negative twin. No network: file sources only.
"""

from __future__ import annotations

import json

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager.services.update.channel import ChannelClient
from harness_manager.services.update.download import Downloader
from harness_manager.services.update.schema import (
    CATALOG_APP,
    LEGACY_CATALOG,
    derive_catalog,
    parse_channel,
)
from harness_manager.services.update.state import SerialStore, UpdateState
from tests.fakes.fake_channel import AssetFile, ChannelBuilder, TestKeys
from tests.fakes.t7_bundles import Release

KEYS = TestKeys()


def client(tmp_path) -> ChannelClient:
    state = UpdateState(tmp_path / "state" / "update")
    return ChannelClient(state, Downloader(state.cache, mirrors=()), KEYS.trust())


def publish(root, *, serial: int, pack: str = "mps3", catalog: str | None = None,
            app_only: bool = False, channel: str = "stable") -> str:
    """A signed channel under ``root``; returns its --source path."""
    b = ChannelBuilder(root, KEYS)
    b.board["pack"] = pack
    if catalog is not None:
        b.extra["catalog"] = catalog
    if app_only:
        b.add_app("0.2.0", AssetFile("harness_manager-0.2.0-py3-none-any.whl", b"PK-w"))
    else:
        Release("1.1.0").add_to(b, root / "art")
    b.publish(channel=channel, serial=serial)
    return str(root / "channel" / channel / "channel.json")


# --- two catalogues sharing a channel name ---------------------------------------------------


def test_two_catalogues_with_one_channel_name_no_longer_collide(tmp_path):
    c = client(tmp_path)
    mps3 = publish(tmp_path / "mps3", serial=5, catalog="mps3-harness")
    kr = publish(tmp_path / "kr260", serial=2, pack="kr260", catalog="kr260-harness")
    assert c.fetch("stable", mps3).catalog == "mps3-harness"
    v = c.fetch("stable", kr)                              # was: refused as a rollback (P8)
    assert v.catalog == "kr260-harness" and v.channel.serial == 2
    doc = json.loads(c.state.serials.read_text())
    assert doc["format"] == 2
    assert doc["catalogs"]["mps3-harness"]["stable"]["serial"] == 5
    assert doc["catalogs"]["kr260-harness"]["stable"]["serial"] == 2


def test_twin_a_rollback_within_one_catalogue_is_still_refused(tmp_path):
    c = client(tmp_path)
    c.fetch("stable", publish(tmp_path / "a", serial=2, pack="kr260", catalog="kr260-harness"))
    with pytest.raises(RefusedError, match="older than serial 2"):
        c.fetch("stable", publish(tmp_path / "b", serial=1, pack="kr260", catalog="kr260-harness"))


def test_a_document_without_a_catalogue_is_keyed_by_its_board_pack(tmp_path):
    c = client(tmp_path)
    assert c.fetch("stable", publish(tmp_path / "m", serial=5)).catalog == "mps3-harness"
    assert c.fetch("stable", publish(tmp_path / "k", serial=2, pack="kr260")).catalog == \
        "kr260-harness"                                   # the spike's P8 documents: no field
    assert c.fetch("stable", publish(tmp_path / "a", serial=1, app_only=True)).catalog == \
        CATALOG_APP


def test_derive_catalog():
    assert derive_catalog("mps3", has_harness=True, has_app=True) == "mps3-harness"
    assert derive_catalog("kr260", has_harness=True, has_app=False) == "kr260-harness"
    assert derive_catalog("mps3", has_harness=False, has_app=True) == CATALOG_APP
    assert derive_catalog("", has_harness=True, has_app=False) == LEGACY_CATALOG


def test_a_malformed_catalogue_id_is_refused():
    doc = {"schema": "harness-manager-channel", "schema_version": 1, "channel": "stable",
           "serial": 1, "issued_at": "2026-09-24T00:00:00Z", "signing_key_id": "0" * 16,
           "catalog": "Not A Catalog!", "app": {"releases": [{
               "version": "0.2.0", "status": "current", "artifacts": [{
                   "kind": "wheel", "name": "harness_manager-0.2.0-py3-none-any.whl",
                   "url": "w.whl", "sha256": "0" * 64, "size": 1}]}]}}
    with pytest.raises(RefusedError, match="catalogue id"):
        parse_channel(doc)
    doc["catalog"] = "hm-app"                             # twin
    assert parse_channel(doc).catalog_id == "hm-app"


# --- the caller names the catalogue it wants ---------------------------------------------------


def test_a_channel_of_another_catalogue_is_refused_when_one_is_asked_for(tmp_path):
    src = publish(tmp_path / "m", serial=1, catalog="mps3-harness")
    with pytest.raises(RefusedError, match="asked for the 'hm-app' catalogue"):
        client(tmp_path).fetch("stable", src, catalog="hm-app")
    assert not (tmp_path / "state" / "update" / "serials.json").exists()   # nothing accepted


def test_twin_the_asked_for_catalogue_is_accepted(tmp_path):
    src = publish(tmp_path / "m", serial=1, catalog="mps3-harness")
    assert client(tmp_path).fetch("stable", src, catalog="mps3-harness").channel.serial == 1


# --- migration from T7's channel-only keys ---------------------------------------------------


def legacy_file(tmp_path, serial: int = 5, sha: str = "ab" * 32):
    state = UpdateState(tmp_path / "state" / "update")
    state.root.mkdir(parents=True)
    state.serials.write_text(json.dumps({"stable": {"serial": serial, "sha256": sha,
                                                    "accepted_at": 1.0}}))
    return state


def test_a_t7_serials_file_is_read_as_the_mps3_harness_history(tmp_path):
    state = legacy_file(tmp_path)
    store = SerialStore(state)
    assert store.last("stable", "mps3-harness") == (5, "ab" * 32)
    with pytest.raises(RefusedError, match="older than serial 5"):
        store.check("stable", 4, "cd" * 32, catalog="mps3-harness")
    store.check("stable", 1, "cd" * 32, catalog=CATALOG_APP)    # twin: a new catalogue is free


def test_migrate_rewrites_format_2_once_and_keeps_the_floor(tmp_path):
    state = legacy_file(tmp_path)
    store = SerialStore(state)
    assert store.migrate() is True
    doc = json.loads(state.serials.read_text())
    assert doc["format"] == 2 and "stable" not in doc
    assert doc["catalogs"] == {"mps3-harness": {"stable": {"serial": 5, "sha256": "ab" * 32,
                                                           "accepted_at": 1.0}}}
    assert doc["migrated"]["from_format"] == 1 and doc["migrated"]["to_catalog"] == "mps3-harness"
    assert store.migrate() is False                             # twin: already format 2
    assert store.last("stable", "mps3-harness")[0] == 5


def test_a_migrated_floor_still_refuses_a_rollback_through_the_client(tmp_path):
    legacy_file(tmp_path, serial=5)
    c = client(tmp_path)
    with pytest.raises(RefusedError, match="older than serial 5"):
        c.fetch("stable", publish(tmp_path / "old", serial=4))       # derived: mps3-harness
    v = c.fetch("stable", publish(tmp_path / "new", serial=6))       # twin: newer accepted
    assert v.channel.serial == 6
    doc = json.loads(c.state.serials.read_text())
    assert doc["format"] == 2 and doc["catalogs"]["mps3-harness"]["stable"]["serial"] == 6
    assert doc["migrated"]["from_format"] == 1                       # rewritten on accept
