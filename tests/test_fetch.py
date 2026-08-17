"""First-block MIME abort does not fetch extra children."""
from observer import fetch, unixfs


class _Node:
    def __init__(self, inline, links, filesize=None):
        self.inline_data = inline
        self.links = links
        self.filesize = filesize
        self.is_directory = False
        self.is_file = True


def test_png_prefix_fetches_no_children(monkeypatch):
    got = []

    def fake_get_block(cid, gateway_offset=0):
        got.append(cid)
        return b"child-bytes"

    monkeypatch.setattr(fetch, "get_block", fake_get_block)
    node = _Node(b"\x89PNG\r\n\x1a\n" + b"xxxx", [("c1", "bafychild1"), ("c2", "bafychild2")])
    data, _trunc = fetch._assemble_file(node, child_fetches=got)
    assert data.startswith(b"\x89PNG")
    assert got == []


def test_empty_inline_stops_after_first_unprocessable_child(monkeypatch):
    got = []

    def fake_get_block(cid, gateway_offset=0):
        got.append(cid)
        return b"\xff\xd8\xff" + b"\x00" * 32

    monkeypatch.setattr(fetch, "get_block", fake_get_block)
    monkeypatch.setattr(unixfs, "parse_dag_pb", lambda block: (_ for _ in ()).throw(ValueError("raw")))
    node = _Node(b"", [("a", "bafya"), ("b", "bafyb")])
    fetch._assemble_file(node)
    assert got == ["bafya"]


def test_nested_unixfs_file_uses_inline_not_protobuf(monkeypatch):
    got = []
    pdf = b"%PDF-1.4 nested-chunk"

    class _File:
        def __init__(self, inline, links=None):
            self.inline_data = inline
            self.links = links or []
            self.is_directory = False
            self.unixfs_type = "file"

    def fake_get_block(cid, gateway_offset=0):
        return b"protobuf-wrapper-" + cid.encode()

    def fake_parse(block):
        if block.endswith(b"bafymid"):
            return _File(b"", [("leaf", "bafyleaf")])
        if block.endswith(b"bafyleaf"):
            return _File(pdf)
        raise ValueError("raw")

    monkeypatch.setattr(fetch, "get_block", fake_get_block)
    monkeypatch.setattr(unixfs, "parse_dag_pb", fake_parse)
    node = _Node(b"", [("mid", "bafymid")])
    data, _trunc = fetch._assemble_file(node, child_fetches=got)
    assert data.startswith(b"%PDF")
    assert b"protobuf-wrapper" not in data
    assert got == ["bafymid", "bafyleaf"]
