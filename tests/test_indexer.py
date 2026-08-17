"""Indexer skip-hook, fingerprint reuse, and local LLM publish."""
import time

from observer import classify, indexer, store, work
from observer.fetch import FetchResult
from tests.cids import cid_for

CID = cid_for("test")
CID_OLD = cid_for("old")
CID_NEW = cid_for("new")
CID_A = cid_for("a")
CID_B = cid_for("b")


def _dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    store._local.conn = None
    work._local.conn = None
    indexer.reset_claims()
    monkeypatch.setattr(indexer.clubd_client, "publish", lambda *a, **k: False)
    return work.connect(), store.connect()


def _discovered(wconn, cid=CID, codec="raw"):
    now = time.time() - 60
    wconn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,?,1,'discovered',0)",
        (cid, codec, now, now, 1),
    )
    wconn.commit()
    return wconn.execute("SELECT * FROM cids WHERE cid = ?", (cid,)).fetchone()


def _sample(data, mime="text/plain"):
    r = FetchResult()
    r.ok = True
    r.data = data
    r.mime_type = mime
    r.size = len(data)
    return r


def test_skip_hook_uses_existing_classify(tmp_path, monkeypatch):
    wconn, club = _dbs(tmp_path, monkeypatch)
    store.ingest_message(club, {
        "kind": "classify", "cid": CID, "publisher": "peer-a",
        "field": "biology", "topic": "crispr", "text_sha256": "abc", "v": 1,
    }, received_at=1)
    store.ingest_message(club, {
        "kind": "classify", "cid": CID, "publisher": "peer-b",
        "field": "physics", "topic": "qft", "text_sha256": "def", "v": 1,
    }, received_at=2)
    called = []
    monkeypatch.setattr(indexer.fetch, "fetch_cid", lambda *a, **k: called.append("fetch"))
    monkeypatch.setattr(indexer.classify, "classify", lambda *a, **k: called.append("llm"))
    row = _discovered(wconn)
    assert indexer.process_one(wconn, row) is True
    assert called == []
    status = wconn.execute("SELECT status FROM cids WHERE cid=?", (CID,)).fetchone()[0]
    assert status == "indexed"


def test_one_voter_runs_second_classify(tmp_path, monkeypatch):
    wconn, club = _dbs(tmp_path, monkeypatch)
    store.ingest_message(club, {
        "kind": "classify", "cid": CID, "publisher": "peer",
        "field": "biology", "topic": "crispr", "text_sha256": "abc", "v": 1,
    })
    monkeypatch.setattr("observer.clubd_client.peer_id", lambda: "me")
    called = []
    published = []
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: called.append("fetch") or _sample(
            b"Abstract. CRISPR genome editing in mice. doi:10.1234/example. References."
        ),
    )
    monkeypatch.setattr(indexer.clubd_client, "publish_claim", lambda cid: True)
    monkeypatch.setattr(indexer.classify, "available", lambda: True)
    monkeypatch.setattr(
        indexer.clubd_client, "publish_classify",
        lambda cid, **fields: published.append(fields) or True,
    )
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: called.append("llm") or {
            "in_scope": True, "field": "physics",
            "topic": "qft", "keywords": "quantum", "license": None,
        },
    )
    row = _discovered(wconn)
    assert indexer.process_one(wconn, row) is True
    assert "fetch" in called
    assert "llm" in called
    assert published[0]["field"] == "physics"
    assert published[0]["classifier"]["kind"] == "llm"


