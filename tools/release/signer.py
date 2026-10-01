"""The key source: WHO signs a channel, as a parameter (david's U2 is still open).

Two signers, one interface (``sign(path, trusted_comment) -> .minisig text``):

- ``minisign`` (the default, and the only one ``--publish`` accepts): the real
  ``minisign`` CLI. The secret key never enters a Python process, and a
  passphrase-protected key prompts on the terminal (docs/KEYS.md). The tool needs the
  PUBLIC key too: ``signing_key_id`` goes inside the signed document, so it must be
  known before signing, and every signature is verified with HM's own verifier after.
- ``python``: HM's own signer (``services/update/minisign.py``), for THROWAWAY keys only:
  an UNENCRYPTED minisign secret key file (``keygen --throwaway`` writes one, as does
  ``minisign -G -W``). An encrypted key is refused: use the CLI.

Key files come from arguments or the environment::

    --signer       HM_RELEASE_SIGNER        minisign | python      (default minisign)
    --secret-key   HM_RELEASE_SECRET_KEY    path to the minisign secret key
    --public-key   HM_RELEASE_PUBLIC_KEY    path to its public key (required for minisign)
    --minisign     HM_MINISIGN              the minisign binary     (default: on PATH)

Nothing here generates or stores a real key. ``keygen_throwaway`` refuses any directory
outside the system temp dir.

**TEST keys** (``harness-release --test-key``, lane RELEASE-PIPE): ``keygen_test`` makes a
throwaway pair whose key id starts ``7E57C0DE`` ("TEST CODE"), so a TEST key is
recognisable wherever its id shows up: in ``channel.json`` (``signing_key_id``), in the
``.minisig`` and in the client's messages. ``--publish`` and the publish script refuse any
such key, and ``tests/unit/test_release_pipe.py`` fails if one is ever pinned in
``trust.PINNED_KEYS``. A real minisign key has a random id: one starting with the marker is
a 1-in-4-billion accident, refused all the same.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from harness_manager.services.update import minisign

from .common import EXIT_USAGE, ReleaseError, Runner, in_temp_dir, run

#: The key-id prefix (``PublicKey.id_hex``) of every key ``keygen_test`` makes.
TEST_KEY_PREFIX = "7E57C0DE"

ENV_SIGNER = "HM_RELEASE_SIGNER"
ENV_SECRET = "HM_RELEASE_SECRET_KEY"
ENV_PUBLIC = "HM_RELEASE_PUBLIC_KEY"
ENV_MINISIGN = "HM_MINISIGN"
SIGNERS = ("minisign", "python")

_KDF_NONE = b"\x00\x00"
_KDF_SCRYPT = b"Sc"
_CHK_ALG = b"B2"
_SK_BLOB = 2 + 2 + 2 + 32 + 8 + 8 + 8 + 64 + 32     # 158 bytes


# --- minisign secret key files -----------------------------------------------------------


def _secret_checksum(key_id: bytes, sk: bytes) -> bytes:
    return hashlib.blake2b(minisign.SIGALG + key_id + sk, digest_size=32).digest()


def parse_secret_key(text: str) -> minisign.SecretKey:
    """An UNENCRYPTED minisign secret key file -> HM's ``SecretKey``. Refuses an encrypted one."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("untrusted comment:")]
    if len(lines) != 1:
        raise ReleaseError("not a minisign secret key file (one base64 line expected)")
    try:
        blob = base64.b64decode(lines[0], validate=True)
    except (binascii.Error, ValueError):
        raise ReleaseError("not a minisign secret key file (bad base64)") from None
    if len(blob) != _SK_BLOB or blob[:2] != minisign.SIGALG or blob[4:6] != _CHK_ALG:
        raise ReleaseError("not a minisign Ed25519 secret key file")
    kdf = blob[2:4]
    if kdf == _KDF_SCRYPT:
        raise ReleaseError(
            "this secret key is passphrase-protected: HM's Python signer never decrypts a key",
            hint="sign with the minisign CLI (--signer minisign), which prompts for the passphrase")
    if kdf != _KDF_NONE:
        raise ReleaseError(f"unknown minisign key derivation {kdf!r}")
    keynum = blob[54:]
    key_id, sk, chk = keynum[:8], keynum[8:72], keynum[72:104]
    if _secret_checksum(key_id, sk) != chk:
        raise ReleaseError("the minisign secret key fails its checksum (damaged file)")
    key = minisign.SecretKey.from_seed(sk[:32], key_id)
    if key.public.raw != sk[32:]:
        raise ReleaseError("the minisign secret key's public half does not match its seed")
    return key


