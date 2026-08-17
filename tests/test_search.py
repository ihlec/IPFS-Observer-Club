"""Search collapses competing classifies to the first-seen record."""
from observer import extract, search, store, work
from tests.cids import cid_for

CID1 = cid_for("1")
CID2 = cid_for("2")
CID3 = cid_for("3")


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    store._local.conn = None
    return store.connect()


def test_search_returns_preferred_not_later_overwrite(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID1, "publisher": "first",
        "field": "biology", "topic": "crispr", "keywords": "gene",
        "text_sha256": extract.fingerprint("a"), "v": 1,
    }, received_at=1)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID1, "publisher": "second",
        "field": "physics", "topic": "crispr", "keywords": "fake",
        "text_sha256": extract.fingerprint("b"), "v": 1,
    }, received_at=2)
    monkeypatch.setattr(search, "store", store)
    # search.search uses store.connect() which is already this tmp db
    rows = search.search("crispr")
    assert len(rows) == 1
    assert rows[0]["publisher"] == "first"
    assert rows[0]["field"] == "biology"
    assert rows[0]["label_voters"] == 2
    assert "attestations" not in rows[0]


def test_ranking_counts_distinct_cids(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID1, "publisher": "busy",
        "field": "biology", "text_sha256": "a", "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID2, "publisher": "busy",
        "field": "biology", "text_sha256": "b", "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID1, "publisher": "busy",
        "field": "physics", "text_sha256": "c", "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID3, "publisher": "quiet",
        "field": "biology", "text_sha256": "d", "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "alias", "publisher": "busy", "alias": "lab-one", "v": 1,
    })
    monkeypatch.setattr(search, "store", store)
    rank = search.observers()
    assert [r["observer"] for r in rank] == ["busy", "quiet"]
    assert rank[0]["n_classify"] == 2
    assert rank[0]["alias"] == "lab-one"
    assert rank[1]["n_classify"] == 1
    assert rank[1]["alias"] == ""
    assert search.stats()["observers"] == 2


def test_browse_uses_one_row_per_cid(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID1, "publisher": "first",
        "field": "biology", "topic": "crispr", "indexed_at": 2, "v": 1,
    }, received_at=2)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID1, "publisher": "second",
        "field": "physics", "topic": "other", "indexed_at": 9, "v": 1,
    }, received_at=9)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID2, "publisher": "first",
        "field": "biology", "topic": "rna", "indexed_at": 1, "v": 1,
    }, received_at=1)
    monkeypatch.setattr(search, "store", store)
    rows, total = search.browse(limit=10)
    assert total == 2
    assert [r["cid"] for r in rows] == [CID1, CID2]
    assert rows[0]["publisher"] == "first"


def test_abusive_report_hidden_from_search_and_browse(tmp_path, monkeypatch):
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    work._local.conn = None
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID1, "publisher": "first",
        "field": "biology", "topic": "crispr", "keywords": "gene",
        "text_sha256": extract.fingerprint("a"), "v": 1,
    }, received_at=1)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID2, "publisher": "first",
        "field": "biology", "topic": "rna", "keywords": "gene",
        "text_sha256": extract.fingerprint("b"), "v": 1,
    }, received_at=2)
    store.ingest_message(conn, {
        "kind": "report", "cid": CID1, "publisher": "reporter",
        "reason": "abusive", "v": 1,
    })
    monkeypatch.setattr(search, "store", store)
    rows = search.search("gene")
    assert [r["cid"] for r in rows] == [CID2]
    browsed, total = search.browse(limit=10)
    assert total == 1
    assert [r["cid"] for r in browsed] == [CID2]


def test_search_matches_label_prefixes(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID1, "publisher": "first",
        "field": "biology", "topic": "CRISPR off-target", "keywords": "genome",
        "text_sha256": extract.fingerprint("a"), "v": 1,
    }, received_at=1)
    monkeypatch.setattr(search, "store", store)
    assert [r["cid"] for r in search.search("bio")] == [CID1]
    assert [r["cid"] for r in search.search("cris")] == [CID1]
    assert [r["cid"] for r in search.search("geno")] == [CID1]
    assert search.search("phys") == []
