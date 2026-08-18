"""CID-verified UnixFS sampling for the indexer.

Every raw block is hashed against its CID before use. Assembled file bytes
use a content-aware PDF budget and a capped child walk so ``text_sha256``
matches what the local classifier hashed.

The first few bytes of a file decide MIME. Non-PDF/HTML/plain payloads
stop after that prefix — no further child fetches. A local Kubo HTTP API,
when configured, is tried before public gateways.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from concurrent import futures

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
# Kubo's default chunk size. Only used to size the PDF child budget.
UNIXFS_CHUNK = 262144
# A PDF is only readable whole, so its child cap follows from the byte budget
# rather than from the prefix cap used for text. The slack covers smaller
# chunkers and the intermediate nodes of a deep DAG.
MAX_PDF_CHILD_BLOCKS = max(
    MAX_CHILD_BLOCKS, -(-MAX_PDF_BYTES // UNIXFS_CHUNK) + 8,
)
# Parallel child GETs per file. Gateway latency, not bandwidth, is the limit.
CHILD_CONCURRENCY = max(1, int(config.FETCH.get("child_concurrency", 8)))
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
_SNIFF_WINDOW = 4096


class RateLimiter:
    def __init__(self, kbps):
        self.rate = kbps * 1024.0
        # Capacity must clear the largest single block or consume() could
        # never gather enough tokens and would spin forever.
        self.capacity = max(self.rate * 2, MAX_BLOCK, 1)
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


class _Resolved(object):
    """A child link whose bytes are already in hand, kept in link order."""

    __slots__ = ("data",)

    def __init__(self, data):
        self.data = data


def _as_unixfs(block):
    try:
        return unixfs.parse_dag_pb(block)
    except Exception:
        return None


def _child_bytes(block):
    """File bytes contributed by one child block, and any grandchild links.

    A raw leaf is its own bytes. A UnixFS wrapper contributes ``inline_data``
    or its own links -- never the protobuf envelope, which would sniff as
    text/plain and corrupt the sample.
    """
    node = _as_unixfs(block)
    if node is None:
        return block, ()
    if node.is_directory:
        return b"", ()
    if node.unixfs_type in ("file", "raw") and (node.inline_data or node.links):
        return node.inline_data or b"", tuple(node.links)
    return block, ()


def _fetch_children(cids, gateway_offset, child_fetches):
    """Fetch sibling blocks concurrently, results in link order.

    One gateway round trip per block dominates the cost of assembling a
    chunked file. A 3 MiB paper is a dozen blocks and a 40 MiB one is
    hundreds, so fetching them serially took longer than the retry window
    and left every multi-block PDF truncated.
    """
    if child_fetches is not None:
        child_fetches.extend(cids)
    if len(cids) == 1:
        blocks = [get_block(cids[0], gateway_offset)]
    else:
        workers = min(len(cids), CHILD_CONCURRENCY)
        with futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="child",
        ) as pool:
            blocks = list(pool.map(
                lambda cid: get_block(cid, gateway_offset), cids,
            ))
    # A single miss discards the whole file, so give the stragglers one more
    # sweep from a different gateway -- most misses are timeouts, not absent
    # blocks. If a whole multi-block wave failed the gateways are down and a
    # second sweep would only add delay.
    missing = [i for i, block in enumerate(blocks) if block is None]
    if missing and not (len(missing) == len(cids) > 1):
        for index in missing:
            blocks[index] = get_block(cids[index], gateway_offset + 1)
    return blocks


def _sniff(buf, seen_names):
    return extract.sniff_mime(
        bytes(buf[:_SNIFF_WINDOW]), filename=unixfs.pick_filename(seen_names),
    )


def _budget_for(mime, filesize):
    """Return (byte budget, max child blocks) once the MIME is known.

    A PDF is only useful whole: the cross-reference table sits at the end of
    the file, so a prefix yields no text at all. Text formats are sampled,
    so the general prefix budget still applies to them.
    """
    if mime != "application/pdf":
        return MAX_BYTES, MAX_CHILD_BLOCKS
    if filesize and filesize > MAX_PDF_BYTES:
        return 0, 0
    return MAX_PDF_BYTES, MAX_PDF_CHILD_BLOCKS


def _assemble_file(root_node, gateway_offset=0, child_fetches=None, names=None):
    """Gather file bytes up to a content-aware budget.

    Returns ``(data, truncated)``. ``truncated`` means the sample is a prefix
    of the real file, which callers must not treat as a whole document.
    Stops after the first sniffable prefix if the MIME is not processable.
    ``child_fetches`` if a list is appended once per child block GET.
    ``names`` if a list is appended with UnixFS link names as they are walked.
    """
    seen_names = names if names is not None else []
    out = bytearray(root_node.inline_data or b"")
    pending = collections.deque(root_node.links)
    blocks_used = 0
    mime = None

    if len(out) >= _SNIFF_MIN:
        mime = _sniff(out, seen_names)
        if not extract.processable(mime):
            return bytes(out[:MAX_BYTES]), False

    # One block at a time until the MIME is known: an unprocessable file must
    # cost a single request, not a whole parallel wave.
    while mime is None and pending:
        name, cid = pending.popleft()
        if name:
            seen_names.append(name)
        block = get_block(cid, gateway_offset)
        if child_fetches is not None:
            child_fetches.append(cid)
        blocks_used += 1
        if block is None:
            return bytes(out), True
        data, links = _child_bytes(block)
        out += data
        if links:
            pending.extendleft(reversed(links))
        if len(out) >= _SNIFF_MIN:
            mime = _sniff(out, seen_names)

    if mime is None or not extract.processable(mime):
        return bytes(out[:MAX_BYTES]), bool(pending)

    budget, max_blocks = _budget_for(mime, root_node.filesize)
    if len(out) > budget:
        # Oversized PDF: the tail we would need is out of reach, so stop here
        # rather than spending a wave of fetches on bytes we must discard.
        return bytes(out), True

    while pending and len(out) < budget and blocks_used < max_blocks:
        wave = []
        room = min(CHILD_CONCURRENCY, max_blocks - blocks_used)
        while pending and len(wave) < room:
            item = pending.popleft()
            if isinstance(item, _Resolved):
                pending.appendleft(item)
                break
            wave.append(item)
        if not wave:
            break
        for name, _cid in wave:
            if name:
                seen_names.append(name)
        blocks = _fetch_children(
            [cid for _name, cid in wave], gateway_offset, child_fetches,
        )
        blocks_used += len(wave)
        replacement = []
        for block in blocks:
            if block is None:
                return bytes(out[:budget]), True
            data, links = _child_bytes(block)
            if links:
                replacement.extend(links)
            elif data:
                replacement.append(_Resolved(data))
        pending.extendleft(reversed(replacement))
        while pending and isinstance(pending[0], _Resolved):
            out += pending.popleft().data

    data = bytes(out[:budget])
    truncated = bool(pending) or len(out) > budget
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


def _spread(items, n):
    """Evenly sample ``n`` items so a HAMT peek is not stuck on prefix ``00``."""
    items = list(items)
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return items
    step = len(items) / float(n)
    picked = []
    seen = set()
    for i in range(n):
        item = items[int(i * step)]
        if item in seen:
            continue
        seen.add(item)
        picked.append(item)
    return picked


def peek_hamt_pdfs(links, max_blocks=None, max_pdfs=None, gateway_offset=0):
    """Look at HAMT shard blocks for ``*.pdf`` names. No catalog row."""
    max_blocks = int(
        max_blocks if max_blocks is not None
        else config.FETCH.get("max_hamt_blocks", 24)
    )
    max_pdfs = int(
        max_pdfs if max_pdfs is not None
        else config.FETCH.get("max_dir_docs", 16)
    )
    found = list(unixfs.doc_child_links(links, max_n=max_pdfs))
    if len(found) >= max_pdfs or max_blocks <= 0:
        return found
    seen_pdf = {cid for _name, cid in found}
    level = []
    seen_level = set()
    for _name, cid in links or ():
        if cid and cid not in seen_pdf and cid not in seen_level:
            level.append(cid)
            seen_level.add(cid)
    # Spend about a third of the budget per shard level so the walk goes down.
    per_level = max(8, (max_blocks + 2) // 3)
    blocks = 0
    while level and blocks < max_blocks and len(found) < max_pdfs:
        room = max_blocks - blocks
        wave = _spread(level, min(room, per_level, len(level)))
        next_level = []
        seen_next = set()
        for cid in wave:
            if blocks >= max_blocks or len(found) >= max_pdfs:
                break
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
            seen_pdf.update(child for _name, child in more)
            if node.unixfs_type == "hamt-shard" or node.is_directory:
                for _name, child in node.links:
                    if child and child not in seen_pdf and child not in seen_next:
                        next_level.append(child)
                        seen_next.add(child)
        level = next_level
    return found
