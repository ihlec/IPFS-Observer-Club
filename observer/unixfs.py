"""Minimal dag-pb + UnixFS decoding, pure Python (no deps).

Used so hash-only checks assemble the same file bytes the classifier
fingerprints. We only need:

  - file vs directory
  - inline data and child link CIDs for chunked files

dag-pb PBNode:   field 1 = Data (bytes), field 2 = Links (repeated PBLink)
dag-pb PBLink:   field 1 = Hash (bytes/CID), field 2 = Name (string), field 3 = Tsize
UnixFS Data:     field 1 = Type (enum), field 2 = Data (bytes), field 3 = filesize
                 Type: 0=Raw 1=Directory 2=File 3=Metadata 4=Symlink 5=HAMTShard
"""
UNIXFS_TYPES = {0: "raw", 1: "directory", 2: "file", 3: "metadata",
                4: "symlink", 5: "hamt-shard"}


def _read_varint(data, pos):
    shift = 0
    result = 0
    while True:
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not b & 0x80:
            return result, pos
        shift += 7


def _read_fields(data):
    """Yield (field_number, wire_type, value) for a protobuf message."""
    pos = 0
    n = len(data)
    while pos < n:
        key, pos = _read_varint(data, pos)
        field = key >> 3
        wt = key & 0x7
        if wt == 0:
            val, pos = _read_varint(data, pos)
            yield field, wt, val
        elif wt == 2:
            length, pos = _read_varint(data, pos)
            yield field, wt, data[pos:pos + length]
            pos += length
        elif wt == 5:
            yield field, wt, data[pos:pos + 4]
            pos += 4
        elif wt == 1:
            yield field, wt, data[pos:pos + 8]
            pos += 8
        else:
            break


def _cid_from_hash(raw):
    """Encode a link Hash (raw CID bytes) as a base32 CIDv1 or base58 CIDv0."""
    try:
        if len(raw) == 34 and raw[0] == 0x12 and raw[1] == 0x20:
            return _b58encode(raw)
        alphabet = "abcdefghijklmnopqrstuvwxyz234567"
        bits = 0
        value = 0
        out = []
        for byte in raw:
            value = (value << 8) | byte
            bits += 8
            while bits >= 5:
                bits -= 5
                out.append(alphabet[(value >> bits) & 0x1F])
        if bits:
            out.append(alphabet[(value << (5 - bits)) & 0x1F])
        return "b" + "".join(out)
    except Exception:
        return None


def _b58encode(raw):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = int.from_bytes(raw, "big")
    enc = ""
    while num:
        num, rem = divmod(num, 58)
        enc = alphabet[rem] + enc
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + enc


class Node(object):
    def __init__(self):
        self.unixfs_type = None
        self.inline_data = b""
        self.links = []
        self.is_directory = False
        self.is_file = False
        self.filesize = None


def parse_dag_pb(block):
    """Parse a dag-pb block into a Node. Raises on malformed input."""
    node = Node()
    pbdata = None
    for field, wt, val in _read_fields(block):
        if field == 1 and wt == 2:
            pbdata = val
        elif field == 2 and wt == 2:
            name, chash = None, None
            for lf, lwt, lval in _read_fields(val):
                if lf == 1 and lwt == 2:
                    chash = _cid_from_hash(lval)
                elif lf == 2 and lwt == 2:
                    name = lval.decode("utf-8", errors="replace")
            if chash:
                node.links.append((name, chash))

    if pbdata is not None:
        for field, wt, val in _read_fields(pbdata):
            if field == 1 and wt == 0:
                node.unixfs_type = UNIXFS_TYPES.get(val, "unknown")
            elif field == 2 and wt == 2:
                node.inline_data = val
            elif field == 3 and wt == 0:
                node.filesize = val

    node.is_directory = node.unixfs_type in ("directory", "hamt-shard")
    node.is_file = node.unixfs_type in ("file", "raw")
    return node


_FILE_EXT = (
    ".pdf", ".html", ".htm", ".txt", ".md", ".css", ".js", ".mjs", ".cjs",
    ".ts", ".jsx", ".tsx", ".map", ".json", ".wasm", ".less", ".scss",
)


def pick_filename(names):
    """Best UnixFS link name that looks like a file, or None."""
    fallback = None
    for raw in names or ():
        if not raw:
            continue
        base = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not base or base in (".", "..") or len(base) > 180:
            continue
        if base.isdigit():
            continue
        lower = base.lower()
        if any(lower.endswith(ext) for ext in _FILE_EXT):
            return base
        if fallback is None:
            fallback = base
    return fallback
