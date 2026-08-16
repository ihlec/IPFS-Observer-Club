"""Local node alias. Gossiped so the ranking can show a name next to a peer id."""
from __future__ import annotations

from . import clubd_client, store

MAX_LEN = 32


def normalize(value):
    name = " ".join(str(value or "").split())
    if not name:
        return ""
    if len(name) > MAX_LEN:
        raise ValueError("alias must be %d characters or fewer" % MAX_LEN)
    if any(ord(c) < 32 for c in name):
        raise ValueError("alias has control characters")
    return name


def current():
    return store.local_alias()


def set_and_publish(value):
    name = normalize(value)
    store.set_local_alias(name, clubd_client.peer_id())
    published = clubd_client.publish_alias(name)
    return {
        "alias": name,
        "published": published,
        "peer_id": clubd_client.peer_id() or "",
    }


def announce():
    name = store.local_alias()
    if not name:
        return False
    return clubd_client.publish_alias(name)
