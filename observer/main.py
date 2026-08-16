"""Supervisor: ingest gossip, sniff Bitswap, classify, web UI.

clubd (Go) is a sibling process started by the Makefile so libp2p can
crash-restart independently of Python. The Bitswap sniffer is managed here
so discovery pauses when the live work queue is at cap.
"""
import logging
import resource
import signal
import threading
import time

import uvicorn

from . import alias, classify, club, config, ingest, indexer, janitor, spool, store, work
from .web import app

log = logging.getLogger("main")


def _raise_fd_limit():
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return
    target = max(8192, int(config.FETCH.get("concurrency", 8)) * 16)
    if soft >= target:
        return
    for candidate in (target, 16384, 10240, 8192, 4096, 2048):
        want = candidate if hard == resource.RLIM_INFINITY else min(candidate, hard)
        if want <= soft:
            continue
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            log.info("raised open-file limit %d -> %d", soft, want)
            return
        except (ValueError, OSError):
            continue


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _raise_fd_limit()
    config.migrate_legacy_paths()
    try:
        profile = club.current()
    except ValueError as e:
        log.error("%s", e)
        raise SystemExit(2) from e
    store.connect()
    catalog = store.connect()
    purged = store.purge_unprocessable_skips(catalog)
    if purged:
        log.info("purged %d leftover unprocessable club skips", purged)
    dropped_cids = store.purge_invalid_cids(catalog)
    if dropped_cids:
        log.info("purged %d catalog rows with invalid CIDs", dropped_cids)
    with work.locked() as conn:
        released = conn.execute(
            "UPDATE cids SET status = 'discovered' WHERE status = 'processing'"
        ).rowcount
    if released:
        log.info("released %d orphaned in-flight CIDs", released)
    republished = indexer.requeue_unpublished()
    if republished:
        log.info("requeued %d CIDs that never published to clubd", republished)
    dropped = work.prune(conn)
    if dropped:
        log.info("dropped %d stale work-queue CIDs", dropped)
    if alias.announce():
        log.info("announced alias %s", alias.current())

    llm_ok = classify.available()
    stop = threading.Event()

    def handle(signum, frame):
        log.info("signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    web_server = uvicorn.Server(uvicorn.Config(
        app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning",
    ))
    sniffer = indexer.SnifferManager(stop)
    workers = int(config.FETCH.get("concurrency", 8))
    threads = [
        threading.Thread(target=ingest.loop, args=(stop,), name="ingest", daemon=True),
        threading.Thread(target=spool.loop, args=(stop,), name="spool", daemon=True),
        threading.Thread(target=janitor.loop, args=(stop,), name="janitor", daemon=True),
        threading.Thread(target=sniffer.loop, name="sniffer-mgr", daemon=True),
        threading.Thread(target=web_server.run, name="web", daemon=True),
    ]
    for i in range(workers):
        threads.append(threading.Thread(
            target=indexer.worker_loop, args=(stop,), name="worker-%d" % i, daemon=True,
        ))
    for t in threads:
        t.start()
    log.info("observer up: club=%s web=http://%s:%s clubd=%s:%s workers=%d llm=%s",
             profile.id if profile else config.CLUB_ID,
             config.WEB_HOST, config.WEB_PORT,
             config.API_HOST, config.API_PORT, workers,
             "up" if llm_ok else "down")
    while not stop.is_set():
        time.sleep(0.5)
    web_server.should_exit = True
    sniffer.stop()
    log.info("shutdown")


if __name__ == "__main__":
    main()
