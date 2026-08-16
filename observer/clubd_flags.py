"""Emit clubd CLI flags from config.toml. Used by the Makefile."""
from __future__ import annotations

from . import config


def flags():
    peers = config.CLUB.get("bootstrap_peers") or []
    if isinstance(peers, str):
        peers = [p.strip() for p in peers.split(",") if p.strip()]
    else:
        peers = [str(p).strip() for p in peers if str(p).strip()]
    out = [
        "-club", config.CLUB_ID,
        "-port", str(int(config.CLUB.get("listen_port", 4713))),
        "-api", "%s:%s" % (config.API_HOST, config.API_PORT),
        "-identity", config.IDENTITY_PATH,
        "-inbox", config.INBOX_DIR,
        "-snapshot-url", "http://%s:%s/api/snapshot" % (
            config.WEB_HOST, config.WEB_PORT),
        "-rate", str(int(config.CLUB.get("max_msgs_per_peer_per_min", 60))),
        "-inbox-max-bytes", str(int(config.CLUB.get("inbox_max_bytes", 64 * 1024 * 1024))),
        "-inbox-keep-days", str(int(config.CLUB.get("inbox_keep_days", 7))),
    ]
    mdns = config.CLUB.get("mdns", True)
    if isinstance(mdns, str):
        mdns = mdns.strip().lower() not in ("0", "false", "no", "off")
    out.append("-mdns=%s" % ("true" if mdns else "false"))
    if peers:
        out.extend(["-bootstrap", ",".join(peers)])
    return out


if __name__ == "__main__":
    print(" ".join(flags()))