def secret_key_text(key: minisign.SecretKey) -> str:
    """An UNENCRYPTED minisign secret key file (what ``minisign -G -W`` writes)."""
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    seed = key.private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    sk = seed + key.public.raw
    blob = (minisign.SIGALG + _KDF_NONE + _CHK_ALG + os.urandom(32) + struct.pack("<QQ", 0, 0)
            + key.key_id + sk + _secret_checksum(key.key_id, sk))
    return ("untrusted comment: THROWAWAY minisign secret key (unencrypted, tests only)\n"
            f"{base64.b64encode(blob).decode('ascii')}\n")


def read_public_key(path: Path) -> minisign.PublicKey:
    try:
        return minisign.PublicKey.from_text(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(f"cannot read the public key {path}: {exc}", code=EXIT_USAGE) from None
    except minisign.SignatureError as exc:
        raise ReleaseError(f"{path} is not a minisign public key: {exc}") from None


def keygen_throwaway(directory: Path, name: str = "release") -> tuple[Path, Path]:
    """A THROWAWAY key pair for tests and dry runs. Refuses anything outside the temp dir."""
    directory = Path(directory)
    if not in_temp_dir(directory):
        raise ReleaseError(
            f"refusing to write a key to {directory}: throwaway keys live under the temp dir only",
            hint="real keys come from david's key ceremony (docs/KEYS.md), never from this tool")
    directory.mkdir(parents=True, exist_ok=True)
    key = minisign.SecretKey.generate()
    sk, pk = directory / f"{name}.key", directory / f"{name}.pub"
    fd = os.open(sk, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret_key_text(key))
    pk.write_text(key.public.to_text(), encoding="utf-8")
    return sk, pk


def is_test_key(public: minisign.PublicKey | str) -> bool:
    """A key ``keygen_test`` made (or a key id that looks like one): never a release key."""
    kid = public if isinstance(public, str) else public.id_hex
    return kid.upper().startswith(TEST_KEY_PREFIX)


def keygen_test(directory: Path | None = None, name: str = "TEST-release") -> tuple[Path, Path]:
    """A THROWAWAY TEST key pair (id ``7E57C0DE…``) in a fresh temp dir. The release it signs
    is marked TEST; ``--publish`` and the publish script refuse it; no client trusts it."""
    import tempfile

    directory = Path(directory) if directory is not None else \
        Path(tempfile.mkdtemp(prefix="hm-test-key-"))
    if not in_temp_dir(directory):
        raise ReleaseError(
            f"refusing to write a TEST key to {directory}: it lives under the temp dir only",
            hint="real keys come from david's key ceremony (docs/KEYS.md), never from this tool")
    directory.mkdir(parents=True, exist_ok=True)
    kid = (bytes.fromhex(TEST_KEY_PREFIX) + os.urandom(minisign.KEY_ID_BYTES - 4))[::-1]
    key = minisign.SecretKey.generate(key_id=kid)
    if not is_test_key(key.public):                       # the id is stored little-endian
        raise ReleaseError("internal: the TEST key id lost its marker")
    sk, pk = directory / f"{name}.key", directory / f"{name}.pub"
    fd = os.open(sk, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret_key_text(key).replace("THROWAWAY", "TEST (throwaway)"))
    pk.write_text(key.public.to_text().replace("minisign public key",
                                               "TEST minisign public key"), encoding="utf-8")
    return sk, pk


def public_key_path_for(secret_key: Path) -> Path:
    """minisign's naming: ``release.key`` <-> ``release.pub``; else ``<file>.pub``."""
    secret_key = Path(secret_key)
    if secret_key.suffix == ".key":
        return secret_key.with_suffix(".pub")
    return secret_key.with_name(secret_key.name + ".pub")


# --- signers -----------------------------------------------------------------------------


class Signer(Protocol):
    kind: str
    public: minisign.PublicKey

    def sign(self, path: Path, trusted_comment: str) -> str:
        """Sign the file at ``path``; return the verified ``.minisig`` text."""
        ...


def _verified(data: bytes, sig_text: str, public: minisign.PublicKey, what: str) -> str:
    try:
        minisign.verify(data, sig_text, public)
    except minisign.SignatureError as exc:
        raise ReleaseError(f"the signature {what} made does not verify with the public key: {exc}",
                           hint="the secret and public key files are not a pair") from None
    return sig_text


@dataclass
class PythonSigner:
    """HM's own signer, for throwaway (unencrypted) keys."""

    secret_key: Path
    kind: str = "python"
    public: minisign.PublicKey = field(init=False)

    def __post_init__(self) -> None:
        try:
            text = Path(self.secret_key).read_text(encoding="utf-8")
        except OSError as exc:
            raise ReleaseError(f"cannot read the secret key {self.secret_key}: {exc}",
                               code=EXIT_USAGE) from None
        self._key = parse_secret_key(text)
        self.public = self._key.public

    def sign(self, path: Path, trusted_comment: str) -> str:
        data = Path(path).read_bytes()
        sig = minisign.sign(data, self._key, trusted_comment=trusted_comment,
                            untrusted_comment="signature from harness-manager tools/release")
        return _verified(data, sig, self.public, "HM's signer")


@dataclass
class MinisignCliSigner:
    """``minisign -S``: the secret key stays in minisign; a passphrase prompts on the tty."""

    secret_key: Path
    public_key: Path
    binary: str = "minisign"
    runner: Runner = run
    kind: str = "minisign"
    public: minisign.PublicKey = field(init=False)

    def __post_init__(self) -> None:
        self.public = read_public_key(self.public_key)
        if not Path(self.secret_key).is_file():
            raise ReleaseError(f"no secret key at {self.secret_key}", code=EXIT_USAGE)

    def sign(self, path: Path, trusted_comment: str) -> str:
        path = Path(path)
        sig_path = path.with_name(path.name + ".minisig")
        sig_path.unlink(missing_ok=True)
        argv = [self.binary, "-S", "-s", str(self.secret_key), "-m", str(path), "-x",
                str(sig_path), "-t", trusted_comment,
                "-c", "signature from harness-manager tools/release"]
        res = self.runner(argv, capture=False, stdin_tty=True)
        if res.returncode != 0 or not sig_path.is_file():
            raise ReleaseError(f"minisign could not sign {path.name} (exit {res.returncode})",
                               hint="check the passphrase and the key path")
        return _verified(path.read_bytes(), sig_path.read_text(encoding="utf-8"), self.public,
                         "minisign")


def from_options(signer: str | None, secret_key: str | None, public_key: str | None,
                 minisign_bin: str | None = None, *, runner: Runner = run,
                 env: dict[str, str] | None = None) -> Signer:
    """Build the signer from arguments, falling back to the HM_RELEASE_* environment."""
    env = dict(os.environ) if env is None else env
    kind = (signer or env.get(ENV_SIGNER, "") or "minisign").strip()
    if kind not in SIGNERS:
        raise ReleaseError(f"unknown signer {kind!r} (one of {', '.join(SIGNERS)})",
                           code=EXIT_USAGE)
    sk = secret_key or env.get(ENV_SECRET, "")
    pk = public_key or env.get(ENV_PUBLIC, "")
    if not sk:
        raise ReleaseError("no signing key: pass --secret-key or set HM_RELEASE_SECRET_KEY",
                           hint="docs/KEYS.md (pending david's U2); for a dry run make a throwaway "
                                "pair with `python -m tools.release keygen --throwaway /tmp/...`",
                           code=EXIT_USAGE)
    if kind == "python":
        s = PythonSigner(Path(sk))
        if pk and read_public_key(Path(pk)).id_hex != s.public.id_hex:
            raise ReleaseError(f"{pk} is not the public half of {sk}")
        return s
    if not pk:
        raise ReleaseError("the minisign signer needs --public-key (or HM_RELEASE_PUBLIC_KEY): "
                           "the key id goes inside the signed channel", code=EXIT_USAGE)
    binary = minisign_bin or env.get(ENV_MINISIGN, "") or shutil.which("minisign") or ""
    if not binary:
        raise ReleaseError("the minisign CLI is not installed",
                           hint="install minisign (https://jedisct1.github.io/minisign/), or "
                                "set HM_MINISIGN; --signer python is for throwaway keys only",
                           code=EXIT_USAGE)
    return MinisignCliSigner(Path(sk), Path(pk), binary=binary, runner=runner)
