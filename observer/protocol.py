"""Canonical JSON, fingerprints, and Ed25519 sign/verify.

Canonical form (must match clubd/internal/canon):
  - object keys sorted lexicographically
  - no insignificant whitespace (separators (',', ':'))
  - UTF-8, ``ensure_ascii=False``
  - the ``sig`` field is omitted before hashing/signing

The Go daemon is the production signer (libp2p identity = publisher).
This module is used by tests and by the Python ingest path to hash
``classify`` payloads.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

KINDS = frozenset(("claim", "skip", "classify", "alias", "report"))
REPORT_REASONS = frozenset(("wrong", "abusive", "clear"))


def wire_dumps(obj: Any) -> str:
    """JSON envelope as stored/gossiped, including ``sig``."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def canonical_dumps(obj: Any) -> str:
    """Deterministic JSON for signatures. ``sig`` is stripped from dicts."""
    return json.dumps(_strip_sig(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _strip_sig(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_sig(v) for k, v in obj.items() if k != "sig"}
    if isinstance(obj, list):
        return [_strip_sig(v) for v in obj]
    return obj


def payload_hash(obj: Mapping) -> str:
    """SHA-256 hex of the canonical unsigned payload (classify_hash)."""
    return hashlib.sha256(canonical_dumps(obj).encode("utf-8")).hexdigest()


def text_sha256(text: str) -> str:
    """Fingerprint of normalized extracted text (UTF-8 SHA-256 hex)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sign(obj: dict, signing_key: SigningKey) -> dict:
    """Return a copy of ``obj`` with ``sig`` (hex) set."""
    body = dict(obj)
    body.pop("sig", None)
    sig = signing_key.sign(canonical_dumps(body).encode("utf-8")).signature
    body["sig"] = sig.hex()
    return body


def verify(obj: Mapping, verify_key: Optional[VerifyKey] = None) -> bool:
    """Verify ``sig`` over the canonical payload.

    If ``verify_key`` is omitted, ``obj['pubkey']`` must be a 32-byte hex
    Ed25519 public key. Production gossip is verified in clubd instead.
    """
    sig_hex = obj.get("sig")
    if not sig_hex:
        return False
    try:
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    key = verify_key
    if key is None:
        pub_hex = obj.get("pubkey")
        if not pub_hex:
            return False
        try:
            key = VerifyKey(bytes.fromhex(pub_hex))
        except (ValueError, TypeError):
            return False
    try:
        key.verify(canonical_dumps(obj).encode("utf-8"), sig)
        return True
    except BadSignatureError:
        return False
