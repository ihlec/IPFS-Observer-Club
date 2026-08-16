"""CID decode and block verification without extra dependencies.

A gateway body is only used if its bytes hash to the CID. Unknown hash
functions are treated as unverified.
"""
from __future__ import annotations

import base64
import hashlib

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

CODECS = {
    0x55: "raw",
    0x70: "dag-pb",
    0x71: "dag-cbor",
    0x0129: "dag-json",
    0x72: "libp2p-key",
    0x0200: "json",
}

SHA2_256 = 0x12
IDENTITY = 0x00


def _b58decode(s):
    num = 0
    for ch in s:
        num = num * 58 + _B58_INDEX[ch]
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def _read_varint(data, offset):
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated varint")
        b = data[offset]
        result |= (b & 0x7F) << shift
        offset += 1
        if not b & 0x80:
            return result, offset
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def decode_cid(cid):
    """Return {version, codec, codec_code, mh_code, digest} or None."""
    if not cid or not isinstance(cid, str):
        return None
    try:
        if len(cid) == 46 and cid.startswith("Qm"):
            mh = _b58decode(cid)
            if len(mh) < 2 or mh[0] != SHA2_256:
                return None
            length = mh[1]
            digest = mh[2:2 + length]
            if len(digest) != length:
                return None
            return {
                "version": 0,
                "codec": "dag-pb",
                "codec_code": 0x70,
                "mh_code": SHA2_256,
                "digest": digest,
            }
        prefix, rest = cid[0], cid[1:]
        if prefix == "b":
            pad = "=" * (-len(rest) % 8)
            data = base64.b32decode(rest.upper() + pad)
        elif prefix == "B":
            pad = "=" * (-len(rest) % 8)
            data = base64.b32decode(rest + pad)
        elif prefix == "z":
            data = _b58decode(rest)
        elif prefix in ("f", "F"):
            data = bytes.fromhex(rest)
        else:
            return None
        version, off = _read_varint(data, 0)
        if version != 1:
            return None
        codec_code, off = _read_varint(data, off)
        mh_code, off = _read_varint(data, off)
        mh_len, off = _read_varint(data, off)
        digest = data[off:off + mh_len]
        if mh_len == 0 or len(digest) != mh_len:
            return None
        return {
            "version": 1,
            "codec": CODECS.get(codec_code, "codec-0x%x" % codec_code),
            "codec_code": codec_code,
            "mh_code": mh_code,
            "digest": digest,
        }
    except Exception:
        return None


def valid(cid):
    """True when cid is a well-formed CIDv0 or CIDv1 with a non-empty digest."""
    parsed = decode_cid(cid)
    return bool(parsed and parsed.get("digest"))


def codec_of(cid):
    parsed = decode_cid(cid)
    return parsed["codec"] if parsed else "unknown"


def verify_block(cid, data):
    """True only when ``data`` is the exact block addressed by ``cid``."""
    if data is None:
        return False
    parsed = decode_cid(cid)
    if not parsed:
        return False
    mh = parsed["mh_code"]
    digest = parsed["digest"]
    if mh == SHA2_256:
        return hashlib.sha256(data).digest() == digest
    if mh == IDENTITY:
        return data == digest
    return False
