"""Admin password hashing, sessions, and HTTP gates."""
import json
import os
import stat

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from observer import auth
from tests.cids import cid_for
from observer.web import (
    AdminLoginBody,
    AdminPasswordBody,
    AliasBody,
    ReportBody,
    ReportDecideBody,
    api_admin_login,
    api_admin_password,
    api_admin_password_clear,
    api_admin_status,
    api_alias_set,
    api_report,
    api_report_decide,
    api_reports,
    api_snapshot,
)


@pytest.fixture
def auth_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_DIR", str(tmp_path))
    auth._failures.clear()
    auth._report_hits.clear()
    yield tmp_path
    auth._failures.clear()
    auth._report_hits.clear()


def make_request(host="127.0.0.1", cookie=None):
    headers = []
    if cookie:
        headers.append((b"cookie", ("%s=%s" % (auth.COOKIE, cookie)).encode("ascii")))
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (host, 12345),
        "server": ("127.0.0.1", 8002),
    })


def cookie_from(resp):
    header = resp.headers.get("set-cookie") or ""
    if not header.startswith(auth.COOKIE + "="):
        return None
    return header.split(";", 1)[0].split("=", 1)[1]


def test_hash_roundtrip_not_plaintext(auth_dir):
    auth.set_password("correct horse")
    assert auth.password_is_set()
    assert auth.verify_password("correct horse")
    assert auth.verify_password("wrong password") is False
    raw = (auth_dir / "admin.json").read_text()
    assert "correct horse" not in raw
    rec = json.loads(raw)
    assert rec["kdf"] in ("scrypt", "pbkdf2_sha256")
    assert rec["salt"]
    assert rec["hash"]
    mode = stat.S_IMODE(os.stat(auth.password_path()).st_mode)
    assert mode == 0o600


def test_change_invalidates_session(auth_dir):
    auth.set_password("abcdefgh")
    token = auth.issue_session()
    assert auth.session_ok(make_request(cookie=token))
    auth.set_password("ijklmnop")
    assert auth.session_ok(make_request(cookie=token)) is False
    assert auth.verify_password("ijklmnop")


def test_expired_session(auth_dir, monkeypatch):
    auth.set_password("abcdefgh")
    monkeypatch.setattr(auth, "SESSION_TTL", -1)
    token = auth.issue_session()
    assert auth.session_ok(make_request(cookie=token)) is False


def test_tampered_session(auth_dir):
    auth.set_password("abcdefgh")
    token = auth.issue_session()
    flipped = ("A" if token[0] != "A" else "B") + token[1:]
    assert auth.session_ok(make_request(cookie=flipped)) is False


def test_unlocked_localhost_can_write(auth_dir, monkeypatch):
    from observer import web
    monkeypatch.setattr(web.alias, "set_and_publish", lambda a: {"alias": a})
    req = make_request()
    assert api_admin_status(req) == {"password_set": False, "authenticated": True}
    assert api_alias_set(AliasBody(alias="lab"), req)["alias"] == "lab"


def test_unlocked_remote_cannot_write(auth_dir, monkeypatch):
    from observer import web
    monkeypatch.setattr(web.alias, "set_and_publish", lambda a: {"alias": a})
    req = make_request(host="8.8.8.8")
    with pytest.raises(HTTPException) as ei:
        api_alias_set(AliasBody(alias="lab"), req)
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei:
        api_admin_password(AdminPasswordBody(password="abcdefgh"), req)
    assert ei.value.status_code == 403


def test_set_password_then_login(auth_dir, monkeypatch):
    from observer import web
    monkeypatch.setattr(web.alias, "set_and_publish", lambda a: {"alias": a})
    resp = api_admin_password(AdminPasswordBody(password="abcdefgh"), make_request())
    assert resp.body and b"abcdefgh" not in resp.body
    token = cookie_from(resp)
    assert token
    header = resp.headers.get("set-cookie", "").lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    raw = (auth_dir / "admin.json").read_text()
    assert "abcdefgh" not in raw
    assert api_alias_set(AliasBody(alias="lab"), make_request(cookie=token))["alias"] == "lab"

    with pytest.raises(HTTPException) as ei:
        api_alias_set(AliasBody(alias="lab"), make_request())
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        api_admin_login(AdminLoginBody(password="nope!!!!"), make_request())
    assert ei.value.status_code == 401
    resp = api_admin_login(AdminLoginBody(password="abcdefgh"), make_request())
    token = cookie_from(resp)
    assert api_alias_set(AliasBody(alias="lab"), make_request(cookie=token))["alias"] == "lab"


def test_wrong_current_password(auth_dir):
    api_admin_password(AdminPasswordBody(password="abcdefgh"), make_request())
    with pytest.raises(HTTPException) as ei:
        api_admin_password(
            AdminPasswordBody(password="newnewnew", current="wrong!!!!"),
            make_request(),
        )
    assert ei.value.status_code == 401
    assert auth.verify_password("abcdefgh")


