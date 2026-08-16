"""Unit tests for canonical JSON and fingerprints."""
from nacl.signing import SigningKey

from observer import protocol


def test_canonical_strips_sig_and_sorts_keys():
    raw = {"kind": "claim", "until": 900, "cid": "bafy", "v": 1, "sig": "dead"}
    assert protocol.canonical_dumps(raw) == '{"cid":"bafy","kind":"claim","until":900,"v":1}'
    assert '"sig":"dead"' in protocol.wire_dumps(raw)


def test_nested_and_unicode():
    raw = {"z": {"b": 2, "a": 1}, "m": "café", "sig": "x"}
    assert protocol.canonical_dumps(raw) == '{"m":"café","z":{"a":1,"b":2}}'


def test_sign_verify_roundtrip():
    key = SigningKey.generate()
    msg = protocol.sign({"kind": "skip", "cid": "bafy", "v": 1, "pubkey": key.verify_key.encode().hex()}, key)
    assert protocol.verify(msg, key.verify_key)


def test_payload_hash_stable():
    a = protocol.payload_hash({"kind": "skip", "cid": "x", "v": 1})
    b = protocol.payload_hash({"v": 1, "cid": "x", "kind": "skip", "sig": "nope"})
    assert a == b
    assert len(a) == 64


def test_text_sha256():
    assert protocol.text_sha256("hello") == protocol.text_sha256("hello")
    assert protocol.text_sha256("hello") != protocol.text_sha256("Hello")
