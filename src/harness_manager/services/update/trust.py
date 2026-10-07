"""Which signing keys the app trusts, and for what.

Three roles (the update-channel design, agent J §3):

| Role              | Signs                                        | Trusted for          |
|-------------------|----------------------------------------------|----------------------|
| ``harness-release`` | ``channel.json`` for stable/beta (offline key) | stable, beta, dev  |
| ``app-ci``        | the dev channel (a CI key)                     | dev only             |
| ``root``          | ``keys.json`` rotation statements only (cold)  | nothing else         |

The public halves are **pinned in the app** (``PINNED_KEYS``). A new release
key is accepted only through a ``keys.json`` signed by a pinned ``root`` key,
with a ``serial`` that never goes down. The accepted ``keys.json`` and its
signature are kept in ``state_dir/update/keys.json(.minisig)`` and re-verified
against the pinned roots every time the trust store loads, so tampering with
the saved copy only ever removes trust, never adds it.

``PINNED_KEYS`` is EMPTY until the release keys exist (key custody is david's
open question 5). With no pinned key, every channel is refused: that is the
safe default, not a bug. The release process fills it (docs: hand-back §6).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from harness_manager.core.errors import IncompatibleError, RefusedError

from . import minisign
from .state import UpdateState, atomic_write_bytes

log = logging.getLogger(__name__)

ROLE_RELEASE = "harness-release"
ROLE_APP_CI = "app-ci"
ROLE_ROOT = "root"
ROLES = (ROLE_RELEASE, ROLE_APP_CI, ROLE_ROOT)
CHANNELS = ("stable", "beta", "dev")

#: What each role may sign, by default.
DEFAULT_CHANNELS: dict[str, tuple[str, ...]] = {
    ROLE_RELEASE: ("stable", "beta", "dev"),
    ROLE_APP_CI: ("dev",),
    ROLE_ROOT: (),
}

KEYS_SCHEMA = "harness-manager-keys"


class UntrustedKeyError(RefusedError):
    """Signed by a key this app does not know (a rotation via ``keys.json`` may fix it)."""


@dataclass(frozen=True)
class TrustedKey:
    key: minisign.PublicKey
    role: str
    channels: tuple[str, ...] = ()
    note: str = ""

    @property
    def id_hex(self) -> str:
        return self.key.id_hex

    def may_sign(self, channel: str) -> bool:
        return self.role != ROLE_ROOT and channel in self.channels


def pinned(public_key_b64: str, role: str, *, channels: Iterable[str] | None = None,
           note: str = "") -> TrustedKey:
    if role not in ROLES:
        raise ValueError(f"unknown key role {role!r}")
    chans = tuple(channels) if channels is not None else DEFAULT_CHANNELS[role]
    return TrustedKey(minisign.PublicKey.from_base64(public_key_b64), role, chans, note)


#: The app's pinned keys (the 2026 key ceremony, 7 Oct 2026).
#: Add entries as ``pinned("RWQ...", ROLE_RELEASE, note="harness-release 2026")``.
PINNED_KEYS: tuple[TrustedKey, ...] = (
    pinned("RWTxPAw9PeRjV1AyVKs0hdSlgIhC71KtkiFpjLhcQC2dLhvAv1Aa4WBS", ROLE_ROOT,
           note="root 2026 (5763E43D3D0C3CF1, key ceremony 7 Oct 2026)"),
    pinned("RWS8cms/h5qHWlcDSLqT2HFLpLMuaR321AKvEC/OWeuOnFPb8RSS8qMe", ROLE_RELEASE,
           note="harness-release 2026 (5A879A873F6B72BC, key ceremony 7 Oct 2026)"),
)


@dataclass
class TrustStore:
    """The keys in force: the pinned set, plus any rotation accepted from ``keys.json``."""

    pinned: tuple[TrustedKey, ...] = field(default_factory=lambda: PINNED_KEYS)
    rotated: dict[str, TrustedKey] = field(default_factory=dict)   # id_hex -> key
    revoked: frozenset[str] = frozenset()
    keys_serial: int = 0

    # -- queries --

    def all_keys(self) -> list[TrustedKey]:
        by_id: dict[str, TrustedKey] = {k.id_hex: k for k in self.pinned}
        for kid, k in self.rotated.items():
            by_id.setdefault(kid, k)
        return [k for kid, k in by_id.items() if kid not in self.revoked]

    def find(self, key_id_hex: str) -> TrustedKey | None:
        want = key_id_hex.upper()
        return next((k for k in self.all_keys() if k.id_hex == want), None)

    def roots(self) -> list[TrustedKey]:
        # Root keys come ONLY from the pinned set: a rotation can never add a root.
        return [k for k in self.pinned if k.role == ROLE_ROOT and k.id_hex not in self.revoked]

    def verify_for_channel(self, message: bytes, signature: bytes, channel: str,
                           *, what: str = "channel.json") -> tuple[TrustedKey, minisign.Signature]:
        """Verify ``message`` was signed by a key allowed to sign ``channel``."""
        if not self.all_keys():
            raise RefusedError(
                f"cannot verify {what}: this build has no pinned update-signing keys",
                hint="the release keys are not provisioned yet; install updates by hand "
                     "until they are: `harness-manager sd TARGET backup DIR`, then "
                     "`harness-manager sd TARGET install BUNDLE_DIR --backup ZIP`")
        try:
            sig = minisign.parse_signature(signature)
        except minisign.SignatureError as exc:
            raise RefusedError(f"{what} signature is unreadable: {exc}",
                               hint="the channel is damaged or not a harness-manager channel") from None
        kid = sig.key_id_hex
        if kid in self.revoked:
            raise RefusedError(f"{what} is signed by key {kid}, which has been revoked",
                               hint="the publisher must re-sign with a current key")
        key = self.find(kid)
        if key is None:
            raise UntrustedKeyError(f"{what} is signed by key {kid}, which this app does not trust",
                                    hint="update the app, or check the channel source is genuine")
        if not key.may_sign(channel):
            raise RefusedError(f"key {kid} ({key.role}) may not sign the {channel!r} channel",
                               hint="the channel is misconfigured or being replayed across channels")
        try:
            parsed = minisign.verify(message, sig, key.key)
        except minisign.SignatureError as exc:
            raise RefusedError(f"{what} fails its signature check: {exc}",
                               hint="do not use this channel; it was altered after signing") \
                from None
        return key, parsed

    # -- rotation --

    def apply_keys_json(self, data: bytes, signature: bytes) -> int:
        """Accept a root-signed ``keys.json``. Returns its serial. Raises on refusal."""
        roots = self.roots()
        if not roots:
            raise RefusedError("cannot accept keys.json: this build pins no root key",
                               hint="key rotation needs a pinned root key")
        try:
            sig = minisign.parse_signature(signature)
        except minisign.SignatureError as exc:
            raise RefusedError(f"keys.json signature is unreadable: {exc}") from None
        root = next((k for k in roots if k.key.key_id == sig.key_id), None)
        if root is None:
            raise RefusedError(f"keys.json is signed by key {sig.key_id_hex}, which is not a "
                               "pinned root key", hint="only a root key may rotate release keys")
        try:
            minisign.verify(data, sig, root.key)
        except minisign.SignatureError as exc:
            raise RefusedError(f"keys.json fails its signature check: {exc}") from None
        doc = parse_keys_json(data)
        if doc["serial"] < self.keys_serial:
            raise RefusedError(
                f"keys.json serial {doc['serial']} is older than the accepted {self.keys_serial}",
                hint="an old rotation statement is being replayed; ignore it")
        rotated: dict[str, TrustedKey] = {}
        for entry in doc["keys"]:
            if entry["role"] == ROLE_ROOT:
                raise RefusedError("keys.json may not add a root key",
                                   hint="roots are pinned in the app only")
            tk = TrustedKey(minisign.PublicKey.from_base64(entry["public_key"]), entry["role"],
                            tuple(entry["channels"]), entry.get("note", ""))
            rotated[tk.id_hex] = tk
        self.rotated = rotated
        self.revoked = frozenset(k.upper() for k in doc["revoked"])
        self.keys_serial = doc["serial"]
        return doc["serial"]


def parse_keys_json(data: bytes) -> dict[str, Any]:
    """Strictly validate a ``keys.json`` document."""
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise IncompatibleError(f"keys.json is not JSON: {exc}") from None
    if not isinstance(doc, dict) or doc.get("schema") != KEYS_SCHEMA:
        raise IncompatibleError(f"keys.json is not a {KEYS_SCHEMA} document")
    if doc.get("schema_version") != 1:
        raise IncompatibleError(f"keys.json schema_version {doc.get('schema_version')!r} "
                                "is not supported", hint="update the app")
    serial = doc.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 1:
        raise IncompatibleError("keys.json serial must be a positive integer")
    keys = doc.get("keys")
    if not isinstance(keys, list):
        raise IncompatibleError("keys.json 'keys' must be a list")
    out = []
    for i, entry in enumerate(keys):
        if not isinstance(entry, Mapping):
            raise IncompatibleError(f"keys.json keys[{i}] is not an object")
        role = entry.get("role")
        if role not in ROLES:
            raise IncompatibleError(f"keys.json keys[{i}] has unknown role {role!r}")
        pk = entry.get("public_key")
        if not isinstance(pk, str):
            raise IncompatibleError(f"keys.json keys[{i}] has no public_key")
        try:
            minisign.PublicKey.from_base64(pk)
        except minisign.SignatureError as exc:
            raise IncompatibleError(f"keys.json keys[{i}]: {exc}") from None
        chans = entry.get("channels", list(DEFAULT_CHANNELS[role]))
        if not isinstance(chans, list) or any(c not in CHANNELS for c in chans):
            raise IncompatibleError(f"keys.json keys[{i}] channels must be a list of {CHANNELS}")
        out.append({**entry, "channels": chans})
    revoked = doc.get("revoked", [])
    if not isinstance(revoked, list) or not all(isinstance(r, str) for r in revoked):
        raise IncompatibleError("keys.json 'revoked' must be a list of key ids")
    return {**doc, "keys": out, "revoked": revoked}


# --- persistence -----------------------------------------------------------------------


def load_trust(state: UpdateState, pinned_keys: tuple[TrustedKey, ...] | None = None) -> TrustStore:
    """The pinned keys plus the saved rotation, re-verified. A bad saved copy is ignored."""
    store = TrustStore(pinned=PINNED_KEYS if pinned_keys is None else pinned_keys)
    data_path, sig_path = state.keys_json, state.keys_json.with_name("keys.json.minisig")
    if data_path.is_file() and sig_path.is_file():
        try:
            store.apply_keys_json(data_path.read_bytes(), sig_path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - a bad saved rotation must never add trust
            log.warning("ignoring the saved keys.json: %s", exc)
            store = TrustStore(pinned=store.pinned)
    return store


def accept_keys_json(state: UpdateState, store: TrustStore, data: bytes, signature: bytes) -> int:
    """Verify and apply a new ``keys.json``, then save it for the next start."""
    serial = store.apply_keys_json(data, signature)
    atomic_write_bytes(state.keys_json, data)
    atomic_write_bytes(state.keys_json.with_name("keys.json.minisig"), signature)
    return serial

