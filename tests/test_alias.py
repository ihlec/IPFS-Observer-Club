"""Node alias normalize and publish."""
import pytest

from observer import alias


def test_normalize_collapses_and_rejects():
    assert alias.normalize("  berlin   lab  ") == "berlin lab"
    assert alias.normalize("") == ""
    assert alias.normalize(None) == ""
    with pytest.raises(ValueError):
        alias.normalize("x" * 33)
    with pytest.raises(ValueError):
        alias.normalize("bad\x01name")


def test_set_and_publish_keeps_local_if_clubd_down(tmp_path, monkeypatch):
    from observer import store
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    store._local.conn = None
    monkeypatch.setattr(alias.clubd_client, "peer_id", lambda: "12D3me")
    monkeypatch.setattr(alias.clubd_client, "publish_alias", lambda name: False)
    out = alias.set_and_publish("  my-node  ")
    assert out["alias"] == "my-node"
    assert out["published"] is False
    assert out["peer_id"] == "12D3me"
    assert store.local_alias() == "my-node"
