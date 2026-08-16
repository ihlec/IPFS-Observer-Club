"""Classification via an OpenAI-compatible chat API."""
from __future__ import annotations

import json
import logging
import threading
import time

import requests

from . import club, config, llm
from .fields import normalize_field

log = logging.getLogger("classify")

_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=32)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)
_BACKOFF_START = 20.0
_CHAT_GAP_START = 4.0
_hosts = {}
_hosts_lock = threading.Lock()


class _Host:
    def __init__(self, pid):
        self.id = pid
        self.slot = threading.Semaphore(1)
        self.lock = threading.Lock()
        self.pause_until = 0.0
        self.backoff = _BACKOFF_START
        self.chat_gap = _CHAT_GAP_START
        self.last_chat = 0.0
        self.last_429 = 0.0
        self.avail_ts = None
        self.avail_ok = False
        self.model = None

    def is_paused(self):
        return bool(self.pause_until and time.monotonic() < self.pause_until)

    def pause(self, seconds, reason):
        seconds = max(20.0, min(float(seconds), 300.0))
        until = time.monotonic() + seconds
        with self.lock:
            extend = until > self.pause_until
            if extend:
                self.pause_until = until
            self.avail_ok = False
            self.avail_ts = time.monotonic()
        if extend:
            log.warning("%s on %s; pausing for %.0fs", reason, self.id, seconds)

    def note_success(self):
        if self.chat_gap > _CHAT_GAP_START:
            self.chat_gap = max(_CHAT_GAP_START, self.chat_gap - 0.5)

    def on_rate_limit(self, resp):
        now = time.monotonic()
        if self.last_429 and now - self.last_429 > 180:
            self.backoff = _BACKOFF_START
        wait = max(_retry_after(resp) or 0.0, self.backoff)
        self.pause(wait, "classifier rate-limited (HTTP 429)")
        self.backoff = min(self.backoff * 2.0, 300.0)
        self.chat_gap = min(max(self.chat_gap, _CHAT_GAP_START) + 4.0, 16.0)
        self.last_429 = now

    def pace(self):
        now = time.monotonic()
        gap = float(self.chat_gap or 0)
        if self.last_chat > 0 and gap > 0:
            wait = gap - (now - self.last_chat)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        self.last_chat = now


def reset():
    global _hosts
    with _hosts_lock:
        _hosts = {}


def _ensure_host(pid):
    with _hosts_lock:
        host = _hosts.get(pid)
        if host is None:
            host = _Host(pid)
            _hosts[pid] = host
        return host


def _schema():
    profile = club.current()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "cid_classification",
                "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "in_scope": {
                        "type": "boolean",
                        "description": profile.in_scope_description,
                    },
                    "field": {
                        "type": "string",
                        "description": "Broad field slug from: "
                                       + ", ".join(profile.fields),
                    },
                    "topic": {
                        "type": "string",
                        "description": "Specific topic in a few words",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "5-10 search keywords",
                    },
                    "license": {
                        "type": ["string", "null"],
                        "description": "An explicitly stated license only; "
                                       "null when absent",
                    },
                },
                "required": ["in_scope", "field", "topic", "keywords", "license"],
                "additionalProperties": False,
            },
        },
    }


def _headers(prov=None):
    key = (prov or llm.active()).get("api_key") or ""
    if not key:
        return {}
    return {"Authorization": "Bearer " + key}


def _timeout():
    return int(config.LLM.get("timeout_seconds", 120))


def prompt_ver():
    return club.current().prompt_ver


def _model_id(prov, host):
    if host.model:
        return host.model
    configured = prov.get("model") or ""
    if configured:
        host.model = configured
        return configured
    resp = _session.get(
        prov["base_url"] + "/models", headers=_headers(prov), timeout=10,
    )
    resp.raise_for_status()
    models = resp.json().get("data", [])
    if not models:
        raise RuntimeError("no model listed at %s" % prov["base_url"])
    host.model = models[0]["id"]
    log.info("using %s model %s", prov["id"], host.model)
    return host.model


def _retry_after(resp):
    if resp is None:
        return None
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _host_up(prov, host):
    now = time.monotonic()
    with host.lock:
        if host.pause_until:
            if now < host.pause_until:
                return False
            host.pause_until = 0.0
            host.avail_ts = None
        ttl = 15.0 if host.avail_ok else 1.0
        if host.avail_ts is not None and now - host.avail_ts < ttl:
            return host.avail_ok
        try:
            _session.get(
                prov["base_url"] + "/models", headers=_headers(prov), timeout=5,
            ).raise_for_status()
            host.avail_ok = True
        except requests.RequestException:
            host.avail_ok = False
        host.avail_ts = now
        return host.avail_ok


def available():
    ok = False
    for prov in llm.enabled():
        host = _ensure_host(prov["id"])
        if _host_up(prov, host):
            ok = True
    return ok


def backends_status():
    """Compact per-host state for the indexer summary line."""
    parts = []
    now = time.monotonic()
    for prov in llm.enabled():
        host = _ensure_host(prov["id"])
        if host.is_paused():
            left = max(0, int(host.pause_until - now))
            parts.append("%s=paused:%ds" % (prov["id"], left))
        elif host.avail_ok:
            parts.append("%s=ok" % prov["id"])
        else:
            parts.append("%s=down" % prov["id"])
    return " ".join(parts) or "llm=off"