def test_unprocessable_stays_local(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    published = []
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: _sample(b"\x89PNG\r\n\x1a\n", "image/png"),
    )
    monkeypatch.setattr(
        indexer.clubd_client, "publish_skip",
        lambda cid, mime, reason: published.append((cid, mime, reason)) or True,
    )
    monkeypatch.setattr(indexer.classify, "classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("llm")))
    row = _discovered(wconn)
    assert indexer.process_one(wconn, row) is True
    assert published == []
    assert wconn.execute("SELECT COUNT(*) FROM cids").fetchone()[0] == 0
    assert work.is_unprocessable(CID) is True


def test_css_stays_local(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    published = []
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: _sample(b"@keyframes van-rotate{0%{opacity:1}}"),
    )
    monkeypatch.setattr(
        indexer.clubd_client, "publish_skip",
        lambda cid, mime, reason: published.append((cid, mime, reason)) or True,
    )
    monkeypatch.setattr(indexer.classify, "classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("llm")))
    row = _discovered(wconn)
    assert indexer.process_one(wconn, row) is True
    assert published == []
    assert work.is_unprocessable(CID) is True


def test_fingerprint_reuse_skips_llm(tmp_path, monkeypatch):
    wconn, club = _dbs(tmp_path, monkeypatch)
    from observer import extract
    text = "hello world"
    fp = extract.fingerprint(text)
    store.ingest_message(club, {
        "kind": "classify", "cid": CID_OLD, "publisher": "peer",
        "field": "physics", "topic": "qft", "keywords": "quantum",
        "text_sha256": fp, "v": 1,
    })
    published = []
    monkeypatch.setattr(indexer.fetch, "fetch_cid", lambda *a, **k: _sample(text.encode()))
    monkeypatch.setattr(
        indexer.clubd_client, "publish_classify",
        lambda cid, **fields: published.append((cid, fields)) or True,
    )
    monkeypatch.setattr(indexer.classify, "classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("llm")))
    row = _discovered(wconn, cid=CID_NEW)
    assert indexer.process_one(wconn, row) is True
    assert len(published) == 1
    cid, fields = published[0]
    assert cid == CID_NEW
    assert fields["classifier"]["kind"] == "reuse"
    assert fields["field"] == "physics"


def test_heuristic_skip_bypasses_llm(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    published = []
    called = []
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: _sample(b"click here to sign in and add to cart"),
    )
    monkeypatch.setattr(indexer.clubd_client, "publish_skip",
                        lambda cid, mime, reason: published.append(reason) or True)
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: called.append("llm") or {"academic_document": True},
    )
    row = _discovered(wconn)
    assert indexer.process_one(wconn, row) is True
    assert published == ["out_of_scope"]
    assert called == []


def test_llm_not_academic_publishes_skip(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    published = []
    monkeypatch.setattr(indexer.fetch, "fetch_cid", lambda *a, **k: _sample(
        ("This travel diary describes our holiday in Spain. Smith et al. " * 40).encode()
    ))
    monkeypatch.setattr(indexer.clubd_client, "publish_claim", lambda cid: True)
    monkeypatch.setattr(indexer.classify, "available", lambda: True)
    monkeypatch.setattr(indexer.clubd_client, "publish_skip",
                        lambda cid, mime, reason: published.append(reason) or True)
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: {"in_scope": False, "field": "other",
                         "topic": "", "keywords": "", "license": None},
    )
    row = _discovered(wconn)
    assert indexer.process_one(wconn, row) is True
    assert published == ["out_of_scope"]


def test_likely_prior_does_not_mint_classify_when_llm_says_no(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    published = []
    skipped = []
    monkeypatch.setattr(indexer.fetch, "fetch_cid", lambda *a, **k: _sample(
        b"Abstract. We report CRISPR genome editing in mice. "
        b"doi:10.1234/example. References. et al."
    ))
    monkeypatch.setattr(indexer.clubd_client, "publish_claim", lambda cid: True)
    monkeypatch.setattr(indexer.classify, "available", lambda: True)
    monkeypatch.setattr(indexer.clubd_client, "publish_skip",
                        lambda *a, **k: skipped.append(a) or True)
    monkeypatch.setattr(
        indexer.clubd_client, "publish_classify",
        lambda cid, **fields: published.append(fields) or True,
    )
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: {"in_scope": False, "field": "biology",
                         "topic": "crispr", "keywords": "gene", "license": None},
    )
    row = _discovered(wconn)
    assert indexer.process_one(wconn, row) is True
    assert skipped == []
    assert published == []
    row = wconn.execute("SELECT status, error FROM cids").fetchone()
    assert row["status"] == "skipped"
    assert row["error"] == "llm_disagreed"
    assert store.already_catalogued(CID) is False


def test_llm_academic_publishes_classify(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    published = []
    monkeypatch.setattr(indexer.fetch, "fetch_cid", lambda *a, **k: _sample(
        b"Abstract. CRISPR genome editing in mice. doi:10.1234/example. References."
    ))
    monkeypatch.setattr(indexer.clubd_client, "publish_claim", lambda cid: True)
    monkeypatch.setattr(indexer.classify, "available", lambda: True)
    monkeypatch.setattr(
        indexer.clubd_client, "publish_classify",
        lambda cid, **fields: published.append(fields) or True,
    )
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: {"in_scope": True, "field": "biology",
                         "topic": "crispr", "keywords": "gene", "license": None},
    )
    row = _discovered(wconn)
    assert indexer.process_one(wconn, row) is True
    assert published[0]["field"] == "biology"
    assert published[0]["classifier"]["kind"] == "llm"
    status = wconn.execute("SELECT status FROM cids").fetchone()[0]
    assert status == "indexed"


def test_publish_failed_does_not_burn_attempts(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: _sample(
            b"Abstract. CRISPR genome editing in mice. doi:10.1234/example. References."
        ),
    )
    monkeypatch.setattr(indexer.clubd_client, "publish_claim", lambda cid: True)
    monkeypatch.setattr(indexer.classify, "available", lambda: True)
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: {"in_scope": True, "field": "biology",
                         "topic": "crispr", "keywords": "gene", "license": None},
    )
    monkeypatch.setattr(indexer.clubd_client, "publish_classify", lambda *a, **k: False)
    row = _discovered(wconn)
    assert indexer.process_one(wconn, row) is False
    out = wconn.execute("SELECT status, attempts, error FROM cids").fetchone()
    assert out["status"] == "discovered"
    assert out["attempts"] == 0
    assert out["error"] == "publish_failed"


def test_failed_classify_does_not_stampede_claims(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    claims = []
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: _sample(
            b"Abstract. CRISPR genome editing in mice. doi:10.1234/example. References."
        ),
    )
    monkeypatch.setattr(
        indexer.clubd_client, "publish_claim",
        lambda cid: claims.append(cid) or True,
    )
    monkeypatch.setattr(indexer.classify, "available", lambda: True)
    monkeypatch.setattr(indexer.classify, "classify", lambda *a, **k: None)
    row = _discovered(wconn, cid=CID_A)
    assert indexer.process_one(wconn, row) is False
    row2 = _discovered(wconn, cid=CID_B)
    assert indexer.process_one(wconn, row2) is False
    assert claims == [CID_A]


def test_successful_skip_frees_claim_for_next_cid(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    claims = []
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: _sample(
            ("This travel diary describes our holiday in Spain. Smith et al. " * 40).encode()
        ),
    )
    monkeypatch.setattr(
        indexer.clubd_client, "publish_claim",
        lambda cid: claims.append(cid) or True,
    )
    monkeypatch.setattr(indexer.classify, "available", lambda: True)
    monkeypatch.setattr(indexer.clubd_client, "publish_skip", lambda *a, **k: True)
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: {"in_scope": False, "field": "other",
                         "topic": "", "keywords": "", "license": None},
    )
    assert indexer.process_one(wconn, _discovered(wconn, cid=CID_A)) is True
    assert indexer.process_one(wconn, _discovered(wconn, cid=CID_B)) is True
    assert claims == [CID_A, CID_B]


