"""Cap the local work queue so discovery cannot fill the disk."""
from __future__ import annotations

import logging
import shutil
import time

from . import config, work

log = logging.getLogger("janitor")

BATCH = 500
LOW_WATER = 0.9
_SCORE = "(peer_count * 1.0) / (1.0 + MAX(0, (? - last_seen) / 604800.0))"


def _over_budget():
    max_bytes = int(config.STORAGE.get("max_db_size_mb", 2048)) * 1024 * 1024
    min_free = int(config.STORAGE.get("min_free_disk_mb", 10240)) * 1024 * 1024
    size = work.db_file_size()
    free = shutil.disk_usage(config.WORK_DB).free
    if size > max_bytes:
        return True, size, max_bytes
    if free < min_free:
        return True, size, max(0, size - (min_free - free))
    return False, size, max_bytes


def evict_batch(conn):
    now = time.time()
    with work.locked():
        return _evict_batch(conn, now)


def _evict_batch(conn, now):
    rows = conn.execute(
        "SELECT rowid, cid, peer_count FROM cids "
        "ORDER BY " + _SCORE + " ASC, "
        "  CASE status WHEN 'indexed' THEN 1 ELSE 0 END ASC "
        "LIMIT ?",
        (now, BATCH),
    ).fetchall()
    if not rows:
        return 0
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO evicted(cid, peer_count, evicted_at) "
            "VALUES (?, ?, ?)",
            (row["cid"], row["peer_count"], now),
        )
        conn.execute("DELETE FROM cid_peers WHERE cid = ?", (row["cid"],))
        conn.execute("DELETE FROM cids WHERE rowid = ?", (row["rowid"],))
    conn.commit()
    return len(rows)


def run_once():
    conn = work.connect()
    dropped = work.prune(conn)
    from . import indexer
    indexer.log_summary(dropped)
    over, size, budget = _over_budget()
    if not over:
        return 0
    target = budget * LOW_WATER
    total = 0
    log.warning("work-queue disk budget exceeded (db=%.1f MB), evicting", size / 1e6)
    while work.db_file_size() > target:
        n = evict_batch(conn)
        total += n
        if n == 0:
            break
        with work.locked():
            conn.execute("PRAGMA incremental_vacuum")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
    with work.locked():
        conn.execute(
            "DELETE FROM evicted WHERE cid IN ("
            "  SELECT cid FROM evicted ORDER BY evicted_at DESC "
            "  LIMIT -1 OFFSET 50000)"
        )
        conn.commit()
    if total:
        log.info("evicted %d work-queue CIDs", total)
    return total


def loop(stop_event):
    interval = int(config.STORAGE.get("janitor_interval_seconds", 30))
    while not stop_event.is_set():
        try:
            run_once()
        except Exception:
            log.exception("janitor cycle failed")
        stop_event.wait(interval)
