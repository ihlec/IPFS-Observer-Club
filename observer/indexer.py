"""Discover CIDs, honour club skip-hook/claims, classify, publish.

This node sniffs Bitswap locally. Club gossip is the shared catalog: if a CID
is already classified or skipped, we do not fetch or call the model, unless a
wrong-classification report asks, or this node can cast the second vote.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import threading
import time

from . import (
    cidutil,
    classify,
    club,
    clubd_client,
    config,
    extract,
    fetch,
    store,
    work,
)
from .fields import normalize_field

log = logging.getLogger("indexer")

_claim_slot = threading.Semaphore(1)
_claimed_until = 0.0
_clubd_down_logged = False
_stats_lock = threading.Lock()
_STAT_KEYS = (
    "fetched", "mime_skips", "dir_drops", "heuristic_skips",
    "llm_skips", "llm_classifies", "reuse",
)
_run_stats = {k: 0 for k in _STAT_KEYS}
_window = {k: 0 for k in _STAT_KEYS}


def runtime_stats():
    with _stats_lock:
        return dict(_run_stats)


def _bump(key):
    with _stats_lock:
        _run_stats[key] = _run_stats.get(key, 0) + 1
        _window[key] = _window.get(key, 0) + 1


def log_summary(dropped=0):
    """One line for the last janitor interval. Per-CID lines stay on classifies."""
    with _stats_lock:
        snap = dict(_window)
        for key in _window:
            _window[key] = 0
    q = work.stats()
    log.info(
        "fetched=%d heuristic=%d llm_skip=%d classified=%d reuse=%d "
        "mime=%d dirs=%d queue=%d dropped=%d %s",
        snap.get("fetched", 0), snap.get("heuristic_skips", 0),
        snap.get("llm_skips", 0), snap.get("llm_classifies", 0),
        snap.get("reuse", 0), snap.get("mime_skips", 0),
        snap.get("dir_drops", 0), q.get("backlog", 0), int(dropped or 0),
        classify.backends_status(),
    )


def _foreign_live_claim(hit):
    if not hit or hit.get("kind") != "claim":
        return False
    if float(hit.get("until") or 0) <= time.time():
        return False
    me = clubd_client.peer_id()
    if me and hit.get("publisher") == me:
        return False
    return True


def reset_claims():
    """Tests: drop a leftover lease so the next case can claim."""
    global _claimed_until
    _claimed_until = 0.0
    if not _claim_slot.acquire(blocking=False):
        try:
            _claim_slot.release()
        except ValueError:
            pass
        return
    _claim_slot.release()


def _try_claim(cid):
    """Advertise at most one live claim. clubd rejects a second until skip/classify."""
    global _claimed_until
    if time.time() < _claimed_until:
        return False
    if not _claim_slot.acquire(blocking=False):
        return False
    if not clubd_client.publish_claim(cid):
        # Lease is still held on clubd (or clubd is unhappy). Do not stampede.
        _claimed_until = time.time() + 30
        _release_claim()
        return False
    _claimed_until = time.time() + float(config.CLAIM_TTL)
    return True


def _claim_cleared():
    global _claimed_until
    _claimed_until = 0.0


def _release_claim():
    try:
        _claim_slot.release()
    except ValueError:
        pass


def _publish_classify(cid, **fields):
    payload = dict(fields)
    if payload.get("indexed_at") is not None:
        payload["indexed_at"] = int(payload["indexed_at"])
    if payload.get("field"):
        payload["field"] = normalize_field(payload["field"])
    return clubd_client.publish_classify(cid, **payload)


def _retry_later(conn, row, **fields):
    """Return the CID to discovered without burning fetch retries."""
    fields.setdefault("error", "publish_failed")
    fields["attempts"] = int(row["attempts"])
    work.mark(conn, row["cid"], "discovered", **fields)
    return False


def process_one(conn, row):
    """Fetch -> extract -> classify one CID. Content never leaves this function."""
    cid, codec = row["cid"], row["codec"]
    if not cidutil.valid(cid):
        work.forget_cid(conn, cid)
        return True
    catalog = store.connect()
    hit = store.lookup_cid(catalog, cid)
    if hit:
        kind = hit.get("kind")
        rerun = store.must_classify(cid)
        if kind == "skip" and not rerun:
            work.mark(conn, cid, "skipped", mime_type=hit.get("mime_type"),
                      error=hit.get("reason") or "club_skip",
                      last_checked=time.time())
            return True
        if kind == "classify" and not rerun:
            work.mark(conn, cid, "indexed", mime_type=hit.get("mime_type"),
                      size=hit.get("size"), filename=hit.get("filename"),
                      error=None)
            return True
        if _foreign_live_claim(hit):
            work.mark(conn, cid, "discovered")
            return False

    attempts = row["attempts"] + 1
    work.bump_attempts(conn, cid, attempts)

    result = fetch.fetch_cid(cid, codec=codec, attempt=attempts - 1)
    work.note_fetch(conn, cid, retrieved=time.time() if result.ok else None)
    if result.ok:
        _bump("fetched")
    if result.ok and (
        result.is_directory or result.mime_type == "inode/directory"
    ):
        # Folders stay local. Gossiping them crowds out classifies.
        work.drop_directory(conn, cid)
        _bump("dir_drops")
        return True
    if not result.ok:
        generic_max = int(config.FETCH.get("max_retries", 1)) + 1
        slow_max = int(config.FETCH.get("max_timeout_retries", 3)) + 1
        if result.dead:
            give_up = True
        elif result.slow:
            give_up = attempts >= slow_max
        else:
            give_up = attempts >= generic_max
        if give_up:
            work.forget_cid(conn, cid)
            conn.commit()
        else:
            work.mark(conn, cid, "discovered", error=result.error)
        return False

    filename = None
    text, mime, license_name, _license_source = extract.extract_document(
        result.data, result.mime_type
    )
    size = result.size if result.size is not None else len(result.data or b"")
    result.data = b""

    if not extract.processable(mime) or not extract.usable_text(text, mime):
        # Images stay off the live cap. Short PDFs can reappear later.
        if extract.binary_mime(mime):
            work.remember_binary(cid, mime)
        else:
            work.mark(conn, cid, "skipped", mime_type=mime, size=size,
                      filename=filename, error="unprocessable")
        _bump("mime_skips")
        log.debug("skip %s unprocessable %s", cid[:24], mime or "?")
        return True

    fp = extract.fingerprint(text)
    reused = store.classify_by_text_hash(catalog, fp)
    if reused and not store.must_classify(cid):
        ok = _publish_classify(
            cid, mime_type=mime, size=size, filename=filename,
            field=reused.get("field"), topic=reused.get("topic"),
            keywords=reused.get("keywords"), license=license_name,
            text_sha256=fp,
            classifier={"kind": "reuse", "prompt_ver": classify.prompt_ver()},
            indexed_at=int(time.time()),
        )
        if ok:
            work.mark(conn, cid, "indexed", mime_type=mime, size=size, filename=filename,
                      error=None)
            _bump("reuse")
            log.info("classified %s field=%s via reuse", cid[:24], reused.get("field"))
            return True
        return _retry_later(conn, row, mime_type=mime, size=size, filename=filename)

    prior = club.current().prior(text, mime=mime, filename=filename)
    if prior == club.UNLIKELY:
        if clubd_client.publish_skip(cid, mime, club.SCOPE_SKIP_REASON):
            work.mark(conn, cid, "skipped", mime_type=mime, size=size,
                      filename=filename, error=club.SCOPE_SKIP_REASON)
            _bump("heuristic_skips")
            log.debug("skip %s %s via heuristic", cid[:24], club.SCOPE_SKIP_REASON)
            return True
        return _retry_later(conn, row, mime_type=mime, size=size, filename=filename)

    if not classify.available():
        return _retry_later(conn, row, mime_type=mime, size=size, filename=filename,
                            error="llm_down")

    claimed = _try_claim(cid)
    try:
        meta = classify.classify(text, mime, filename=filename, codec=codec)
        if not meta:
            return _retry_later(conn, row, mime_type=mime, size=size,
                                filename=filename, error="classify_failed")
        in_scope = meta.get("in_scope")
        if in_scope is None:
            in_scope = meta.get("academic_document")
        if not in_scope and prior == club.LIKELY:
            work.mark(conn, cid, "skipped", mime_type=mime, size=size,
                      filename=filename, error="llm_disagreed",
                      last_checked=time.time())
            _bump("llm_skips")
            _claim_cleared()
            log.info("llm marked %s out of scope; prior=likely, not publishing",
                     cid[:24])
            return True
        if not in_scope:
            if clubd_client.publish_skip(cid, mime, club.SCOPE_SKIP_REASON):
                work.mark(conn, cid, "skipped", mime_type=mime, size=size,
                          filename=filename, error=club.SCOPE_SKIP_REASON)
                _bump("llm_skips")
                _claim_cleared()
                log.debug("skip %s %s via llm mime=%s prior=%s (%s)",
                          cid[:24], club.SCOPE_SKIP_REASON, mime or "?",
                          prior or "none", meta.get("provider") or "?")
                return True
            return _retry_later(conn, row, mime_type=mime, size=size, filename=filename)
        if not license_name:
            license_name = extract.normalize_license(meta["license"])
        ok = _publish_classify(
            cid, mime_type=mime, size=size, filename=filename,
            field=meta["field"], topic=meta["topic"],
            keywords=meta["keywords"], license=license_name,
            text_sha256=fp,
            classifier={
                "kind": "llm",
                "model": meta.get("model") or "",
                "prompt_ver": classify.prompt_ver(),
            },
            indexed_at=int(time.time()),
        )
        if ok:
            work.mark(conn, cid, "indexed", mime_type=mime, size=size, filename=filename,
                      error=None)
            _bump("llm_classifies")
            _claim_cleared()
            log.info("classified %s field=%s via llm (%s)",
                     cid[:24], meta["field"], meta.get("provider") or "")
            return True
        return _retry_later(conn, row, mime_type=mime, size=size, filename=filename)
    finally:
        if claimed:
            _release_claim()


def requeue_unpublished():
    """Put work-queue rows back to discovered if they never landed in the club.

    Local mime-skips are not republished. Only indexed records and gossiped
    out-of-scope skips need a club record.
    """
    catalog = store.connect()
    classified = {r[0] for r in catalog.execute("SELECT cid FROM classifies")}
    skipped = {r[0] for r in catalog.execute("SELECT cid FROM skips")}
    n = 0
    scope_errors = tuple(club.PERSIST_SKIP_REASONS)
    placeholders = ",".join("?" * len(scope_errors))
    with work.locked() as conn:
        for row in conn.execute("SELECT cid FROM cids WHERE status = 'indexed'"):
            if row[0] in classified:
                continue
            conn.execute(
                "UPDATE cids SET status = 'discovered', attempts = 0, error = 'republish' "
                "WHERE cid = ?",
                (row[0],),
            )
            n += 1
        for row in conn.execute(
            "SELECT cid FROM cids WHERE status = 'skipped' AND error IN (%s)"
            % placeholders,
            scope_errors,
        ):
            if row[0] in skipped:
                continue
            conn.execute(
                "UPDATE cids SET status = 'discovered', attempts = 0, error = 'republish' "
                "WHERE cid = ?",
                (row[0],),
            )
            n += 1
        n += conn.execute(
            "UPDATE cids SET attempts = 0, status = 'discovered' "
            "WHERE error = 'publish_failed'"
        ).rowcount
        conn.commit()
    return n


def worker_loop(stop_event):
    global _clubd_down_logged
    conn = work.connect()
    while not stop_event.is_set():
        if not clubd_client.available():
            if not _clubd_down_logged:
                log.info("clubd not reachable at %s - waiting to publish",
                         clubd_client.api_base())
                _clubd_down_logged = True
            stop_event.wait(2)
            continue
        if _clubd_down_logged:
            log.info("clubd reachable, publishing")
            _clubd_down_logged = False
        try:
            rows = work.take_batch(conn, limit=5)
        except sqlite3.OperationalError as e:
            log.warning("work queue busy, backing off: %s", e)
            stop_event.wait(1)
            continue
        if not rows:
            stop_event.wait(15)
            continue
        for row in rows:
            if stop_event.is_set():
                return
            try:
                process_one(conn, row)
            except sqlite3.OperationalError as e:
                log.warning("work queue busy on %s, later: %s", row["cid"][:24], e)
                _retry_later(conn, row, error="db_locked")
            except Exception:
                log.exception("processing %s failed", row["cid"])
                _retry_later(conn, row, error="worker_error")
        # Mime-skips do not need LM Studio. If a batch needed the model and
        # it is down, back off so we do not spin on the same PDFs.
        if not classify.available():
            stop_event.wait(15)


class SnifferManager:
    """Start/stop the Bitswap sniffer from the live work-queue cap."""

    def __init__(self, stop_event):
        self.stop_event = stop_event
        self.proc = None
        self.log_fh = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running():
            return
        os.makedirs(os.path.dirname(config.SNIFFER_LOG) or ".", exist_ok=True)
        os.makedirs(config.SPOOL_DIR, exist_ok=True)
        os.makedirs(config.SNIFFER_HOME, exist_ok=True)
        if self.log_fh is None:
            self.log_fh = open(config.SNIFFER_LOG, "ab")
        s = config.SNIFFER
        args = [
            config.SNIFFER_BIN,
            "-port", str(int(s.get("listen_port", 4712))),
            "-low", str(int(s.get("low_connections", 50))),
            "-high", str(int(s.get("high_connections", 80))),
            "-spool", config.SPOOL_DIR,
            "-interval", "%ds" % int(s.get("discovery_interval_seconds", 30)),
        ]
        self.proc = subprocess.Popen(
            args, stdout=self.log_fh, stderr=self.log_fh, cwd=config.SNIFFER_HOME,
        )
        log.info("sniffer started (pid %d)", self.proc.pid)

    def stop(self):
        if not self.running():
            self.proc = None
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        log.info("sniffer stopped")
        self.proc = None

    def loop(self):
        if not os.path.exists(config.SNIFFER_BIN):
            log.error("sniffer binary missing at %s - run 'make build'; "
                      "continuing without discovery", config.SNIFFER_BIN)
            return
        conn = work.connect()
        self.start()
        while not self.stop_event.is_set():
            try:
                live = work.live_count(conn)
                cap = work.max_queue()
                low = int(config.CONTROL.get("backlog_low", 200))
                if self.running() and live >= cap:
                    log.info("live queue %d >= %d: pausing discovery", live, cap)
                    self.stop()
                elif not self.running() and live <= low:
                    log.info("live queue %d: resuming discovery", live)
                    self.start()
            except Exception:
                log.exception("sniffer control check failed")
            self.stop_event.wait(config.CONTROL_INTERVAL)
        self.stop()
        if self.log_fh:
            self.log_fh.close()
