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


def _req(method: str, url: str, body: bytes | None = None, ctype: str | None = None, timeout: int = 60):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if ctype:
        headers["Content-Type"] = ctype
    r = urllib.request.Request(url, data=body, method=method, headers=headers)
    return urllib.request.urlopen(r, timeout=timeout)


def signed_upload() -> dict:
    """Returns {'path': ..., 'upload_url': ...}; the browser PUTs the file to upload_url."""
    path = f"uploads/{uuid.uuid4()}.pdf"
    with _req("POST", f"{_base()}/object/upload/sign/{BUCKET}/{path}", b"{}", "application/json") as resp:
        data = json.loads(resp.read())
    rel = data.get("url") or data.get("signedURL") or ""
    if not rel:
        raise RuntimeError("Supabase did not return a signed upload URL")
    return {"path": path, "upload_url": _base() + (rel if rel.startswith("/") else "/" + rel)}


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
