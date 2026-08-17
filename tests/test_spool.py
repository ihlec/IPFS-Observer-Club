"""Sniffer spool ingest into the local work queue."""
import json
import os
import time

from observer import spool, store, work
from tests.cids import cid_for, cid_pb_for

CID_ONE = cid_for("one")
CID_TWO = cid_for("two")


def test_spool_ingest_records(tmp_path, monkeypatch):
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    monkeypatch.setattr(spool.config, "SPOOL_DIR", str(spool_dir))
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    work._local.conn = None
    store._local.conn = None
    path = spool_dir / "cids-20260101-000000.jsonl"
    now = int(time.time())
    recs = [
        {"ts": now - 2, "cid": CID_ONE, "peer": "12D3aaa"},
        {"ts": now - 1, "cid": CID_ONE, "peer": "12D3bbb"},
        {"ts": now, "cid": CID_TWO, "peer": "12D3aaa"},
        {"ts": now, "cid": "bafya", "peer": "12D3aaa"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    n = spool.run_once()
    assert n == 3
    conn = work.connect()
    rows = {r["cid"]: r for r in conn.execute("SELECT * FROM cids")}
    assert rows[CID_ONE]["peer_count"] == 2
    assert rows[CID_ONE]["want_count"] == 2
    assert rows[CID_TWO]["peer_count"] == 1
    assert "bafya" not in rows
    assert os.path.exists(path)  # active file kept


def test_spool_skips_raw_at_cap_to_reach_unixfs(tmp_path, monkeypatch):
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    monkeypatch.setattr(spool.config, "SPOOL_DIR", str(spool_dir))
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    monkeypatch.setitem(work.config.FETCH, "max_queue", 1)
    work._local.conn = None
    store._local.conn = None
    now = int(time.time())
    raw = cid_for("held-raw")
    unixfs = cid_pb_for("paper")
    path = spool_dir / "cids-20260101-000000.jsonl"
    path.write_text("".join(
        json.dumps({"ts": now, "cid": cid, "peer": "12D3aaa"}) + "\n"
        for cid in (raw, unixfs)
    ))
    assert spool.run_once() == 2
    conn = work.connect()
    rows = {r[0]: r[1] for r in conn.execute("SELECT cid, codec FROM cids")}
    assert unixfs in rows and rows[unixfs] == "dag-pb"
    assert raw not in rows


def test_spool_holds_unixfs_at_cap_when_queue_is_unixfs(tmp_path, monkeypatch):
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    monkeypatch.setattr(spool.config, "SPOOL_DIR", str(spool_dir))
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    monkeypatch.setitem(work.config.FETCH, "max_queue", 1)
    work._local.conn = None
    store._local.conn = None
    now = int(time.time())
    first = cid_pb_for("first-pdf")
    second = cid_pb_for("second-pdf")
    path = spool_dir / "cids-20260101-000000.jsonl"
    path.write_text("".join(
        json.dumps({"ts": now, "cid": cid, "peer": "12D3aaa"}) + "\n"
        for cid in (first, second)
    ))
    assert spool.run_once() == 1
    conn = work.connect()
    assert [r[0] for r in conn.execute("SELECT cid FROM cids")] == [first]
    conn.execute("UPDATE cids SET status = 'indexed' WHERE cid = ?", (first,))
    conn.commit()
    assert spool.run_once() == 1
    cids = {r[0] for r in conn.execute("SELECT cid FROM cids")}
    assert cids == {first, second}


def test_spool_queues_dag_pb(tmp_path, monkeypatch):
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    monkeypatch.setattr(spool.config, "SPOOL_DIR", str(spool_dir))
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    work._local.conn = None
    store._local.conn = None
    now = int(time.time())
    raw = cid_for("keep-raw")
    unixfs = cid_pb_for("paper")
    path = spool_dir / "cids-20260101-000000.jsonl"
    path.write_text("".join(
        json.dumps({"ts": now, "cid": cid, "peer": "12D3aaa"}) + "\n"
        for cid in (unixfs, raw)
    ))
    assert spool.run_once() == 2
    rows = {r[0]: r[1] for r in work.connect().execute("SELECT cid, codec FROM cids")}
    assert rows[raw] == "raw"
    assert rows[unixfs] == "dag-pb"


def test_spool_skips_catalogued_cid(tmp_path, monkeypatch):
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    monkeypatch.setattr(spool.config, "SPOOL_DIR", str(spool_dir))
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    work._local.conn = None
    store._local.conn = None
    known = cid_for("already")
    fresh = cid_for("fresh")
    store.ingest_message(store.connect(), {
        "kind": "classify", "cid": known, "publisher": "peer-a",
        "field": "biology", "text_sha256": "x", "v": 1,
    }, received_at=1)
    store.ingest_message(store.connect(), {
        "kind": "classify", "cid": known, "publisher": "peer-b",
        "field": "physics", "text_sha256": "y", "v": 1,
    }, received_at=2)
    now = int(time.time())
    path = spool_dir / "cids-20260101-000000.jsonl"
    path.write_text("".join(
        json.dumps({"ts": now, "cid": cid, "peer": "12D3aaa"}) + "\n"
        for cid in (known, fresh)
    ))
    assert spool.run_once() == 1
    rows = [r[0] for r in work.connect().execute("SELECT cid FROM cids")]
    assert rows == [fresh]


def test_spool_skips_remembered_binary(tmp_path, monkeypatch):
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    monkeypatch.setattr(spool.config, "SPOOL_DIR", str(spool_dir))
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    work._local.conn = None
    store._local.conn = None
    cid = cid_for("png")
    work.remember_binary(cid, "image/png")
    now = int(time.time())
    path = spool_dir / "cids-20260101-000000.jsonl"
    path.write_text(json.dumps({
        "ts": now, "cid": cid, "peer": "12D3aaa",
    }) + "\n")
    assert spool.run_once() == 0
    assert work.connect().execute("SELECT COUNT(*) FROM cids").fetchone()[0] == 0


def test_spool_old_want_stays_live(tmp_path, monkeypatch):
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    monkeypatch.setattr(spool.config, "SPOOL_DIR", str(spool_dir))
    monkeypatch.setattr(work.config, "WORK_DB", str(tmp_path / "work.sqlite"))
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    monkeypatch.setitem(work.config.FETCH, "max_age_seconds", 60)
    monkeypatch.setitem(work.config.FETCH, "min_age_seconds", 0)
    monkeypatch.setitem(work.config.FETCH, "prefer_min_peer_count", 1)
    work._local.conn = None
    store._local.conn = None
    cid = cid_for("old-want")
    now = time.time()
    path = spool_dir / "cids-20260101-000000.jsonl"
    path.write_text(json.dumps({
        "ts": now - 3600, "cid": cid, "peer": "12D3aaa",
    }) + "\n")
    assert spool.run_once() == 1
    row = work.connect().execute(
        "SELECT first_seen, last_seen FROM cids WHERE cid = ?", (cid,)
    ).fetchone()
    assert row["first_seen"] < now - 3000
    assert row["last_seen"] >= now - 5
    assert work.prune(work.connect()) == 0
    taken = work.take_batch(work.connect(), limit=5)
    assert [r["cid"] for r in taken] == [cid]
