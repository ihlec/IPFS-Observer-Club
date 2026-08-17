"""Cheap academic-origin prior. Loaded via clubs/academic/prior.py."""
from __future__ import annotations

import re

_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_ARXIV = re.compile(r"\barxiv[:\s]+\d{4}\.\d{4,5}", re.I)
_PMID = re.compile(r"\b(?:pmid|pubmed)[:\s]?\d{5,8}\b", re.I)
_ISSN = re.compile(r"\bissn[:\s]?\d{4}-?\d{3}[\dXx]\b", re.I)
_ORCID = re.compile(
    r"\borcid(?:\.org/|[:\s]+)\d{4}-\d{4}-\d{4}-\d{3}[\dXx]\b", re.I
)
_ABSTRACT = re.compile(r"\babstract\b", re.I)
_REFERENCES = re.compile(r"\b(?:references|bibliography)\b", re.I)
_ETAL = re.compile(r"\bet\s+al\.?\b", re.I)
_INTRO = re.compile(r"\b(?:introduction|corresponding author)\b", re.I)
_KEYWORDS_HEAD = re.compile(r"\bkeywords?\s*:", re.I)
_JOURNAL = re.compile(
    r"\b(?:journal of|proceedings of the|ieee transactions|"
    r"acm transactions|nature |science |plos one|arxiv\.org|doi\.org)\b",
    re.I,
)
_REPO = re.compile(
    r"\b(?:zenodo|osf\.io|ssrn|jstor|figshare|dryad|datacite|"
    r"handle\.net|hal\.science|biorxiv|medrxiv|philpapers|"
    r"eric\.ed\.gov|worldcat|crossref|pubmed\.ncbi|pmc\.ncbi)\b",
    re.I,
)
_INST = re.compile(
    r"(?:\.edu\b|\.ac\.(?:uk|jp|kr|za|in)\b|"
    r"\b(?:university|universit[aä]t|faculty of|department of|"
    r"school of|institute of|research group|graduate school|"
    r"max planck|cnrs|cern|nih\.gov)\b)",
    re.I,
)
_TEACH = re.compile(
    r"\b(?:lecture notes|course syllabus|problem set|"
    r"seminar series|tutorial notes)\b",
    re.I,
)
_THESIS = re.compile(
    r"\b(?:dissertation|phd thesis|master'?s thesis|"
    r"habilitationsschrift)\b",
    re.I,
)
_DATA = re.compile(
    r"\b(?:data availability|supplementary (?:data|material)|"
    r"codebook|dataset doi)\b",
    re.I,
)
_NAV = re.compile(
    r"\b(?:click here|sign in|log in|add to cart|privacy policy|"
    r"cookie policy|subscribe now)\b",
    re.I,
)
_SOFTWARE = re.compile(
    r"\b(?:npm install|pip install|cargo add|hello world|"
    r"getting started|api reference|changelog)\b",
    re.I,
)

LIKELY = "likely"
UNLIKELY = "unlikely"
UNCERTAIN = "uncertain"


def score(text, mime=None, filename=None):
    """Integer prior from origin and first-page markers. Higher is more academic."""
    text = text or ""
    n = 0
    if _DOI.search(text):
        n += 3
    if _ARXIV.search(text):
        n += 3
    if _ORCID.search(text):
        n += 3
    if _REPO.search(text):
        n += 3
    if _INST.search(text):
        n += 3
    if _PMID.search(text):
        n += 2
    if _THESIS.search(text):
        n += 2
    if _TEACH.search(text):
        n += 2
    if _ISSN.search(text):
        n += 1
    if _ABSTRACT.search(text):
        n += 2
    if _REFERENCES.search(text):
        n += 1
    if _ETAL.search(text):
        n += 1
    if _JOURNAL.search(text):
        n += 1
    if _INTRO.search(text):
        n += 1
    if _KEYWORDS_HEAD.search(text):
        n += 1
    if _DATA.search(text):
        n += 1
    name = (filename or "").lower()
    if name.endswith(".pdf") or (mime or "") == "application/pdf":
        n += 1
    return n


def prior(text, mime=None, filename=None):
    """Return likely, unlikely, or uncertain.

    Likely and uncertain still go to the LLM for field/topic. Unlikely is
    skipped locally and gossiped as out_of_scope so peers do not fetch it.
    Origin (repository, university, ORCID) counts as much as a paper shape.
    PDFs are documents: a short extract is common (first page, scan) and
    does not by itself mean out of scope.
    """
    text = text or ""
    n = score(text, mime=mime, filename=filename)
    mime = mime or ""
    pdf = mime == "application/pdf" or (filename or "").lower().endswith(".pdf")
    if n >= 3 or (pdf and n >= 2):
        return LIKELY
    if pdf:
        return UNCERTAIN
    if n < 2:
        if len(_NAV.findall(text)) >= 2:
            return UNLIKELY
        if _SOFTWARE.search(text):
            return UNLIKELY
        if len(text) < 400:
            return UNLIKELY
    return UNCERTAIN
