"""Consume verified JSONL from clubd's inbox directory."""
from __future__ import annotations

import json
import logging
import os
import time

from . import config, store

log = logging.getLogger("ingest")


def _offset_path():
    return os.path.join(config.INBOX_DIR, ".offsets.json")


def _load_offsets():
    p = _offset_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_offsets(offsets):
    os.makedirs(config.INBOX_DIR, exist_ok=True)
    tmp = _offset_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(offsets, f)
    os.replace(tmp, _offset_path())


def drain_once():
    """Ingest complete JSONL lines. Returns count of new messages."""
    os.makedirs(config.INBOX_DIR, exist_ok=True)
    offsets = _load_offsets()
    conn = store.connect()
    n = 0
    for name in sorted(os.listdir(config.INBOX_DIR)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(config.INBOX_DIR, name)
        pos = int(offsets.get(path, 0))
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if pos > size:
            pos = 0
        with open(path, "rb") as f:
            f.seek(pos)
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    # incomplete record; wait for the next drain
                    break
                pos = f.tell()
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except ValueError:
                    log.warning("bad json in %s", name)
                    continue
                if store.ingest_message(conn, obj):
                    n += 1
                    if obj.get("kind") == "classify":
                        store.maybe_enqueue_second(obj.get("cid") or "")
            offsets[path] = pos
    prune_inbox(offsets)
    _save_offsets(offsets)
    store.expire_claims(conn)
    return n


def prune_inbox(offsets):
    """Drop old jsonl once ingested; cap total inbox size."""
    keep_days = int(config.CLUB.get("inbox_keep_days", 7))
    max_bytes = int(config.CLUB.get("inbox_max_bytes", 64 * 1024 * 1024))
    today = time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl"
    files = []
    try:
        names = os.listdir(config.INBOX_DIR)
    except OSError:
        return
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(config.INBOX_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        files.append((name, path, st.st_size, st.st_mtime))
    files.sort(key=lambda x: x[0])
    total = sum(f[2] for f in files)
    cutoff = time.time() - keep_days * 86400

    def _remove(item):
        name, path, size, _mtime = item
        if name == today:
            return False
        try:
            os.remove(path)
        except OSError:
            return False
        offsets.pop(path, None)
        return size

    for item in list(files):
        name, path, size, mtime = item
        consumed = int(offsets.get(path, 0)) >= size
        if name != today and consumed and mtime < cutoff:
            removed = _remove(item)
            if removed:
                total -= removed
                files.remove(item)
    for item in list(files):
        if total <= max_bytes:
            break
        removed = _remove(item)
        if removed:
            total -= removed
            files.remove(item)


def loop(stop_event):
    os.makedirs(config.INBOX_DIR, exist_ok=True)
    while not stop_event.is_set():
        try:
            n = drain_once()
            if n:
                log.debug("ingested %d new club messages", n)
        except Exception:
            log.exception("inbox drain failed")
        stop_event.wait(1.0)
