"""Work queue keeps only recent WANTs."""
import time

from observer import work


def _conn(tmp_path, monkeypatch, max_queue=80, max_age=900):
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    monkeypatch.setitem(work.config.FETCH, "max_queue", max_queue)
    monkeypatch.setitem(work.config.FETCH, "max_age_seconds", max_age)
    monkeypatch.setitem(work.config.FETCH, "min_age_seconds", 0)
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


def test_take_batch_prefers_raw(tmp_path, monkeypatch):
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
    assert [r["cid"] for r in rows] == ["bafy-raw"]


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


def test_prune_drops_sniffed_dag_pb(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch, max_queue=80, max_age=3600)
    now = time.time()
    conn.execute(
        "INSERT INTO cids(cid, codec, first_seen, last_seen, peer_count, "
        "want_count, status, attempts) VALUES (?,?,?,?,1,1,'discovered',0)",
        ("bafy-pb", "dag-pb", now - 5, now - 5),
    )
    conn.commit()
    assert work.prune(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM cids").fetchone()[0] == 0


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
