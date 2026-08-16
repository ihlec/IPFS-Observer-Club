"""Load the joined club profile from clubs/<id>/."""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from . import config

LIKELY = "likely"
UNLIKELY = "unlikely"
UNCERTAIN = "uncertain"

# Gossiped membership skip. not_academic is the pre-namespace academic reason.
SCOPE_SKIP_REASON = "out_of_scope"
SCOPE_SKIP_REASONS = frozenset((SCOPE_SKIP_REASON, "not_academic"))
DIRECTORY_SKIP_REASON = "directory"
PERSIST_SKIP_REASONS = SCOPE_SKIP_REASONS | frozenset((DIRECTORY_SKIP_REASON,))

_current = None


def validate_id(value):
    return config.validate_club_id(value)


def is_persist_skip(reason):
    """Skips that stay in the catalog and replicate to peers."""
    return (reason or "") in PERSIST_SKIP_REASONS


_PROMPT = (
    "You classify textual content found on IPFS for a shared club index.\n"
    "%s\n"
    "For in-scope documents, provide a broad field, a specific topic, and "
    "5-10 search keywords.\n"
    "Field must be one of: %s.\n"
    "Be factual rather than guessing. Only report a license when it is "
    "explicitly stated in the supplied sample; otherwise use null. Never "
    "infer it from the publisher, author, topic, or open access status."
)


@dataclass(frozen=True)
class Club:
    id: str
    name: str
    prompt_ver: str
    fields: tuple
    aliases: dict
    in_scope_description: str
    prior_fn: Optional[Callable]
    directory: str

    def normalize_field(self, value):
        if not value:
            return "other"
        key = str(value).strip().lower().replace("_", "-")
        key = " ".join(key.split())
        field_set = frozenset(self.fields)
        if key in field_set:
            return key
        collapsed = key.replace(" ", "-")
        if collapsed in field_set:
            return collapsed
        return self.aliases.get(key) or self.aliases.get(collapsed) or "other"

    def prior(self, text, mime=None, filename=None):
        if self.prior_fn is None:
            return None
        return self.prior_fn(text, mime=mime, filename=filename)

    def system_prompt(self):
        return _PROMPT % (self.in_scope_description, ", ".join(self.fields))


def _read_lines(path):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def _load_prior(club_id, path):
    spec = importlib.util.spec_from_file_location(
        "ipfs_observer_club_%s_prior" % club_id.replace("-", "_"), path,
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "prior", None)
    if fn is None:
        raise ValueError("clubs/%s/prior.py must define prior()" % club_id)
    return fn


def load(club_id, clubs_root=None):
    """Load clubs/<id>/. Raises ValueError if the profile is missing or invalid."""
    club_id = validate_id(club_id)
    root = clubs_root or os.path.join(config.ROOT, "clubs")
    directory = os.path.join(root, club_id)
    meta_path = os.path.join(directory, "club.toml")
    fields_path = os.path.join(directory, "fields.txt")
    if not os.path.isfile(meta_path):
        raise ValueError(
            "unknown club %r — add clubs/%s/ or set club.id to a shipped club"
            % (club_id, club_id)
        )
    with open(meta_path, "rb") as f:
        meta = tomllib.load(f)
    file_id = str(meta.get("id") or club_id).strip().lower()
    if file_id != club_id:
        raise ValueError("clubs/%s/club.toml id %r does not match folder" % (
            club_id, file_id))
    if not os.path.isfile(fields_path):
        raise ValueError("clubs/%s/ needs fields.txt" % club_id)
    fields = tuple(_read_lines(fields_path))
    if not fields:
        raise ValueError("clubs/%s/fields.txt has no slugs" % club_id)
    if "other" not in fields:
        fields = fields + ("other",)
    aliases = {}
    for key, value in (meta.get("aliases") or {}).items():
        aliases[str(key).strip().lower()] = str(value).strip().lower()
    prior_path = os.path.join(directory, "prior.py")
    prior_fn = None
    if os.path.isfile(prior_path):
        prior_fn = _load_prior(club_id, prior_path)
    return Club(
        id=club_id,
        name=str(meta.get("name") or club_id),
        prompt_ver=str(meta.get("prompt_ver") or "1"),
        fields=fields,
        aliases=aliases,
        in_scope_description=str(meta.get("in_scope_description") or (
            "True when the content matches this club's topic."
        )),
        prior_fn=prior_fn,
        directory=directory,
    )


def available(clubs_root=None):
    """Installed clubs as ``{id, name}``, sorted by id."""
    root = clubs_root or os.path.join(config.ROOT, "clubs")
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in sorted(names):
        directory = os.path.join(root, name)
        if not os.path.isdir(directory):
            continue
        try:
            profile = load(name, clubs_root=root)
        except ValueError:
            continue
        out.append({"id": profile.id, "name": profile.name})
    out.sort(key=lambda c: (0 if c["id"] == "academic" else 1, c["id"]))
    return out


def current():
    global _current
    if _current is None:
        _current = load(config.CLUB_ID)
    return _current


def reset():
    """Drop the cached profile (tests)."""
    global _current
    _current = None
