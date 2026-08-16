"""SQLite catalog for verified club messages.

``classifies`` is append-only history. ``docs`` is one row per CID (first
seen) and is what search uses. A skip never hides a live classify.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from . import cidutil, club, config, protocol
from .fields import normalize_field

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS messages (
    payload_hash TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    cid          TEXT NOT NULL,
    publisher    TEXT NOT NULL,
    body         TEXT NOT NULL,
    received_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_cid_kind ON messages(cid, kind);
CREATE INDEX IF NOT EXISTS idx_messages_kind_time ON messages(kind, received_at);

CREATE TABLE IF NOT EXISTS classifies (
    payload_hash  TEXT PRIMARY KEY,
    cid           TEXT NOT NULL,
    publisher     TEXT NOT NULL,
    mime_type     TEXT,
    size          INTEGER,
    filename      TEXT,
    field         TEXT,
    topic         TEXT,
    keywords      TEXT,
    license       TEXT,
    text_sha256   TEXT,
    classifier    TEXT,
    indexed_at    REAL,
    received_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_classifies_cid ON classifies(cid, received_at);
CREATE INDEX IF NOT EXISTS idx_classifies_hash ON classifies(text_sha256);
CREATE INDEX IF NOT EXISTS idx_classifies_field ON classifies(field);

CREATE TABLE IF NOT EXISTS docs (
    cid         TEXT PRIMARY KEY,
    publisher   TEXT NOT NULL,
    mime_type   TEXT,
    size        INTEGER,
    filename    TEXT,
    field       TEXT,
    topic       TEXT,
    keywords    TEXT,
    license     TEXT,
    text_sha256 TEXT,
    indexed_at  REAL,
    received_at REAL
);
CREATE INDEX IF NOT EXISTS idx_docs_field_time ON docs(field, indexed_at DESC);
CREATE INDEX IF NOT EXISTS idx_docs_time ON docs(indexed_at DESC);
CREATE INDEX IF NOT EXISTS idx_docs_mime ON docs(mime_type);
CREATE INDEX IF NOT EXISTS idx_docs_hash ON docs(text_sha256);

CREATE TABLE IF NOT EXISTS skips (
    cid         TEXT PRIMARY KEY,
    publisher   TEXT NOT NULL,
    mime_type   TEXT,
    reason      TEXT,
    received_at REAL
);

CREATE TABLE IF NOT EXISTS claims (
    publisher  TEXT PRIMARY KEY,
    cid        TEXT NOT NULL,
    until      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_cid ON claims(cid, until);

CREATE TABLE IF NOT EXISTS reports (
    cid         TEXT NOT NULL,
    publisher   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    received_at REAL,
    PRIMARY KEY (cid, publisher)
);
CREATE INDEX IF NOT EXISTS idx_reports_reason ON reports(reason, cid);

CREATE TABLE IF NOT EXISTS aliases (
    publisher    TEXT PRIMARY KEY,
    alias        TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    received_at  REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklisted (
    publisher   TEXT PRIMARY KEY,
    alias       TEXT,
    received_at REAL
);

CREATE TABLE IF NOT EXISTS report_proposals (
    cid         TEXT NOT NULL,
    reason      TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    proposed_at REAL,
    PRIMARY KEY (cid, reason)
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    cid UNINDEXED, filename, field, topic, keywords
);
"""


def connect():
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA cache_size = -65536")
        conn.execute("PRAGMA mmap_size = 268435456")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        _local.conn = conn
    return conn


