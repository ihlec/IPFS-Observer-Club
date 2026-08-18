"""Work queue keeps only recent WANTs."""
import time

from observer import work


def _conn(tmp_path, monkeypatch, max_queue=80, max_age=900):
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    monkeypatch.setitem(work.config.FETCH, "max_queue", max_queue)
    monkeypatch.setitem(work.config.FETCH, "max_age_seconds", max_age)
    monkeypatch.setitem(work.config.FETCH, "min_age_seconds", 0)
    monkeypatch.setitem(work.config.FETCH, "max_dir_queue", 40)
    work._local.conn = None
    return work.connect()


def _insert(conn, cid, last_seen, status="discovered"):
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,?,0)",
        (cid, "raw", last_seen, last_seen, status),
    )
    conn.commit()


def test_prune_drops_stale(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_age=60)
    now = time.time()
    _insert(conn, "bafy-old", now - 120)
    _insert(conn, "bafy-new", now - 10)
    assert work.prune(conn) == 1
    cids = {r[0] for r in conn.execute("SELECT cid FROM cids")}
    assert cids == {"bafy-new"}


def test_prune_caps_queue_to_newest(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_queue=2, max_age=3600)
    now = time.time()
    _insert(conn, "bafy-a", now - 30)
    _insert(conn, "bafy-b", now - 20)
    _insert(conn, "bafy-c", now - 5)
    assert work.prune(conn) == 1
    cids = {r[0] for r in conn.execute("SELECT cid FROM cids")}
    assert cids == {"bafy-b", "bafy-c"}


def test_prune_evicts_folders_before_raw(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_queue=1, max_age=3600)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-raw", "raw", now - 5, now - 5),
    )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-pb", "dag-pb", now - 30, now - 30),
    )
    conn.commit()
    assert work.prune(conn) == 1
    cids = {r[0] for r in conn.execute("SELECT cid FROM cids")}
    assert cids == {"bafy-raw"}


def test_take_batch_includes_sniffed_dag_pb(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_age=3600)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-raw", "raw", now - 20, now - 5),
    )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-pb", "dag-pb", now - 20, now - 10),
    )
    conn.commit()
    monkeypatch.setitem(work.config.FETCH, "prefer_min_peer_count", 1)
    rows = work.take_batch(conn, limit=5)
    assert [r["cid"] for r in rows] == ["bafy-pb", "bafy-raw"]


def test_take_batch_prefers_unixfs_over_popular_raw(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_age=3600)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,5,5,'discovered',0)",
        ("bafy-raw", "raw", now - 20, now - 5),
    )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-pb", "dag-pb", now - 20, now - 10),
    )
    conn.commit()
    monkeypatch.setitem(work.config.FETCH, "prefer_min_peer_count", 2)
    rows = work.take_batch(conn, limit=1)
    assert [r["cid"] for r in rows] == ["bafy-pb"]


def test_take_batch_fetches_named_after_age(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_age=10)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts, source, filename) "
        "VALUES (?,?,?,?,1,1,'discovered',0,'named','paper.pdf')",
        ("bafy-pdf", "dag-pb", now - 60, now - 60),
    )
    conn.commit()
    monkeypatch.setitem(work.config.FETCH, "prefer_min_peer_count", 1)
    rows = work.take_batch(conn, limit=5)
    assert [r["cid"] for r in rows] == ["bafy-pdf"]


def test_take_batch_fetches_reported_dag_pb(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_age=3600)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts, source) VALUES (?,?,?,?,1,1,'discovered',0,'report')",
        ("bafy-pb", "dag-pb", now - 20, now - 5),
    )
    conn.commit()
    monkeypatch.setitem(work.config.FETCH, "prefer_min_peer_count", 1)
    rows = work.take_batch(conn, limit=5)
    assert [r["cid"] for r in rows] == ["bafy-pb"]


def test_prune_keeps_sniffed_dag_pb(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_queue=80, max_age=3600)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-pb", "dag-pb", now - 5, now - 5),
    )
    conn.commit()
    assert work.prune(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM cids").fetchone()[0] == 1


def test_prune_keeps_reported(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_queue=1, max_age=10)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts, source) VALUES (?,?,?,?,1,1,'discovered',0,'report')",
        ("bafy-keep", "dag-pb", now - 200, now - 200),
    )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-old", "raw", now - 200, now - 200),
    )
    conn.commit()
    work.prune(conn)
    cids = {r[0] for r in conn.execute("SELECT cid FROM cids")}
    assert "bafy-keep" in cids
    assert "bafy-old" not in cids