def test_change_requires_current(auth_dir):
    api_admin_password(AdminPasswordBody(password="abcdefgh"), make_request())
    with pytest.raises(HTTPException) as ei:
        api_admin_password(AdminPasswordBody(password="newnewnew"), make_request())
    assert ei.value.status_code == 400


def test_clear_password(auth_dir, monkeypatch):
    from observer import web
    monkeypatch.setattr(web.alias, "set_and_publish", lambda a: {"alias": a})
    api_admin_password(AdminPasswordBody(password="abcdefgh"), make_request())
    token = auth.issue_session()
    resp = api_admin_password_clear(
        AdminPasswordBody(current="abcdefgh"), make_request(cookie=token),
    )
    assert json.loads(resp.body)["password_set"] is False
    assert auth.password_is_set() is False
    assert api_alias_set(AliasBody(alias="lab"), make_request())["alias"] == "lab"


def test_clear_requires_current_on_localhost(auth_dir):
    api_admin_password(AdminPasswordBody(password="abcdefgh"), make_request())
    with pytest.raises(HTTPException) as ei:
        api_admin_password_clear(AdminPasswordBody(), make_request())
    assert ei.value.status_code == 401
    assert auth.password_is_set()


def test_corrupt_hash_localhost_can_reset(auth_dir):
    (auth_dir / "admin.json").write_text("{not json")
    assert auth.is_corrupt()
    resp = api_admin_password(AdminPasswordBody(password="abcdefgh"), make_request())
    assert cookie_from(resp)
    assert auth.verify_password("abcdefgh")


def test_report_requires_admin(auth_dir, monkeypatch):
    from observer import web
    posted = []
    monkeypatch.setattr(
        web.clubd_client, "publish_report",
        lambda cid, reason: posted.append((cid, reason)) or True,
    )
    auth.set_password("abcdefgh")
    cid = cid_for("report")
    token = auth.issue_session()
    out = api_report(ReportBody(cid=cid, reason="wrong"), make_request(cookie=token))
    assert out["ok"] is True
    assert posted == [(cid, "wrong")]


def test_guest_proposes_abusive_only(auth_dir, tmp_path, monkeypatch):
    from observer import store, web
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    store._local.conn = None
    posted = []
    monkeypatch.setattr(
        web.clubd_client, "publish_report",
        lambda cid, reason: posted.append((cid, reason)) or True,
    )
    auth.set_password("abcdefgh")
    cid = cid_for("guest")
    out = api_report(ReportBody(cid=cid, reason="wrong"), make_request(host="8.8.8.8"))
    assert out["pending"] is True
    assert posted == []
    out = api_report(ReportBody(cid=cid, reason="abusive"), make_request(host="8.8.8.8"))
    assert out["pending"] is True
    assert posted == []
    assert store.is_abusive(cid) is False
    listed = api_reports(make_request(cookie=auth.issue_session()))
    reasons = {p["reason"] for p in listed["proposals"]}
    assert reasons == {"wrong", "abusive"}
    decided = api_report_decide(
        ReportDecideBody(cid=cid, action="accept", reason="abusive"),
        make_request(cookie=auth.issue_session()),
    )
    assert decided["action"] == "accept"
    assert posted == [(cid, "abusive")]
    assert [p["reason"] for p in store.list_proposals()] == ["wrong"]


def test_admin_rejects_proposal(auth_dir, tmp_path, monkeypatch):
    from observer import store, web
    monkeypatch.setattr(store.config, "DB_PATH", str(tmp_path / "club.sqlite"))
    store._local.conn = None
    posted = []
    monkeypatch.setattr(
        web.clubd_client, "publish_report",
        lambda cid, reason: posted.append((cid, reason)) or True,
    )
    auth.set_password("abcdefgh")
    cid = cid_for("reject")
    api_report(ReportBody(cid=cid, reason="abusive"), make_request(host="8.8.8.8"))
    api_report_decide(
        ReportDecideBody(cid=cid, action="reject"),
        make_request(cookie=auth.issue_session()),
    )
    assert posted == []
    assert store.list_proposals() == []


def test_snapshot_localhost_only(auth_dir, monkeypatch):
    from observer import web
    monkeypatch.setattr(web.store, "export_snapshot", lambda *a, **k: "")
    monkeypatch.setattr(web.store, "connect", lambda: None)
    assert api_snapshot(make_request()).status_code == 200
    with pytest.raises(HTTPException) as ei:
        api_snapshot(make_request(host="8.8.8.8"))
    assert ei.value.status_code == 403


def test_login_rate_limit(auth_dir):
    auth.set_password("abcdefgh")
    req = make_request()
    for _ in range(auth._FAIL_LIMIT):
        with pytest.raises(HTTPException) as ei:
            api_admin_login(AdminLoginBody(password="wrong!!!!"), req)
        assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        api_admin_login(AdminLoginBody(password="abcdefgh"), req)
    assert ei.value.status_code == 429
