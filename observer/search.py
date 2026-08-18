"""FTS search over the one-row-per-CID docs catalog."""
from __future__ import annotations

import logging
import time

from . import indexer, labels, store

log = logging.getLogger("search")


def _fts_escape(query):
    """Quoted prefix tokens so 'bio' matches field/topic/keyword 'biology'."""
    terms = []
    for raw in query.split():
        token = raw.replace('"', "")
        if not token:
            continue
        terms.append('"%s"*' % token.replace('"', '""'))
    return " ".join(terms)


def _decorate(conn, rows):
    """Attach club votes to result rows in one batched lookup."""
    out = []
    for row in rows:
        item = dict(row)
        item.pop("rank", None)
        out.append(item)
    return labels.apply_many(conn, out)


# docs.field is the first-seen classify, but the UI shows the club vote, so a
# field filter has to consider every CID any publisher filed under that field.
_FIELD_WHERE = "(%s.field = ? OR %s.cid IN (SELECT cid FROM classifies WHERE field = ?))"


_CATALOG_MIMES = ("text/html", "application/pdf")


def _mime_sql(column, mime):
    """Restrict listings to PDF and HTML. XHTML counts as HTML."""
    if mime == "application/pdf":
        return " AND %s = 'application/pdf'" % column, []
    if mime == "text/html":
        return (
            " AND %s IN ('text/html', 'application/xhtml+xml')" % column,
            [],
        )
    return "", []


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
        sql += " AND " + (_FIELD_WHERE % ("d", "d"))
        params.extend((field, field))
    extra, extra_params = _mime_sql("d.mime_type", mime)
    sql += extra
    params.extend(extra_params)
    sql += " ORDER BY rank LIMIT ?"
    limit = int(limit)
    params.append(min(limit * 3 if field else limit, 600))
    rows = _decorate(conn, conn.execute(sql, params).fetchall())
    if field:
        rows = [r for r in rows if r.get("field") == field]
    return rows[:limit]


def browse(field=None, mime=None, limit=20, offset=0):
    conn = store.connect()
    where = "WHERE cid NOT IN (SELECT cid FROM reports WHERE reason = 'abusive')"
    params = []
    if field:
        where += " AND " + (_FIELD_WHERE % ("docs", "docs"))
        params.extend((field, field))
    extra, extra_params = _mime_sql("mime_type", mime)
    where += extra
    params.extend(extra_params)
    total = conn.execute(
        "SELECT COUNT(*) FROM docs " + where, params
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM docs " + where +
        " ORDER BY indexed_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return _decorate(conn, rows), total


def _local_peer_id():
    from . import clubd_client
    try:
        return clubd_client.peer_id()
    except Exception:
        log.debug("clubd peer id unavailable", exc_info=True)
        return None


def abusive_reports():
    """CIDs hidden by an abusive report, for the admin review list."""
    conn = store.connect()
    me = _local_peer_id()
    blacklisted = {
        r[0] for r in conn.execute("SELECT publisher FROM blacklisted")
    }
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
            "blacklisted": r["publisher"] in blacklisted,
        })
        if me and r["publisher"] == me:
            item["mine"] = True
    return out


def mimes():
    """Datatypes the search filter offers. Only PDF and HTML are in-scope."""
    return list(_CATALOG_MIMES)


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
    me = _local_peer_id()
    local = store.local_alias()
    out = []
    for r in rows:
        alias = r[2] or ""
        if me and r[0] == me and local:
            alias = local
        out.append({"observer": r[0], "alias": alias, "n_classify": r[1]})
    return out