def test_expire_local_unprocessable(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    monkeypatch.setitem(work.config.FETCH, "skip_ttl_seconds", 60)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, last_checked, "
        "peer_count, want_count, status, attempts, error) "
        "VALUES ('bafy-old','raw',?,?,?,1,1,'skipped',0,'unprocessable')",
        (now - 200, now - 200, now - 200),
    )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, last_checked, "
        "peer_count, want_count, status, attempts, error) "
        "VALUES ('bafy-keep','raw',?,?,?,1,1,'skipped',0,'not_academic')",
        (now - 200, now - 200, now - 200),
    )
    conn.commit()
    assert work.expire_local_skips(conn) == 1
    rows = {r["cid"]: r["status"] for r in conn.execute("SELECT cid, status FROM cids")}
    assert rows["bafy-old"] == "discovered"
    assert rows["bafy-keep"] == "skipped"


def _insert_skipped_pdf(conn, cid, checked_at):
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, last_checked, "
        "peer_count, want_count, status, attempts, mime_type, error) "
        "VALUES (?,'dag-pb',?,?,?,1,1,'skipped',0,'application/pdf','out_of_scope')",
        (cid, checked_at, checked_at, checked_at),
    )
    conn.commit()


def test_expire_requeues_scope_skipped_pdfs_after_ttl(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    ttl = work.skip_ttl_seconds()
    _insert_skipped_pdf(conn, "bafy-pdf", time.time() - ttl - 60)
    assert work.expire_local_skips(conn) == 1
    row = conn.execute("SELECT status, error FROM cids WHERE cid='bafy-pdf'").fetchone()
    assert row["status"] == "discovered"
    assert row["error"] == "pdf_retry"


def test_expire_leaves_fresh_scope_skipped_pdf_alone(tmp_path, monkeypatch):
    """A PDF skipped moments ago must not be refetched on the next prune."""
    conn = _conn(tmp_path, monkeypatch)
    _insert_skipped_pdf(conn, "bafy-pdf", time.time())
    assert work.expire_local_skips(conn) == 0
    row = conn.execute("SELECT status FROM cids WHERE cid='bafy-pdf'").fetchone()
    assert row["status"] == "skipped"


def test_expire_requeues_incomplete_pdf(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    ttl = work.skip_ttl_seconds()
    then = time.time() - ttl - 60
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, last_checked, "
        "peer_count, want_count, status, attempts, mime_type, error) "
        "VALUES ('bafy-inc','dag-pb',?,?,?,1,1,'skipped',0,'application/pdf','incomplete')",
        (then, then, then),
    )
    conn.commit()
    assert work.expire_local_skips(conn) >= 1
    row = conn.execute("SELECT status FROM cids WHERE cid='bafy-inc'").fetchone()
    assert row["status"] == "discovered"


def test_prune_keeps_named(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_queue=1, max_age=10)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts, source, filename) "
        "VALUES (?,?,?,?,1,1,'discovered',0,'named','paper.pdf')",
        ("bafy-keep", "dag-pb", now - 200, now - 200),
    )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-old", "raw", now - 200, now - 200),
    )
    conn.commit()
    work.prune(conn)
    cids = {r[0] for r in conn.execute("SELECT cid FROM cids")}
    assert "bafy-keep" in cids
    assert "bafy-old" not in cids


def test_prune_caps_sniffed_folders(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_queue=80, max_age=3600)
    monkeypatch.setitem(work.config.FETCH, "max_dir_queue", 3)
    now = time.time()
    for i in range(6):
        conn.execute(
            "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
            "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
            ("bafy-pb-%d" % i, "dag-pb", now - 10 + i, now - 10 + i),
        )
    conn.commit()
    work.prune(conn)
    left = {r[0] for r in conn.execute("SELECT cid FROM cids")}
    assert "bafy-pb-5" in left
    assert "bafy-pb-0" not in left
    assert len(left) == 3