def _migrate_claims(conn):
    """One live claim per publisher. Older catalogs keyed claims by cid."""
    cols = list(conn.execute("PRAGMA table_info(claims)"))
    if not cols:
        return
    pk = [r[1] for r in cols if r[5]]
    if pk == ["publisher"]:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_cid ON claims(cid, until)")
        return
    rows = list(conn.execute("SELECT publisher, cid, until FROM claims"))
    conn.execute("DROP TABLE claims")
    conn.execute("""
        CREATE TABLE claims (
            publisher  TEXT PRIMARY KEY,
            cid        TEXT NOT NULL,
            until      REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_cid ON claims(cid, until)")
    for publisher, cid, until in rows:
        conn.execute(
            "INSERT OR REPLACE INTO claims(publisher, cid, until) VALUES (?, ?, ?)",
            (publisher, cid, until),
        )


def _migrate_proposals(conn):
    cols = list(conn.execute("PRAGMA table_info(report_proposals)"))
    if not cols:
        return
    pk = [r[1] for r in cols if r[5]]
    if pk == ["cid", "reason"]:
        return
    rows = list(conn.execute(
        "SELECT cid, reason, count, proposed_at FROM report_proposals"
    ))
    conn.execute("DROP TABLE report_proposals")
    conn.execute("""
        CREATE TABLE report_proposals (
            cid         TEXT NOT NULL,
            reason      TEXT NOT NULL,
            count       INTEGER NOT NULL DEFAULT 1,
            proposed_at REAL,
            PRIMARY KEY (cid, reason)
        )
    """)
    for cid, reason, count, proposed_at in rows:
        conn.execute(
            "INSERT OR REPLACE INTO report_proposals(cid, reason, count, proposed_at) "
            "VALUES (?, ?, ?, ?)",
            (cid, reason or "abusive", count or 1, proposed_at),
        )


def _migrate(conn):
    """Drop retired check tables and rebuild classifies if cid was the PK."""
    conn.execute("DROP TABLE IF EXISTS checks")
    conn.execute("DROP TABLE IF EXISTS local_observations")
    bl = [r[1] for r in conn.execute("PRAGMA table_info(blacklisted)")]
    if bl and "alias" not in bl:
        conn.execute("ALTER TABLE blacklisted ADD COLUMN alias TEXT")
    conn.execute("DELETE FROM messages WHERE kind IN ('attest', 'challenge')")
    _migrate_claims(conn)
    _migrate_proposals(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_classifies_cid ON classifies(cid, received_at)"
    )
    cols = list(conn.execute("PRAGMA table_info(classifies)"))
    if not cols:
        _ensure_docs(conn)
        return
    pk_cols = [r[1] for r in cols if r[5]]
    if pk_cols == ["payload_hash"]:
        _ensure_docs(conn)
        return
    conn.execute("ALTER TABLE classifies RENAME TO classifies_old")
    conn.execute("DROP TABLE IF EXISTS classifies_fts")
    conn.execute("""
        CREATE TABLE classifies (
            payload_hash  TEXT PRIMARY KEY,
            cid           TEXT NOT NULL,
            publisher     TEXT NOT NULL,
            mime_type     TEXT,
            size          INTEGER,
            filename      TEXT,
            field         TEXT,
            topic         TEXT,
            keywords      TEXT,
            license       TEXT,
            text_sha256   TEXT,
            classifier    TEXT,
            indexed_at    REAL,
            received_at   REAL
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO classifies("
        "payload_hash, cid, publisher, mime_type, size, filename, field, topic, "
        "keywords, license, text_sha256, classifier, indexed_at, received_at) "
        "SELECT payload_hash, cid, publisher, mime_type, size, filename, field, "
        "topic, keywords, license, text_sha256, classifier, indexed_at, received_at "
        "FROM classifies_old"
    )
    conn.execute("DROP TABLE classifies_old")
    _ensure_docs(conn)


def _add_stat(conn, key, delta):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    n = (int(row[0]) if row else 0) + int(delta)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (key, str(n)),
    )


def cached_count(conn, key, fallback_sql, fallback_params=()):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    if row is not None:
        try:
            return int(row[0])
        except (TypeError, ValueError):
            pass
    n = conn.execute(fallback_sql, fallback_params).fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (key, str(int(n))),
    )
    return int(n)


def _index_doc(conn, cid):
    row = conn.execute(
        "SELECT rowid, cid, filename, field, topic, keywords FROM docs WHERE cid = ?",
        (cid,),
    ).fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (row[0],))
    conn.execute(
        "INSERT INTO docs_fts(rowid, cid, filename, field, topic, keywords) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row[0], row[1], row[2] or "", row[3] or "", row[4] or "", row[5] or ""),
    )


def _upsert_doc(conn, obj, now):
    field = normalize_field(obj.get("field"))
    cur = conn.execute(
        "INSERT OR IGNORE INTO docs("
        "cid, publisher, mime_type, size, filename, field, topic, keywords, "
        "license, text_sha256, indexed_at, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (obj["cid"], obj["publisher"], obj.get("mime_type"), obj.get("size"),
         obj.get("filename"), field, obj.get("topic"), obj.get("keywords"),
         obj.get("license"), obj.get("text_sha256"),
         float(obj.get("indexed_at") or now), now),
    )
    if cur.rowcount:
        _index_doc(conn, obj["cid"])
        _add_stat(conn, "docs_count", 1)


