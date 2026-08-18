"""Text extraction and license sniffing. Fingerprints must stay stable.

``text_sha256`` is SHA-256 of whitespace-normalized UTF-8 text, truncated to
``llm.max_text_chars`` (default 3000).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from html.parser import HTMLParser

from . import config

log = logging.getLogger("extract")
logging.getLogger("pypdf").setLevel(logging.ERROR)

MAX_CHARS = int(config.LLM.get("max_text_chars", 3000))

_CC_RE = re.compile(
    r"\b(CC\s*(?:BY[-\s]NC[-\s](?:SA|ND)|BY[-\s](?:SA|ND)|"
    r"BY[-\s]NC|BY)(?:[-\s]?(?:[1-4]\.0|[1-4]))?)\b",
    re.I,
)
_CC_URL_RE = re.compile(
    r"https?://(?:www\.)?creativecommons\.org/licenses/"
    r"(by(?:-nc)?(?:-nd|-sa)?)/([1-4](?:\.0)?)/?",
    re.I,
)
_SPDX_RE = re.compile(
    r"\b(Apache[-\s]?2\.0|MIT|BSD[-\s]?(?:2|3)[-\s]Clause|"
    r"GPL[-\s]?[vV]?(?:2|3)(?:\.0)?(?:[-\s]or[-\s]later)?|"
    r"LGPL[-\s]?[vV]?(?:2\.1|3)(?:[-\s]or[-\s]later)?|"
    r"AGPL[-\s]?[vV]?3(?:\.0)?(?:[-\s]or[-\s]later)?|"
    r"MPL[-\s]?2\.0|EPL[-\s]?2\.0|Unlicense|CC0(?:[-\s]?1\.0)?)\b",
    re.I,
)
_ALL_RIGHTS_RE = re.compile(r"\ball\s+rights\s+reserved\b", re.I)


def _named_creative_commons(value):
    match = re.search(r"\bcreative\s+commons\b(.{0,180})", value, re.I | re.S)
    if not match:
        return None
    terms = re.sub(r"[-_/]+", " ", match.group(1).lower())
    if "attribution" not in terms:
        return None
    if "noncommercial" in terms or "non commercial" in terms:
        if "noderivatives" in terms or "no derivatives" in terms:
            return "CC-BY-NC-ND"
        if "sharealike" in terms or "share alike" in terms:
            return "CC-BY-NC-SA"
        return "CC-BY-NC"
    if "noderivatives" in terms or "no derivatives" in terms:
        return "CC-BY-ND"
    if "sharealike" in terms or "share alike" in terms:
        return "CC-BY-SA"
    return "CC-BY"


def normalize_license(value):
    if not value:
        return None
    value = " ".join(str(value).split())
    match = _CC_URL_RE.search(value)
    if match:
        return "CC-" + match.group(1).upper() + "-" + match.group(2)
    named_cc = _named_creative_commons(value)
    if named_cc:
        return named_cc
    match = _CC_RE.search(value)
    if match:
        return re.sub(r"[\s-]+", "-", match.group(1).upper())
    match = _SPDX_RE.search(value)
    if match:
        raw = re.sub(r"\s+", "-", match.group(1)).upper()
        aliases = {
            "APACHE-2.0": "Apache-2.0", "MIT": "MIT",
            "BSD-2-CLAUSE": "BSD-2-Clause", "BSD-3-CLAUSE": "BSD-3-Clause",
            "MPL-2.0": "MPL-2.0", "EPL-2.0": "EPL-2.0",
            "UNLICENSE": "Unlicense", "CC0-1.0": "CC0-1.0", "CC0": "CC0-1.0",
        }
        return aliases.get(raw, raw.replace("-V", "-"))
    if _ALL_RIGHTS_RE.search(value):
        return "All rights reserved"
    return None


class _HTMLLicenseParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "license" in attrs.get("rel", "").lower().split():
            self.values.append(attrs.get("href", ""))
        elif tag == "meta" and attrs.get("name", "").lower() in (
            "license", "dc.rights", "dc.rightslicense", "dcterms.rights",
        ):
            self.values.append(attrs.get("content", ""))


def _html_license(data):
    parser = _HTMLLicenseParser()
    html = data.decode("utf-8", errors="replace")
    try:
        parser.feed(html)
    except Exception:
        pass
    for value in parser.values:
        license_name = normalize_license(value)
        if license_name:
            return license_name, "html_metadata"
    license_name = normalize_license(html)
    if license_name:
        return license_name, "html_text"
    return None, None


def _pdf_license(reader):
    values = []
    try:
        values.extend(str(v) for v in (reader.metadata or {}).values() if v)
    except Exception:
        pass
    try:
        xmp = reader.xmp_metadata
        if xmp:
            values.extend(str(getattr(xmp, attr, "")) for attr in (
                "dc_rights", "dc_description", "pdf_keywords",
            ))
    except Exception:
        pass
    for value in values:
        license_name = normalize_license(value)
        if license_name:
            return license_name, "pdf_metadata"
    return None, None

_MAGIC = [
    (b"%PDF", "application/pdf"),
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"RIFF", "application/riff"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"7z\xbc\xaf", "application/x-7z-compressed"),
    (b"Rar!", "application/vnd.rar"),
    (b"\x00\x00\x00\x18ftyp", "video/mp4"),
    (b"\x00\x00\x00\x1cftyp", "video/mp4"),
    (b"\x00\x00\x00 ftyp", "video/mp4"),
    (b"OggS", "audio/ogg"),
    (b"ID3", "audio/mpeg"),
    (b"fLaC", "audio/flac"),
    (b"\x1aE\xdf\xa3", "video/webm"),
    (b"CAR\x01", "application/vnd.ipld.car"),
]

_PROCESSABLE_EXACT = {
    "text/html",
    "application/pdf",
}

_SOURCE_MIMES = frozenset((
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
))
_SOURCE_EXT = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".cjs": "application/javascript",
    ".ts": "application/javascript",
    ".jsx": "application/javascript",
    ".tsx": "application/javascript",
    ".less": "text/css",
    ".scss": "text/css",
    ".map": "application/json",
    ".wasm": "application/wasm",
}
_WEBPACK_MARKERS = (
    b"webpackJsonp", b"webpackChunk", b"__webpack", b"__WEBPACK",
)


def _filename_ext_mime(filename):
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext, mime in _SOURCE_EXT.items():
        if name.endswith(ext):
            return mime
    return None


def _sniff_source(data):
    """CSS / bundled JS that would otherwise sniff as text/plain."""
    sample = data[:4096]
    if any(marker in sample for marker in _WEBPACK_MARKERS):
        return "application/javascript"
    if re.search(
        br"@keyframes\b|@media\s+[^{]+\{|@font-face\b|@import\s+(?:url\(|['\"])",
        sample, re.I,
    ):
        return "text/css"
    if sample.count(b"{") >= 8 and sample.count(b";") >= 15:
        n = max(len(sample), 1)
        if sample.count(b" ") / n <= 0.12:
            return "application/javascript"
    return None


def sniff_mime(data, header_mime=None, filename=None):
    if header_mime == "application/xhtml+xml":
        header_mime = "text/html"
    named = _filename_ext_mime(filename)
    if named:
        return named
    if data.startswith(b"RIFF") and len(data) >= 12:
        sub = data[8:12]
        if sub == b"WEBP":
            return "image/webp"
        if sub == b"AVI ":
            return "video/x-msvideo"
        if sub == b"WAVE":
            return "audio/wav"
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    head = data[:2048].lstrip()
    if head.startswith((b"<!DOCTYPE", b"<!doctype", b"<html", b"<HTML",
                        b"<?xml")) and b"<html" in data[:2048].lower():
        return "text/html"
    if head.startswith((b"{", b"[")):
        try:
            json.loads(data.decode("utf-8", errors="strict"))
            return "application/json"
        except (ValueError, UnicodeDecodeError):
            pass
    source = _sniff_source(data)
    if source:
        return source
    if header_mime and header_mime not in ("application/octet-stream", ""):
        return header_mime
    sample = data[:4096]
    try:
        sample.decode("utf-8")
        if sample and sum(1 for b in sample if b < 9) < len(sample) * 0.01:
            return "text/plain"
    except UnicodeDecodeError:
        pass
    return header_mime or "application/octet-stream"


MIN_PDF_CHARS = 80


def binary_mime(mime):
    """True for image/video/audio. These should not occupy the live queue."""
    return bool(mime) and mime.startswith(("image/", "video/", "audio/"))


def source_mime(mime):
    """Frontend assets that must not reach the classifier."""
    return mime in _SOURCE_MIMES


def sticky_skip_mime(mime):
    """True when this CID should stay off the live queue (images, CSS, JS)."""
    return binary_mime(mime) or source_mime(mime)


def processable(mime):
    if not mime:
        return False
    if sticky_skip_mime(mime):
        return False
    return mime in _PROCESSABLE_EXACT


def needs_whole_file(mime):
    """True when a prefix of the file cannot be extracted at all.

    PDF stores its cross-reference table at the end, so a truncated sample
    yields no text and must not be classified from what did arrive.
    """
    return mime == "application/pdf"


def usable_text(text, mime=None):
    """False for empty extracts and image-only / scanned PDFs."""
    text = text or ""
    if not text:
        return False
    if (mime or "") == "application/pdf" and len(text) < MIN_PDF_CHARS:
        return False
    return True


_SCRIPT_RE = re.compile(rb"<script.*?</script>|<style.*?</style>", re.S | re.I)
_TAG_RE = re.compile(rb"<[^>]+>")


def _load_trafilatura():
    """Resolve the article extractor once.

    A failed import is not cached by Python, so importing inside the per-page
    path retried the whole lxml chain for every document and silently fell
    back to tag stripping. Resolving once makes the degradation visible.
    """
    try:
        import trafilatura
        return trafilatura.extract
    except Exception as e:
        log.warning(
            "trafilatura unavailable (%s); HTML text falls back to tag "
            "stripping, which lowers classify quality. Fix with "
            "'pip install -r requirements.txt'", e,
        )
        return None


_trafilatura_extract = _load_trafilatura()


def _html_text(data):
    html = data.decode("utf-8", errors="replace")
    if _trafilatura_extract is not None:
        try:
            text = _trafilatura_extract(html)
            if text:
                return text
        except Exception:
            pass
    txt = _TAG_RE.sub(b" ", _SCRIPT_RE.sub(b" ", data))
    return " ".join(txt.decode("utf-8", errors="replace").split())


def _pdf_text(data):
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        parts = []
        total = 0
        for page in reader.pages[:10]:
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total >= MAX_CHARS:
                break
        return "\n".join(parts), _pdf_license(reader)
    except Exception as e:
        log.debug("pdf extraction failed: %s", e)
        return "", (None, None)


def extract_document(data, mime=None, filename=None):
    """Return (text, mime, license, license_source). Bytes stay in memory."""
    mime = sniff_mime(data, mime, filename=filename)
    if mime == "application/xhtml+xml":
        mime = "text/html"
    if not processable(mime) or not data:
        return "", mime, None, None
    license_name, license_source = None, None
    if mime == "text/html":
        text = _html_text(data)
        license_name, license_source = _html_license(data)
    elif mime == "application/pdf":
        text, (license_name, license_source) = _pdf_text(data)
    else:
        text = data.decode("utf-8", errors="replace")
    text = " ".join(text.split())
    if not license_name:
        license_name = normalize_license(text)
        license_source = "text" if license_name else None
    return text[:MAX_CHARS], mime, license_name, license_source


def fingerprint(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
