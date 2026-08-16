#!/usr/bin/env python3
"""Boot clubd + observer on ephemeral ports and hit the APIs.

Usage: python scripts/smoke_live.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.cids import cid_for

PY = os.path.join(ROOT, "venv", "bin", "python")
CLUBD = os.path.join(ROOT, "build", "clubd")
SMOKE_CID = cid_for("smoke-classify")
SMOKE_SKIP = cid_for("smoke-skip")


def _http(url, method="GET", body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        if "json" in ctype and "ndjson" not in ctype:
            return json.loads(raw.decode("utf-8") or "null")
        return raw.decode("utf-8")


def _wait(fn, tries=40, delay=0.25, label="service"):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(delay)
    raise SystemExit("timeout waiting for %s: %s" % (label, last))


def main():
    failures = []

    def check(name, cond, detail=""):
        if cond:
            print("  ok  %s" % name)
        else:
            print("  FAIL %s %s" % (name, detail))
            failures.append(name)

    print("== unit tests ==")
    r = subprocess.run(
        [PY, "-m", "pytest", "-q"], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    print(r.stdout.strip().split("\n")[-1])
    check("pytest", r.returncode == 0, "exit %s" % r.returncode)
    r = subprocess.run(
        ["go", "test", "./..."], cwd=os.path.join(ROOT, "clubd"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    print(r.stdout.strip())
    check("go test", r.returncode == 0, "exit %s" % r.returncode)

    subprocess.check_call(["make", "build"], cwd=ROOT)

    tmp = tempfile.mkdtemp(prefix="observer-smoke-")
    cfg = os.path.join(tmp, "config.toml")
    web_port, api_port, listen = 18002, 18003, 14713
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(
            "\n".join([
                "[club]",
                'api_host = "127.0.0.1"',
                "api_port = %d" % api_port,
                "listen_port = %d" % listen,
                "bootstrap_peers = []",
                'identity_path = "%s"' % os.path.join(tmp, "identity.key"),
                'inbox_dir = "%s"' % os.path.join(tmp, "inbox"),
                'db_path = "%s"' % os.path.join(tmp, "club.sqlite"),
                "max_msgs_per_peer_per_min = 60",
                "inbox_max_bytes = 1048576",
                "inbox_keep_days = 7",
                "snapshot_limit = 100",
                "[web]",
                'host = "127.0.0.1"',
                "port = %d" % web_port,
                "[sniffer]",
                "listen_port = %d" % (listen + 1),
                'spool_dir = "%s"' % os.path.join(tmp, "spool"),
                "low_connections = 2",
                "high_connections = 4",
                "[storage]",
                'work_db = "%s"' % os.path.join(tmp, "work.sqlite"),
                "[fetch]",
                "timeout_seconds = 2",
                "concurrency = 0",
                "[llm]",
                "max_text_chars = 3000",
                "",
            ])
        )

    env = os.environ.copy()
    env["OBSERVER_CLUB_CONFIG"] = cfg
    flags = subprocess.check_output(
        [PY, "-m", "observer.clubd_flags"], cwd=ROOT, env=env, text=True,
    ).split()

    print("== live stack (%s) ==" % tmp)
    clubd = subprocess.Popen(
        [CLUBD] + flags, cwd=ROOT, env=env,
        stdout=open(os.path.join(tmp, "clubd.log"), "w"),
        stderr=subprocess.STDOUT,
    )
    observer = subprocess.Popen(
        [PY, "-m", "observer.main"], cwd=ROOT, env=env,
        stdout=open(os.path.join(tmp, "observer.log"), "w"),
        stderr=subprocess.STDOUT,
    )
    try:
        web = "http://127.0.0.1:%d" % web_port
        api = "http://127.0.0.1:%d" % api_port

        health = _wait(lambda: _http(api + "/health"), label="clubd")
        check("clubd /health", health == "ok", repr(health))

        ident = _wait(lambda: _http(api + "/id"), label="clubd /id")
        check("clubd /id peer_id", bool(ident.get("peer_id")))
        check("clubd /id addrs", bool(ident.get("addrs")))

        stats = _wait(lambda: _http(web + "/api/stats"), label="observer")
        check("observer clubd flag", stats.get("clubd") is True, repr(stats))
        check("observer peer_id", stats.get("peer_id") == ident.get("peer_id"))

        html = _http(web + "/")
        check("web UI", "Observer" in html)

        snap0 = _http(web + "/api/snapshot")
        check("empty snapshot", snap0 in ("", "\n"), repr(snap0[:80]))

        published = _http(api + "/v1/publish", method="POST", body={
            "kind": "classify",
            "cid": SMOKE_CID,
            "field": "biology",
            "topic": "smoke CRISPR",
            "keywords": "smoke, test",
            "text_sha256": "abc123",
            "mime_type": "application/pdf",
        })
        check("publish classify signed", bool(published.get("sig")))
        check("publish publisher bound", published.get("publisher") == ident["peer_id"])

        def search_classify():
            res = _http(web + "/api/search?q=CRISPR")
            rows = res.get("results") or []
            if not rows:
                raise RuntimeError(res)
            return rows

        rows = _wait(search_classify, tries=20, delay=0.3, label="ingest classify")
        check("ingest classify", rows[0].get("field") == "biology")

        _http(api + "/v1/publish", method="POST", body={
            "kind": "skip",
            "cid": SMOKE_CID,
            "reason": "unprocessable",
        })
        time.sleep(1.2)
        still = _http(web + "/api/search?q=CRISPR")
        check("skip does not hide classify",
              len(still.get("results") or []) >= 1, repr(still))

        stats2 = _http(web + "/api/stats")
        check("stats classifies >= 1", (stats2.get("classifies") or 0) >= 1)

        snap = _http(web + "/api/snapshot")
        check("snapshot has sig", '"sig":' in snap)
        check("snapshot has cid", SMOKE_CID in snap)

        peers = _http(api + "/v1/peers")
        check("peers endpoint", "peers" in peers)

        skip_pub = _http(api + "/v1/publish", method="POST", body={
            "kind": "skip",
            "cid": SMOKE_SKIP,
            "reason": "out_of_scope",
            "mime_type": "image/png",
        })
        check("publish skip signed", bool(skip_pub.get("sig")))

        def snap_has_skip():
            body = _http(web + "/api/snapshot")
            if SMOKE_SKIP not in body or "out_of_scope" not in body:
                raise RuntimeError(body[:200])
            return body

        _wait(snap_has_skip, tries=20, delay=0.3, label="ingest skip")
        check("skip in snapshot", True)

    finally:
        for proc in (observer, clubd):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    if failures:
        print("\n%d smoke check(s) failed: %s" % (len(failures), ", ".join(failures)))
        sys.exit(1)
    print("\nall smoke checks passed")


if __name__ == "__main__":
    main()
