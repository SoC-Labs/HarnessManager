"""minisign (Ed25519) signatures, verified in pure Python through ``cryptography``.

The formats follow minisign (https://jedisct1.github.io/minisign/, minisign.c):

- **Public key**: an ``untrusted comment:`` line, then base64 of
  ``"Ed" || key_id[8] || ed25519_public_key[32]``.
- **Signature** (``<file>.minisig``), four lines::

      untrusted comment: <anything; NOT covered by the signature>
      base64("ED" || key_id[8] || ed25519_sig[64])       # "ED": signs BLAKE2b-512(file)
      trusted comment: <text>                            # "Ed" (legacy): signs the file itself
      base64(global_sig[64])                             # signs ed25519_sig || <text>

- **Key id**: 8 bytes, displayed as the little-endian u64 in upper-case hex
  (``%016llX``), as ``minisign -V`` prints it.

Verification order (``verify``): the algorithm is known; the signature's key id
is the key's; the file signature verifies; the trusted comment's global
signature verifies. Any failure is ``SignatureError`` and nothing is returned,
so a caller cannot use unverified content by accident.

``sign`` exists for the release tooling and the tests. It produces files the
real ``minisign -V`` accepts (prehashed, ``ED``). The app itself never holds a
secret key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SIGALG = b"Ed"            # legacy: the signature covers the message itself
SIGALG_HASHED = b"ED"     # the signature covers BLAKE2b-512(message) (minisign >= 0.8 default)
UNTRUSTED_PREFIX = "untrusted comment: "
TRUSTED_PREFIX = "trusted comment: "
KEY_ID_BYTES = 8
MAX_SIG_FILE = 64 * 1024  # a .minisig is ~300 bytes; anything huge is not one


class SignatureError(Exception):
    """The signature is missing, malformed, from another key, or does not verify."""


def key_id_hex(key_id: bytes) -> str:
    """minisign's display form: the little-endian u64, upper-case hex."""
    if len(key_id) != KEY_ID_BYTES:
        raise ValueError("a minisign key id is 8 bytes")
    return key_id[::-1].hex().upper()


def key_id_from_hex(text: str) -> bytes:
    raw = bytes.fromhex(text.strip())
    if len(raw) != KEY_ID_BYTES:
        raise ValueError(f"a minisign key id is 16 hex digits, got {text!r}")
    return raw[::-1]


