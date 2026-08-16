"""Web UI: python -m observer.web  ->  http://127.0.0.1:8002"""
import os
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import alias, auth, classify, club, clubd_client, config, llm, search, store, work

app = FastAPI(title="IPFS Observer Club", version="0.1.0")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class AliasBody(BaseModel):
    alias: str = ""


class ClubBody(BaseModel):
    id: str = ""


class AdminPasswordBody(BaseModel):
    password: str = ""
    current: str = ""


class AdminLoginBody(BaseModel):
    password: str = ""


class LlmBody(BaseModel):
    id: str = ""
    name: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    enabled: Optional[bool] = None


class ReportBody(BaseModel):
    cid: str = ""
    reason: str = ""


class ReportDecideBody(BaseModel):
    cid: str = ""
    action: str = ""
    reason: str = ""


class BlacklistBody(BaseModel):
    publisher: str = ""


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/admin")
def admin():
    return FileResponse(os.path.join(STATIC, "admin.html"))


@app.get("/api/search")
def api_search(
    q: str = Query(..., min_length=1),
    field: str = None,
    mime: str = None,
    limit: int = 50,
):
    return {"results": search.search(q, field=field, mime=mime,
                                     limit=min(limit, 200))}


@app.get("/api/browse")
def api_browse(
    field: str = None,
    mime: str = None,
    limit: int = 20,
    offset: int = 0,
):
    results, total = search.browse(field=field, mime=mime,
                                   limit=min(limit, 100), offset=max(offset, 0))
    return {"results": results, "total": total}


@app.get("/api/export")
def api_export(q: str = None, field: str = None, mime: str = None):
    if q:
        rows = search.search(q, field=field, mime=mime, limit=100000)
    else:
        rows, _ = search.browse(field=field, mime=mime, limit=100000, offset=0)
    body = "\n".join(r["cid"] for r in rows)
    return PlainTextResponse(
        body + ("\n" if body else ""),
        headers={"Content-Disposition": 'attachment; filename="cids.txt"'},
    )


@app.get("/api/club")
def api_club():
    profile = club.current()
    return {
        "id": profile.id,
        "name": profile.name,
        "fields": list(profile.fields),
        "mimes": search.mimes(),
        "prompt_ver": profile.prompt_ver,
    }


def _club_choice():
    profile = club.current()
    nxt = config.read_club_id()
    return {
        "current": {"id": profile.id, "name": profile.name},
        "next": nxt,
        "restart_needed": nxt != profile.id,
        "env_locked": config.env_locks_club(),
        "clubs": club.available(),
    }


@app.get("/api/admin/status")
def api_admin_status(request: Request):
    return auth.status(request)


@app.post("/api/admin/login")
def api_admin_login(body: AdminLoginBody, request: Request):
    auth.check_login_rate(request)
    if not auth.password_is_set():
        raise HTTPException(400, "no password configured")
    if not auth.verify_password(body.password):
        auth.record_login_failure(request)
        raise HTTPException(401, "invalid password")
    auth.clear_login_failures(request)
    resp = JSONResponse({"ok": True, "password_set": True, "authenticated": True})
    auth.set_session_cookie(resp, request)
    return resp


@app.post("/api/admin/logout")
def api_admin_logout():
    resp = JSONResponse({"ok": True, "password_set": auth.password_is_set(),
                         "authenticated": False})
    auth.clear_session_cookie(resp)
    return resp


@app.post("/api/admin/password")
def api_admin_password(body: AdminPasswordBody, request: Request):
    locked = auth.password_is_set()
    recover = auth.is_corrupt() and auth.is_localhost(request)
    if locked and not recover:
        if not body.current:
            raise HTTPException(400, "enter your current password")
        if not auth.verify_password(body.current):
            raise HTTPException(401, "invalid password")
    elif not locked and not auth.is_localhost(request):
        raise HTTPException(403, "admin is localhost only")
    try:
        auth.set_password(body.password)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    resp = JSONResponse({"ok": True, "password_set": True, "authenticated": True})
    auth.set_session_cookie(resp, request)
    return resp


@app.post("/api/admin/password/clear")
def api_admin_password_clear(body: AdminPasswordBody, request: Request):
    if auth.password_is_set() and not auth.verify_password(body.current):
        if not (auth.is_corrupt() and auth.is_localhost(request)):
            raise HTTPException(401, "invalid password")
    auth.clear_password()
    resp = JSONResponse({
        "ok": True,
        "password_set": False,
        "authenticated": auth.is_localhost(request),
    })
    auth.clear_session_cookie(resp)
    return resp


@app.get("/api/llm")
def api_llm_get(request: Request):
    auth.require_admin_read(request)
    out = llm.public()
    out["available"] = classify.available()
    return out


