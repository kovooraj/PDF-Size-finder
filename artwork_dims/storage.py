"""Optional Supabase Storage hand-off for PDFs larger than Vercel's ~4.5 MB
request-body limit.

Flow: the page asks /api/upload-url for a one-time signed upload URL, PUTs the
PDF straight to Supabase, then calls /api/analyze with the storage path. The
server downloads it with the service-role key and deletes it afterwards.
Keys never reach the browser.

Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ARTWORK_DIMS_BUCKET (default
'dimension-finder'; create it as a PRIVATE bucket).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid

BUCKET = os.environ.get("ARTWORK_DIMS_BUCKET", "dimension-finder")
PATH_RE = re.compile(r"^uploads/[0-9a-f-]{36}\.pdf$")


def enabled() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


def _base() -> str:
    return os.environ["SUPABASE_URL"].rstrip("/") + "/storage/v1"


def auth_headers() -> dict:
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    # New-style secret keys (sb_secret_...) are not JWTs and go in the apikey header only;
    # legacy service_role JWTs also go in Authorization.
    if key.startswith("sb_secret_"):
        return {"apikey": key}
    return {"Authorization": f"Bearer {key}", "apikey": key}


def _req(method: str, url: str, body: bytes | None = None, ctype: str | None = None, timeout: int = 60):
    headers = auth_headers()
    if ctype:
        headers["Content-Type"] = ctype
    r = urllib.request.Request(url, data=body, method=method, headers=headers)
    return urllib.request.urlopen(r, timeout=timeout)


def cleanup_stale(max_age_hours: float = 24) -> int:
    """Best effort: delete uploads that were never analysed (e.g. the tab was closed)."""
    import datetime as dt
    try:
        body = json.dumps({"prefix": "uploads/", "limit": 100,
                           "sortBy": {"column": "created_at", "order": "asc"}}).encode()
        with _req("POST", f"{_base()}/object/list/{BUCKET}", body, "application/json", timeout=10) as resp:
            items = json.loads(resp.read())
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)
        old = []
        for it in items:
            ts = it.get("created_at")
            if not ts or not it.get("name"):
                continue
            when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if when < cutoff:
                old.append(f"uploads/{it['name']}")
        if old:
            _req("DELETE", f"{_base()}/object/{BUCKET}", json.dumps({"prefixes": old}).encode(),
                 "application/json", timeout=10).close()
        return len(old)
    except Exception:
        return 0


def signed_upload() -> dict:
    """Returns {'path': ..., 'upload_url': ...}; the browser PUTs the file to upload_url."""
    cleanup_stale()
    path = f"uploads/{uuid.uuid4()}.pdf"
    with _req("POST", f"{_base()}/object/upload/sign/{BUCKET}/{path}", b"{}", "application/json") as resp:
        data = json.loads(resp.read())
    rel = data.get("url") or data.get("signedURL") or ""
    if not rel:
        raise RuntimeError("Supabase did not return a signed upload URL")
    return {"path": path, "upload_url": _base() + (rel if rel.startswith("/") else "/" + rel)}


def object_url(path: str) -> str:
    if not PATH_RE.match(path):
        raise ValueError("bad storage path")
    return f"{_base()}/object/authenticated/{BUCKET}/{path}"


def download(path: str, max_bytes: int) -> bytes:
    if not PATH_RE.match(path):
        raise ValueError("bad storage path")
    with _req("GET", f"{_base()}/object/{BUCKET}/{path}") as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("file too large")
    return data


def delete(path: str) -> None:
    if not PATH_RE.match(path):
        return
    try:
        body = json.dumps({"prefixes": [path]}).encode()
        _req("DELETE", f"{_base()}/object/{BUCKET}", body, "application/json").close()
    except urllib.error.URLError:
        pass
