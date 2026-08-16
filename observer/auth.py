"""Optional admin password. Unset means open (localhost writes). Hash only on disk."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time

from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request

from . import config

COOKIE = "observer_admin"
SESSION_TTL = 12 * 3600
MAX_PASSWORD = 128
_FAIL_LIMIT = 5
_FAIL_WINDOW = 60.0
_SALT_LEN = 16
_DKLEN = 32

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
_report_hits: dict[str, list[float]] = {}
_DIR = None  # override in tests
_REPORT_LIMIT = 8
_REPORT_WINDOW = 600.0


def data_dir():
    return _DIR or os.path.join(config.ROOT, "data")


def password_path():
    return os.path.join(data_dir(), "admin.json")


def session_key_path():
    return os.path.join(data_dir(), "session.key")


def _default_kdf():
    if hasattr(hashlib, "scrypt"):
        return {"kdf": "scrypt", "n": 2**14, "r": 8, "p": 1, "dklen": _DKLEN}
    return {"kdf": "pbkdf2_sha256", "iterations": 600_000, "dklen": _DKLEN}


def _derive(password: str, salt: bytes, params: dict) -> bytes:
    raw = password.encode("utf-8")
    kdf = params.get("kdf")
    dklen = int(params.get("dklen") or _DKLEN)
    if kdf == "scrypt":
        return hashlib.scrypt(
            raw,
            salt=salt,
            n=int(params["n"]),
            r=int(params["r"]),
            p=int(params["p"]),
            dklen=dklen,
        )
    if kdf == "pbkdf2_sha256":
        return hashlib.pbkdf2_hmac(
            "sha256", raw, salt, int(params["iterations"]), dklen=dklen,
        )
    raise ValueError("unknown kdf")


def _write_secret_file(path: str, data: bytes):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=".tmp-admin-",
    )
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


def _load_record():
    path = password_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        if not isinstance(rec, dict) or not rec.get("hash") or not rec.get("salt"):
            return {"corrupt": True}
        return rec
    except (OSError, json.JSONDecodeError, TypeError):
        return {"corrupt": True}


def password_is_set() -> bool:
    rec = _load_record()
    return rec is not None


def is_corrupt() -> bool:
    rec = _load_record()
    return bool(rec and rec.get("corrupt"))


def _rev() -> int:
    rec = _load_record()
    if not rec or rec.get("corrupt"):
        return 0
    try:
        return int(rec.get("rev") or 1)
    except (TypeError, ValueError):
        return 1


def _check_length(password: str):
    if not isinstance(password, str) or password == "":
        raise ValueError("enter a password")
    if len(password) > MAX_PASSWORD:
        raise ValueError("password must be at most %d characters" % MAX_PASSWORD)


def set_password(password: str):
    _check_length(password)
    salt = secrets.token_bytes(_SALT_LEN)
    params = _default_kdf()
    digest = _derive(password, salt, params)
    rec = {
        **params,
        "salt": salt.hex(),
        "hash": digest.hex(),
        "rev": _rev() + 1,
    }
    _write_secret_file(
        password_path(),
        (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8"),
    )


def clear_password():
    path = password_path()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def verify_password(password: str) -> bool:
    rec = _load_record()
    if not rec or rec.get("corrupt"):
        return False
    if not isinstance(password, str):
        return False
    if len(password) > MAX_PASSWORD:
        return False
    try:
        salt = bytes.fromhex(rec["salt"])
        expected = bytes.fromhex(rec["hash"])
        got = _derive(password, salt, rec)
    except (ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def _session_secret() -> bytes:
    path = session_key_path()
    if os.path.isfile(path):
        with open(path, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key[:32]
    key = secrets.token_bytes(32)
    _write_secret_file(path, key)
    return key


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_session() -> str:
    exp = int(time.time()) + SESSION_TTL
    nonce = secrets.token_hex(16)
    rev = _rev()
    payload = ("%d:%s:%d" % (exp, nonce, rev)).encode("ascii")
    mac = hmac.new(_session_secret(), payload, hashlib.sha256).digest()
    return _b64e(payload + mac)


def session_ok(request: Request) -> bool:
    token = request.cookies.get(COOKIE) or ""
    if not token:
        return False
    try:
        raw = _b64d(token)
    except (ValueError, TypeError):
        return False
    if len(raw) < 33:
        return False
    payload, mac = raw[:-32], raw[-32:]
    expect = hmac.new(_session_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expect):
        return False
    try:
        exp_s, nonce, rev_s = payload.decode("ascii").split(":")
        if not nonce:
            return False
        exp = int(exp_s)
        rev = int(rev_s)
    except (ValueError, UnicodeDecodeError):
        return False
    if exp < int(time.time()):
        return False
    return rev == _rev()


def set_session_cookie(response: Response, request: Request):
    response.set_cookie(
        key=COOKIE,
        value=issue_session(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def clear_session_cookie(response: Response):
    response.delete_cookie(key=COOKIE, path="/")


def _client_ip(request: Request) -> str:
    host = request.client.host if request.client else ""
    return host or "unknown"


def check_login_rate(request: Request):
    ip = _client_ip(request)
    now = time.monotonic()
    with _lock:
        stamps = [t for t in _failures.get(ip, []) if now - t < _FAIL_WINDOW]
        _failures[ip] = stamps
        if len(stamps) >= _FAIL_LIMIT:
            raise HTTPException(429, "too many attempts; try again later")


def record_login_failure(request: Request):
    ip = _client_ip(request)
    with _lock:
        _failures.setdefault(ip, []).append(time.monotonic())


def clear_login_failures(request: Request):
    ip = _client_ip(request)
    with _lock:
        _failures.pop(ip, None)


def is_localhost(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1")


def is_admin(request: Request) -> bool:
    """True when this request may publish reports and change admin state."""
    if password_is_set():
        return session_ok(request)
    return is_localhost(request)


def check_report_rate(request: Request):
    ip = _client_ip(request)
    now = time.monotonic()
    with _lock:
        stamps = [t for t in _report_hits.get(ip, []) if now - t < _REPORT_WINDOW]
        if len(stamps) >= _REPORT_LIMIT:
            _report_hits[ip] = stamps
            raise HTTPException(429, "too many reports; try again later")
        stamps.append(now)
        _report_hits[ip] = stamps


def require_admin(request: Request):
    """Writes: localhost when unlocked; session when a password is set."""
    if password_is_set():
        if not session_ok(request):
            raise HTTPException(401, "admin login required")
        return
    if not is_localhost(request):
        raise HTTPException(403, "admin is localhost only")


def require_admin_read(request: Request):
    """Admin GETs stay public until a password is set."""
    if password_is_set() and not session_ok(request):
        raise HTTPException(401, "admin login required")


def status(request: Request) -> dict:
    locked = password_is_set()
    return {
        "password_set": locked,
        "authenticated": session_ok(request) if locked else is_localhost(request),
    }