def test_invalid_cid_is_dropped(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    row = _discovered(wconn, cid="bafya")
    assert indexer.process_one(wconn, row) is True
    assert wconn.execute("SELECT COUNT(*) FROM cids").fetchone()[0] == 0


def test_directory_is_forgotten(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    called = []
    published = []
    result = FetchResult()
    result.ok = True
    result.is_directory = True
    result.mime_type = "inode/directory"
    monkeypatch.setattr(indexer.fetch, "fetch_cid", lambda *a, **k: result)
    monkeypatch.setattr(
        indexer.clubd_client, "publish_skip",
        lambda cid, mime, reason: published.append(reason) or True,
    )
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: called.append("llm") or {},
    )
    assert indexer.process_one(wconn, _discovered(wconn)) is True
    assert called == []
    assert published == []
    assert wconn.execute("SELECT COUNT(*) FROM cids").fetchone()[0] == 0
    evicted = wconn.execute("SELECT COUNT(*) FROM evicted").fetchone()[0]
    assert evicted == 1


def test_directory_enqueues_named_children(tmp_path, monkeypatch):
    from tests.cids import cid_pb_for

    wconn, _club = _dbs(tmp_path, monkeypatch)
    pdf = cid_pb_for("paper")
    html = cid_pb_for("html")
    result = FetchResult()
    result.ok = True
    result.is_directory = True
    result.mime_type = "inode/directory"
    result.links = [
        ("paper.pdf", pdf), ("index.html", html),
        ("photo.jpg", cid_pb_for("jpg")),
    ]
    monkeypatch.setattr(indexer.fetch, "fetch_cid", lambda *a, **k: result)
    monkeypatch.setattr(indexer.clubd_client, "publish_skip", lambda *a, **k: True)
    monkeypatch.setattr(indexer.classify, "classify", lambda *a, **k: {})
    assert indexer.process_one(wconn, _discovered(wconn)) is True
    assert wconn.execute("SELECT cid FROM cids WHERE cid=?", (CID,)).fetchone() is None
    assert wconn.execute("SELECT cid FROM cids WHERE cid=?", (html,)).fetchone() is None
    row = wconn.execute("SELECT source, filename FROM cids WHERE cid=?", (pdf,)).fetchone()
    assert row["source"] == "named"
    assert row["filename"] == "paper.pdf"


def test_hamt_directory_peeks_pdf_children(tmp_path, monkeypatch):
    from tests.cids import cid_pb_for

    wconn, _club = _dbs(tmp_path, monkeypatch)
    pdf = cid_pb_for("paper")
    result = FetchResult()
    result.ok = True
    result.is_directory = True
    result.unixfs_type = "hamt-shard"
    result.mime_type = "inode/directory"
    result.links = [("aa", cid_pb_for("shard"))]
    monkeypatch.setattr(indexer.fetch, "fetch_cid", lambda *a, **k: result)
    monkeypatch.setattr(
        indexer.fetch, "peek_hamt_pdfs",
        lambda links, **k: [("paper.pdf", pdf)],
    )
    monkeypatch.setattr(indexer.clubd_client, "publish_skip", lambda *a, **k: True)
    monkeypatch.setattr(indexer.classify, "classify", lambda *a, **k: {})
    assert indexer.process_one(wconn, _discovered(wconn)) is True
    row = wconn.execute("SELECT source, filename FROM cids WHERE cid=?", (pdf,)).fetchone()
    assert row["source"] == "named"
    assert row["filename"] == "paper.pdf"


def test_short_pdf_goes_to_llm(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    called = []
    published = []
    text = "Page 1. Extracted words from a scanned methods article. " * 4
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: _sample(b"%PDF-1.4 " + text.encode(), "application/pdf"),
    )
    monkeypatch.setattr(
        indexer.extract, "extract_document",
        lambda *a, **k: (text, "application/pdf", None, None),
    )
    monkeypatch.setattr(indexer.classify, "available", lambda: True)
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: called.append("llm") or {
            "in_scope": True, "field": "biology", "topic": "methods",
            "keywords": "scan", "license": None, "model": "x", "provider": "lmstudio",
        },
    )
    monkeypatch.setattr(
        indexer.clubd_client, "publish_classify",
        lambda cid, **fields: published.append(cid) or True,
    )
    monkeypatch.setattr(indexer, "_try_claim", lambda cid: False)
    assert indexer.process_one(wconn, _discovered(wconn)) is True
    assert called == ["llm"]
    assert published == [CID]


