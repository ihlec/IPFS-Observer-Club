"""FTS search over the one-row-per-CID docs catalog."""
from __future__ import annotations

import time

from . import indexer, labels, store


def _fts_escape(query):
    terms = [t.replace('"', '""') for t in query.split()]
    return " ".join('"%s"' % t for t in terms)


def _decorate(conn, row):
    out = dict(row)
    out.pop("rank", None)
    labels.apply(out, conn, out["cid"])
    return out


def search(query, field=None, mime=None, limit=50):
    conn = store.connect()
    sql = (
        "SELECT d.*, bm25(docs_fts) AS rank "
        "FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid "
        "WHERE docs_fts MATCH ? "
        "AND d.cid NOT IN (SELECT cid FROM reports WHERE reason = 'abusive')"
    )
    params = [_fts_escape(query)]
    if field:
        sql += " AND d.field = ?"
        params.append(field)
    if mime:
        sql += " AND d.mime_type LIKE ?"
        params.append("%" + mime + "%")
    sql += " ORDER BY rank LIMIT ?"
    params.append(min(int(limit) * 3 if field else int(limit), 600))
    out = []
    for r in conn.execute(sql, params).fetchall():
        row = _decorate(conn, r)
        if field and row.get("field") != field:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def browse(field=None, mime=None, limit=20, offset=0):
    conn = store.connect()
    where = "WHERE cid NOT IN (SELECT cid FROM reports WHERE reason = 'abusive')"
    params = []
    if field:
        where += " AND field = ?"
        params.append(field)
    if mime:
        where += " AND mime_type LIKE ?"
        params.append("%" + mime + "%")
    total = conn.execute(
        "SELECT COUNT(*) FROM docs " + where, params
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM docs " + where +
        " ORDER BY indexed_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    results = [_decorate(conn, r) for r in rows]
    return results, total


def abusive_reports():
    """CIDs hidden by an abusive report, for the admin review list."""
    conn = store.connect()
    me = None
    try:
        from . import clubd_client
        me = clubd_client.peer_id()
    except Exception:
        me = None
    rows = conn.execute(
        "SELECT r.cid, r.publisher, r.received_at, a.alias, "
        "d.field, d.topic, d.keywords, d.mime_type, d.filename "
        "FROM reports r "
        "LEFT JOIN docs d ON d.cid = r.cid "
        "LEFT JOIN aliases a ON a.publisher = r.publisher "
        "WHERE r.reason = 'abusive' "
        "ORDER BY r.received_at DESC"
    ).fetchall()
    out = []
    seen = {}
    for r in rows:
        cid = r["cid"]
        item = seen.get(cid)
        if item is None:
            item = {
                "cid": cid,
                "field": r["field"],
                "topic": r["topic"],
                "keywords": r["keywords"],
                "mime_type": r["mime_type"],
                "filename": r["filename"],
                "mine": False,
                "reporters": [],
            }
            seen[cid] = item
            out.append(item)
        item["reporters"].append({
            "observer": r["publisher"],
            "alias": r["alias"] or "",
            "received_at": r["received_at"],
            "blacklisted": store.is_blacklisted(r["publisher"]),
        })
        if me and r["publisher"] == me:
            item["mine"] = True
    return out


def mimes():
    conn = store.connect()
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT mime_type FROM docs "
        "WHERE mime_type IS NOT NULL AND mime_type != '' "
        "AND cid NOT IN (SELECT cid FROM reports WHERE reason = 'abusive') "
        "ORDER BY mime_type"
    )]


def stats():
    conn = store.connect()
    out = {
        "classifies": store.cached_count(
            conn, "docs_count", "SELECT COUNT(*) FROM docs"
        ),
        "skips": conn.execute("SELECT COUNT(*) FROM skips").fetchone()[0],
        "claims": conn.execute(
            "SELECT COUNT(*) FROM claims WHERE until > ?", (time.time(),)
        ).fetchone()[0],
        "observers": conn.execute(
            "SELECT COUNT(DISTINCT publisher) FROM classifies"
        ).fetchone()[0],
    }
    last = conn.execute("SELECT MAX(indexed_at) FROM docs").fetchone()[0]
    out["last_classify_at"] = last
    reasons = {}
    for row in conn.execute("SELECT reason, COUNT(*) FROM skips GROUP BY reason"):
        reasons[row[0] or ""] = row[1]
    out["skip_reasons"] = reasons
    out["run"] = indexer.runtime_stats()
    return out


def observers(limit=50):
    """Nodes ranked by distinct CIDs they classified."""
    conn = store.connect()
    rows = conn.execute(
        "SELECT c.publisher, COUNT(DISTINCT c.cid) AS n, a.alias "
        "FROM classifies c "
        "LEFT JOIN aliases a ON a.publisher = c.publisher "
        "GROUP BY c.publisher ORDER BY n DESC, c.publisher ASC LIMIT ?",
        (limit,),
    )
    me = None
    try:
        from . import clubd_client
        me = clubd_client.peer_id()
    except Exception:
        me = None
    local = store.local_alias()
    out = []
    for r in rows:
        alias = r[2] or ""
        if me and r[0] == me and local:
            alias = local
        out.append({"observer": r[0], "alias": alias, "n_classify": r[1]})
    return out
