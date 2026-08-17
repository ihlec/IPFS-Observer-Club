"""Club profiles, path migration, and wrong-club ingest."""
import os

import pytest

from observer import club, config, store
from tests.cids import cid_for

CID_OURS = cid_for("ours")


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "clubs")


def test_validate_id():
    assert club.validate_id("academic") == "academic"
    assert club.validate_id("field-recordings") == "field-recordings"
    assert club.validate_id("Academic") == "academic"
    with pytest.raises(ValueError):
        club.validate_id("has_underscore")
    with pytest.raises(ValueError):
        club.validate_id("-x")


def test_academic_club_loads():
    profile = club.load("academic")
    assert profile.name == "Academic"
    assert "biology" in profile.fields
    assert profile.normalize_field("Computer Science") == "computer-science"
    assert profile.normalize_field("nope") == "other"
    assert "{fields}" not in profile.system_prompt()
    assert "biology" in profile.system_prompt()
    assert profile.prior_fn is not None
    assert profile.prior("click here to sign in and add to cart",
                         mime="text/html") == club.UNLIKELY


def test_shipped_club_is_academic():
    names = {c["id"]: c["name"] for c in club.available()}
    assert names == {"academic": "Academic"}


def test_custom_club_fields():
    profile = club.load("widgets", clubs_root=FIXTURES)
    assert profile.name == "Widget manuals"
    assert profile.normalize_field("gadget") == "widget"
    assert profile.normalize_field("biology") == "other"
    assert profile.prior("anything") is None


def test_write_club_id_keeps_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "# header\n"
        "[club]\n"
        "# Which club\n"
        'id = "academic"\n'
        "listen_port = 4713\n"
        "\n"
        "[web]\n"
        'id = "keep-me"\n',
        encoding="utf-8",
    )
    assert config.write_club_id("widgets", path=str(path)) == "widgets"
    text = path.read_text(encoding="utf-8")
    assert 'id = "widgets"' in text
    assert "# Which club" in text
    assert 'id = "keep-me"' in text
    assert config.read_club_id(path=str(path)) == "widgets"


def test_write_bootstrap_peers_keeps_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "# header\n"
        "[club]\n"
        'id = "academic"\n'
        "# Optional multiaddrs\n"
        "bootstrap_peers = []\n"
        "listen_port = 4713\n"
        "\n"
        "[web]\n"
        "port = 8002\n",
        encoding="utf-8",
    )
    addr = (
        "/ip4/203.0.113.8/tcp/4713/p2p/"
        "12D3KooWMRcoucT8Mp2nSYC89y9hKkBWpVXRkUu6oyDhdUowEZnQ"
    )
    assert config.write_bootstrap_peers([addr], path=str(path)) == [addr]
    text = path.read_text(encoding="utf-8")
    assert "# Optional multiaddrs" in text
    assert addr in text
    assert "listen_port = 4713" in text
    assert config.read_bootstrap_peers(path=str(path)) == [addr]
    assert config.write_bootstrap_peers([], path=str(path)) == []
    assert "bootstrap_peers = []" in path.read_text(encoding="utf-8")


def test_unknown_club():
    with pytest.raises(ValueError, match="unknown club"):
        club.load("does-not-exist", clubs_root=FIXTURES)


def test_ingest_drops_wrong_club(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    monkeypatch.setattr(store.config, "CLUB_ID", "academic")
    store._local.conn = None
    conn = store.connect()
    assert store.ingest_message(conn, {
        "kind": "skip", "cid": "bafy-other", "publisher": "p",
        "reason": "out_of_scope", "club": "widgets", "v": 1,
    }) is False
    assert store.lookup_cid(conn, "bafy-other") is None
    assert store.ingest_message(conn, {
        "kind": "skip", "cid": CID_OURS, "publisher": "p",
        "reason": "out_of_scope", "club": "academic", "v": 1,
    }) is True
    assert store.lookup_cid(conn, CID_OURS)["kind"] == "skip"


def test_migrate_legacy_academic(tmp_path):
    old_db = tmp_path / "data" / "club.sqlite"
    old_db.parent.mkdir()
    old_db.write_text("db")
    (tmp_path / "data" / "club.sqlite-wal").write_text("wal")
    old_inbox = tmp_path / "data" / "inbox"
    old_inbox.mkdir()
    (old_inbox / "2020-01-01.jsonl").write_text("{}\n")
    new_db = tmp_path / "data" / "academic" / "club.sqlite"
    new_inbox = tmp_path / "data" / "academic" / "inbox"
    assert config.migrate_legacy_paths(
        root=str(tmp_path), club_id="academic",
        db_path=str(new_db), inbox_dir=str(new_inbox),
    )
    assert new_db.is_file()
    assert (tmp_path / "data" / "academic" / "club.sqlite-wal").is_file()
    assert not old_db.exists()
    assert (new_inbox / "2020-01-01.jsonl").is_file()