@dataclass(frozen=True)
class PublicKey:
    key_id: bytes            # 8 bytes, as stored in the key and the signature
    raw: bytes               # 32-byte Ed25519 public key

    @property
    def id_hex(self) -> str:
        return key_id_hex(self.key_id)

    @classmethod
    def from_base64(cls, line: str) -> PublicKey:
        """Parse the base64 payload line of a minisign public key."""
        try:
            blob = base64.b64decode(line.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SignatureError(f"public key is not base64: {exc}") from None
        if len(blob) != 2 + KEY_ID_BYTES + 32 or blob[:2] != SIGALG:
            raise SignatureError("not a minisign Ed25519 public key")
        return cls(key_id=blob[2:10], raw=blob[10:])

    @classmethod
    def from_text(cls, text: str) -> PublicKey:
        """Parse a whole ``.pub`` file (comment line optional) or the bare base64 line."""
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        lines = [ln for ln in lines if not ln.startswith(UNTRUSTED_PREFIX.rstrip())]
        if len(lines) != 1:
            raise SignatureError("a minisign public key is one base64 line")
        return cls.from_base64(lines[0])

    def to_base64(self) -> str:
        return base64.b64encode(SIGALG + self.key_id + self.raw).decode("ascii")

    def to_text(self) -> str:
        return f"{UNTRUSTED_PREFIX}minisign public key {self.id_hex}\n{self.to_base64()}\n"

    def _ed25519(self) -> Ed25519PublicKey:
        return Ed25519PublicKey.from_public_bytes(self.raw)


@dataclass(frozen=True)
class Signature:
    algorithm: bytes         # b"Ed" or b"ED"
    key_id: bytes
    signature: bytes         # 64 bytes over the message (or its BLAKE2b-512)
    trusted_comment: str
    global_signature: bytes  # 64 bytes over signature || trusted_comment
    untrusted_comment: str = ""

    @property
    def key_id_hex(self) -> str:
        return key_id_hex(self.key_id)


def parse_signature(data: bytes | str) -> Signature:
    """Parse a ``.minisig`` file. Raises ``SignatureError`` for anything malformed."""
    if isinstance(data, bytes):
        if len(data) > MAX_SIG_FILE:
            raise SignatureError("signature file is too large to be a minisign signature")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise SignatureError("signature file is not UTF-8 text") from None
    else:
        text = data
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if len(lines) != 4:
        raise SignatureError(f"a minisign signature has 4 lines, this has {len(lines)}")
    if not lines[0].startswith(UNTRUSTED_PREFIX):
        raise SignatureError("line 1 is not an 'untrusted comment:' line")
    if not lines[2].startswith(TRUSTED_PREFIX):
        raise SignatureError("line 3 is not a 'trusted comment:' line")
    try:
        blob = base64.b64decode(lines[1].strip(), validate=True)
        global_sig = base64.b64decode(lines[3].strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("signature lines are not base64") from None
    if len(blob) != 2 + KEY_ID_BYTES + 64:
        raise SignatureError("signature line has the wrong length")
    if blob[:2] not in (SIGALG, SIGALG_HASHED):
        raise SignatureError(f"unsupported signature algorithm {blob[:2]!r}")
    if len(global_sig) != 64:
        raise SignatureError("global signature has the wrong length")
    return Signature(
        algorithm=blob[:2], key_id=blob[2:10], signature=blob[10:],
        trusted_comment=lines[2][len(TRUSTED_PREFIX):], global_signature=global_sig,
        untrusted_comment=lines[0][len(UNTRUSTED_PREFIX):],
    )


def _blake2b512(message: bytes) -> bytes:
    return hashlib.blake2b(message, digest_size=64).digest()


def verify(message: bytes, signature: bytes | str | Signature, key: PublicKey) -> Signature:
    """Verify ``message`` against a minisign signature made by ``key``.

    Returns the parsed signature (its trusted comment is authentic). Raises
    ``SignatureError`` on any failure.
    """
    sig = signature if isinstance(signature, Signature) else parse_signature(signature)
    if sig.key_id != key.key_id:
        raise SignatureError(
            f"signed by key {sig.key_id_hex}, not by key {key.id_hex}")
    signed = _blake2b512(message) if sig.algorithm == SIGALG_HASHED else message
    pk = key._ed25519()
    try:
        pk.verify(sig.signature, signed)
    except InvalidSignature:
        raise SignatureError("the signature does not match the content (tampered or corrupt)") \
            from None
    try:
        pk.verify(sig.global_signature, sig.signature + sig.trusted_comment.encode("utf-8"))
    except InvalidSignature:
        raise SignatureError("the trusted comment has been altered") from None
    return sig


# --- signing (release tooling and tests) -------------------------------------------------


@dataclass(frozen=True)
class SecretKey:
    """An unencrypted signing key held in memory (the tests' and the dev tool's)."""

    key_id: bytes
    private: Ed25519PrivateKey

    @classmethod
    def generate(cls, key_id: bytes | None = None) -> SecretKey:
        import os

        return cls(key_id=key_id or os.urandom(KEY_ID_BYTES), private=Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes, key_id: bytes) -> SecretKey:
        """Deterministic keys for test vectors (``seed`` is the 32-byte Ed25519 seed)."""
        return cls(key_id=key_id, private=Ed25519PrivateKey.from_private_bytes(seed))

    @property
    def public(self) -> PublicKey:
        raw = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return PublicKey(key_id=self.key_id, raw=raw)


def sign(message: bytes, key: SecretKey, *, trusted_comment: str | None = None,
         untrusted_comment: str = "signature from harness-manager release tooling",
         prehashed: bool = True) -> str:
    """A ``.minisig`` for ``message`` (what ``minisign -S`` writes)."""
    if trusted_comment is None:
        trusted_comment = f"timestamp:{int(time.time())}"
    if "\n" in trusted_comment or "\r" in trusted_comment:
        raise ValueError("a trusted comment is one line")
    alg = SIGALG_HASHED if prehashed else SIGALG
    signed = _blake2b512(message) if prehashed else message
    sig = key.private.sign(signed)
    global_sig = key.private.sign(sig + trusted_comment.encode("utf-8"))
    return (f"{UNTRUSTED_PREFIX}{untrusted_comment}\n"
            f"{base64.b64encode(alg + key.key_id + sig).decode('ascii')}\n"
            f"{TRUSTED_PREFIX}{trusted_comment}\n"
            f"{base64.b64encode(global_sig).decode('ascii')}\n")
