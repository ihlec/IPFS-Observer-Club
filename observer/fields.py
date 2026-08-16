"""Field vocabulary for the joined club (see clubs/<id>/fields.txt)."""
from . import club


def normalize_field(value):
    """Return a vocabulary slug, or ``other`` if unrecognised."""
    return club.current().normalize_field(value)
