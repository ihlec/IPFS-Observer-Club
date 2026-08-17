"""HTTP client for clubd (sign + gossip)."""
from __future__ import annotations

import logging
import time

import requests

from . import cidutil, config, protocol

log = logging.getLogger("clubd")

_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=32)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)
_peer_id_cache = None


def api_base():
    return "http://%s:%s" % (config.API_HOST, config.API_PORT)


def peer_id():
    global _peer_id_cache
    if _peer_id_cache:
        return _peer_id_cache
    try:
        r = _session.get(api_base() + "/id", timeout=2)
        r.raise_for_status()
        _peer_id_cache = r.json().get("peer_id")
        return _peer_id_cache
    except requests.RequestException:
        return None


def identity():
    """clubd /id JSON, or {} if clubd is down."""
    global _peer_id_cache
    try:
        r = _session.get(api_base() + "/id", timeout=2)
        r.raise_for_status()
        data = r.json()
        if data.get("peer_id"):
            _peer_id_cache = data["peer_id"]
        return data
    except (requests.RequestException, ValueError):
        return {}


def connected_peers():
    try:
        r = _session.get(api_base() + "/v1/peers", timeout=2)
        r.raise_for_status()
        return list(r.json().get("peers") or [])
    except (requests.RequestException, ValueError, AttributeError):
        return []


def set_bootstrap(peers):
    """Replace clubd's invite list and dial. None if clubd is down."""
    try:
        r = _session.post(
            api_base() + "/v1/bootstrap",
            json={"peers": list(peers or [])},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("bootstrap apply failed: %s", e)
        return None


def available():
    try:
        _session.get(api_base() + "/health", timeout=1).raise_for_status()
        return True
    except requests.RequestException:
        return False


def publish(obj: dict) -> bool:
    """Hand an unsigned payload to clubd. Returns False if clubd is down."""
    body = dict(obj)
    body.setdefault("v", 1)
    body["club"] = config.CLUB_ID
    body.pop("sig", None)
    kind = body.get("kind")
    if kind != "alias" and not cidutil.valid(body.get("cid") or ""):
        log.warning("publish refused: invalid cid")
        return False
    try:
        r = _session.post(api_base() + "/v1/publish", json=body, timeout=10)
        r.raise_for_status()
        try:
            signed = r.json()
        except ValueError:
            return True
        return signed if isinstance(signed, dict) else True
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        detail = ""
        if e.response is not None:
            detail = (e.response.text or "").strip().splitlines()[:1]
            detail = detail[0][:160] if detail else ""
        if status == 429 and "claim" in detail.lower():
            log.debug("claim rejected: %s", detail)
            return False
        log.warning("publish failed: %s%s", e, (" (%s)" % detail) if detail else "")
        return False
    except requests.RequestException as e:
        log.warning("publish failed: %s", e)
        return False


def publish_claim(cid, ttl=None):
    ttl = int(ttl or config.CLAIM_TTL)
    return publish({
        "kind": "claim",
        "cid": cid,
        "until": int(time.time()) + ttl,
    })


def publish_skip(cid, mime_type=None, reason="unprocessable"):
    return publish({
        "kind": "skip",
        "cid": cid,
        "mime_type": mime_type,
        "reason": reason,
    })


def publish_classify(cid, **fields):
    payload = {"kind": "classify", "cid": cid}
    payload.update(fields)
    return publish(payload)


def publish_alias(alias):
    return publish({
        "kind": "alias",
        "alias": alias or "",
    })


def publish_report(cid, reason):
    if reason not in protocol.REPORT_REASONS:
        log.warning("publish refused: bad report reason")
        return False
    signed = publish({
        "kind": "report",
        "cid": cid,
        "reason": reason,
    })
    if not signed:
        return False
    if isinstance(signed, dict) and signed.get("sig"):
        from . import store
        store.ingest_message(store.connect(), signed)
    return True
