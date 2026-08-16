"""Store ingest: append-only classifies, skip cannot hide a live classify."""
import sqlite3
import time

from observer import store, work
from observer.fields import normalize_field
from tests.cids import cid_for

CID = cid_for("x")
CID_A = cid_for("a")
CID_B = cid_for("b")
CID_PNG = cid_for("png")
CID_BLOG = cid_for("blog")
CID_TEST = cid_for("test")


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    store._local.conn = None
    return store.connect()


def test_normalize_field():
    assert normalize_field("Computer Science") == "computer-science"
    assert normalize_field("biology") == "biology"
    assert normalize_field("nope") == "other"


def test_ingest_classify_and_lookup(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    obj = {
        "kind": "classify",
        "cid": CID_TEST,
        "publisher": "peer1",
        "field": "Biology",
        "topic": "crispr",
        "keywords": "gene, editing",
        "text_sha256": "abc",
        "v": 1,
    }
    assert store.ingest_message(conn, obj) is True
    assert store.ingest_message(conn, obj) is False  # duplicate
    hit = store.lookup_cid(conn, CID_TEST)
    assert hit["kind"] == "classify"
    assert hit["field"] == "biology"
    found = store.classify_by_text_hash(conn, "abc")
    assert found["cid"] == CID_TEST


def test_ingest_rejects_placeholder_cid(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    assert store.ingest_message(conn, {
        "kind": "classify", "cid": "bafya", "publisher": "p",
        "field": "other", "v": 1,
    }) is False
    assert store.lookup_cid(conn, "bafya") is None


def test_purge_invalid_cids(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID, "publisher": "p",
        "field": "biology", "text_sha256": "ok", "v": 1,
    })
    conn.execute(
        "INSERT INTO classifies(payload_hash, cid, publisher, field, received_at) "
        "VALUES ('junk', 'bafya', 'p', 'other', 1)"
    )
    conn.commit()
    assert store.purge_invalid_cids(conn) == 1
    assert store.lookup_cid(conn, "bafya") is None
    assert store.lookup_cid(conn, CID)["kind"] == "classify"


def test_claim_expiry(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "claim", "cid": CID, "publisher": "p",
        "until": int(time.time()) + 60, "v": 1,
    })
    assert store.lookup_cid(conn, CID)["kind"] == "claim"
    conn.execute("UPDATE claims SET until = 1")
    conn.commit()
    store.expire_claims(conn)
    assert store.lookup_cid(conn, CID) is None


def test_purge_unprocessable_skips(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "skip", "cid": CID_PNG, "publisher": "p",
        "reason": "unprocessable", "v": 1, "sig": "aa",
    })
    store.ingest_message(conn, {
        "kind": "skip", "cid": CID_BLOG, "publisher": "p",
        "reason": "not_academic", "v": 1, "sig": "bb",
    })
    assert store.purge_unprocessable_skips(conn) == 1
    assert store.lookup_cid(conn, CID_PNG) is None
    assert store.lookup_cid(conn, CID_BLOG)["kind"] == "skip"
    kinds = [r[0] for r in conn.execute("SELECT kind FROM messages")]
    assert "skip" in kinds
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE cid = ?", (CID_PNG,)
    ).fetchone()[0] == 0


