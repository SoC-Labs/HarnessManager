"""T7: minisign (Ed25519) verification. Every check has a negative twin.

The known-answer vector is minisign's own published example (the py-minisign
README; legacy ``Ed`` signature over the 4 bytes ``test``). Our signer's output
was also cross-checked against py-minisign 0.21.1 by hand (both algorithms).
"""

from __future__ import annotations

import pytest

from harness_manager.services.update import minisign as m

VECTOR_PK = "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3"
VECTOR_SIG = (
    "untrusted comment: signature from minisign secret key\n"
    "RWQf6LRCGA9i59SLOFxz6NxvASXDJeRtuZykwQepbDEGt87ig1BNpWaVWuNrm73YiIiJbq71Wi+dP9eKL8OC351vwIasSSbXxwA=\n"
    "trusted comment: timestamp:1555779966\tfile:test\n"
    "QtKMXWyYcwdpZAlPF7tE2ENJkRd1ujvKjlj1m9RtHTBnZPa5WKU5uWRs5GoP5M/VqE81QFuMKI5k/SfNQUaOAA==\n"
)


def test_known_answer_vector_verifies():
    pk = m.PublicKey.from_base64(VECTOR_PK)
    sig = m.verify(b"test", VECTOR_SIG, pk)
    assert sig.algorithm == m.SIGALG and sig.key_id_hex == pk.id_hex == "E7620F1842B4E81F"
    assert sig.trusted_comment == "timestamp:1555779966\tfile:test"


def test_known_answer_vector_rejects_other_content():
    with pytest.raises(m.SignatureError, match="does not match"):
        m.verify(b"Test", VECTOR_SIG, m.PublicKey.from_base64(VECTOR_PK))


@pytest.mark.parametrize("prehashed", [True, False])
def test_round_trip(prehashed):
    key = m.SecretKey.generate()
    sig = m.sign(b"channel bytes", key, trusted_comment="timestamp:1", prehashed=prehashed)
    parsed = m.verify(b"channel bytes", sig, key.public)
    assert parsed.algorithm == (m.SIGALG_HASHED if prehashed else m.SIGALG)


def test_tampered_content_is_refused():
    key = m.SecretKey.generate()
    sig = m.sign(b'{"serial": 3}', key)
    with pytest.raises(m.SignatureError, match="does not match"):
        m.verify(b'{"serial": 4}', sig, key.public)


def test_other_key_is_refused_by_key_id():
    key, other = m.SecretKey.generate(), m.SecretKey.generate()
    sig = m.sign(b"x", key)
    with pytest.raises(m.SignatureError, match="signed by key"):
        m.verify(b"x", sig, other.public)


def test_other_key_with_the_same_id_is_refused_by_the_signature():
    # A forger who copies the key id still cannot produce the Ed25519 signature.
    key = m.SecretKey.generate(key_id=b"\x01" * 8)
    forger = m.SecretKey.generate(key_id=b"\x01" * 8)
    with pytest.raises(m.SignatureError):
        m.verify(b"x", m.sign(b"x", forger), key.public)


def test_trusted_comment_is_authenticated():
    key = m.SecretKey.generate()
    lines = m.sign(b"x", key, trusted_comment="file:channel.json").splitlines()
    lines[2] = "trusted comment: file:other.json"
    with pytest.raises(m.SignatureError, match="trusted comment"):
        m.verify(b"x", "\n".join(lines) + "\n", key.public)


def test_untrusted_comment_is_not_authenticated():
    # Twin: the untrusted comment may change freely (it is not signed, by design).
    key = m.SecretKey.generate()
    lines = m.sign(b"x", key).splitlines()
    lines[0] = "untrusted comment: anything at all"
    m.verify(b"x", "\n".join(lines), key.public)


@pytest.mark.parametrize("bad", [
    "",                                                        # empty
    "untrusted comment: x\n",                                  # too short
    "comment: x\nAAAA\ntrusted comment: y\nAAAA\n",            # wrong prefix
    "untrusted comment: x\n!!notbase64!!\ntrusted comment: y\nAAAA\n",
    "untrusted comment: x\nRWQf6LRC\ntrusted comment: y\nQtKM\n",   # wrong lengths
])
def test_malformed_signatures_are_refused(bad):
    with pytest.raises(m.SignatureError):
        m.parse_signature(bad)


def test_unknown_algorithm_is_refused():
    import base64

    key = m.SecretKey.generate()
    lines = m.sign(b"x", key).splitlines()
    blob = bytearray(base64.b64decode(lines[1]))
    blob[:2] = b"Xx"
    lines[1] = base64.b64encode(bytes(blob)).decode()
    with pytest.raises(m.SignatureError, match="unsupported"):
        m.parse_signature("\n".join(lines))


def test_public_key_text_round_trip_and_key_id_display():
    key = m.SecretKey.generate(key_id=bytes(range(8)))
    text = key.public.to_text()
    assert m.PublicKey.from_text(text) == key.public
    assert key.public.id_hex == "0706050403020100"          # little-endian u64, as minisign prints
    assert m.key_id_from_hex("0706050403020100") == bytes(range(8))


def test_a_non_minisign_public_key_is_refused():
    with pytest.raises(m.SignatureError):
        m.PublicKey.from_base64("AAAA")