def test_pdf_scope_skip_is_fetched_again(tmp_path, monkeypatch):
    wconn, club = _dbs(tmp_path, monkeypatch)
    store.ingest_message(club, {
        "kind": "skip", "cid": CID, "publisher": "peer",
        "mime_type": "application/pdf", "reason": "out_of_scope", "v": 1,
    })
    called = []
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: called.append("fetch") or _sample(b"%PDF-1.4 x", "application/pdf"),
    )
    monkeypatch.setattr(
        indexer.extract, "extract_document",
        lambda *a, **k: ("Page 1. Extracted words from a scanned article. " * 4,
                         "application/pdf", None, None),
    )
    monkeypatch.setattr(indexer.classify, "available", lambda: True)
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: called.append("llm") or {
            "in_scope": True, "field": "biology", "topic": "x",
            "keywords": "", "license": None, "model": "x", "provider": "lmstudio",
        },
    )
    monkeypatch.setattr(
        indexer.clubd_client, "publish_classify",
        lambda cid, **fields: True,
    )
    monkeypatch.setattr(indexer, "_try_claim", lambda cid: False)
    assert indexer.process_one(wconn, _discovered(wconn)) is True
    assert "fetch" in called
    assert "llm" in called


def test_short_pdf_stays_local(tmp_path, monkeypatch):
    wconn, _club = _dbs(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: _sample(b"%PDF-1.4 scan", "application/pdf"),
    )
    monkeypatch.setattr(
        indexer.extract, "extract_document",
        lambda *a, **k: ("ab", "application/pdf", None, None),
    )
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: called.append("llm") or {},
    )
    assert indexer.process_one(wconn, _discovered(wconn)) is True
    assert called == []
    row = wconn.execute("SELECT status, error FROM cids").fetchone()
    assert row["status"] == "skipped"
    assert row["error"] == "unprocessable"


