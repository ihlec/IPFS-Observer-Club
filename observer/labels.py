"""Club-facing labels: vote across independent classifies of the same CID.

Reuse copies are not votes. One ballot per publisher (latest classify).
Field and topic show the unique winner. Keywords with more agreement
come first; split labels go last.
"""
from __future__ import annotations

import json

from .fields import normalize_field


def _classifier_kind(row):
    raw = row.get("classifier") or ""
    if isinstance(raw, dict):
        return str(raw.get("kind") or "llm")
    if not raw:
        return "llm"
    try:
        obj = json.loads(raw)
    except ValueError:
        return "llm"
    if isinstance(obj, dict):
        return str(obj.get("kind") or "llm")
    return "llm"


def _norm_text(value):
    return " ".join(str(value or "").strip().lower().split())


def _split_keywords(value):
    out = []
    seen = set()
    for part in str(value or "").split(","):
        key = _norm_text(part)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _prefer_independent(rows):
    """Reuse copies do not vote, unless a CID has nothing else."""
    independent = [r for r in rows if _classifier_kind(r) != "reuse"]
    return independent or rows


def _eligible(conn, cid):
    rows = [dict(raw) for raw in conn.execute(
        "SELECT * FROM classifies WHERE cid = ? ORDER BY received_at ASC",
        (cid,),
    )]
    return _prefer_independent(rows)


def independent_publishers(conn, cid):
    """Publishers with a non-reuse classify. Reuse-only CIDs return empty."""
    rows = [dict(raw) for raw in conn.execute(
        "SELECT * FROM classifies WHERE cid = ? ORDER BY received_at ASC",
        (cid,),
    )]
    independent = [r for r in rows if _classifier_kind(r) != "reuse"]
    if not independent:
        return []
    return [r["publisher"] for r in _latest_per_publisher(independent)]


def _latest_per_publisher(rows):
    by_pub = {}
    for row in rows:
        prev = by_pub.get(row["publisher"])
        if prev is None or float(row["received_at"] or 0) >= float(prev["received_at"] or 0):
            by_pub[row["publisher"]] = row
    return list(by_pub.values())


def _count(items):
    """items is a list of (key, display). First display for a key wins."""
    votes = {}
    display = {}
    for key, shown in items:
        if not key:
            continue
        votes[key] = votes.get(key, 0) + 1
        display.setdefault(key, shown)
    return votes, display


def _winner(votes, display):
    if not votes:
        return None
    top = max(votes.values())
    for key, shown in display.items():
        if votes.get(key) == top:
            return shown
    return None


def _keep_keywords(votes, display):
    if not votes:
        return []
    kept = [(k, votes[k]) for k in votes]
    kept.sort(key=lambda kv: (-kv[1], kv[0]))
    return [display[k] for k, _ in kept[:10]]


def _tally(rows):
    """Vote across already-loaded classify rows for one CID."""
    voters = _latest_per_publisher(rows)
    field_items = []
    topic_items = []
    keyword_items = []
    for row in voters:
        field = normalize_field(row.get("field"))
        field_items.append((field, field))
        topic_key = _norm_text(row.get("topic"))
        if topic_key:
            topic_items.append((topic_key, str(row.get("topic") or "").strip()))
        for key in _split_keywords(row.get("keywords")):
            keyword_items.append((key, key))

    field_votes, field_disp = _count(field_items)
    topic_votes, topic_disp = _count(topic_items)
    keyword_votes, keyword_disp = _count(keyword_items)
    return {
        "field": _winner(field_votes, field_disp),
        "topic": _winner(topic_votes, topic_disp),
        "keywords": ", ".join(_keep_keywords(keyword_votes, keyword_disp)),
        "label_voters": len(voters),
        "field_votes": field_votes,
        "topic_votes": topic_votes,
        "keyword_votes": keyword_votes,
    }


def consensus(conn, cid):
    """Return display labels plus vote tallies for one CID."""
    return _tally(_eligible(conn, cid))


# SQLite's default parameter limit is 999; stay well inside it.
_IN_CHUNK = 400


def consensus_many(conn, cids):
    """Votes for many CIDs, keyed by CID.

    Search and browse decorate every row, so a per-CID query made result
    pages cost one round trip per hit and made export cost thousands.
    """
    order = list(dict.fromkeys(cid for cid in cids if cid))
    if not order:
        return {}
    grouped = {cid: [] for cid in order}
    for start in range(0, len(order), _IN_CHUNK):
        chunk = order[start:start + _IN_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        for raw in conn.execute(
            "SELECT * FROM classifies WHERE cid IN (%s) "
            "ORDER BY received_at ASC" % placeholders,
            chunk,
        ):
            row = dict(raw)
            bucket = grouped.get(row["cid"])
            if bucket is not None:
                bucket.append(row)
    return {
        cid: _tally(_prefer_independent(rows))
        for cid, rows in grouped.items()
    }


def apply(out, conn, cid):
    """Overwrite display labels on a classify dict with the club vote."""
    out.update(consensus(conn, cid))
    return out


def apply_many(conn, rows):
    """Overwrite display labels on many classify dicts with the club vote."""
    votes = consensus_many(conn, [row["cid"] for row in rows])
    for row in rows:
        voted = votes.get(row["cid"])
        if voted:
            row.update(voted)
    return rows
