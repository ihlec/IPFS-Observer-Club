"""Multi-block PDFs must arrive whole, or not be judged at all.

These run against real UnixFS dag-pb blocks with real CIDs (tests/pdfdag.py),
so they cover assembly, CID verification and pypdf extraction together.
"""
import threading

import pytest

from observer import extract, fetch
from tests import pdfdag


class _Blockstore:
    """Serves fixture blocks, optionally failing a chosen CID."""

    def __init__(self, blocks, dead=()):
        self.blocks = blocks
        self.dead = set(dead)
        self.requests = []
        self.lock = threading.Lock()

    def get_block_ex(self, cid, gateway_offset=0):
        with self.lock:
            self.requests.append(cid)
        if cid in self.dead:
            return None, "timeout"
        block = self.blocks.get(cid)
        if block is None:
            return None, "http 404"
        return block, None

    def get_block(self, cid, gateway_offset=0):
        return self.get_block_ex(cid, gateway_offset)[0]

    def install(self, monkeypatch):
        monkeypatch.setattr(fetch, "get_block_ex", self.get_block_ex)
        monkeypatch.setattr(fetch, "get_block", self.get_block)
        return self


@pytest.fixture
def paper():
    """A 2.4 MB PDF spread over ten 256 KiB UnixFS leaves."""
    pdf = pdfdag.make_pdf(pages=600)
    root, blocks = pdfdag.file_dag(pdf)
    assert len(blocks) > 10, "fixture must be a multi-block DAG"
    return pdf, root, blocks


def test_fixture_blocks_verify_against_their_cids(paper):
    from observer import cidutil

    _pdf, _root, blocks = paper
    for cid, block in blocks.items():
        assert cidutil.verify_block(cid, block), cid


def test_multi_block_pdf_assembles_whole(paper, monkeypatch):
    pdf, root, blocks = paper
    store = _Blockstore(blocks).install(monkeypatch)

    result = fetch.fetch_cid(root, codec="dag-pb")

    assert result.ok
    assert not result.truncated
    assert result.data == pdf
    # Root plus every leaf, each fetched exactly once.
    assert sorted(store.requests) == sorted(blocks)


def test_multi_block_pdf_yields_extractable_text(paper, monkeypatch):
    """The xref table lives at the end, so a short read yields no text at all.

    Extraction itself only reads the opening pages, so the assertion that
    separates a whole file from a plausible prefix is that pypdf resolves the
    trailing xref and sees every page.
    """
    import io

    import pypdf

    _pdf, root, blocks = paper
    _Blockstore(blocks).install(monkeypatch)

    result = fetch.fetch_cid(root, codec="dag-pb")
    text, mime, _license, _source = extract.extract_document(
        result.data, result.mime_type, filename=result.filename,
    )

    assert mime == "application/pdf"
    assert extract.usable_text(text, mime)
    assert "protein structure prediction" in text
    reader = pypdf.PdfReader(io.BytesIO(result.data))
    assert len(reader.pages) == 600, "trailing xref did not survive assembly"


def test_one_dead_block_reports_truncated(paper, monkeypatch):
    pdf, root, blocks = paper
    dead = [cid for cid in blocks if cid != root][3]
    _Blockstore(blocks, dead=[dead]).install(monkeypatch)

    result = fetch.fetch_cid(root, codec="dag-pb")

    assert result.truncated
    assert len(result.data) < len(pdf)


def test_dead_block_retries_on_another_gateway(paper, monkeypatch):
    """A block missing from one gateway must not discard the whole paper."""
    pdf, root, blocks = paper
    flaky = [cid for cid in blocks if cid != root][3]
    seen = set()

    def get_block(cid, gateway_offset=0):
        if cid == flaky and cid not in seen:
            seen.add(cid)
            return None
        return blocks.get(cid)

    monkeypatch.setattr(fetch, "get_block", get_block)
    monkeypatch.setattr(
        fetch, "get_block_ex",
        lambda cid, gateway_offset=0: (blocks.get(cid), None),
    )

    result = fetch.fetch_cid(root, codec="dag-pb")

    assert not result.truncated
    assert result.data == pdf


def test_oversized_pdf_stops_after_sniff(monkeypatch):
    """Past the byte budget the tail is unreachable, so spend no more fetches."""
    pdf = pdfdag.make_pdf(pages=200)
    root, blocks = pdfdag.file_dag(pdf)
    monkeypatch.setattr(fetch, "MAX_PDF_BYTES", 400 * 1024)
    monkeypatch.setattr(fetch, "MAX_PDF_CHILD_BLOCKS", 40)
    store = _Blockstore(blocks).install(monkeypatch)

    result = fetch.fetch_cid(root, codec="dag-pb")

    assert result.truncated
    # Root plus the one block needed to recognise a PDF, and nothing more.
    assert len(store.requests) == 2


def test_children_are_fetched_concurrently(paper, monkeypatch):
    """Serial block fetches are what made large PDFs miss the retry window."""
    pdf, root, blocks = paper
    state = {"inside": 0, "peak": 0}
    lock = threading.Lock()
    overlapped = threading.Event()

    def get_block(cid, gateway_offset=0):
        with lock:
            state["inside"] += 1
            state["peak"] = max(state["peak"], state["inside"])
            if state["inside"] > 1:
                overlapped.set()
        # Linger only until overlap is observed, so a serial implementation
        # finishes (and fails the assert) instead of hanging.
        overlapped.wait(timeout=0.5)
        with lock:
            state["inside"] -= 1
        return blocks.get(cid)

    monkeypatch.setattr(fetch, "get_block", get_block)
    monkeypatch.setattr(
        fetch, "get_block_ex",
        lambda cid, gateway_offset=0: (blocks.get(cid), None),
    )

    result = fetch.fetch_cid(root, codec="dag-pb")

    assert result.data == pdf
    assert state["peak"] > 1, "child blocks were fetched one at a time"


def test_single_block_pdf_still_works(monkeypatch):
    pdf = pdfdag.make_pdf(pages=40)
    root, blocks = pdfdag.file_dag(pdf)
    assert len(blocks) == 1
    store = _Blockstore(blocks).install(monkeypatch)

    result = fetch.fetch_cid(root, codec="dag-pb")
    text, mime, _license, _source = extract.extract_document(result.data)

    assert not result.truncated
    assert result.data == pdf
    assert mime == "application/pdf"
    assert extract.usable_text(text, mime)
    assert len(store.requests) == 1


def test_directory_is_reported_not_assembled(monkeypatch):
    pdf = pdfdag.make_pdf(pages=40)
    file_cid, file_blocks = pdfdag.file_dag(pdf)
    dir_cid, dir_blocks = pdfdag.directory_dag([
        ("paper.pdf", file_cid, len(file_blocks[file_cid])),
    ])
    blocks = dict(file_blocks)
    blocks.update(dir_blocks)
    store = _Blockstore(blocks).install(monkeypatch)

    result = fetch.fetch_cid(dir_cid, codec="dag-pb")

    assert result.is_directory
    assert result.mime_type == "inode/directory"
    assert result.links == [("paper.pdf", file_cid)]
    # A folder costs one block; children are queued, not walked here.
    assert store.requests == [dir_cid]
