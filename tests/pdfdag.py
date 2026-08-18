"""Build real PDFs and real UnixFS dag-pb blocks for fetch/extract tests.

The blocks here are byte-identical in structure to what Kubo produces, so
``cidutil.verify_block`` and ``unixfs.parse_dag_pb`` accept them unchanged.
Tests can therefore exercise the assembly path with genuine CIDs instead of
stubbing ``get_block`` behaviour.
"""
from __future__ import annotations

import hashlib

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

CHUNK = 262144


def _b58encode(raw):
    num = int.from_bytes(raw, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + out


def cid_v0(block):
    """CIDv0 (dag-pb, sha2-256) of an encoded block."""
    return _b58encode(b"\x12\x20" + hashlib.sha256(block).digest())


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field, wire):
    return _varint((field << 3) | wire)


def _bytes_field(field, value):
    return _tag(field, 2) + _varint(len(value)) + value


def _varint_field(field, value):
    return _tag(field, 0) + _varint(value)


def unixfs_data(type_code, data=b"", filesize=None, blocksizes=()):
    out = _varint_field(1, type_code)
    if data:
        out += _bytes_field(2, data)
    if filesize is not None:
        out += _varint_field(3, filesize)
    for size in blocksizes:
        out += _varint_field(4, size)
    return out


def _link(cid_str, name, tsize):
    digest = _b58decode(cid_str) if cid_str.startswith("Qm") else None
    if digest is None:
        raise ValueError("expected CIDv0 link")
    out = _bytes_field(1, digest)
    if name is not None:
        out += _bytes_field(2, name.encode())
    out += _varint_field(3, tsize)
    return out


def _b58decode(s):
    num = 0
    for ch in s:
        num = num * 58 + _B58.index(ch)
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def pb_node(pbdata=None, links=()):
    """Encode a PBNode. Links come first, matching go-merkledag output."""
    out = b""
    for cid_str, name, tsize in links:
        out += _bytes_field(2, _link(cid_str, name, tsize))
    if pbdata is not None:
        out += _bytes_field(1, pbdata)
    return out


def leaf_block(chunk):
    """A UnixFS file leaf that wraps ``chunk`` (Kubo without --raw-leaves)."""
    return pb_node(unixfs_data(2, data=chunk, filesize=len(chunk)))


def file_dag(payload, chunk_size=CHUNK):
    """Return (root_cid, {cid: block}) for ``payload`` as a chunked UnixFS file.

    Single-chunk payloads collapse into one root node, exactly as Kubo does.
    """
    blocks = {}
    chunks = [payload[i:i + chunk_size]
              for i in range(0, len(payload), chunk_size)] or [b""]
    if len(chunks) == 1:
        root = pb_node(unixfs_data(2, data=chunks[0], filesize=len(payload)))
        cid = cid_v0(root)
        blocks[cid] = root
        return cid, blocks

    links = []
    sizes = []
    for chunk in chunks:
        block = leaf_block(chunk)
        cid = cid_v0(block)
        blocks[cid] = block
        links.append((cid, None, len(block)))
        sizes.append(len(chunk))
    root = pb_node(
        unixfs_data(2, filesize=len(payload), blocksizes=sizes), links=links,
    )
    root_cid = cid_v0(root)
    blocks[root_cid] = root
    return root_cid, blocks


def directory_dag(entries):
    """Return (root_cid, {cid: block}) for a UnixFS directory of (name, cid, tsize)."""
    root = pb_node(unixfs_data(1), links=[
        (cid, name, tsize) for name, cid, tsize in entries
    ])
    cid = cid_v0(root)
    return cid, {cid: root}


def make_pdf(pages=1, line="Deep learning for protein structure prediction"):
    """A valid, uncompressed PDF whose text pypdf can extract.

    ``pages`` scales the byte size so tests can cross the 256 KiB chunk
    boundary the way a real paper does.
    """
    objects = []
    page_ids = list(range(3, 3 + pages * 2, 2))
    kids = " ".join("%d 0 R" % pid for pid in page_ids)
    objects.append((1, "<< /Type /Catalog /Pages 2 0 R >>"))
    objects.append((2, "<< /Type /Pages /Count %d /Kids [%s] >>" % (pages, kids)))
    for index, pid in enumerate(page_ids):
        stream_id = pid + 1
        text = []
        # Enough lines per page to make each page a few KiB of real content.
        for row in range(40):
            text.append(
                "BT /F1 11 Tf 40 %d Td (%s page %d line %d) Tj ET"
                % (740 - row * 18, line, index + 1, row)
            )
        body = "\n".join(text)
        objects.append((pid,
                        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                        "/Resources << /Font << /F1 << /Type /Font "
                        "/Subtype /Type1 /BaseFont /Helvetica >> >> >> "
                        "/Contents %d 0 R >>" % stream_id))
        objects.append((stream_id,
                        "<< /Length %d >>\nstream\n%s\nendstream"
                        % (len(body), body)))

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num, body in objects:
        offsets[num] = len(out)
        out += ("%d 0 obj\n%s\nendobj\n" % (num, body)).encode("latin-1")
    startxref = len(out)
    highest = max(offsets)
    out += ("xref\n0 %d\n" % (highest + 1)).encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, highest + 1):
        if num in offsets:
            out += ("%010d 00000 n \n" % offsets[num]).encode()
        else:
            out += b"0000000000 65535 f \n"
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (highest + 1, startxref)).encode()
    return bytes(out)