def test_unprocessable_skip_expires(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    monkeypatch.setitem(store.config.FETCH, "skip_ttl_seconds", 60)
    store.ingest_message(conn, {
        "kind": "skip", "cid": CID, "publisher": "p",
        "reason": "unprocessable", "v": 1,
    }, received_at=time.time() - 120)
    assert store.lookup_cid(conn, CID) is None
    store.expire_claims(conn)
    assert conn.execute("SELECT COUNT(*) FROM skips").fetchone()[0] == 0


def test_out_of_scope_skip_does_not_expire(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    monkeypatch.setitem(store.config.FETCH, "skip_ttl_seconds", 60)
    store.ingest_message(conn, {
        "kind": "skip", "cid": CID, "publisher": "p",
        "reason": "out_of_scope", "v": 1,
    }, received_at=time.time() - 120)
    assert store.lookup_cid(conn, CID)["kind"] == "skip"
    assert store.already_catalogued(CID) is True


def test_second_claim_from_same_publisher_ignored(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    until = int(time.time()) + 120
    store.ingest_message(conn, {
        "kind": "claim", "cid": CID_A, "publisher": "p",
        "until": until, "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "claim", "cid": CID_B, "publisher": "p",
        "until": until, "v": 1,
    })
    assert store.lookup_cid(conn, CID_A)["kind"] == "claim"
    assert store.lookup_cid(conn, CID_B) is None


def test_two_publishers_can_claim_same_cid(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    until = int(time.time()) + 120
    store.ingest_message(conn, {
        "kind": "claim", "cid": CID, "publisher": "a",
        "until": until, "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "claim", "cid": CID, "publisher": "b",
        "until": until, "v": 1,
    })
    n = conn.execute("SELECT COUNT(*) FROM claims WHERE cid = ?", (CID,)).fetchone()[0]
    assert n == 2
    assert store.lookup_cid(conn, CID)["kind"] == "claim"


def test_own_claim_is_not_skip_hook(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    monkeypatch.setattr("observer.clubd_client.peer_id", lambda: "me")
    store.ingest_message(conn, {
        "kind": "claim", "cid": CID, "publisher": "me",
        "until": int(time.time()) + 120, "v": 1,
    })
    assert store.lookup_cid(conn, CID) is None


def test_classify_beats_skip(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "skip", "cid": CID, "publisher": "attacker",
        "reason": "unprocessable", "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID, "publisher": "honest",
        "field": "biology", "text_sha256": "abc", "v": 1,
    })
    hit = store.lookup_cid(conn, CID)
    assert hit["kind"] == "classify"
    assert hit["publisher"] == "honest"

    store.ingest_message(conn, {
        "kind": "skip", "cid": CID, "publisher": "attacker2",
        "reason": "not_academic", "v": 1,
    })
    hit = store.lookup_cid(conn, CID)
    assert hit["kind"] == "classify"


def test_later_classify_does_not_replace(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID, "publisher": "first",
        "field": "biology", "topic": "real", "text_sha256": "aaa", "v": 1,
    }, received_at=100)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID, "publisher": "second",
        "field": "physics", "topic": "fake", "text_sha256": "bbb", "v": 1,
    }, received_at=200)
    n = conn.execute(
        "SELECT COUNT(*) FROM classifies WHERE cid = ?", (CID,)
    ).fetchone()[0]
    assert n == 2
    hit = store.lookup_cid(conn, CID)
    assert hit["publisher"] == "first"
    assert hit["field"] == "biology"


def test_alias_latest_wins_and_empty_clears(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    assert store.ingest_message(conn, {
        "kind": "alias", "publisher": "p1", "alias": "  lab one  ", "v": 1,
    }) is True
    row = conn.execute("SELECT alias FROM aliases WHERE publisher = 'p1'").fetchone()
    assert row[0] == "lab one"
    store.ingest_message(conn, {
        "kind": "alias", "publisher": "p1", "alias": "lab-two", "v": 1,
    })
    row = conn.execute("SELECT alias FROM aliases WHERE publisher = 'p1'").fetchone()
    assert row[0] == "lab-two"
    store.ingest_message(conn, {
        "kind": "alias", "publisher": "p1", "alias": "", "v": 1,
    })
    assert conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0] == 0


def test_local_alias_roundtrip(tmp_path, monkeypatch):
    _conn(tmp_path, monkeypatch)
    assert store.local_alias() == ""
    store.set_local_alias("  Berlin Lab  ", "12D3me")
    assert store.local_alias() == "Berlin Lab"
    row = store.connect().execute(
        "SELECT alias FROM aliases WHERE publisher = '12D3me'"
    ).fetchone()
    assert row[0] == "Berlin Lab"


def test_migrate_drops_retired_check_tables(tmp_path, monkeypatch):
    path = tmp_path / "club.sqlite"
    monkeypatch.setattr(store.config, "DB_PATH", str(path))
    store._local.conn = None
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE checks (payload_hash TEXT PRIMARY KEY);"
        "CREATE TABLE local_observations (cid TEXT PRIMARY KEY);"
        "CREATE TABLE messages ("
        " payload_hash TEXT PRIMARY KEY, kind TEXT, cid TEXT,"
        " publisher TEXT, body TEXT, received_at REAL);"
        "INSERT INTO messages VALUES ("
        " 'h1','attest','bafy','p','{}',1);"
    )
    raw.commit()
    raw.close()
    conn = store.connect()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}
    assert "checks" not in names
    assert "local_observations" not in names
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE kind IN ('attest', 'challenge')"
    ).fetchone()[0] == 0


def test_migrate_cid_primary_key(tmp_path, monkeypatch):
    path = tmp_path / "club.sqlite"
    monkeypatch.setattr(store.config, "DB_PATH", str(path))
    store._local.conn = None
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE classifies ("
        " cid TEXT PRIMARY KEY, publisher TEXT, payload_hash TEXT,"
        " mime_type TEXT, size INTEGER, filename TEXT, field TEXT, topic TEXT,"
        " keywords TEXT, license TEXT, text_sha256 TEXT, classifier TEXT,"
        " indexed_at REAL, received_at REAL);"
        " INSERT INTO classifies VALUES ("
        " '%s','p1','hash1',NULL,NULL,NULL,'biology','t','k',NULL,'aaa','',1,1);"
        % CID
    )
    raw.commit()
    raw.close()
    conn = store.connect()
    pk = [r[1] for r in conn.execute("PRAGMA table_info(classifies)") if r[5]]
    assert pk == ["payload_hash"]
    row = conn.execute("SELECT cid, publisher FROM classifies").fetchone()
    assert row[0] == CID and row[1] == "p1"
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID, "publisher": "p2",
        "field": "physics", "text_sha256": "bbb", "v": 1,
    })
    assert conn.execute("SELECT COUNT(*) FROM classifies").fetchone()[0] == 2


