"""Local discovery work queue. Club.sqlite is the shared catalog; this file
only tracks sniffed CIDs that this node has not yet processed.

One connection per thread, autocommit, and a short busy wait. A single
shared handle used from 16 workers still raised SQLITE_BUSY.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import cidutil, config, unixfs

_db_lock = threading.RLock()
_tls = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cids (
    cid         TEXT PRIMARY KEY,
    codec       TEXT,
    first_seen  REAL,
    last_seen   REAL,
    peer_count  INTEGER DEFAULT 0,
    want_count  INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'discovered',
    attempts    INTEGER DEFAULT 0,
    mime_type   TEXT,
    size        INTEGER,
    filename    TEXT,
    last_retrieved REAL,
    last_checked   REAL,
    error       TEXT,
    source      TEXT DEFAULT 'sniff'
);
CREATE INDEX IF NOT EXISTS idx_cids_status ON cids(status, peer_count DESC);
CREATE INDEX IF NOT EXISTS idx_cids_last_seen ON cids(last_seen);

CREATE TABLE IF NOT EXISTS cid_peers (
    cid  TEXT,
    peer TEXT,
    PRIMARY KEY (cid, peer)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS evicted (
    cid        TEXT PRIMARY KEY,
    peer_count INTEGER,
    evicted_at REAL
);

CREATE TABLE IF NOT EXISTS seen_cids (
    cid        TEXT PRIMARY KEY,
    first_seen REAL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS spool_offsets (
    path   TEXT PRIMARY KEY,
    offset INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS unprocessable (
    cid       TEXT PRIMARY KEY,
    mime_type TEXT,
    seen_at   REAL
);
"""


def reset():
    """Close this thread's connection. Tests call this after changing WORK_DB."""
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _tls.conn = None


class _Local:
    """Test shim: ``work._local.conn = None`` still resets the handle."""

    @property
    def conn(self):
        return getattr(_tls, "conn", None)

    @conn.setter
    def conn(self, value):
        if value is None:
            reset()
        else:
            _tls.conn = value


_local = _Local()

_KEEP_SRC = "IFNULL(source, 'sniff') IN ('report', 'named')"


def connect():
    conn = getattr(_tls, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(config.WORK_DB) or ".", exist_ok=True)
        first = not os.path.exists(config.WORK_DB)
        conn = sqlite3.connect(
            config.WORK_DB,
            timeout=30,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        if first:
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _tls.conn = conn
    return conn


def _migrate(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cids)")]
    if "source" not in cols:
        conn.execute("ALTER TABLE cids ADD COLUMN source TEXT DEFAULT 'sniff'")


def _locked_write(fn):
    """Run a work-db write. Wait out SQLITE_BUSY from spool/janitor/WAL."""
    last = None
    for attempt in range(6):
        try:
            with _db_lock:
                return fn(connect())
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = e
            time.sleep(0.05 * (2 ** min(attempt, 4)))
    raise last


@contextmanager
def locked():
    """Hold the process lock. Do not block on network inside this."""
    with _db_lock:
        yield connect()


def enqueue_review(cid):
    """Queue a CID for a second classify. Kept like a live WANT that must not age out."""
    from . import store
    if not cidutil.valid(cid) or store.local_classified(cid):
        return False
    now = time.time()
    min_age = int(config.FETCH.get("min_age_seconds", 10))
    first = now - min_age - 1
    codec = cidutil.codec_of(cid)
    with locked() as conn:
        conn.execute(
            "INSERT INTO cids (cid, codec, first_seen, last_seen, "
            "peer_count, want_count, status, attempts, source) "
            "VALUES (?, ?, ?, ?, 1, 1, 'discovered', 0, 'report') "
            "ON CONFLICT(cid) DO UPDATE SET "
            "  last_seen = excluded.last_seen, "
            "  first_seen = MIN(first_seen, excluded.first_seen), "
            "  source = 'report', "
            "  peer_count = MAX(peer_count, 1), "
            "  status = 'discovered', "
            "  attempts = 0, "
            "  error = NULL",
            (cid, codec, first, now),
        )
    return True


def remember_binary(cid, mime_type=None):
    """Drop a binary CID from the live queue and do not ingest it again."""
    now = time.time()
    with locked() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO unprocessable(cid, mime_type, seen_at) "
            "VALUES (?, ?, ?)",
            (cid, mime_type, now),
        )
        forget_cid(conn, cid)
    return True