@app.post("/api/llm")
def api_llm_set(body: LlmBody, request: Request):
    auth.require_admin(request)
    try:
        if body.enabled is False:
            out = llm.disable(body.id)
        elif body.base_url or body.model or body.api_key or body.name:
            out = llm.save_provider(
                body.id,
                name=body.name,
                base_url=body.base_url,
                model=body.model,
                api_key=body.api_key or None,
            )
        else:
            out = llm.set_active(body.id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    out["available"] = classify.available()
    return out


@app.get("/api/clubs")
def api_clubs(request: Request):
    auth.require_admin_read(request)
    return _club_choice()


@app.post("/api/clubs")
def api_clubs_set(body: ClubBody, request: Request):
    auth.require_admin(request)
    if config.env_locks_club():
        raise HTTPException(409, "OBSERVER_CLUB_ID is set; unset it to use the picker")
    try:
        club_id = config.validate_club_id(body.id)
        club.load(club_id)
        config.write_club_id(club_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return _club_choice()


@app.get("/api/stats")
def api_stats():
    out = search.stats()
    out["clubd"] = clubd_client.available()
    out["peer_id"] = clubd_client.peer_id()
    out["llm"] = classify.available()
    out["work"] = work.stats()
    profile = club.current()
    out["club"] = {"id": profile.id, "name": profile.name}
    rows = llm.enabled()
    out["model"] = " · ".join(r.get("model") or "" for r in rows if r.get("model"))
    out["llm_name"] = ", ".join(r.get("name") or r.get("id") for r in rows)
    return out


@app.get("/api/publishers")
def api_publishers():
    return {"publishers": search.publishers()}


@app.post("/api/report")
def api_report(body: ReportBody, request: Request):
    reason = (body.reason or "").strip().lower()
    if auth.is_admin(request):
        if reason not in ("wrong", "abusive", "clear"):
            raise HTTPException(400, "reason must be wrong, abusive, or clear")
        if not clubd_client.publish_report(body.cid, reason):
            raise HTTPException(503, "clubd did not accept the report")
        if reason in ("abusive", "wrong"):
            store.drop_proposal(body.cid, reason)
        return {"ok": True, "cid": body.cid, "reason": reason, "pending": False}
    if reason not in ("abusive", "wrong"):
        raise HTTPException(401, "admin login required")
    auth.check_report_rate(request)
    if reason == "abusive" and store.is_abusive(body.cid):
        return {"ok": True, "cid": body.cid, "reason": reason, "pending": False}
    if not store.propose_report(body.cid, reason):
        raise HTTPException(400, "invalid cid")
    return {"ok": True, "cid": body.cid, "reason": reason, "pending": True}


@app.get("/api/reports")
def api_reports(request: Request):
    auth.require_admin_read(request)
    return {
        "results": search.abusive_reports(),
        "proposals": store.list_proposals(),
    }


@app.post("/api/reports/decide")
def api_report_decide(body: ReportDecideBody, request: Request):
    auth.require_admin(request)
    action = (body.action or "").strip().lower()
    if action not in ("accept", "reject"):
        raise HTTPException(400, "action must be accept or reject")
    reason = (body.reason or "abusive").strip().lower()
    if reason not in ("abusive", "wrong"):
        raise HTTPException(400, "reason must be abusive or wrong")
    if action == "reject":
        store.drop_proposal(body.cid, reason)
        return {"ok": True, "cid": body.cid, "action": action, "reason": reason}
    if not clubd_client.publish_report(body.cid, reason):
        raise HTTPException(503, "clubd did not accept the report")
    store.drop_proposal(body.cid, reason)
    return {"ok": True, "cid": body.cid, "action": action, "reason": reason}


@app.get("/api/blacklist")
def api_blacklist_get(request: Request):
    auth.require_admin_read(request)
    return {"publishers": store.list_blacklisted()}


@app.post("/api/blacklist")
def api_blacklist_add(body: BlacklistBody, request: Request):
    auth.require_admin(request)
    publisher = (body.publisher or "").strip()
    if not publisher:
        raise HTTPException(400, "publisher required")
    if not store.blacklist(publisher):
        raise HTTPException(400, "cannot blacklist this node")
    return {"ok": True, "publisher": publisher}


@app.post("/api/blacklist/clear")
def api_blacklist_clear(body: BlacklistBody, request: Request):
    auth.require_admin(request)
    publisher = (body.publisher or "").strip()
    if not publisher:
        raise HTTPException(400, "publisher required")
    store.unblacklist(publisher)
    return {"ok": True, "publisher": publisher}


@app.get("/api/alias")
def api_alias_get(request: Request):
    auth.require_admin_read(request)
    return {
        "alias": alias.current(),
        "peer_id": clubd_client.peer_id() or "",
    }


@app.post("/api/alias")
def api_alias_set(body: AliasBody, request: Request):
    auth.require_admin(request)
    try:
        return alias.set_and_publish(body.alias)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/snapshot")
def api_snapshot(request: Request, limit: int = None):
    """Signed JSONL catalog for clubd catch-up. Localhost only."""
    if not auth.is_localhost(request):
        raise HTTPException(403, "snapshot is localhost only")
    cap = int(config.CLUB.get("snapshot_limit", 20000))
    n = cap if limit is None else min(max(int(limit), 0), cap)
    body = store.export_snapshot(store.connect(), n)
    return PlainTextResponse(body, media_type="application/x-ndjson")


def main():
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT)


if __name__ == "__main__":
    main()
