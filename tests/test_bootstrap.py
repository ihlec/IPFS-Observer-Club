"""Bootstrap invite list: validation, persist, admin API."""
import pytest
from fastapi import HTTPException

from observer import auth, bootstrap, config, web
from observer.web import BootstrapBody
from tests.test_auth import make_request


PEER = "12D3KooWMRcoucT8Mp2nSYC89y9hKkBWpVXRkUu6oyDhdUowEZnQ"
ADDR = "/ip4/203.0.113.8/tcp/4713/p2p/" + PEER
DNS = "/dns4/observer.example/tcp/4713/p2p/" + PEER


@pytest.fixture
def auth_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_DIR", str(tmp_path))
    auth._failures.clear()
    yield tmp_path
    auth._failures.clear()


def test_parse_bootstrap_addr():
    assert config.parse_bootstrap_addr("  " + ADDR + "  ") == ADDR
    assert config.parse_bootstrap_addr(DNS) == DNS
    with pytest.raises(ValueError):
        config.parse_bootstrap_addr("/ip4/203.0.113.8/tcp/4713")
    with pytest.raises(ValueError):
        config.parse_bootstrap_addr("not-a-multiaddr")
    with pytest.raises(ValueError):
        config.parse_bootstrap_addr("http://203.0.113.8:4713")


def test_normalize_skips_invalid_unless_strict():
    assert config.normalize_bootstrap_peers([ADDR, ADDR, "nope", DNS]) == [ADDR, DNS]
    with pytest.raises(ValueError):
        config.normalize_bootstrap_peers(["nope"], strict=True)
    assert config.normalize_bootstrap_peers([]) == []


def test_shareable_addrs_drop_loopback():
    addrs = [
        "/ip4/127.0.0.1/tcp/4713/p2p/" + PEER,
        "/ip4/192.168.2.119/udp/4713/quic-v1/p2p/" + PEER,
        "/ip4/192.168.2.119/tcp/4713/p2p/" + PEER,
    ]
    share = bootstrap.shareable_addrs(addrs)
    assert share[0].startswith("/ip4/192.168.2.119/tcp/")
    assert all("/127.0.0.1/" not in a for a in share)
    assert len(share) == 2
    mixed = [
        "/ip4/172.17.0.1/tcp/4713/p2p/" + PEER,
        "/ip4/192.168.2.119/tcp/4713/p2p/" + PEER,
    ]
    assert bootstrap.shareable_addrs(mixed)[0].startswith("/ip4/192.168.2.119/")


def test_bootstrap_get_requires_admin(auth_dir):
    auth.set_password("abcdefgh")
    with pytest.raises(HTTPException) as ei:
        web.api_bootstrap_get(make_request(host="8.8.8.8"))
    assert ei.value.status_code == 401


def test_bootstrap_set_persists_and_dials(auth_dir, tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        '[club]\nid = "academic"\nbootstrap_peers = []\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "config_write_path", lambda: str(path))
    monkeypatch.setitem(config.CLUB, "bootstrap_peers", [])
    applied = {}

    def fake_set(peers):
        applied["peers"] = list(peers)
        return {
            "peers": peers,
            "connected": ["other"],
            "dial": [{"addr": ADDR, "peer": "other"}],
        }

    monkeypatch.setattr(bootstrap.clubd_client, "set_bootstrap", fake_set)
    monkeypatch.setattr(bootstrap.clubd_client, "identity", lambda: {
        "peer_id": PEER,
        "club": "academic",
        "addrs": ["/ip4/192.168.2.119/tcp/4713/p2p/" + PEER],
    })
    monkeypatch.setattr(bootstrap.clubd_client, "connected_peers", lambda: ["other"])
    monkeypatch.setattr(bootstrap.clubd_client, "available", lambda: True)
    monkeypatch.setattr(bootstrap.clubd_client, "peer_id", lambda: PEER)

    auth.set_password("abcdefgh")
    out = web.api_bootstrap_set(
        BootstrapBody(peers=[ADDR, ADDR]),
        make_request(cookie=auth.issue_session()),
    )
    assert out["bootstrap"] == [ADDR]
    assert out["applied"] is True
    assert applied["peers"] == [ADDR]
    assert config.read_bootstrap_peers(path=str(path)) == [ADDR]


def test_bootstrap_set_rejects_garbage(auth_dir, tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        '[club]\nid = "academic"\nbootstrap_peers = []\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "config_write_path", lambda: str(path))
    auth.set_password("abcdefgh")
    with pytest.raises(HTTPException) as ei:
        web.api_bootstrap_set(
            BootstrapBody(peers=["not-a-multiaddr"]),
            make_request(cookie=auth.issue_session()),
        )
    assert ei.value.status_code == 400
    assert config.read_bootstrap_peers(path=str(path)) == []
