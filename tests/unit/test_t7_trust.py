"""T7: pinned keys, per-channel roles, and key rotation through a root-signed keys.json."""

from __future__ import annotations

import json

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager.services.update import minisign
from harness_manager.services.update.state import UpdateState
from harness_manager.services.update.trust import (
    PINNED_KEYS,
    TrustStore,
    accept_keys_json,
    load_trust,
)
from tests.fakes.fake_channel import TestKeys

KEYS = TestKeys()


def keys_doc(serial: int, keys: list[dict], revoked: list[str] | None = None) -> bytes:
    return json.dumps({"schema": "harness-manager-keys", "schema_version": 1, "serial": serial,
                       "keys": keys, "revoked": revoked or []}).encode()


def rotated_entry(role: str = "harness-release", channels=("stable", "beta", "dev")) -> dict:
    return {"public_key": KEYS.rotated.public.to_base64(), "role": role,
            "channels": list(channels)}


def test_this_build_pins_no_keys_so_every_channel_is_refused():
    # The release keys do not exist yet: the safe default refuses everything.
    assert PINNED_KEYS == ()
    with pytest.raises(RefusedError, match="no pinned update-signing keys"):
        TrustStore().verify_for_channel(b"x", minisign.sign(b"x", KEYS.release), "stable")


def test_release_key_signs_stable():
    key, _ = KEYS.trust().verify_for_channel(b"x", minisign.sign(b"x", KEYS.release), "stable")
    assert key.role == "harness-release"


def test_app_ci_key_may_sign_dev_but_not_stable():
    store = KEYS.trust()
    store.verify_for_channel(b"x", minisign.sign(b"x", KEYS.app_ci), "dev")
    with pytest.raises(RefusedError, match="may not sign the 'stable' channel"):
        store.verify_for_channel(b"x", minisign.sign(b"x", KEYS.app_ci), "stable")


def test_root_key_never_signs_a_channel():
    with pytest.raises(RefusedError, match="may not sign"):
        KEYS.trust().verify_for_channel(b"x", minisign.sign(b"x", KEYS.root), "stable")


def test_unknown_key_is_refused():
    with pytest.raises(RefusedError, match="does not trust"):
        KEYS.trust().verify_for_channel(b"x", minisign.sign(b"x", KEYS.rogue), "stable")


def test_rotation_signed_by_root_adds_a_release_key(tmp_path):
    state = UpdateState(tmp_path / "update")
    store = KEYS.trust()
    data = keys_doc(1, [rotated_entry()])
    assert accept_keys_json(state, store, data, minisign.sign(data, KEYS.root).encode()) == 1
    store.verify_for_channel(b"x", minisign.sign(b"x", KEYS.rotated), "stable")
    # ...and it survives a restart, re-verified against the pinned root.
    reloaded = load_trust(state, KEYS.trust().pinned)
    reloaded.verify_for_channel(b"x", minisign.sign(b"x", KEYS.rotated), "stable")


def test_rotation_signed_by_a_release_key_is_refused():
    data = keys_doc(1, [rotated_entry()])
    with pytest.raises(RefusedError, match="not a pinned root key"):
        KEYS.trust().apply_keys_json(data, minisign.sign(data, KEYS.release).encode())


def test_rotation_cannot_add_a_root():
    data = keys_doc(1, [rotated_entry(role="root", channels=())])
    with pytest.raises(RefusedError, match="may not add a root"):
        KEYS.trust().apply_keys_json(data, minisign.sign(data, KEYS.root).encode())


def test_rotation_serial_never_goes_down():
    store = KEYS.trust()
    newer = keys_doc(5, [rotated_entry()])
    store.apply_keys_json(newer, minisign.sign(newer, KEYS.root).encode())
    older = keys_doc(4, [])
    with pytest.raises(RefusedError, match="older than the accepted 5"):
        store.apply_keys_json(older, minisign.sign(older, KEYS.root).encode())


def test_revocation_removes_a_pinned_release_key():
    store = KEYS.trust()
    data = keys_doc(2, [rotated_entry()], revoked=[KEYS.release.public.id_hex])
    store.apply_keys_json(data, minisign.sign(data, KEYS.root).encode())
    with pytest.raises(RefusedError, match="revoked"):
        store.verify_for_channel(b"x", minisign.sign(b"x", KEYS.release), "stable")


def test_a_tampered_saved_rotation_is_ignored_not_trusted(tmp_path):
    state = UpdateState(tmp_path / "update")
    data = keys_doc(1, [rotated_entry()])
    accept_keys_json(state, KEYS.trust(), data, minisign.sign(data, KEYS.root).encode())
    state.keys_json.write_bytes(keys_doc(1, [rotated_entry()]).replace(b'"serial": 1', b'"serial": 9'))
    reloaded = load_trust(state, KEYS.trust().pinned)
    with pytest.raises(RefusedError, match="does not trust"):
        reloaded.verify_for_channel(b"x", minisign.sign(b"x", KEYS.rotated), "stable")
