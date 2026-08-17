"""CID-verified UnixFS sampling for the indexer.

Every raw block is hashed against its CID before use. Assembled file bytes
use a content-aware PDF budget and a capped child walk so ``text_sha256``
matches what the local classifier hashed.

The first few bytes of a file decide MIME. Non-PDF/HTML/plain payloads
stop after that prefix — no further child fetches. A local Kubo HTTP API,
when configured, is tried before public gateways.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from . import cidutil, config, extract, unixfs

log = logging.getLogger("fetch")

_session = requests.Session()
_session.headers["User-Agent"] = "ipfs-observer-club/0.1"
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=4,
    pool_maxsize=int(config.FETCH.get("concurrency", 8)) + 4,
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

CONNECT_TIMEOUT = int(config.FETCH.get("connect_timeout_seconds", 5))
TIMEOUT = int(config.FETCH.get("request_timeout_seconds")
              or config.FETCH.get("timeout_seconds", 6))
MAX_BYTES = int(config.FETCH.get("max_bytes")
                or config.FETCH.get("max_bytes_per_cid", 2097152))
MAX_PDF_BYTES = int(config.FETCH.get("max_pdf_bytes", 8 * 1048576))
MAX_CHILD_BLOCKS = int(config.FETCH.get("max_child_blocks", 6))
# Single IPLD blocks are small. Refuse oversized gateway bodies rather than
# hashing a truncated prefix (that would never match the CID).
MAX_BLOCK = min(max(MAX_BYTES, 2 * 1048576), 4 * 1048576)
GATEWAYS = list(config.FETCH.get("gateways") or [
    "https://trustless-gateway.link",
    "https://ipfs.io",
])
_DEAD_ERRORS = frozenset(("http 404", "http 410", "http 451"))
_SLOW_ERRORS = frozenset((
    "timeout", "http 408", "http 425", "http 429",
    "http 500", "http 502", "http 503", "http 504",
))
_SNIFF_MIN = 4


class RateLimiter:
    def __init__(self, kbps):
        self.rate = kbps * 1024.0
        self.capacity = max(self.rate * 2, 1)
        self.tokens = self.capacity
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, nbytes):
        if self.rate <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= nbytes:
                    self.tokens -= nbytes
                    return
                needed = (nbytes - self.tokens) / self.rate
            time.sleep(min(needed, 1.0))


RATE_LIMITER = RateLimiter(int(config.FETCH.get("rate_limit_kbps", 1500)))


def _gateway_order(offset=0):
    n = len(GATEWAYS)
    if n <= 1:
        return GATEWAYS
    k = offset % n
    return GATEWAYS[k:] + GATEWAYS[:k]


class Sample(object):
    def __init__(self):
        self.ok = False
        self.data = b""
        self.codec = None
        self.truncated = False
        self.is_directory = False
        self.unixfs_type = None
        self.links = []
        self.error = None


class FetchResult(Sample):
    def __init__(self):
        Sample.__init__(self)
        self.dead = False
        self.slow = False
        self.mime_type = None
        self.size = None
        self.filename = None


def _ipfs_api():
    return (config.FETCH.get("ipfs_api") or "").rstrip("/")


def _kubo_block(cid):
    """Verified raw block from a local Kubo HTTP API, or (None, err)."""
    api = _ipfs_api()
    if not api:
        return None, None
    url = api + "/api/v0/block/get"
    try:
        resp = _session.post(
            url, params={"arg": cid},
            timeout=(CONNECT_TIMEOUT, TIMEOUT),
        )
    except requests.Timeout:
        return None, "timeout"
    except requests.RequestException as e:
        return None, "request: %s" % e.__class__.__name__
    try:
        if resp.status_code in (404, 410, 451):
            return None, "http %d" % resp.status_code
        if resp.status_code != 200:
            return None, "http %d" % resp.status_code
        data = resp.content
        if len(data) > MAX_BLOCK:
            return None, "block too large"
        if not cidutil.verify_block(cid, data):
            log.debug("cid mismatch from kubo for %s", cid[:24])
            return None, "cid mismatch"
        RATE_LIMITER.consume(len(data))
        return data, None
    finally:
        resp.close()


def get_block_ex(cid, gateway_offset=0):
    """Return (verified bytes, None) or (None, error_str)."""
    data, err = _kubo_block(cid)
    if data is not None:
        return data, None
    last = err
    for gw in _gateway_order(gateway_offset):
        url = "%s/ipfs/%s?format=raw" % (gw.rstrip("/"), cid)
        try:
            resp = _session.get(
                url,
                headers={"Accept": "application/vnd.ipld.raw"},
                timeout=(CONNECT_TIMEOUT, TIMEOUT),
            )
        except requests.Timeout:
            last = "timeout"
            continue
        except requests.RequestException as e:
            last = "request: %s" % e.__class__.__name__
            continue
        try:
            if resp.status_code in (404, 410, 451):
                last = "http %d" % resp.status_code
                continue
            if resp.status_code != 200:
                last = "http %d" % resp.status_code
                continue
            data = resp.content
            if len(data) > MAX_BLOCK:
                last = "block too large"
                continue
            if not cidutil.verify_block(cid, data):
                last = "cid mismatch"
                log.debug("cid mismatch from %s for %s", gw, cid[:24])
                continue
            RATE_LIMITER.consume(len(data))
            return data, None
        finally:
            resp.close()
    log.debug("block fetch failed for %s: %s", cid[:24], last)
    return None, (last or "unavailable")


def get_block(cid, gateway_offset=0):
    """Return verified raw block bytes, or None if no gateway matches the CID."""
    data, _err = get_block_ex(cid, gateway_offset)
    return data


def _assemble_file(root_node, gateway_offset=0, child_fetches=None, names=None):
    """Gather file bytes up to a content-aware budget.

    Stops after the first sniffable prefix if the MIME is not processable.
    ``child_fetches`` if a list is appended once per child block GET.
    ``names`` if a list is appended with UnixFS link names as they are walked.
    """
    out = bytearray(root_node.inline_data or b"")
    budget = MAX_BYTES
    max_children = MAX_CHILD_BLOCKS
    sniffed = False
    truncated = False
    pending = list(root_node.links)
    seen_names = names if names is not None else []

    def _apply_budget_from(buf):
        mime = extract.sniff_mime(
            bytes(buf[:4096]), filename=unixfs.pick_filename(seen_names),
        )
        extra = MAX_CHILD_BLOCKS
        extra_budget = MAX_BYTES
        stop = not extract.processable(mime)
        if not stop and mime == "application/pdf":
            total = root_node.filesize
            if total and total <= MAX_PDF_BYTES:
                extra_budget = MAX_PDF_BYTES
                extra = max(len(root_node.links), len(pending))
        return stop, extra, extra_budget

    if len(out) >= _SNIFF_MIN:
        sniffed = True
        stop, max_children, budget = _apply_budget_from(out)
        if stop:
            return bytes(out[:budget]), False

    while pending:
        if max_children <= 0 or len(out) >= budget:
            truncated = True
            break
        if sniffed:
            mime = extract.sniff_mime(bytes(out[:4096])) if len(out) >= _SNIFF_MIN else None
            if mime and not extract.processable(mime):
                break
        _name, child_cid = pending.pop(0)
        if _name:
            seen_names.append(_name)
        block = get_block(child_cid, gateway_offset)
        if child_fetches is not None:
            child_fetches.append(child_cid)
        if block is None:
            truncated = True
            break
        max_children -= 1
        child = None
        try:
            child = unixfs.parse_dag_pb(block)
        except Exception:
            child = None
        if child is not None and child.is_directory:
            continue
        if (
            child is not None
            and child.unixfs_type in ("file", "raw")
            and (child.inline_data or child.links)
        ):
            # Nested UnixFS: file bytes are inline_data / grandchildren,
            # never the protobuf wrapper (which sniffs as text/plain).
            if child.inline_data:
                out += child.inline_data
            if child.links:
                pending = list(child.links) + pending
        else:
            out += block
        if not sniffed and len(out) >= _SNIFF_MIN:
            sniffed = True
            stop, max_children, budget = _apply_budget_from(out)
            if stop:
                break
    data = bytes(out[:budget])
    if root_node.filesize and len(data) < root_node.filesize:
        truncated = True
    return data, truncated


def fetch_cid(cid, codec=None, attempt=0, depth=0):
    """Indexer fetch: verified sample plus dead/slow flags for retries."""
    result = FetchResult()
    codec = codec or cidutil.codec_of(cid)
    result.codec = codec
    block, err = get_block_ex(cid, attempt)
    if block is None:
        result.error = err
        result.dead = err in _DEAD_ERRORS
        result.slow = err in _SLOW_ERRORS
        return result

    if codec == "raw":
        result.data = block
        result.size = len(block)
        result.ok = True
        return result

    if codec not in (None, "unknown", "dag-pb"):
        result.data = block
        result.size = len(block)
        result.ok = True
        return result

    try:
        node = unixfs.parse_dag_pb(block)
    except Exception:
        result.data = block
        result.size = len(block)
        result.ok = True
        return result

    if node.is_directory:
        result.is_directory = True
        result.unixfs_type = node.unixfs_type
        result.mime_type = "inode/directory"
        result.links = list(node.links)
        result.size = 0
        result.ok = True
        return result

    names = [n for n, _ in node.links if n]
    result.data, result.truncated = _assemble_file(node, attempt, names=names)
    result.filename = unixfs.pick_filename(names)
    result.size = len(result.data)
    result.ok = True
    return result


def peek_hamt_pdfs(links, max_blocks=None, max_pdfs=None, gateway_offset=0):
    """Look at a few HAMT shard blocks for ``*.pdf`` names. No catalog row."""
    max_blocks = int(
        max_blocks if max_blocks is not None
        else config.FETCH.get("max_hamt_blocks", 4)
    )
    max_pdfs = int(
        max_pdfs if max_pdfs is not None
        else config.FETCH.get("max_dir_docs", 8)
    )
    found = list(unixfs.doc_child_links(links, max_n=max_pdfs))
    if len(found) >= max_pdfs or max_blocks <= 0:
        return found
    seen_pdf = {cid for _name, cid in found}
    pending = []
    for name, cid in links or ():
        if cid and cid not in seen_pdf:
            pending.append(cid)
    blocks = 0
    while pending and blocks < max_blocks and len(found) < max_pdfs:
        cid = pending.pop(0)
        block = get_block(cid, gateway_offset)
        blocks += 1
        if not block:
            continue
        try:
            node = unixfs.parse_dag_pb(block)
        except Exception:
            continue
        more = unixfs.doc_child_links(node.links, max_n=max_pdfs - len(found))
        found.extend(more)
        seen_pdf.update(cid for _name, cid in more)
        if node.unixfs_type == "hamt-shard" or node.is_directory:
            for _name, child in node.links:
                if child and child not in seen_pdf and child not in pending:
                    pending.append(child)
    return found