def is_unprocessable(cid):
    with _db_lock:
        row = connect().execute(
            "SELECT 1 FROM unprocessable WHERE cid = ?", (cid,),
        ).fetchone()
    return row is not None


def forget_cid(conn, cid):
    """Drop a CID so a later sighting can retry. Skips stay recorded."""
    with _db_lock:
        conn.execute("DELETE FROM cid_peers WHERE cid = ?", (cid,))
        conn.execute("DELETE FROM cids WHERE cid = ?", (cid,))


def drop_directory(conn, cid):
    """Forget a UnixFS folder and ignore later Bitswap WANTs unless peers grow."""
    with _db_lock:
        row = conn.execute(
            "SELECT peer_count FROM cids WHERE cid = ?", (cid,)
        ).fetchone()
        peers = int((row["peer_count"] if row else 1) or 1)
        conn.execute(
            "INSERT OR REPLACE INTO evicted(cid, peer_count, evicted_at) "
            "VALUES (?, ?, ?)",
            (cid, peers, time.time()),
        )
        conn.execute("DELETE FROM cid_peers WHERE cid = ?", (cid,))
        conn.execute("DELETE FROM cids WHERE cid = ?", (cid,))


def enqueue_doc_children(links, skip_cid=None):
    """Queue PDF names from a dropped directory. Folder itself stays out."""
    from . import store
    max_n = int(config.FETCH.get("max_dir_docs", 16))
    cap_named = int(config.FETCH.get("max_named", 80))
    picked = unixfs.doc_child_links(links, max_n=max_n)
    now = time.time()
    min_age = int(config.FETCH.get("min_age_seconds", 10))
    first = now - min_age - 1
    n = 0
    with locked() as conn:
        drop_named_non_pdf(conn)
        if not picked:
            return 0
        named_n = conn.execute(
            "SELECT COUNT(*) FROM cids WHERE IFNULL(source, 'sniff') = 'named' "
            "AND status IN ('discovered', 'processing')"
        ).fetchone()[0]
        for filename, cid in picked:
            if named_n >= cap_named:
                break
            if cid == skip_cid or not cidutil.valid(cid):
                continue
            if is_unprocessable(cid) or store.already_catalogued(cid):
                continue
            row = conn.execute(
                "SELECT source, status FROM cids WHERE cid = ?", (cid,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE cids SET filename = COALESCE(filename, ?) "
                    "WHERE cid = ?",
                    (filename, cid),
                )
                continue
            if live_count(conn) >= max_queue():
                if not (evict_dir_probes(conn) or evict_for_unixfs(conn)):
                    break
            conn.execute(
                "INSERT INTO cids (cid, codec, first_seen, last_seen, "
                "peer_count, want_count, status, attempts, source, filename) "
                "VALUES (?, ?, ?, ?, 1, 1, 'discovered', 0, 'named', ?)",
                (cid, cidutil.codec_of(cid), first, now, filename),
            )
            named_n += 1
            n += 1
    return n


def drop_named_non_pdf(conn=None):
    """Forget named HTML so Wikipedia dumps cannot fill the PDF cap."""
    conn = conn or connect()
    dropped = 0
    with _db_lock:
        rows = conn.execute(
            "SELECT cid FROM cids WHERE IFNULL(source, 'sniff') = 'named' "
            "AND status = 'discovered' "
            "AND lower(IFNULL(filename,'')) NOT LIKE '%.pdf'"
        ).fetchall()
        for row in rows:
            forget_cid(conn, row[0])
            dropped += 1
    return dropped


def max_age_seconds():
    return int(config.FETCH.get("max_age_seconds", 900))


def max_queue():
    return int(config.FETCH.get("max_queue", 400))


def skip_ttl_seconds():
    return int(config.FETCH.get("skip_ttl_seconds", 21600))


def prefer_min_peer_count():
    return int(config.FETCH.get("prefer_min_peer_count", 2))


def max_dir_queue():
    """Live sniffed folders. Extra slots stay free for raw HTML."""
    return int(config.FETCH.get("max_dir_queue", 40))


def prune(conn=None):
    """Keep only recent WANTs. Stale IPFS CIDs are usually gone."""
    conn = conn or connect()
    now = time.time()
    cutoff = now - max_age_seconds()
    cap = max_queue()
    dropped = 0
    with _db_lock:
        dropped += drop_named_non_pdf(conn)
        stale = conn.execute(
            "SELECT cid FROM cids WHERE status IN ('discovered', 'processing') "
            "AND NOT " + _KEEP_SRC + " "
            "AND last_seen < ?",
            (cutoff,),
        ).fetchall()
        for row in stale:
            forget_cid(conn, row[0])
            dropped += 1
        extra = conn.execute(
            "SELECT COUNT(*) FROM cids WHERE status IN ('discovered', 'processing')"
        ).fetchone()[0] - cap
        if extra > 0:
            old = conn.execute(
                "SELECT cid FROM cids WHERE status IN ('discovered', 'processing') "
                "AND NOT " + _KEEP_SRC + " "
                "ORDER BY "
                "  CASE WHEN codec = 'dag-pb' "
                "         AND lower(IFNULL(filename,'')) NOT LIKE '%.pdf' "
                "       THEN 0 "
                "       WHEN codec = 'raw' THEN 1 "
                "       ELSE 2 END ASC, "
                "  last_seen ASC LIMIT ?",
                (extra,),
            ).fetchall()
            for row in old:
                forget_cid(conn, row[0])
                dropped += 1
        dir_cap = max_dir_queue()
        if dir_cap:
            dir_n = conn.execute(
                "SELECT COUNT(*) FROM cids WHERE status IN ('discovered', 'processing') "
                "AND " + _PROBE_SQL,
            ).fetchone()[0]
            overflow = dir_n - dir_cap
            if overflow > 0:
                old = conn.execute(
                    "SELECT cid FROM cids WHERE status = 'discovered' "
                    "AND " + _PROBE_SQL + " "
                    "ORDER BY last_seen ASC LIMIT ?",
                    (overflow,),
                ).fetchall()
                for row in old:
                    forget_cid(conn, row[0])
                    dropped += 1
        n = expire_local_skips(conn)
    return dropped + n


def expire_local_skips(conn=None):
    """Requeue local skips that should run again."""
    conn = conn or connect()
    ttl = skip_ttl_seconds()
    n = 0
    with _db_lock:
        n += conn.execute(
            "UPDATE cids SET status = 'discovered', attempts = 0, error = 'pdf_retry' "
            "WHERE status = 'skipped' "
            "AND mime_type = 'application/pdf' "
            "AND IFNULL(error, '') IN "
            "('out_of_scope', 'not_academic', 'not_academic_document')"
        ).rowcount
        if ttl <= 0:
            return n
        cutoff = time.time() - ttl
        n += conn.execute(
            "UPDATE cids SET status = 'discovered', attempts = 0, error = 'skip_expired' "
            "WHERE status = 'skipped' "
            "AND IFNULL(error, '') IN ('unprocessable', 'llm_disagreed') "
            "AND IFNULL(mime_type, '') NOT LIKE 'image/%' "
            "AND IFNULL(mime_type, '') NOT LIKE 'video/%' "
            "AND IFNULL(mime_type, '') NOT LIKE 'audio/%' "
            "AND IFNULL(last_checked, last_seen) < ?",
            (cutoff,),
        ).rowcount
    return n


def mark(conn, cid, status, **fields):
    cols = ["status = ?"]
    vals = [status]
    for key, value in fields.items():
        cols.append("%s = ?" % key)
        vals.append(value)
    vals.append(cid)
    _locked_write(lambda c: c.execute(
        "UPDATE cids SET " + ", ".join(cols) + " WHERE cid = ?", vals,
    ))


def bump_attempts(conn, cid, attempts):
    _locked_write(lambda c: c.execute(
        "UPDATE cids SET attempts = ? WHERE cid = ?", (attempts, cid),
    ))


def note_fetch(conn, cid, retrieved=None):
    now = time.time()
    _locked_write(lambda c: c.execute(
        "UPDATE cids SET last_checked = ?, last_retrieved = COALESCE(?, last_retrieved) "
        "WHERE cid = ?",
        (now, retrieved, cid),
    ))


_DOC_SQL = (
    "(codec IN ('raw', 'dag-pb') OR lower(IFNULL(filename,'')) LIKE '%.pdf' "
    "OR " + _KEEP_SRC + ")"
)
_NAMED_SQL = (
    "(" + _KEEP_SRC + " OR lower(IFNULL(filename,'')) LIKE '%.pdf')"
)
_PROBE_SQL = (
    "codec = 'dag-pb' AND NOT " + _KEEP_SRC + " "
    "AND lower(IFNULL(filename,'')) NOT LIKE '%.pdf'"
)
_FILL_SQL = "(" + _DOC_SQL + ") AND NOT (" + _PROBE_SQL + ")"


_TAKE_ORDER = (
    "CASE "
    "  WHEN IFNULL(source, 'sniff') IN ('report', 'named') "
    "    OR lower(IFNULL(filename,'')) LIKE '%.pdf' THEN 0 "
    "  WHEN lower(IFNULL(filename,'')) LIKE '%.html' "
    "    OR lower(IFNULL(filename,'')) LIKE '%.htm' THEN 1 "
    "  WHEN codec = 'dag-pb' THEN 2 "
    "  ELSE 3 END"
)


def _select_discovered(conn, extra_where, min_peers, max_first_seen,
                       min_last_seen, attempt_cap, limit):
    return conn.execute(
        "SELECT cid, codec, peer_count, attempts FROM cids "
        "WHERE status = 'discovered' "
        "  AND peer_count >= ? "
        "  AND first_seen <= ? "
        "  AND (last_seen >= ? OR " + _KEEP_SRC + ") "
        "  AND attempts < ? "
        "  AND IFNULL(codec, '') NOT IN ('libp2p-key', 'json', 'dag-json') "
        "  AND " + extra_where + " "
        "ORDER BY " + _TAKE_ORDER + ", "
        "  peer_count DESC, last_seen DESC LIMIT ?",
        (min_peers, max_first_seen, min_last_seen, attempt_cap, limit),
    ).fetchall()


def evict_for_unixfs(conn, n=1):
    """Free live slots occupied by raw WANTs so a UnixFS file can be queued."""
    rows = conn.execute(
        "SELECT cid FROM cids WHERE status = 'discovered' "
        "AND NOT " + _KEEP_SRC + " "
        "AND IFNULL(codec, '') != 'dag-pb' "
        "AND lower(IFNULL(filename,'')) NOT LIKE '%.pdf' "
        "AND lower(IFNULL(filename,'')) NOT LIKE '%.html' "
        "AND lower(IFNULL(filename,'')) NOT LIKE '%.htm' "
        "ORDER BY last_seen ASC LIMIT ?",
        (n,),
    ).fetchall()
    for row in rows:
        forget_cid(conn, row[0])
    return len(rows)


def max_dir_probes():
    return int(config.FETCH.get("max_dir_probes", 4))


def dir_queue_count(conn=None):
    """Sniffed folders occupying the live queue."""
    conn = conn or connect()
    with _db_lock:
        return conn.execute(
            "SELECT COUNT(*) FROM cids WHERE status IN ('discovered', 'processing') "
            "AND " + _PROBE_SQL,
        ).fetchone()[0]


def evict_dir_probes(conn, n=1):
    """Free live slots occupied by sniffed folders."""
    rows = conn.execute(
        "SELECT cid FROM cids WHERE status = 'discovered' "
        "AND " + _PROBE_SQL + " "
        "ORDER BY last_seen ASC LIMIT ?",
        (n,),
    ).fetchall()
    for row in rows:
        forget_cid(conn, row[0])
    return len(rows)


def dir_probe_count(conn=None):
    """Sniffed dag-pb (folders) currently occupying a worker."""
    conn = conn or connect()
    with _db_lock:
        return conn.execute(
            "SELECT COUNT(*) FROM cids WHERE status = 'processing' "
            "AND " + _PROBE_SQL,
        ).fetchone()[0]


def _extend(rows, extra):
    seen = {r["cid"] for r in rows}
    rows.extend(r for r in extra if r["cid"] not in seen)
    return rows


def take_batch(conn, limit=5):
    """Claim work. At most max_dir_probes sniffed folders in flight."""
    now = time.time()
    max_first_seen = now - int(config.FETCH.get("min_age_seconds", 10))
    min_last_seen = now - max_age_seconds()
    attempt_cap = max(
        int(config.FETCH.get("max_retries", 1)),
        int(config.FETCH.get("max_timeout_retries", 3)),
    ) + 1
    min_peers = int(config.FETCH.get("min_peer_count", 1))
    prefer = max(min_peers, prefer_min_peer_count())
    args = (max_first_seen, min_last_seen, attempt_cap)
    with _db_lock:
        rows = list(_select_discovered(
            conn, _NAMED_SQL, min_peers, *args, limit,
        ))
        if len(rows) < limit:
            room = max(0, max_dir_probes() - dir_probe_count(conn))
            need_pb = min(limit - len(rows), room)
            if need_pb:
                _extend(rows, _select_discovered(
                    conn, _PROBE_SQL, min_peers, *args, need_pb,
                ))
        if len(rows) < limit:
            need = limit - len(rows)
            extra = _select_discovered(conn, _FILL_SQL, prefer, *args, need)
            if not extra:
                extra = _select_discovered(
                    conn, _FILL_SQL, min_peers, *args, need,
                )
            _extend(rows, extra)
        if rows:
            conn.executemany(
                "UPDATE cids SET status = 'processing' WHERE cid = ?",
                [(r["cid"],) for r in rows],
            )
        return rows


def live_count(conn=None):
    """CIDs that occupy the fetch cap (discovered + processing)."""
    conn = conn or connect()
    with _db_lock:
        return conn.execute(
            "SELECT COUNT(*) FROM cids WHERE status IN ('discovered', 'processing')"
        ).fetchone()[0]


def at_cap(conn=None):
    return live_count(conn) >= max_queue()


def stats():
    with _db_lock:
        conn = connect()
        out = {}
        for status in ("discovered", "processing", "indexed", "skipped"):
            out[status] = conn.execute(
                "SELECT COUNT(*) FROM cids WHERE status = ?", (status,)
            ).fetchone()[0]
        out["seen"] = conn.execute("SELECT COUNT(*) FROM seen_cids").fetchone()[0]
        out["backlog"] = conn.execute(
            "SELECT COUNT(*) FROM cids WHERE status = 'discovered' AND peer_count >= ?",
            (int(config.FETCH.get("min_peer_count", 1)),),
        ).fetchone()[0]
        oldest = conn.execute(
            "SELECT MIN(last_seen) FROM cids WHERE status IN ('discovered', 'processing')"
        ).fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(last_seen) FROM cids WHERE status IN ('discovered', 'processing')"
        ).fetchone()[0]
        now = time.time()
        out["oldest_age_seconds"] = int(now - oldest) if oldest else 0
        out["newest_age_seconds"] = int(now - newest) if newest else 0
        return out


def db_file_size():
    with _db_lock:
        conn = connect()
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return page_count * page_size
