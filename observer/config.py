"""Load config.toml. Copy config.toml.example to config.toml to get started."""
import os
import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get(
    "OBSERVER_CLUB_CONFIG", os.path.join(ROOT, "config.toml")
)

_CLUB_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
_LEGACY_DB = "data/club.sqlite"
_LEGACY_INBOX = "data/inbox"


def validate_club_id(value):
    value = str(value or "").strip().lower()
    if not _CLUB_ID_RE.fullmatch(value):
        raise ValueError(
            "club.id must be 1-32 chars of [a-z0-9-], "
            "not starting or ending with a hyphen"
        )
    return value


def _load():
    path = CONFIG_PATH
    if not os.path.exists(path):
        example = os.path.join(ROOT, "config.toml.example")
        if os.path.exists(example):
            path = example
    with open(path, "rb") as f:
        return tomllib.load(f)


_raw = _load()


def _abspath(p):
    if not p:
        return p
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


CLUB = _raw["club"]
WEB = _raw.get("web", {"host": "127.0.0.1", "port": 8002})
SNIFFER = _raw.get("sniffer", {})
FETCH = _raw.get("fetch", {})
LLM = _raw.get("llm", {})
STORAGE = _raw.get("storage", {})
CONTROL = _raw.get("control", {})

CLUB_ID = validate_club_id(
    os.environ.get("OBSERVER_CLUB_ID") or CLUB.get("id") or "academic"
)
_CLUB_SECTION_ID = re.compile(
    r"(?m)^(\s*id\s*=\s*)(['\"])([^'\"]*)\2"
)


def env_locks_club():
    return bool(os.environ.get("OBSERVER_CLUB_ID"))


def config_write_path():
    if os.path.isfile(CONFIG_PATH):
        return CONFIG_PATH
    return os.path.join(ROOT, "config.toml")


def read_club_id(path=None):
    """Club id saved in config.toml (not the running process)."""
    path = path or config_write_path()
    if not os.path.isfile(path):
        return CLUB_ID
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return validate_club_id((raw.get("club") or {}).get("id") or "academic")


def write_club_id(club_id, path=None):
    """Set ``[club] id`` in config.toml, keeping comments and other keys."""
    club_id = validate_club_id(club_id)
    path = path or config_write_path()
    if not os.path.isfile(path):
        raise ValueError("no config.toml to update")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    parts = re.split(r"(?=^\[)", text, flags=re.M)
    found = False
    out = []
    for part in parts:
        if re.match(r"^\[club\]\s*$", part.split("\n", 1)[0].rstrip("\r")):
            replaced, n = _CLUB_SECTION_ID.subn(
                r"\1\2%s\2" % club_id, part, count=1,
            )
            if n:
                part = replaced
            else:
                part = re.sub(
                    r"^(\[club\][ \t]*\n)",
                    r'\1id = "%s"\n' % club_id,
                    part,
                    count=1,
                )
            found = True
        out.append(part)
    if not found:
        raise ValueError("config.toml has no [club] section")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(out))
    return club_id


def _club_path(key, legacy, namespaced):
    raw = CLUB.get(key)
    if not raw or raw == legacy:
        return _abspath(namespaced)
    return _abspath(raw)


DB_PATH = _club_path("db_path", _LEGACY_DB, "data/%s/club.sqlite" % CLUB_ID)
INBOX_DIR = _club_path("inbox_dir", _LEGACY_INBOX, "data/%s/inbox" % CLUB_ID)
IDENTITY_PATH = _abspath(CLUB.get("identity_path", "data/identity.key"))
WORK_DB = _abspath(STORAGE.get("work_db", "data/work.sqlite"))
SPOOL_DIR = _abspath(SNIFFER.get("spool_dir", "data/spool"))
SNIFFER_BIN = os.path.join(ROOT, "build", "sniffer")
SNIFFER_LOG = os.path.join(ROOT, "data", "sniffer.log")
SNIFFER_HOME = os.path.join(ROOT, "data", "sniffer")

CLAIM_TTL = int(CLUB.get("claim_ttl_seconds") or 900)
API_HOST = CLUB.get("api_host", "127.0.0.1")
API_PORT = int(CLUB.get("api_port", 8003))
WEB_HOST = WEB.get("host", "127.0.0.1")
WEB_PORT = int(WEB.get("port", 8002))
def _inbox_has_jsonl(path):
    try:
        return any(name.endswith(".jsonl") for name in os.listdir(path))
    except OSError:
        return False


def migrate_legacy_paths(root=None, club_id=None, db_path=None, inbox_dir=None):
    """Move pre-namespace academic catalog into data/academic/."""
    root = root or ROOT
    club_id = club_id or CLUB_ID
    db_path = db_path or DB_PATH
    inbox_dir = inbox_dir or INBOX_DIR
    if club_id != "academic":
        return False
    moved = False
    old_db = os.path.join(root, "data", "club.sqlite")
    if (
        os.path.isfile(old_db)
        and os.path.abspath(old_db) != os.path.abspath(db_path)
        and not os.path.isfile(db_path)
    ):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        os.rename(old_db, db_path)
        for suffix in ("-wal", "-shm"):
            src, dst = old_db + suffix, db_path + suffix
            if os.path.exists(src):
                os.rename(src, dst)
        moved = True
    old_inbox = os.path.join(root, "data", "inbox")
    if (
        os.path.isdir(old_inbox)
        and os.path.abspath(old_inbox) != os.path.abspath(inbox_dir)
    ):
        os.makedirs(inbox_dir, exist_ok=True)
        if not _inbox_has_jsonl(inbox_dir):
            for name in os.listdir(old_inbox):
                src = os.path.join(old_inbox, name)
                dst = os.path.join(inbox_dir, name)
                if not os.path.exists(dst):
                    os.rename(src, dst)
                    moved = True
            try:
                os.rmdir(old_inbox)
            except OSError:
                pass
    return moved


CONTROL_INTERVAL = int(CONTROL.get("check_interval_seconds", 30))
