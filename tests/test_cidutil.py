"""CID verification and extract fingerprints."""
import hashlib

from observer import cidutil, extract, protocol


def _uvarint(n):
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _cidv1_raw(data, codec=0x55, mh=0x12):
    digest = hashlib.sha256(data).digest()
    raw = _uvarint(1) + _uvarint(codec) + _uvarint(mh) + _uvarint(len(digest)) + digest
    import base64
    return "b" + base64.b32encode(raw).decode().lower().rstrip("=")


def test_verify_block_raw_sha256():
    data = b"hello observer"
    cid = _cidv1_raw(data)
    assert cidutil.codec_of(cid) == "raw"
    assert cidutil.verify_block(cid, data) is True
    assert cidutil.verify_block(cid, data + b"x") is False
    assert cidutil.verify_block(cid, data[:-1]) is False


def test_verify_refuses_unknown_hash():
    # mh code 0x13 = sha2-512 — unknown hash functions stay unverified.
    data = b"x"
    digest = hashlib.sha256(data).digest()  # wrong length for 512, still parsed
    raw = _uvarint(1) + _uvarint(0x55) + _uvarint(0x13) + _uvarint(len(digest)) + digest
    import base64
    cid = "b" + base64.b32encode(raw).decode().lower().rstrip("=")
    assert cidutil.verify_block(cid, data) is False


def test_verify_identity_hash():
    data = b"tiny"
    raw = _uvarint(1) + _uvarint(0x55) + _uvarint(0x00) + _uvarint(len(data)) + data
    import base64
    cid = "b" + base64.b32encode(raw).decode().lower().rstrip("=")
    assert cidutil.verify_block(cid, data) is True
    assert cidutil.verify_block(cid, b"nope") is False


def test_placeholders_are_not_valid():
    assert cidutil.valid("bafya") is False
    assert cidutil.valid("bafyb") is False
    assert cidutil.valid("bafy") is False
    assert cidutil.valid("") is False
    from tests.cids import cid_for
    assert cidutil.valid(cid_for("x")) is True


def test_fingerprint_matches_protocol_and_normalization():
    text, mime, _, _ = extract.extract_document(b"hello   world\n", "text/plain")
    assert mime == "text/plain"
    assert text == "hello world"
    assert extract.fingerprint(text) == protocol.text_sha256("hello world")
    assert extract.fingerprint(text) != extract.fingerprint("hello  world")


def test_unprocessable_yields_empty_text():
    text, mime, _, _ = extract.extract_document(b"\x89PNG\r\n\x1a\n", None)
    assert mime == "image/png"
    assert text == ""


def test_usable_text_rejects_short_pdf():
    assert extract.usable_text("", "application/pdf") is False
    assert extract.usable_text("short", "application/pdf") is False
    assert extract.usable_text("x" * 80, "application/pdf") is True
    assert extract.usable_text("hello world", "text/html") is True
