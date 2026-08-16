"""Club-facing labels: vote across independent classifies of the same CID.

Reuse copies are not votes. One ballot per publisher (latest classify).
Field and topic show the unique winner. Keywords need enough support or
they drop once several nodes have spoken.
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


def _eligible(conn, cid):
    rows = conn.execute(
        "SELECT * FROM classifies WHERE cid = ? ORDER BY received_at ASC",
        (cid,),
    ).fetchall()
    out = [dict(raw) for raw in rows]
    independent = [r for r in out if _classifier_kind(r) != "reuse"]
    return independent or out


def independent_publishers(conn, cid):
    """Publishers with a non-reuse classify. Reuse-only CIDs return empty."""
    rows = [dict(raw) for raw in conn.execute(
        "SELECT * FROM classifies WHERE cid = ? ORDER BY received_at ASC",
        (cid,),
    )]
    independent = [r for r in rows if _classifier_kind(r) != "reuse"]
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


def _keep_keywords(votes, display, n_voters):
    if not votes:
        return []
    max_v = max(votes.values())
    floor = 1
    if n_voters >= 2:
        floor = max(2, (max_v + 1) // 2)
    kept = [(k, votes[k]) for k in votes if votes[k] >= floor]
    if not kept:
        kept = [(k, votes[k]) for k in votes if votes[k] == max_v]
    kept.sort(key=lambda kv: (-kv[1], kv[0]))
    return [display[k] for k, _ in kept[:10]]


def consensus(conn, cid):
    """Return display labels plus vote tallies for one CID."""
    voters = _latest_per_publisher(_eligible(conn, cid))
    n = len(voters)
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
    keywords = _keep_keywords(keyword_votes, keyword_disp, n)
    return {
        "field": _winner(field_votes, field_disp),
        "topic": _winner(topic_votes, topic_disp),
        "keywords": ", ".join(keywords),
        "label_voters": n,
        "field_votes": field_votes,
        "topic_votes": topic_votes,
        "keyword_votes": keyword_votes,
    }


def apply(out, conn, cid):
    """Overwrite display labels on a classify dict with the club vote."""
    voted = consensus(conn, cid)
    out.update(voted)
    return out
