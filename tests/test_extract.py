"""MIME sniff keeps CSS and bundled JS off the classifier."""
from observer import extract, unixfs


def test_sniff_css_keyframes():
    data = b"@keyframes van-rotate{0%{transform:rotate(0)}to{opacity:0}}"
    assert extract.sniff_mime(data) == "text/css"
    assert extract.processable("text/css") is False
    assert extract.sticky_skip_mime("text/css") is True


def test_sniff_webpack_javascript():
    data = b'__webpack_modules__[85117]=function(n){r[e]=t;return t}'
    assert extract.sniff_mime(data) == "application/javascript"
    assert extract.processable("application/javascript") is False


def test_sniff_filename_css_extension():
    assert extract.sniff_mime(b"not really css", filename="theme.css") == "text/css"


def test_plain_prose_is_not_processable():
    data = (b"Abstract. We report CRISPR genome editing in mice. "
            b"Smith et al. discuss off-target effects. References. ")
    assert extract.sniff_mime(data) == "text/plain"
    assert extract.processable("text/plain") is False


def test_xhtml_sniffs_as_html():
    data = b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><p>Hi</p></html>'
    assert extract.sniff_mime(data) == "text/html"
    assert extract.sniff_mime(b"not html", "application/xhtml+xml") == "text/html"
    text, mime, _, _ = extract.extract_document(data, "application/xhtml+xml")
    assert mime == "text/html"
    assert "Hi" in text


def test_pick_filename_prefers_extension():
    assert unixfs.pick_filename(["0", "paper.pdf", "1"]) == "paper.pdf"
    assert unixfs.pick_filename(["12", "34"]) is None
    assert unixfs.pick_filename(["bundle.js"]) == "bundle.js"
