"""Voted labels: majority in front, minority drops, reuse does not vote."""
from observer import labels, store
from tests.cids import cid_for

CID = cid_for("x")


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    store._local.conn = None
    return store.connect()


def _classify(conn, cid, publisher, field, topic, keywords, kind="llm",
              received_at=1, text="aaa"):
    store.ingest_message(conn, {
        "kind": "classify", "cid": cid, "publisher": publisher,
        "field": field, "topic": topic, "keywords": keywords,
        "text_sha256": text + publisher, "v": 1,
        "classifier": {"kind": kind, "prompt_ver": "1"},
    }, received_at=received_at)


def test_single_classify_keeps_its_labels(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _classify(conn, CID, "p1", "biology", "CRISPR", "gene, mice")
    out = labels.consensus(conn, CID)
    assert out["field"] == "biology"
    assert out["topic"] == "CRISPR"
    assert out["keywords"] == "gene, mice"
    assert out["label_voters"] == 1


def test_majority_field_wins(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _classify(conn, CID, "p1", "biology", "CRISPR", "gene", received_at=1)
    _classify(conn, CID, "p2", "biology", "crispr", "gene, editing",
              received_at=2, text="bbb")
    _classify(conn, CID, "p3", "physics", "unrelated", "fake",
              received_at=3, text="ccc")
    out = labels.consensus(conn, CID)
    assert out["field"] == "biology"
    assert out["field_votes"]["biology"] == 2
    assert out["field_votes"]["physics"] == 1
    assert out["topic"].lower() == "crispr"
    assert "gene" in out["keywords"]
    assert "fake" not in out["keywords"]
    assert out["label_voters"] == 3


def test_reuse_does_not_vote_when_llm_exists(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _classify(conn, CID, "p1", "biology", "CRISPR", "gene",
              kind="llm", received_at=1)
    _classify(conn, CID, "p2", "physics", "copied", "copied",
              kind="reuse", received_at=2, text="bbb")
    out = labels.consensus(conn, CID)
    assert out["field"] == "biology"
    assert out["label_voters"] == 1
    assert "physics" not in out["field_votes"]


def test_tie_keeps_first_seen_field(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    _classify(conn, CID, "p1", "biology", "a", "x", received_at=1)
    _classify(conn, CID, "p2", "physics", "b", "y", received_at=2, text="bbb")
    out = labels.consensus(conn, CID)
    assert out["field"] == "biology"
    assert out["label_voters"] == 2
