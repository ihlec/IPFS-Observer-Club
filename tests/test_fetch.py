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


def test_css_prefix_fetches_no_children(monkeypatch):
    got = []

    def fake_get_block(cid, gateway_offset=0):
        got.append(cid)
        return b"child-bytes"

    monkeypatch.setattr(fetch, "get_block", fake_get_block)
    node = _Node(
        b"@keyframes van-rotate{0%{opacity:1}to{opacity:0}}",
        [("c1", "bafychild1")],
    )
    fetch._assemble_file(node, child_fetches=got)
    assert got == []


def test_peek_hamt_pdfs_reads_one_shard(monkeypatch):
    from tests.cids import cid_pb_for

    pdf = cid_pb_for("paper")
    shard = cid_pb_for("shard")
    got = []

    class _Shard:
        unixfs_type = "hamt-shard"
        is_directory = True
        links = [("paper.pdf", pdf)]

    monkeypatch.setattr(fetch, "get_block", lambda cid, gateway_offset=0: got.append(cid) or b"block")
    monkeypatch.setattr(unixfs, "parse_dag_pb", lambda block: _Shard())
    out = fetch.peek_hamt_pdfs([("aa", shard)], max_blocks=1, max_pdfs=8)
    assert got == [shard]
    assert out == [("paper.pdf", pdf)]


def test_peek_hamt_pdfs_spreads_fanout(monkeypatch):
    from tests.cids import cid_pb_for

    shards = [cid_pb_for("s%d" % i) for i in range(8)]
    pdfs = [cid_pb_for("p%d" % i) for i in range(8)]
    got = []
    nodes = {}
    for i, shard in enumerate(shards):
        class _Shard:
            unixfs_type = "hamt-shard"
            is_directory = True
            links = [("paper.pdf", pdfs[i])]
        nodes[shard] = _Shard()

    monkeypatch.setattr(
        fetch, "get_block",
        lambda cid, gateway_offset=0: got.append(cid) or cid.encode(),
    )
    monkeypatch.setattr(unixfs, "parse_dag_pb", lambda block: nodes[block.decode()])
    links = [("%02x" % i, shards[i]) for i in range(8)]
    out = fetch.peek_hamt_pdfs(links, max_blocks=2, max_pdfs=8)
    assert got == [shards[0], shards[4]]
    assert out == [("paper.pdf", pdfs[0]), ("paper.pdf", pdfs[4])]


def test_peek_hamt_pdfs_walks_second_level(monkeypatch):
    from tests.cids import cid_pb_for

    l1 = cid_pb_for("l1")
    l2 = cid_pb_for("l2")
    pdf = cid_pb_for("paper")
    got = []

    class _L1:
        unixfs_type = "hamt-shard"
        is_directory = True
        links = [("bb", l2)]

    class _L2:
        unixfs_type = "hamt-shard"
        is_directory = True
        links = [("paper.pdf", pdf)]

    nodes = {l1: _L1(), l2: _L2()}
    monkeypatch.setattr(
        fetch, "get_block",
        lambda cid, gateway_offset=0: got.append(cid) or cid.encode(),
    )
    monkeypatch.setattr(unixfs, "parse_dag_pb", lambda block: nodes[block.decode()])
    out = fetch.peek_hamt_pdfs([("aa", l1)], max_blocks=2, max_pdfs=8)
    assert got == [l1, l2]
    assert out == [("paper.pdf", pdf)]
