"""Cheap academic-origin prior."""
from observer.academic import LIKELY, UNCERTAIN, UNLIKELY, prior, score


def test_doi_abstract_is_likely():
    text = (
        "Abstract. We describe a method for CRISPR off-target detection. "
        "doi:10.1038/s41586-021-1234-5. References. Smith et al."
    )
    assert prior(text, mime="application/pdf", filename="paper.pdf") == LIKELY
    assert score(text, mime="application/pdf") >= 3


def test_short_nav_html_is_unlikely():
    text = "Home. Click here to sign in. Add to cart. Privacy policy."
    assert prior(text, mime="text/html") == UNLIKELY


def test_software_readme_is_unlikely():
    text = (
        "Getting started. Run pip install cooltool and see the API reference. "
        "Changelog for v2. Hello world example below. "
    ) * 8
    assert prior(text, mime="text/html") == UNLIKELY


def test_long_unmarked_text_is_uncertain():
    text = "x " * 500
    assert prior(text, mime="text/plain") == UNCERTAIN


def test_pdf_with_one_marker_is_likely():
    text = "Smith et al. describe a method for genome editing in mice."
    assert prior(text, mime="application/pdf") == LIKELY


def test_repository_html_is_likely():
    text = "Dataset deposited on zenodo. Title: river sediment counts."
    assert prior(text, mime="text/html") == LIKELY


def test_university_course_html_is_likely():
    text = (
        "Department of Biology, University of Helsinki. "
        "Lecture notes for GEN-101."
    )
    assert prior(text, mime="text/html") == LIKELY


def test_orcid_page_is_likely():
    text = "Author profile. ORCID: 0000-0002-1825-0097. Publications listed below."
    assert prior(text, mime="text/html") == LIKELY