def _ensure_docs(conn):
    """One search row per CID. Backfill once from first-seen classifies."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_docs_field_time ON docs(field, indexed_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_time ON docs(indexed_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_mime ON docs(mime_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_hash ON docs(text_sha256)")
    n = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    if n:
        cached_count(conn, "docs_count", "SELECT COUNT(*) FROM docs")
        conn.execute("DROP TABLE IF EXISTS classifies_fts")
        return
    conn.execute(
        "INSERT OR IGNORE INTO docs("
        "cid, publisher, mime_type, size, filename, field, topic, keywords, "
        "license, text_sha256, indexed_at, received_at) "
        "SELECT c.cid, c.publisher, c.mime_type, c.size, c.filename, c.field, "
        "c.topic, c.keywords, c.license, c.text_sha256, c.indexed_at, c.received_at "
        "FROM classifies c "
        "INNER JOIN ("
        "  SELECT cid, MIN(received_at) AS ts FROM classifies GROUP BY cid"
        ") first ON first.cid = c.cid AND first.ts = c.received_at"
    )
    for row in conn.execute("SELECT rowid, cid, filename, field, topic, keywords FROM docs"):
        conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (row[0],))
        conn.execute(
            "INSERT INTO docs_fts(rowid, cid, filename, field, topic, keywords) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row[0], row[1], row[2] or "", row[3] or "", row[4] or "", row[5] or ""),
        )
    cached_count(conn, "docs_count", "SELECT COUNT(*) FROM docs")
    conn.execute("DROP TABLE IF EXISTS classifies_fts")


def already_catalogued(cid):
    """True when the club already has a classify or live skip for this CID."""
    try:
        hit = lookup_cid(connect(), cid)
    except sqlite3.Error:
        return False
    if not hit or hit.get("kind") not in ("classify", "skip"):
        return False
    if must_classify(cid):
        return False
    return True


def local_classified(cid, publisher=None):
    """True when this node already published a classify for the CID."""
    me = publisher
    if not me:
        from . import clubd_client
        me = clubd_client.peer_id()
    if not me:
        return False
    row = connect().execute(
        "SELECT 1 FROM classifies WHERE cid = ? AND publisher = ?",
        (cid, me),
    ).fetchone()
    return row is not None


def needs_review(cid):
    """True when a peer asked for a second classification."""
    row = connect().execute(
        "SELECT 1 FROM reports WHERE cid = ? AND reason = 'wrong'",
        (cid,),
    ).fetchone()
    return row is not None


def _peer_id():
    from . import clubd_client
    return clubd_client.peer_id()


def independent_publishers(cid):
    """Publishers with a non-reuse classify for this CID."""
    from . import labels
    return labels.independent_publishers(connect(), cid)


def should_second_classify(cid):
    """True when this node should cast the second independent vote."""
    if local_classified(cid):
        return False
    pubs = independent_publishers(cid)
    return len(pubs) == 1


def must_classify(cid):
    """True when skip-hook and fingerprint reuse must not run."""
    if local_classified(cid):
        return False
    return needs_review(cid) or should_second_classify(cid)


def is_abusive(cid):
    row = connect().execute(
        "SELECT 1 FROM reports WHERE cid = ? AND reason = 'abusive'",
        (cid,),
    ).fetchone()
    return row is not None


def propose_report(cid, reason):
    """Queue a guest report for the local admin. Not gossiped."""
    reason = (reason or "").strip().lower()
    if reason not in ("abusive", "wrong") or not cidutil.valid(cid):
        return False
    if reason == "abusive" and is_abusive(cid):
        return False
    conn = connect()
    now = time.time()
    conn.execute(
        "INSERT INTO report_proposals(cid, reason, count, proposed_at) "
        "VALUES (?, ?, 1, ?) "
        "ON CONFLICT(cid, reason) DO UPDATE SET "
        "  count = count + 1, "
        "  proposed_at = excluded.proposed_at",
        (cid, reason, now),
    )
    conn.commit()
    return True


def drop_proposal(cid, reason=None):
    conn = connect()
    if reason:
        conn.execute(
            "DELETE FROM report_proposals WHERE cid = ? AND reason = ?",
            (cid, reason),
        )
    else:
        conn.execute("DELETE FROM report_proposals WHERE cid = ?", (cid,))
    conn.commit()


def list_proposals():
    conn = connect()
    rows = conn.execute(
        "SELECT p.cid, p.reason, p.count, p.proposed_at, "
        "d.field, d.topic, d.keywords, d.mime_type, d.filename "
        "FROM report_proposals p "
        "LEFT JOIN docs d ON d.cid = p.cid "
        "ORDER BY p.proposed_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def is_blacklisted(publisher):
    if not publisher:
        return False
    row = connect().execute(
        "SELECT 1 FROM blacklisted WHERE publisher = ?", (publisher,),
    ).fetchone()
    return row is not None


def list_blacklisted():
    conn = connect()
    rows = conn.execute(
        "SELECT publisher, received_at, alias FROM blacklisted "
        "ORDER BY received_at DESC"
    ).fetchall()
    return [
        {"publisher": r[0], "received_at": r[1], "alias": r[2] or ""}
        for r in rows
    ]


def _drop_doc(conn, cid):
    for (rowid,) in conn.execute("SELECT rowid FROM docs WHERE cid = ?", (cid,)):
        conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (rowid,))
        _add_stat(conn, "docs_count", -1)
    conn.execute("DELETE FROM docs WHERE cid = ?", (cid,))


def _rebuild_doc(conn, cid):
    _drop_doc(conn, cid)
    row = conn.execute(
        "SELECT * FROM classifies WHERE cid = ? ORDER BY received_at ASC LIMIT 1",
        (cid,),
    ).fetchone()
    if row:
        _upsert_doc(conn, dict(row), row["received_at"])


def blacklist(publisher):
    """Ignore this publisher locally. Drops their catalog rows."""
    publisher = (publisher or "").strip()
    if not publisher:
        return False
    from . import clubd_client
    me = clubd_client.peer_id()
    if me and publisher == me:
        return False
    conn = connect()
    now = time.time()
    alias_row = conn.execute(
        "SELECT alias FROM aliases WHERE publisher = ?", (publisher,),
    ).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO blacklisted(publisher, alias, received_at) "
        "VALUES (?, ?, ?)",
        (publisher, (alias_row[0] if alias_row else "") or "", now),
    )
    cids = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT cid FROM classifies WHERE publisher = ?",
            (publisher,),
        )
        if r[0]
    }
    cids.update(
        r[0] for r in conn.execute(
            "SELECT DISTINCT cid FROM reports WHERE publisher = ?",
            (publisher,),
        )
        if r[0]
    )
    conn.execute("DELETE FROM classifies WHERE publisher = ?", (publisher,))
    conn.execute("DELETE FROM reports WHERE publisher = ?", (publisher,))
    conn.execute("DELETE FROM skips WHERE publisher = ?", (publisher,))
    conn.execute("DELETE FROM claims WHERE publisher = ?", (publisher,))
    conn.execute("DELETE FROM aliases WHERE publisher = ?", (publisher,))
    conn.execute("DELETE FROM messages WHERE publisher = ?", (publisher,))
    for cid in cids:
        _rebuild_doc(conn, cid)
    conn.commit()
    return True


def unblacklist(publisher):
    publisher = (publisher or "").strip()
    if not publisher:
        return False
    conn = connect()
    conn.execute("DELETE FROM blacklisted WHERE publisher = ?", (publisher,))
    conn.commit()
    return True


def ingest_message(conn, obj: dict, received_at=None):
    """Insert a verified message and update denormalized tables.

    Returns True if this payload_hash was new.
    """
    kind = obj.get("kind")
    if kind not in protocol.KINDS:
        return False
    msg_club = obj.get("club")
    if msg_club and str(msg_club) != config.CLUB_ID:
        return False
    cid = obj.get("cid") or ""
    publisher = obj.get("publisher")
    if not publisher:
        return False
    if is_blacklisted(publisher):
        return False
    if kind != "alias" and not cid:
        return False
    if kind != "alias" and not cidutil.valid(cid):
        return False
    if kind == "report" and obj.get("reason") not in protocol.REPORT_REASONS:
        return False
    payload_hash = protocol.payload_hash(obj)
    now = received_at if received_at is not None else time.time()
    try:
        conn.execute(
            "INSERT INTO messages(payload_hash, kind, cid, publisher, body, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (payload_hash, kind, cid, publisher, protocol.wire_dumps(obj), now),
        )
    except sqlite3.IntegrityError:
        return False

    if kind == "classify":
        _insert_classify(conn, obj, payload_hash, now)
    elif kind == "alias":
        _upsert_alias(conn, publisher, obj.get("alias"), payload_hash, now)
    elif kind == "skip":
        conn.execute(
            "INSERT OR REPLACE INTO skips(cid, publisher, mime_type, reason, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, publisher, obj.get("mime_type"), obj.get("reason"), now),
        )
        conn.execute("DELETE FROM claims WHERE cid = ?", (cid,))
    elif kind == "claim":
        until = float(obj.get("until") or 0)
        if until > time.time():
            other = conn.execute(
                "SELECT cid FROM claims WHERE publisher = ? AND cid != ? AND until > ?",
                (publisher, cid, time.time()),
            ).fetchone()
            if other is None:
                conn.execute(
                    "INSERT OR REPLACE INTO claims(publisher, cid, until) VALUES (?, ?, ?)",
                    (publisher, cid, until),
                )
    elif kind == "report":
        if obj.get("reason") == "clear":
            conn.execute(
                "DELETE FROM reports WHERE cid = ? AND publisher = ?",
                (cid, publisher),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO reports(cid, publisher, reason, received_at) "
                "VALUES (?, ?, ?, ?)",
                (cid, publisher, obj.get("reason"), now),
            )
    conn.commit()
    if kind == "report" and obj.get("reason") == "wrong":
        from . import work
        work.enqueue_review(cid)
    return True


def maybe_enqueue_second(cid):
    """Queue a second independent classify when this node is not the voter."""
    if not should_second_classify(cid):
        return False
    from . import work
    if work.at_cap():
        return False
    return work.enqueue_review(cid)


def _insert_classify(conn, obj, payload_hash, now):
    field = normalize_field(obj.get("field"))
    classifier = obj.get("classifier") or {}
    classifier_s = protocol.canonical_dumps(classifier) if classifier else ""
    conn.execute(
        "INSERT OR IGNORE INTO classifies("
        "payload_hash, cid, publisher, mime_type, size, filename, field, topic, "
        "keywords, license, text_sha256, classifier, indexed_at, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (payload_hash, obj["cid"], obj["publisher"], obj.get("mime_type"),
         obj.get("size"), obj.get("filename"), field, obj.get("topic"),
         obj.get("keywords"), obj.get("license"), obj.get("text_sha256"),
         classifier_s, float(obj.get("indexed_at") or now), now),
    )
    conn.execute("DELETE FROM claims WHERE cid = ?", (obj["cid"],))
    _upsert_doc(conn, obj, now)


def _upsert_alias(conn, publisher, alias, payload_hash, now):
    name = " ".join(str(alias or "").split())
    if not name:
        conn.execute("DELETE FROM aliases WHERE publisher = ?", (publisher,))
        return
    conn.execute(
        "INSERT OR REPLACE INTO aliases(publisher, alias, payload_hash, received_at) "
        "VALUES (?, ?, ?, ?)",
        (publisher, name[:32], payload_hash, now),
    )


def local_alias():
    conn = connect()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'alias'"
    ).fetchone()
    return (row[0] or "") if row else ""


def set_local_alias(alias, peer_id=None):
    conn = connect()
    name = " ".join(str(alias or "").split())[:32]
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('alias', ?)",
        (name,),
    )
    if peer_id:
        if name:
            conn.execute(
                "INSERT OR REPLACE INTO aliases(publisher, alias, payload_hash, received_at) "
                "VALUES (?, ?, ?, ?)",
                (peer_id, name, "local", time.time()),
            )
        else:
            conn.execute("DELETE FROM aliases WHERE publisher = ?", (peer_id,))
    conn.commit()
    return name


def preferred_classify(conn, cid):
    """First-seen live classify for a CID, or None."""
    row = conn.execute("SELECT * FROM docs WHERE cid = ?", (cid,)).fetchone()
    if row:
        return dict(row)
    row = conn.execute(
        "SELECT * FROM classifies WHERE cid = ? ORDER BY received_at ASC LIMIT 1",
        (cid,),
    ).fetchone()
    return dict(row) if row else None


def skip_ttl_seconds():
    return int(config.FETCH.get("skip_ttl_seconds", 21600))


def skip_is_live(skip):
    if not skip:
        return False
    reason = skip["reason"] or ""
    if club.is_persist_skip(reason):
        return True
    ttl = skip_ttl_seconds()
    if ttl <= 0:
        return True
    received = float(skip["received_at"] or 0)
    return received + ttl > time.time()


def lookup_cid(conn, cid):
    """Skip-hook lookup: live classify beats foreign claim beats skip."""
    cl = preferred_classify(conn, cid)
    if cl:
        return {"kind": "classify", **cl}
    me = _peer_id() or ""
    claim = conn.execute(
        "SELECT * FROM claims WHERE cid = ? AND until > ? AND publisher != ?",
        (cid, time.time(), me),
    ).fetchone()
    if claim:
        return {"kind": "claim", **dict(claim)}
    skip = conn.execute("SELECT * FROM skips WHERE cid = ?", (cid,)).fetchone()
    if skip and skip_is_live(skip):
        return {"kind": "skip", **dict(skip)}
    return None


def classify_by_text_hash(conn, text_sha256):
    row = conn.execute(
        "SELECT * FROM docs WHERE text_sha256 = ? LIMIT 1",
        (text_sha256,),
    ).fetchone()
    if row:
        return dict(row)
    row = conn.execute(
        "SELECT * FROM classifies WHERE text_sha256 = ? ORDER BY received_at ASC LIMIT 1",
        (text_sha256,),
    ).fetchone()
    return dict(row) if row else None


def purge_unprocessable_skips(conn):
    """Drop leftover mime-skips. They are local-only and should not be club truth."""
    rows = conn.execute(
        "SELECT cid FROM skips WHERE IFNULL(reason, '') = 'unprocessable'"
    ).fetchall()
    if not rows:
        return 0
    cids = [(r[0],) for r in rows]
    conn.executemany("DELETE FROM skips WHERE cid = ?", cids)
    conn.executemany(
        "DELETE FROM messages WHERE kind = 'skip' AND cid = ?", cids,
    )
    conn.commit()
    return len(cids)


def purge_invalid_cids(conn):
    """Drop catalog rows whose cid is not a well-formed CIDv0/v1."""
    dead = set()
    for table in ("classifies", "skips", "claims", "messages"):
        for (cid,) in conn.execute("SELECT DISTINCT cid FROM %s" % table):
            if cid and not cidutil.valid(cid):
                dead.add(cid)
    if not dead:
        return 0
    for cid in dead:
        had_doc = False
        for (rowid,) in conn.execute(
            "SELECT rowid FROM docs WHERE cid = ?", (cid,)
        ):
            had_doc = True
            conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM classifies WHERE cid = ?", (cid,))
        conn.execute("DELETE FROM docs WHERE cid = ?", (cid,))
        conn.execute("DELETE FROM skips WHERE cid = ?", (cid,))
        conn.execute("DELETE FROM reports WHERE cid = ?", (cid,))
        conn.execute("DELETE FROM claims WHERE cid = ?", (cid,))
        conn.execute(
            "DELETE FROM messages WHERE cid = ? AND kind != 'alias'", (cid,),
        )
        if had_doc:
            _add_stat(conn, "docs_count", -1)
    conn.commit()
    return len(dead)


def expire_claims(conn):
    conn.execute("DELETE FROM claims WHERE until <= ?", (time.time(),))
    ttl = skip_ttl_seconds()
    if ttl > 0:
        persist = tuple(sorted(club.PERSIST_SKIP_REASONS))
        placeholders = ",".join("?" * len(persist))
        conn.execute(
            "DELETE FROM skips WHERE IFNULL(reason, '') NOT IN (%s) "
            "AND IFNULL(received_at, 0) < ?" % placeholders,
            persist + (time.time() - ttl,),
        )
    conn.commit()


def export_snapshot(conn, limit=20000):
    """Signed message envelopes for late-joiner catch-up, oldest first.

    Unprocessable skips stay off the wire: they are local and they expire.
    Classify and out-of-scope skip replicate.
    """
    limit = max(0, int(limit))
    rows = conn.execute(
        "SELECT m.body FROM messages m "
        "WHERE instr(m.body, '\"sig\":') > 0 "
        "  AND ("
        "    m.kind = 'classify' "
        "    OR (m.kind = 'alias' AND EXISTS ("
        "      SELECT 1 FROM aliases a WHERE a.payload_hash = m.payload_hash"
        "    )) "
        "    OR (m.kind = 'skip' AND EXISTS ("
        "      SELECT 1 FROM skips s WHERE s.cid = m.cid "
        "        AND s.reason IN ('out_of_scope', 'not_academic', 'directory')"
        "    )) "
        "    OR m.kind = 'report'"
        "  ) "
        "ORDER BY m.received_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    lines = [r[0] for r in rows if r[0]]
    lines.reverse()
    if not lines:
        return ""
    return "\n".join(lines) + "\n"