def test_take_batch_caps_sniffed_dir_probes(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_age=3600)
    monkeypatch.setitem(work.config.FETCH, "max_dir_probes", 4)
    monkeypatch.setitem(work.config.FETCH, "prefer_min_peer_count", 1)
    now = time.time()
    for i in range(4):
        conn.execute(
            "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
            "want_count, status, attempts) VALUES (?,?,?,?,1,1,'processing',0)",
            ("bafy-busy-%d" % i, "dag-pb", now - 20, now - 5),
        )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-pb", "dag-pb", now - 20, now - 5),
    )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-raw", "raw", now - 20, now - 5),
    )
    conn.commit()
    rows = work.take_batch(conn, limit=1)
    assert [r["cid"] for r in rows] == ["bafy-raw"]


def test_take_batch_named_pdf_ignores_dir_probe_cap(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_age=3600)
    monkeypatch.setitem(work.config.FETCH, "max_dir_probes", 0)
    monkeypatch.setitem(work.config.FETCH, "prefer_min_peer_count", 1)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts, source, filename) "
        "VALUES (?,?,?,?,1,1,'discovered',0,'named','paper.pdf')",
        ("bafy-pdf", "dag-pb", now - 20, now - 5),
    )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-raw", "raw", now - 20, now - 5),
    )
    conn.commit()
    rows = work.take_batch(conn, limit=1)
    assert [r["cid"] for r in rows] == ["bafy-pdf"]


def test_take_batch_prefers_named_pdf_over_dag_pb(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_age=3600)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,5,5,'discovered',0)",
        ("bafy-pb", "dag-pb", now - 20, now - 5),
    )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts, source, filename) "
        "VALUES (?,?,?,?,1,1,'discovered',0,'named','paper.pdf')",
        ("bafy-pdf", "raw", now - 20, now - 30),
    )
    conn.commit()
    monkeypatch.setitem(work.config.FETCH, "prefer_min_peer_count", 1)
    rows = work.take_batch(conn, limit=1)
    assert [r["cid"] for r in rows] == ["bafy-pdf"]


def test_enqueue_doc_children_pdf_only(tmp_path, monkeypatch):
    from observer import store
    from tests.cids import cid_for, cid_pb_for

    conn = _conn(tmp_path, monkeypatch, max_age=3600)
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    store._local.conn = None
    pdf = cid_pb_for("paper")
    html = cid_for("notes")
    png = cid_for("fig")
    n = work.enqueue_doc_children([
        ("paper.pdf", pdf),
        ("notes.HTML", html),
        ("fig.png", png),
        ("readme.txt", cid_for("readme")),
    ])
    assert n == 1
    rows = {r["cid"]: r for r in conn.execute("SELECT * FROM cids")}
    assert rows[pdf]["source"] == "named"
    assert rows[pdf]["filename"] == "paper.pdf"
    assert html not in rows
    assert png not in rows


def test_prune_drops_named_html(tmp_path, monkeypatch):
    from tests.cids import cid_for, cid_pb_for

    conn = _conn(tmp_path, monkeypatch, max_age=3600)
    now = time.time()
    html = cid_for("wiki")
    pdf = cid_pb_for("paper")
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts, source, filename) "
        "VALUES (?,?,?,?,1,1,'discovered',0,'named','Chick_fil_A.html')",
        (html, "raw", now, now),
    )
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts, source, filename) "
        "VALUES (?,?,?,?,1,1,'discovered',0,'named','paper.pdf')",
        (pdf, "dag-pb", now, now),
    )
    conn.commit()
    assert work.drop_named_non_pdf(conn) == 1
    cids = {r[0] for r in conn.execute("SELECT cid FROM cids")}
    assert cids == {pdf}


def test_enqueue_doc_children_caps(tmp_path, monkeypatch):
    from observer import store
    from tests.cids import cid_pb_for

    conn = _conn(tmp_path, monkeypatch, max_age=3600)
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    store._local.conn = None
    monkeypatch.setitem(work.config.FETCH, "max_dir_docs", 2)
    monkeypatch.setitem(work.config.FETCH, "max_named", 2)
    links = [("p%d.pdf" % i, cid_pb_for("p%d" % i)) for i in range(5)]
    assert work.enqueue_doc_children(links) == 2
    assert conn.execute("SELECT COUNT(*) FROM cids").fetchone()[0] == 2
