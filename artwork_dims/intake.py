"""Turn an upload (small bytes, a large local temp file, or a file sitting in
storage) into PDF bytes the analyzer can handle quickly. Large files are
reduced to a measurement copy (see lite.py) instead of being read in full."""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import urllib.request

from .lite import make_lite
from .remote import HTTPRangeFile, RangeNotSupported

LITE_THRESHOLD = int(float(os.environ.get("ARTWORK_DIMS_LITE_MB", "25")) * 1024 * 1024)
FALLBACK_DOWNLOAD_LIMIT = 400 * 1024 * 1024  # if storage ignores Range, download at most this to /tmp


def _check_pdf_header(head: bytes):
    if b"%PDF-" not in head[:1024]:
        raise ValueError("not-a-pdf")


def from_fileobj(f, size: int, max_pages: int = 4) -> tuple[bytes, dict]:
    f.seek(0)
    head = f.read(1024)
    _check_pdf_header(head)
    f.seek(0)
    if size <= LITE_THRESHOLD:
        return f.read(), {"size_bytes": size, "reduced": False}
    lite, info = make_lite(f, max_pages=max_pages)
    return lite, {"size_bytes": size, "reduced": True, **info}


def from_url(url: str, headers: dict, max_bytes: int, max_pages: int = 4) -> tuple[bytes, dict]:
    try:
        rf = HTTPRangeFile(url, headers)
    except RangeNotSupported:
        return _from_full_download(url, headers, max_bytes, max_pages)
    if rf.size > max_bytes:
        raise ValueError("too-large")
    lite, info = from_fileobj(io.BufferedReader(rf, buffer_size=1 << 16), rf.size, max_pages)
    info.update({"fetched_bytes": rf.bytes_fetched, "range_requests": rf.requests})
    return lite, info


def _from_full_download(url, headers, max_bytes, max_pages):
    limit = min(max_bytes, FALLBACK_DOWNLOAD_LIMIT)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r, tempfile.TemporaryFile() as tmp:
        copied = 0
        while chunk := r.read(1 << 20):
            copied += len(chunk)
            if copied > limit:
                raise ValueError("too-large")
            tmp.write(chunk)
        lite, info = from_fileobj(tmp, copied, max_pages)
    info["fetched_bytes"] = copied
    return lite, info
