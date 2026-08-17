"""Invite-list bootstrap peers. Persisted in config.toml, dialed by clubd."""
from __future__ import annotations

from . import clubd_client, config


def _loopback(addr):
    return any(token in addr for token in (
        "/ip4/127.0.0.1/",
        "/ip4/0.0.0.0/",
        "/ip6/::1/",
        "/ip6/0:0:0:0:0:0:0:1/",
    ))


def _docker_bridge(addr):
    return any(token in addr for token in (
        "/ip4/172.17.",
        "/ip4/172.18.",
        "/ip4/172.19.",
    ))


def shareable_addrs(addrs):
    """Non-loopback listen addrs, LAN TCP first. These are the ones to send a friend."""
    out = [a for a in addrs if a and not _loopback(a)]
    out.sort(key=lambda a: (
        1 if _docker_bridge(a) else 0,
        0 if "/tcp/" in a else 1,
        a,
    ))
    return out


def status():
    ident = clubd_client.identity()
    addrs = list(ident.get("addrs") or [])
    connected = clubd_client.connected_peers()
    return {
        "clubd": clubd_client.available(),
        "peer_id": ident.get("peer_id") or clubd_client.peer_id() or "",
        "club": ident.get("club") or config.CLUB_ID,
        "addrs": addrs,
        "share": shareable_addrs(addrs),
        "connected": connected,
        "bootstrap": config.read_bootstrap_peers(),
    }


def save(peers):
    saved = config.write_bootstrap_peers(peers)
    applied = clubd_client.set_bootstrap(saved)
    out = status()
    out["bootstrap"] = saved
    if applied is None:
        out["applied"] = False
        return out
    if applied.get("connected") is not None:
        out["connected"] = list(applied.get("connected") or [])
    out["dial"] = list(applied.get("dial") or [])
    out["applied"] = True
    return out
