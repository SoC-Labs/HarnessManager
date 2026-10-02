"""CCR-2 (lane RELEASE-PIPE): a TEST key (id ``7E57C0DE…``, what ``harness-release
--test-key`` makes) is never trusted, at each of the trust module's entry points:

- ``pinned()`` refuses one, so ``PINNED_KEYS`` cannot hold one;
- ``load_trust()`` refuses one among the keys it is given (the app's start);
- ``apply_keys_json()`` refuses a root-signed rotation that names one (and a saved rotation
  that names one is ignored at the next start, so it adds no trust).

Each has a twin: a normal (random) key id goes through as before. Tests still hand a TEST
key to a client through an explicit ``TrustStore(pinned=...)`` (tests/integration/
test_release_e2e.py), which is not one of these entry points.
"""

from __future__ import annotations

import json
import os

import pytest

from harness_manager.core.errors import RefusedError
from harness_manager.services.update import minisign, trust
from harness_manager.services.update.state import UpdateState
from harness_manager.services.update.trust import (
    ROLE_RELEASE,
    ROLE_ROOT,
    TrustedKey,
    TrustStore,
    accept_keys_json,
    load_trust,
    pinned,
)
from tools.release import signer as signer_mod


def make_test_key() -> minisign.SecretKey:
    return minisign.SecretKey.generate(
        key_id=(bytes.fromhex(trust.TEST_KEY_PREFIX) + os.urandom(4))[::-1])


def normal_key() -> minisign.SecretKey:
    while True:                                  # a random id; 1 in 2**32 needs a retry
        k = minisign.SecretKey.generate()
        if not trust.is_test_key_id(k.public.id_hex):
            return k




def keys_doc(serial: int, *keys: minisign.SecretKey) -> bytes:
    return json.dumps({"schema": "harness-manager-keys", "schema_version": 1, "serial": serial,
                       "keys": [{"public_key": k.public.to_base64(), "role": ROLE_RELEASE,
                                 "channels": ["stable", "beta", "dev"]} for k in keys],
                       "revoked": []}).encode()


def test_the_tool_and_the_client_share_one_marker():
    assert signer_mod.TEST_KEY_PREFIX == trust.TEST_KEY_PREFIX == "7E57C0DE"
    assert make_test_key().public.id_hex.startswith("7E57C0DE")


# --- pinned() ------------------------------------------------------------------------------


def test_pinned_refuses_a_test_key():
    with pytest.raises(RefusedError, match=r"a TEST key \(7E57C0DE…\) is never trusted"):
        pinned(make_test_key().public.to_base64(), ROLE_RELEASE, note="never")
    with pytest.raises(RefusedError, match="never trusted"):
        pinned(make_test_key().public.to_base64(), ROLE_ROOT)


def test_twin_pinned_takes_a_normal_key():
    k = normal_key()
    tk = pinned(k.public.to_base64(), ROLE_RELEASE, note="release")
    assert tk.id_hex == k.public.id_hex and tk.may_sign("stable")


def test_no_test_key_is_pinned_in_this_build():
    assert not [k.id_hex for k in trust.PINNED_KEYS if trust.is_test_key_id(k.id_hex)]


# --- load_trust() --------------------------------------------------------------------------


def test_load_trust_refuses_a_test_key_among_the_pinned(tmp_path):
    state = UpdateState(tmp_path / "update")
    keys = (TrustedKey(normal_key().public, ROLE_RELEASE, trust.CHANNELS, "release"),
            TrustedKey(make_test_key().public, ROLE_RELEASE, trust.CHANNELS, "slipped in"))
    with pytest.raises(RefusedError, match="never trusted") as exc:
        load_trust(state, keys)
    assert "pinned harness-release key" in exc.value.message


def test_twin_load_trust_takes_normal_keys(tmp_path):
    state = UpdateState(tmp_path / "update")
    k = normal_key()
    store = load_trust(state, (TrustedKey(k.public, ROLE_RELEASE, trust.CHANNELS, "r"),))
    key, _ = store.verify_for_channel(b"x", minisign.sign(b"x", k), "stable")
    assert key.id_hex == k.public.id_hex


# --- apply_keys_json() ---------------------------------------------------------------------


def _store(root: minisign.SecretKey) -> TrustStore:
    return TrustStore(pinned=(TrustedKey(root.public, ROLE_ROOT, (), "root"),))


def test_apply_keys_json_refuses_a_rotation_to_a_test_key(tmp_path):
    root, tk = normal_key(), make_test_key()
    store = _store(root)
    data = keys_doc(1, tk)
    with pytest.raises(RefusedError, match="never trusted") as exc:
        store.apply_keys_json(data, minisign.sign(data, root).encode())
    assert "keys.json" in exc.value.message
    assert store.find(tk.public.id_hex) is None and store.keys_serial == 0


def test_twin_apply_keys_json_takes_a_normal_rotation(tmp_path):
    root, rk = normal_key(), normal_key()
    store = _store(root)
    data = keys_doc(1, rk)
    assert store.apply_keys_json(data, minisign.sign(data, root).encode()) == 1
    store.verify_for_channel(b"x", minisign.sign(b"x", rk), "stable")


def test_a_saved_rotation_naming_a_test_key_adds_no_trust_at_the_next_start(tmp_path):
    root, rk, tk = normal_key(), normal_key(), make_test_key()
    state = UpdateState(tmp_path / "update")
    good = keys_doc(1, rk)
    accept_keys_json(state, _store(root), good, minisign.sign(good, root).encode())
    # someone swaps the saved copy for one (validly root-signed) that adds a TEST key
    bad = keys_doc(2, rk, tk)
    state.keys_json.write_bytes(bad)
    state.keys_json.with_name("keys.json.minisig").write_bytes(minisign.sign(bad, root).encode())
    store = load_trust(state, _store(root).pinned)
    assert store.find(tk.public.id_hex) is None and store.find(rk.public.id_hex) is None
    with pytest.raises(RefusedError):
        store.verify_for_channel(b"x", minisign.sign(b"x", tk), "stable")
