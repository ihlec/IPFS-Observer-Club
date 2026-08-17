"""Catch-up snapshot, inbox prune, clubd flags."""
import os
import time

from observer import clubd_flags, ingest, store, work
from tests.cids import cid_for

CID1 = cid_for("1")
CID2 = cid_for("2")
CID_PNG = cid_for("png")
CID_BLOG = cid_for("blog")
CID_SCOPE = cid_for("scope")
CID_PAPER = cid_for("paper")
CID_DIR = cid_for("dir")


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    store._local.conn = None
    return store.connect()


def test_export_snapshot_signed_only(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID1, "publisher": "p",
        "field": "biology", "text_sha256": "a", "v": 1,
    })
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID2, "publisher": "p",
        "field": "biology", "text_sha256": "b", "v": 1, "sig": "aa",
    })
    body = store.export_snapshot(conn, 100)
    assert CID2 in body
    assert CID1 not in body
    assert '"sig":' in body


def test_inbox_offsets_live_outside_jsonl_dir(tmp_path, monkeypatch):
    inbox = tmp_path / "academic" / "inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setattr(ingest.config, "INBOX_DIR", str(inbox))
    key = str(inbox / "2026-08-17.jsonl")
    ingest._save_offsets({key: 42})
    stored = tmp_path / "academic" / "inbox-offsets.json"
    assert stored.is_file()
    assert not (inbox / ".offsets.json").exists()
    assert ingest._load_offsets()[key] == 42
    stored.unlink()
    (inbox / ".offsets.json").write_text('{"legacy": 7}', encoding="utf-8")
    assert ingest._load_offsets()["legacy"] == 7


def test_prune_inbox_drops_old_consumed(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(ingest.config, "INBOX_DIR", str(inbox))
    monkeypatch.setitem(ingest.config.CLUB, "inbox_keep_days", 1)
    monkeypatch.setitem(ingest.config.CLUB, "inbox_max_bytes", 10 * 1024 * 1024)
    old = inbox / "2000-01-01.jsonl"
    old.write_text("{}\n")
    os.utime(old, (1, 1))
    today = time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl"
    (inbox / today).write_text("{}\n")
    offsets = {str(old): old.stat().st_size}
    ingest.prune_inbox(offsets)
    assert not old.exists()
    assert (inbox / today).exists()
    assert str(old) not in offsets


def test_clubd_snapshot_url_uses_loopback(monkeypatch):
    monkeypatch.setattr(clubd_flags.config, "WEB_HOST", "0.0.0.0")
    monkeypatch.setattr(clubd_flags.config, "WEB_PORT", 8002)
    flags = clubd_flags.flags()
    url = flags[flags.index("-snapshot-url") + 1]
    assert url == "http://127.0.0.1:8002/api/snapshot"


def test_clubd_flags_include_bootstrap(monkeypatch):
    monkeypatch.setitem(clubd_flags.config.CLUB, "bootstrap_peers", [
        "/ip4/1.2.3.4/tcp/4713/p2p/12D3",
    ])
    flags = clubd_flags.flags()
    assert "-bootstrap" in flags
    assert flags[flags.index("-bootstrap") + 1] == "/ip4/1.2.3.4/tcp/4713/p2p/12D3"
    assert "-rate" in flags
    assert "-snapshot-url" in flags
    assert "-club" in flags
    assert flags[flags.index("-club") + 1] == clubd_flags.config.CLUB_ID
    assert any(f.startswith("-mdns=") for f in flags)


def test_export_snapshot_drops_unprocessable_skips(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "skip", "cid": CID_PNG, "publisher": "p",
        "reason": "unprocessable", "v": 1, "sig": "aa",
    })
    store.ingest_message(conn, {
        "kind": "skip", "cid": CID_BLOG, "publisher": "p",
        "reason": "not_academic", "v": 1, "sig": "bb",
    })
    store.ingest_message(conn, {
        "kind": "skip", "cid": CID_SCOPE, "publisher": "p",
        "reason": "out_of_scope", "v": 1, "sig": "dd",
    })
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID_PAPER, "publisher": "p",
        "field": "biology", "text_sha256": "x", "v": 1, "sig": "cc",
    })
    store.ingest_message(conn, {
        "kind": "skip", "cid": CID_DIR, "publisher": "p",
        "reason": "directory", "v": 1, "sig": "ee",
    })
    body = store.export_snapshot(conn, 100)
    assert CID_PNG not in body
    assert CID_BLOG in body
    assert CID_SCOPE in body
    assert CID_PAPER in body
    assert CID_DIR not in body


def test_export_snapshot_prefers_documents_over_skips(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    for i in range(3):
        store.ingest_message(conn, {
            "kind": "skip", "cid": cid_for("skip-%d" % i), "publisher": "p",
            "reason": "out_of_scope", "v": 1, "sig": "s%d" % i,
        }, received_at=i)
    store.ingest_message(conn, {
        "kind": "classify", "cid": CID_PAPER, "publisher": "p",
        "field": "biology", "text_sha256": "x", "v": 1, "sig": "cc",
    }, received_at=10)
    body = store.export_snapshot(conn, 2)
    assert CID_PAPER in body
    assert body.index(CID_PAPER) < body.index("out_of_scope")
    assert body.count("\n") == 2


def test_export_snapshot_includes_current_alias(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "alias", "publisher": "p", "alias": "old", "v": 1, "sig": "aa",
    })
    store.ingest_message(conn, {
        "kind": "alias", "publisher": "p", "alias": "new", "v": 1, "sig": "bb",
    })
    body = store.export_snapshot(conn, 100)
    assert '"alias":"new"' in body
    assert '"alias":"old"' not in body


def test_export_snapshot_includes_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    work._local.conn = None
    conn = _conn(tmp_path, monkeypatch)
    store.ingest_message(conn, {
        "kind": "report", "cid": CID1, "publisher": "p",
        "reason": "wrong", "v": 1, "sig": "aa",
    })
    store.ingest_message(conn, {
        "kind": "report", "cid": CID2, "publisher": "p",
        "reason": "abusive", "v": 1, "sig": "bb",
    })
    store.ingest_message(conn, {
        "kind": "report", "cid": CID_PNG, "publisher": "p",
        "reason": "spam", "v": 1, "sig": "cc",
    })
    body = store.export_snapshot(conn, 100)
    assert CID1 in body
    assert CID2 in body
    assert CID_PNG not in body
    assert '"kind":"report"' in body