def test_wrong_report_reruns_foreign_classify(tmp_path, monkeypatch):
    wconn, club = _dbs(tmp_path, monkeypatch)
    store.ingest_message(club, {
        "kind": "classify", "cid": CID, "publisher": "peer",
        "field": "biology", "topic": "crispr", "text_sha256": "abc", "v": 1,
    })
    store.ingest_message(club, {
        "kind": "report", "cid": CID, "publisher": "reporter",
        "reason": "wrong", "v": 1,
    })
    monkeypatch.setattr("observer.clubd_client.peer_id", lambda: "me")
    called = []
    published = []
    monkeypatch.setattr(
        indexer.fetch, "fetch_cid",
        lambda *a, **k: called.append("fetch") or _sample(
            b"Abstract. CRISPR genome editing in mice. doi:10.1234/example. References."
        ),
    )
    monkeypatch.setattr(indexer.clubd_client, "publish_claim", lambda cid: True)
    monkeypatch.setattr(indexer.classify, "available", lambda: True)
    monkeypatch.setattr(
        indexer.clubd_client, "publish_classify",
        lambda cid, **fields: published.append(fields) or True,
    )
    monkeypatch.setattr(
        indexer.classify, "classify",
        lambda *a, **k: called.append("llm") or {
            "in_scope": True, "field": "physics",
            "topic": "qft", "keywords": "quantum", "license": None,
        },
    )
    row = wconn.execute("SELECT * FROM cids WHERE cid = ?", (CID,)).fetchone()
    if row is None:
        row = _discovered(wconn)
    else:
        row = wconn.execute("SELECT * FROM cids WHERE cid = ?", (CID,)).fetchone()
    assert indexer.process_one(wconn, row) is True
    assert "fetch" in called
    assert "llm" in called
    assert published[0]["field"] == "physics"


def test_wrong_report_does_not_rerun_local_classify(tmp_path, monkeypatch):
    wconn, club = _dbs(tmp_path, monkeypatch)
    monkeypatch.setattr("observer.clubd_client.peer_id", lambda: "me")
    store.ingest_message(club, {
        "kind": "classify", "cid": CID, "publisher": "me",
        "field": "biology", "topic": "crispr", "text_sha256": "abc", "v": 1,
    })
    store.ingest_message(club, {
        "kind": "report", "cid": CID, "publisher": "reporter",
        "reason": "wrong", "v": 1,
    })
    called = []
    monkeypatch.setattr(indexer.fetch, "fetch_cid", lambda *a, **k: called.append("fetch"))
    monkeypatch.setattr(indexer.classify, "classify", lambda *a, **k: called.append("llm"))
    row = _discovered(wconn)
    assert indexer.process_one(wconn, row) is True
    assert called == []
    status = wconn.execute("SELECT status FROM cids WHERE cid=?", (CID,)).fetchone()[0]
    assert status == "indexed"