def _choose(exclude):
    ready = []
    for prov in llm.enabled():
        if prov["id"] in exclude:
            continue
        host = _ensure_host(prov["id"])
        if host.is_paused():
            continue
        ready.append((prov, host))
    if not ready:
        return None
    ready.sort(key=lambda item: item[1].last_chat)
    return ready[0]


def _flatten_text(value):
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out = []
        parsed = value.get("parsed")
        if isinstance(parsed, dict):
            out.append(json.dumps(parsed))
        for key in ("text", "content", "reasoning", "arguments"):
            if key in value:
                out.extend(_flatten_text(value.get(key)))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_flatten_text(item))
        return out
    return []


def _parse_json_reply(message):
    parsed = message.get("parsed")
    if isinstance(parsed, dict) and parsed:
        return parsed
    for call in message.get("tool_calls") or []:
        args = (call.get("function") or {}).get("arguments")
        if isinstance(args, dict):
            return args
        if isinstance(args, str):
            try:
                return json.loads(args)
            except ValueError:
                pass
    candidates = []
    for key in ("content", "reasoning_content", "reasoning"):
        candidates.extend(_flatten_text(message.get(key)))
    for cand in candidates:
        try:
            return json.loads(cand)
        except ValueError:
            pass
    for cand in candidates:
        start = cand.find("{")
        while start != -1:
            depth = 0
            for i in range(start, len(cand)):
                if cand[i] == "{":
                    depth += 1
                elif cand[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cand[start:i + 1])
                        except ValueError:
                            break
            start = cand.find("{", start + 1)
    return None


def _reply_hint(message):
    content = message.get("content")
    if isinstance(content, str):
        shape = "str:%d" % len(content)
    elif isinstance(content, list):
        shape = "list:%d" % len(content)
    elif content is None:
        shape = "null"
    else:
        shape = type(content).__name__
    keys = ",".join(sorted(k for k in message if not str(k).startswith("_")))
    finish = message.get("_finish") or ""
    return "keys=%s content=%s%s" % (
        keys, shape, (" finish=%s" % finish) if finish else "",
    )


def _request(prov, host, text, mime, filename, codec, max_chars):
    user = (
        "Datatype: %s\nCodec: %s\nFilename: %s\n\nContent sample:\n%s"
        % (mime or "unknown", codec or "unknown", (filename or "unknown")[:120],
           text[:max_chars])
    )
    messages = [
        {"role": "system", "content": club.current().system_prompt()},
        {"role": "user", "content": user},
    ]
    attempts = (
        {"response_format": _schema(), "reasoning_effort": "none"},
        {"response_format": _schema()},
        {},
    )
    last = None
    for extra in attempts:
        payload = {
            "model": _model_id(prov, host),
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 2500,
            "stream": False,
        }
        payload.update(extra)
        if host.is_paused():
            raise _Paused()
        host.pace()
        resp = _session.post(
            prov["base_url"] + "/chat/completions",
            json=payload,
            headers=_headers(prov),
            timeout=_timeout(),
        )
        if resp.status_code == 400 and extra:
            last = resp
            continue
        resp.raise_for_status()
        body = resp.json()
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        message["_finish"] = choice.get("finish_reason")
        return message
    if last is not None:
        last.raise_for_status()
    raise requests.HTTPError("classification failed")


def _in_scope(out):
    if "in_scope" in out:
        return bool(out.get("in_scope"))
    return bool(out.get("academic_document"))


class _Paused(Exception):
    """Classifier is cooling down; do not hit the chat API."""


def _parse_meta(message):
    out = _parse_json_reply(message)
    if out is None:
        log.warning("no JSON object found in model reply (%s)", _reply_hint(message))
        return None
    keywords = out.get("keywords") or []
    if isinstance(keywords, list):
        keywords = ", ".join(str(k) for k in keywords)
    return {
        "in_scope": _in_scope(out),
        "field": normalize_field(out.get("field")),
        "topic": str(out.get("topic", ""))[:200],
        "keywords": str(keywords)[:500],
        "license": str(out["license"])[:200] if out.get("license") else None,
    }


def classify(text, mime, filename=None, codec=None):
    """Return derived metadata, or None if the chat API failed."""
    if not llm.enabled():
        return None
    max_chars = int(config.LLM.get("max_text_chars", 3000))
    tried = set()
    while True:
        choice = _choose(tried)
        if not choice:
            return None
        prov, host = choice
        tried.add(prov["id"])
        with host.slot:
            if host.is_paused():
                continue
            for chars in (max_chars, max_chars // 2, max_chars // 4):
                try:
                    message = _request(prov, host, text, mime, filename, codec, chars)
                except _Paused:
                    break
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else None
                    if status == 400 and chars > 300:
                        continue
                    if status == 401:
                        host.pause(60, "classifier rejected the API key (HTTP 401)")
                    elif status == 429:
                        host.on_rate_limit(e.response)
                        log.info("%s rate-limited; trying next backend", prov["id"])
                    else:
                        log.warning("classification failed on %s: HTTP %s",
                                    prov["id"], status)
                    break
                except requests.RequestException as e:
                    log.warning("classification request failed on %s: %s",
                                prov["id"], e)
                    break
                else:
                    meta = _parse_meta(message)
                    if meta:
                        host.note_success()
                        meta["model"] = host.model or prov.get("model") or ""
                        meta["provider"] = prov["id"]
                        return meta
                    log.warning("unusable reply from %s; trying next backend",
                                prov["id"])
                    break
    return None
