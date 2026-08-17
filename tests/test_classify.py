"""Classifier reply parsing and HTTP backoff."""
import json
import time

import requests

from observer import classify


GROQ = {
    "id": "groq", "name": "Groq",
    "base_url": "http://127.0.0.1:9/v1",
    "api_key": "x", "model": "openai/gpt-oss-20b", "needs_key": True,
}


def _one(monkeypatch, *rows):
    classify.reset()
    monkeypatch.setattr(classify.llm, "enabled", lambda: list(rows or [GROQ]))


def test_parse_json_from_reasoning_field():
    msg = {
        "content": "",
        "reasoning": '{"in_scope": false, "field": "other", "topic": "", '
                     '"keywords": [], "license": null}',
    }
    out = classify._parse_json_reply(msg)
    assert out["in_scope"] is False


def test_parse_json_from_content_parts():
    msg = {
        "content": [
            {"type": "reasoning", "text": "thinking"},
            {"type": "text", "text": '{"in_scope": true, "field": "biology", '
                                     '"topic": "x", "keywords": [], "license": null}'},
        ],
    }
    out = classify._parse_json_reply(msg)
    assert out["field"] == "biology"


def test_parse_json_from_parsed_field():
    msg = {"content": None, "parsed": {"in_scope": False, "field": "other"}}
    assert classify._parse_json_reply(msg)["in_scope"] is False


def test_available_false_during_pause(monkeypatch):
    _one(monkeypatch)
    host = classify._ensure_host("groq")
    host.pause(30, "classifier rate-limited (HTTP 429)")
    assert classify.available() is False
    host.pause_until = time.monotonic() - 1
    called = []

    class _Resp:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        classify._session, "get",
        lambda *a, **k: called.append(1) or _Resp(),
    )
    assert classify.available() is True
    assert called == [1]


def test_available_caches_ok_probe(monkeypatch):
    _one(monkeypatch)
    called = []

    class _Resp:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        classify._session, "get",
        lambda *a, **k: called.append(1) or _Resp(),
    )
    assert classify.available() is True
    assert classify.available() is True
    assert called == [1]


def test_classify_401_pauses(monkeypatch):
    _one(monkeypatch)

    class _Resp:
        status_code = 401
        headers = {}

        def raise_for_status(self):
            err = requests.HTTPError("401")
            err.response = self
            raise err

    monkeypatch.setattr(classify._session, "post", lambda *a, **k: _Resp())
    assert classify.classify("hello", "text/plain") is None
    assert classify.available() is False


def test_classify_402_pauses(monkeypatch):
    _one(monkeypatch)
    posts = []

    class _Resp:
        status_code = 402
        headers = {}

        def json(self):
            return {"error": {"message": "Payment required"}}

        def raise_for_status(self):
            err = requests.HTTPError("402")
            err.response = self
            raise err

    monkeypatch.setattr(
        classify._session, "post",
        lambda *a, **k: posts.append(1) or _Resp(),
    )
    assert classify.classify("hello", "text/plain") is None
    assert classify.available() is False
    host = classify._ensure_host("groq")
    assert host.is_paused()
    assert classify.classify("hello", "text/plain") is None
    assert posts == [1]


def test_paused_workers_do_not_post(monkeypatch):
    _one(monkeypatch)
    classify._ensure_host("groq").pause(30, "classifier rate-limited (HTTP 429)")
    posts = []
    monkeypatch.setattr(
        classify._session, "post",
        lambda *a, **k: posts.append(1) or (_ for _ in ()).throw(RuntimeError("hit")),
    )
    assert classify.classify("hello", "text/plain") is None
    assert posts == []


def test_429_backoff_survives_success(monkeypatch):
    _one(monkeypatch)
    host = classify._ensure_host("groq")
    host.chat_gap = 0

    class _Busy:
        status_code = 429
        headers = {}

        def raise_for_status(self):
            err = requests.HTTPError("429")
            err.response = self
            raise err

    monkeypatch.setattr(classify._session, "post", lambda *a, **k: _Busy())
    assert classify.classify("a", "text/plain") is None
    assert host.backoff == 40
    assert host.chat_gap == 8
    host.pause_until = 0
    host.last_chat = 0
    assert classify.classify("a", "text/plain") is None
    assert host.backoff == 80
    assert host.chat_gap == 12

    payload = {
        "in_scope": False, "field": "other", "topic": "",
        "keywords": [], "license": None,
    }

    class _Ok:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{
                    "message": {"content": json.dumps(payload)},
                    "finish_reason": "stop",
                }],
            }

    host.pause_until = 0
    host.last_chat = 0
    monkeypatch.setattr(classify._session, "post", lambda *a, **k: _Ok())
    out = classify.classify("a", "text/plain")
    assert out is not None
    assert out["provider"] == "groq"
    assert host.backoff == 80
    assert host.chat_gap >= 8


def test_429_uses_second_provider(monkeypatch):
    groq = dict(GROQ)
    ac = {
        "id": "academiccloud", "name": "Academic Cloud",
        "base_url": "http://127.0.0.1:8/v1",
        "api_key": "y", "model": "qwen3.6-35b-a3b", "needs_key": True,
    }
    _one(monkeypatch, groq, ac)
    urls = []
    payload = {
        "in_scope": False, "field": "other", "topic": "",
        "keywords": [], "license": None,
    }

    class _Busy:
        status_code = 429
        headers = {}

        def raise_for_status(self):
            err = requests.HTTPError("429")
            err.response = self
            raise err

    class _Ok:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{
                    "message": {"content": json.dumps(payload)},
                    "finish_reason": "stop",
                }],
            }

    def post(url, **k):
        urls.append(url)
        if ":9/" in url:
            return _Busy()
        return _Ok()

    monkeypatch.setattr(classify._session, "post", post)
    out = classify.classify("a", "text/plain")
    assert out["provider"] == "academiccloud"
    assert any(":9/" in u for u in urls)
    assert any(":8/" in u for u in urls)


def test_unusable_reply_uses_second_provider(monkeypatch):
    groq = dict(GROQ)
    ac = {
        "id": "academiccloud", "name": "Academic Cloud",
        "base_url": "http://127.0.0.1:8/v1",
        "api_key": "y", "model": "qwen3.6-35b-a3b", "needs_key": True,
    }
    _one(monkeypatch, groq, ac)
    payload = {
        "in_scope": True, "field": "biology", "topic": "x",
        "keywords": [], "license": None,
    }

    class _Empty:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "no json here"},
                                 "finish_reason": "stop"}]}

    class _Ok:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{
                    "message": {"content": json.dumps(payload)},
                    "finish_reason": "stop",
                }],
            }

    def post(url, **k):
        if ":9/" in url:
            return _Empty()
        return _Ok()

    monkeypatch.setattr(classify._session, "post", post)
    out = classify.classify("a", "text/plain")
    assert out["provider"] == "academiccloud"
    assert out["field"] == "biology"
