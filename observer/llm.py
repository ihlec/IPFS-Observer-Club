"""Named OpenAI-compatible classify backends. ``active`` is the enabled set."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading

from . import config

_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
_DIR = None
_lock = threading.Lock()
_cache = None

def _local_model():
    return str((config.LLM or {}).get("model") or "").strip()


PRESETS = (
    {
        "id": "lmstudio",
        "name": "Local",
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "",
        "needs_key": False,
    },
    {
        "id": "academiccloud",
        "name": "Academic Cloud",
        "base_url": "https://chat-ai.academiccloud.de/v1",
        "model": "qwen3.6-35b-a3b",
        "needs_key": True,
    },
    {
        "id": "groq",
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-20b",
        "needs_key": True,
    },
    {
        "id": "cerebras",
        "name": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "model": "gpt-oss-120b",
        "needs_key": True,
    },
)


def reset():
    global _cache
    with _lock:
        _cache = None


def data_dir():
    return _DIR or os.path.join(config.ROOT, "data")


def path():
    return os.path.join(data_dir(), "llm.json")


def _write(path, data: bytes):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-llm-")
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _seed_from_config():
    llm = config.LLM or {}
    providers = {}
    for p in PRESETS:
        row = {
            "name": p["name"],
            "base_url": p["base_url"],
            "model": p["model"],
            "api_key": "",
        }
        providers[p["id"]] = row
    url = (llm.get("base_url") or "").rstrip("/")
    model = llm.get("model") or ""
    key = llm.get("api_key") or llm.get("key") or ""
    if url:
        providers["lmstudio"]["base_url"] = url
    if model:
        providers["lmstudio"]["model"] = model
    if key:
        providers["lmstudio"]["api_key"] = key
    return {"active": "lmstudio", "providers": providers}


def _load():
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        p = path()
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    rec = json.load(f)
                if isinstance(rec, dict) and isinstance(rec.get("providers"), dict):
                    _cache = rec
                    if not _active_ids(rec) and rec.get("active") not in (None, "", []):
                        rec["active"] = ["lmstudio"] if "lmstudio" in rec["providers"] else []
                    return _cache
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        _cache = _seed_from_config()
        return _cache


def _save(rec):
    global _cache
    _write(path(), (json.dumps(rec, indent=2) + "\n").encode("utf-8"))
    _cache = rec
    from . import classify
    classify.reset()


def validate_id(value):
    value = str(value or "").strip().lower()
    if not _ID_RE.fullmatch(value):
        raise ValueError("provider id must be 1-32 chars of [a-z0-9-]")
    return value


def _clean_url(url):
    url = str(url or "").strip().rstrip("/")
    if not url:
        raise ValueError("API URL required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("API URL must be http(s)")
    return url


def _active_ids(rec):
    raw = rec.get("active")
    providers = rec.get("providers") or {}
    if raw is None or raw == "":
        ids = []
    elif isinstance(raw, list):
        ids = [str(x).strip() for x in raw if str(x).strip()]
    else:
        ids = [str(raw).strip()] if str(raw).strip() else []
    out = []
    for pid in ids:
        if pid in providers and pid not in out:
            out.append(pid)
    return out


def _provider_row(pid, rec=None):
    rec = rec or _load()
    row = dict(rec.get("providers") or {}).get(pid) or {}
    preset = next((p for p in PRESETS if p["id"] == pid), None) or {
        "id": pid, "name": pid, "base_url": "", "model": "", "needs_key": True,
    }
    return {
        "id": pid,
        "name": row.get("name") or preset["name"],
        "base_url": (row.get("base_url") or preset.get("base_url") or "").rstrip("/"),
        "model": row.get("model") or preset.get("model") or "",
        "api_key": row.get("api_key") or "",
        "needs_key": bool(preset.get("needs_key")),
    }


def enabled():
    """Providers currently in use. More than one means parallel classify."""
    rec = _load()
    return [_provider_row(pid, rec) for pid in _active_ids(rec)]


def active():
    rows = enabled()
    if rows:
        return rows[0]
    return {
        "id": "",
        "name": "",
        "base_url": "",
        "model": "",
        "api_key": "",
        "needs_key": False,
    }


def public():
    rec = _load()
    rows = enabled()
    ids = [r["id"] for r in rows]
    cur = rows[0] if rows else active()
    out = []
    seen = set()
    for p in PRESETS:
        row = (rec.get("providers") or {}).get(p["id"]) or {}
        key = row.get("api_key") or ""
        out.append({
            "id": p["id"],
            "name": p["name"],
            "base_url": (row.get("base_url") or p["base_url"]).rstrip("/"),
            "model": row.get("model") or p["model"],
            "default_base_url": p["base_url"].rstrip("/"),
            "default_model": p["model"] or (_local_model() if p["id"] == "lmstudio" else ""),
            "has_key": bool(key),
            "needs_key": p["needs_key"],
            "in_use": p["id"] in ids,
        })
        seen.add(p["id"])
    for pid, row in (rec.get("providers") or {}).items():
        if pid in seen:
            continue
        out.append({
            "id": pid,
            "name": row.get("name") or pid,
            "base_url": (row.get("base_url") or "").rstrip("/"),
            "model": row.get("model") or "",
            "has_key": bool(row.get("api_key")),
            "needs_key": True,
            "in_use": pid in ids,
        })
    names = [r["name"] for r in rows]
    models = [r["model"] for r in rows if r.get("model")]
    return {
        "active": cur["id"],
        "active_ids": ids,
        "name": ", ".join(names),
        "model": " · ".join(models),
        "base_url": cur["base_url"],
        "enabled": bool(ids),
        "providers": out,
    }


def set_active(provider_id):
    rec = json.loads(json.dumps(_load()))
    if not str(provider_id or "").strip():
        rec["active"] = []
        _save(rec)
        return public()
    provider_id = validate_id(provider_id)
    if provider_id not in rec["providers"]:
        preset = next((p for p in PRESETS if p["id"] == provider_id), None)
        if not preset:
            raise ValueError("unknown provider")
        rec["providers"][provider_id] = {
            "name": preset["name"],
            "base_url": preset["base_url"],
            "model": preset["model"],
            "api_key": "",
        }
    rec["active"] = [provider_id]
    _save(rec)
    return public()


def disable(provider_id):
    rec = json.loads(json.dumps(_load()))
    if not str(provider_id or "").strip():
        rec["active"] = []
        _save(rec)
        return public()
    provider_id = validate_id(provider_id)
    rec["active"] = [pid for pid in _active_ids(rec) if pid != provider_id]
    _save(rec)
    return public()


def save_provider(provider_id, *, name="", base_url="", model="", api_key=None):
    provider_id = validate_id(provider_id)
    rec = json.loads(json.dumps(_load()))
    preset = next((p for p in PRESETS if p["id"] == provider_id), None)
    row = rec["providers"].get(provider_id) or {
        "name": (preset or {}).get("name") or provider_id,
        "base_url": (preset or {}).get("base_url") or "",
        "model": (preset or {}).get("model") or "",
        "api_key": "",
    }
    if name:
        row["name"] = str(name).strip()[:64]
    if base_url:
        row["base_url"] = _clean_url(base_url)
    if model is not None and model != "":
        row["model"] = str(model).strip()[:128]
    if api_key:
        if len(api_key) > 512:
            raise ValueError("API key too long")
        row["api_key"] = api_key
    rec["providers"][provider_id] = row
    ids = _active_ids(rec)
    if provider_id not in ids:
        ids.append(provider_id)
    rec["active"] = ids
    _save(rec)
    return public()
