"""Import sniffer spool files into the local work queue."""
from __future__ import annotations

import glob
import json
import logging
import os
import time

from . import cidutil, config, store, work

log = logging.getLogger("spool")

_SKIP_CODECS = frozenset(("libp2p-key", "json", "dag-json", "dag-cbor"))
_COMMIT_EVERY = 50


def spool_files():
    return sorted(glob.glob(os.path.join(config.SPOOL_DIR, "cids-*.jsonl")))


def _offset(conn, path, size):
    row = conn.execute(
        "SELECT offset FROM spool_offsets WHERE path = ?", (path,)
    ).fetchone()
    offset = row["offset"] if row else 0
    return offset if offset <= size else 0


def _record(conn, line):
    line = line.strip()
    if not line:
        return 0, 0
    try:
        rec = json.loads(line)
        cid, peer, ts = rec["cid"], rec["peer"], float(rec["ts"])
    except (ValueError, KeyError):
        return 0, 0
    if not cidutil.valid(cid):
        return 0, 1

    codec = cidutil.codec_of(cid)
    if codec in _SKIP_CODECS:
        return 0, 1
    if work.is_unprocessable(cid):
        return 0, 1

    queued = conn.execute("SELECT 1 FROM cids WHERE cid = ?", (cid,)).fetchone()
    if queued is None and work.at_cap(conn):
        # Bitswap WANTs are mostly raw leaves. Holding the offset there
        # starves UnixFS file roots (PDFs) sitting in later spool files.
        if codec != "dag-pb":
            return 0, 1
        if not work.evict_for_unixfs(conn):
            return None

    conn.execute(
        "INSERT OR IGNORE INTO seen_cids(cid, first_seen) VALUES (?, ?)",
        (cid, ts),
    )

    ev = conn.execute(
        "SELECT peer_count FROM evicted WHERE cid = ?", (cid,)
    ).fetchone()
    if ev is not None:
        new_peers = conn.execute(
            "SELECT COUNT(*) FROM cid_peers WHERE cid = ?", (cid,)
        ).fetchone()[0]
        if new_peers + 1 <= ev[0]:
            conn.execute(
                "INSERT OR IGNORE INTO cid_peers(cid, peer) VALUES (?, ?)",
                (cid, peer),
            )
            return 0, 1
        conn.execute("DELETE FROM evicted WHERE cid = ?", (cid,))

    now = time.time()
    conn.execute(
        "INSERT INTO cids (cid, codec, first_seen, last_seen, want_count) "
        "VALUES (?, ?, ?, ?, 1) "
        "ON CONFLICT(cid) DO UPDATE SET "
        "  last_seen = MAX(last_seen, excluded.last_seen), "
        "  want_count = want_count + 1",
        (cid, codec, ts, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO cid_peers(cid, peer) VALUES (?, ?)",
        (cid, peer),
    )
    conn.execute(
        "UPDATE cids SET peer_count = "
        "(SELECT COUNT(*) FROM cid_peers WHERE cid_peers.cid = cids.cid) "
        "WHERE cid = ?",
        (cid,),
    )
    return 1, 0


def _save_offset(conn, path, offset):
    conn.execute(
        "INSERT INTO spool_offsets(path, offset) VALUES (?, ?) "
        "ON CONFLICT(path) DO UPDATE SET offset = excluded.offset",
        (path, offset),
    )


def ingest_file(conn, path, final=False):
    """Import one spool file. Commits in small batches so workers are not blocked.

    Stops without advancing the offset when the live queue is at cap, so
    leftover WANTs wait instead of being pruned as overflow.
    """
    inserted = skipped = 0
    paused = False
    size = os.path.getsize(path)
    with work.locked():
        offset = _offset(conn, path, size)
    pending = 0
    with open(path, "rb") as f:
        f.seek(offset)
        for line in f.read().splitlines(keepends=True):
            complete = line.endswith(b"\n")
            if not complete and not final:
                break
            text = line.decode("utf-8", errors="replace")
            try:
                rec = json.loads(text.strip() or "{}")
                cid = rec.get("cid")
            except ValueError:
                cid = None
            if cid and store.already_catalogued(cid):
                offset += len(line)
                skipped += 1
                pending += 1
                if pending >= _COMMIT_EVERY:
                    with work.locked():
                        _save_offset(conn, path, offset)
                    pending = 0
                continue
            with work.locked():
                out = _record(conn, text)
                if out is None:
                    paused = True
                    break
                inserted_one, skipped_one = out
                inserted += inserted_one
                skipped += skipped_one
                offset += len(line)
                pending += 1
                if pending >= _COMMIT_EVERY:
                    _save_offset(conn, path, offset)
                    conn.commit()
                    pending = 0
    with work.locked():
        _save_offset(conn, path, offset)
        conn.commit()
    return inserted, skipped, paused


def run_once():
    os.makedirs(config.SPOOL_DIR, exist_ok=True)
    conn = work.connect()
    total = 0
    files = spool_files()
    for path in files[:-1]:
        try:
            inserted, skipped, paused = ingest_file(conn, path, final=True)
            total += inserted
            if paused:
                log.debug("work queue at cap, holding %s", os.path.basename(path))
                return total
            os.remove(path)
            with work.locked():
                conn.execute("DELETE FROM spool_offsets WHERE path = ?", (path,))
                conn.commit()
            log.debug("ingested %s: %d records (%d evicted-skipped)",
                     os.path.basename(path), inserted, skipped)
        except Exception:
            log.exception("failed ingesting %s", path)
    if files:
        path = files[-1]
        try:
            inserted, skipped, paused = ingest_file(conn, path)
            total += inserted
            if paused:
                log.debug("work queue at cap, holding spool")
            elif inserted or skipped:
                log.debug("ingested active %s: %d records (%d evicted-skipped)",
                         os.path.basename(path), inserted, skipped)
        except Exception:
            log.exception("failed ingesting active spool %s", path)
    return total


def loop(stop_event, interval=30):
    while not stop_event.is_set():
        try:
            run_once()
        except Exception:
            log.exception("spool cycle failed")
        stop_event.wait(interval)