def _work(tmp_path, monkeypatch):
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    work._local.conn = None


def test_ingest_report_wrong_queues_review(tmp_path, monkeypatch):
    _work(tmp_path, monkeypatch)
    conn = _conn(tmp_path, monkeypatch)
    monkeypatch.setattr("observer.clubd_client.peer_id", lambda: "me")
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID, "publisher": "first",
        "field": "biology", "text_sha256": "aaa", "v": 1,
    })
    assert store.should_second_classify(CID) is True
    assert store.already_catalogued(CID) is False
    assert store.ingest_message(conn, {
        "kind": "report", "cid": CID, "publisher": "reporter",
        "reason": "wrong", "v": 1,
    }) is True
    assert store.needs_review(CID) is True
    assert store.local_classified(CID) is False
    assert store.already_catalogued(CID) is False
    row = work.connect().execute(
        "SELECT status, source FROM cids WHERE cid = ?", (CID,)
    ).fetchone()
    assert row["source"] == "report"
    assert row["status"] == "discovered"


def test_one_voter_enqueues_second_classify(tmp_path, monkeypatch):
    _work(tmp_path, monkeypatch)
    conn = _conn(tmp_path, monkeypatch)
    monkeypatch.setattr("observer.clubd_client.peer_id", lambda: "me")
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID, "publisher": "first",
        "field": "biology", "text_sha256": "aaa", "v": 1,
    })
    assert store.should_second_classify(CID) is True
    assert store.already_catalogued(CID) is False
    assert store.maybe_enqueue_second(CID) is True
    row = work.connect().execute(
        "SELECT status, source FROM cids WHERE cid = ?", (CID,)
    ).fetchone()
    assert row["source"] == "report"
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID, "publisher": "me",
        "field": "physics", "text_sha256": "bbb", "v": 1,
    })
    assert store.should_second_classify(CID) is False
    assert store.already_catalogued(CID) is True


def test_ingest_report_rejects_bad_reason(tmp_path, monkeypatch):
    _work(tmp_path, monkeypatch)
    conn = _conn(tmp_path, monkeypatch)
    assert store.ingest_message(conn, {
        "kind": "report", "cid": CID, "publisher": "p",
        "reason": "spam", "v": 1,
    }) is False
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0


def test_ingest_report_clear_retracts(tmp_path, monkeypatch):
    _work(tmp_path, monkeypatch)
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "report", "cid": CID, "publisher": "p",
        "reason": "abusive", "v": 1,
    })
    assert store.is_abusive(CID) is True
    store.ingest_message(conn, {
        "kind": "report", "cid": CID, "publisher": "p",
        "reason": "clear", "v": 1,
    })
    assert store.is_abusive(CID) is False
    assert conn.execute(
        "SELECT COUNT(*) FROM reports WHERE cid = ?", (CID,)
    ).fetchone()[0] == 0


def test_blacklist_drops_events_and_rebuilds_doc(tmp_path, monkeypatch):
    _work(tmp_path, monkeypatch)
    conn = _conn(tmp_path, monkeypatch)
    monkeypatch.setattr("observer.clubd_client.peer_id", lambda: "me")
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID, "publisher": "bad",
        "field": "biology", "topic": "fake", "text_sha256": "aaa", "v": 1,
    }, received_at=1)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID, "publisher": "good",
        "field": "physics", "topic": "real", "text_sha256": "bbb", "v": 1,
    }, received_at=2)
    store.ingest_message(conn, {
        "kind": "report", "cid": CID, "publisher": "bad",
        "reason": "abusive", "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "alias", "publisher": "bad", "alias": "spam-node", "v": 1,
    })
    assert store.is_abusive(CID) is True
    assert store.blacklist("bad") is True
    assert store.is_blacklisted("bad") is True
    assert store.is_abusive(CID) is False
    assert store.ingest_message(conn, {
        "kind": "classify", "cid": CID_A, "publisher": "bad",
        "field": "biology", "v": 1,
    }) is False
    hit = store.lookup_cid(conn, CID)
    assert hit["kind"] == "classify"
    assert hit["publisher"] == "good"
    assert hit["field"] == "physics"
    listed = store.list_blacklisted()
    assert listed[0]["publisher"] == "bad"
    assert listed[0]["alias"] == "spam-node"
    assert store.blacklist("me") is False


def test_ingest_report_clear_only_own_row(tmp_path, monkeypatch):
    _work(tmp_path, monkeypatch)
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "report", "cid": CID, "publisher": "a",
        "reason": "abusive", "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "report", "cid": CID, "publisher": "b",
        "reason": "abusive", "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "report", "cid": CID, "publisher": "a",
        "reason": "clear", "v": 1,
    })
    assert store.is_abusive(CID) is True
    pubs = {r[0] for r in conn.execute("SELECT publisher FROM reports")}
    assert pubs == {"b"}
