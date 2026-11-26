"""Artwork Dimension Finder: upload a PDF, get product dimensions.

Local:   uvicorn app:app --reload --port 8000
Vercel:  zero-config FastAPI (this file exports `app`); the page is public/index.html.

Env:
  ANTHROPIC_API_KEY            AI review (without it the measurement pass still works)
  ARTWORK_DIMS_TOKEN           optional shared secret; callers send  Authorization: Bearer <token>
                               (the page picks it up once from  ?key=<token>  in its URL)
  ARTWORK_DIMS_ALLOWED_ORIGINS comma-separated origins allowed to call /api (e.g. your Next.js site)
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ARTWORK_DIMS_BUCKET
                               optional: lets files over ~4 MB go straight to Supabase Storage,
                               because Vercel Functions only accept ~4.5 MB request bodies
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from artwork_dims import intake, storage
from artwork_dims.analyze import analyze
from artwork_dims.dimensions import PRODUCT_TYPES

MAX_BYTES = int(float(os.environ.get("ARTWORK_DIMS_MAX_MB", "1024")) * 1024 * 1024)
# Above this the page sends the file to storage first. Vercel Functions take ~4.5 MB per request;
# on a host without that limit (local, Railway, Fly) raise it, e.g. ARTWORK_DIMS_DIRECT_LIMIT_MB=1024.
DIRECT_LIMIT = int(float(os.environ.get("ARTWORK_DIMS_DIRECT_LIMIT_MB", "4")) * 1024 * 1024)
PAGE = Path(__file__).parent / "public" / "index.html"

app = FastAPI(title="Artwork Dimension Finder")
origins = [o.strip() for o in os.environ.get("ARTWORK_DIMS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["*"])


def _auth(authorization: str | None):
    token = os.environ.get("ARTWORK_DIMS_TOKEN")
    if token and authorization != f"Bearer {token}":
        raise HTTPException(401, "Not authorised. Open the page with ?key=<access key>.")


@app.get("/", include_in_schema=False)
def index():
    # On Vercel the CDN serves public/index.html; this covers local runs.
    if PAGE.exists():
        return FileResponse(PAGE)
    return RedirectResponse("/index.html")


@app.get("/nesting.js", include_in_schema=False)
def nesting_js():
    # Vercel's CDN serves public/; this covers local runs.
    return FileResponse(PAGE.parent / "nesting.js", media_type="text/javascript")


@app.get("/api/config")
def config():
    return {
        "ai": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "large_uploads": storage.enabled(),
        "direct_limit_bytes": DIRECT_LIMIT,
        "max_bytes": MAX_BYTES,
        "auth_required": bool(os.environ.get("ARTWORK_DIMS_TOKEN")),
    }


@app.post("/api/upload-url")
def upload_url(authorization: str | None = Header(default=None)):
    _auth(authorization)
    if not storage.enabled():
        raise HTTPException(413, f"Files over {DIRECT_LIMIT // (1024 * 1024)} MB need large uploads switched on "
                                 "(Supabase storage). Ask your admin, or upload a smaller PDF (the dieline page only).")
    try:
        return storage.signed_upload()
    except Exception as e:
        raise HTTPException(502, f"Couldn't prepare the upload ({type(e).__name__}).")


@app.post("/api/analyze")
async def analyze_endpoint(
    file: UploadFile | None = File(default=None),
    storage_path: str | None = Form(default=None),
    product_type: str = Form("auto"),
    use_ai: bool = Form(True),
    authorization: str | None = Header(default=None),
):
    _auth(authorization)
    try:
        if file is not None:
            f = file.file
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size > MAX_BYTES:
                raise HTTPException(413, f"File is larger than {MAX_BYTES // (1024 * 1024)} MB.")
            data, source = await run_in_threadpool(intake.from_fileobj, f, size)
        elif storage_path and storage.enabled():
            try:
                data, source = await run_in_threadpool(
                    intake.from_url, storage.object_url(storage_path), storage.auth_headers(), MAX_BYTES)
            finally:
                await run_in_threadpool(storage.delete, storage_path)
        else:
            raise HTTPException(400, "No file received.")
    except HTTPException:
        raise
    except ValueError as e:
        if str(e) == "not-a-pdf":
            raise HTTPException(415, "That file isn't a PDF. Export the artwork as a PDF and try again.")
        if str(e) == "too-large":
            raise HTTPException(413, f"File is larger than {MAX_BYTES // (1024 * 1024)} MB.")
        raise HTTPException(400, str(e))
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg:
            raise HTTPException(422, "This PDF is password-protected. Ask for an unlocked copy.")
        raise HTTPException(422, f"This PDF couldn't be read ({type(e).__name__}). It may be damaged.")
    pt = product_type if product_type in PRODUCT_TYPES else None
    try:
        result = await run_in_threadpool(analyze, data, pt, use_ai)
        result["source"] = source
        if source.get("reduced"):
            result["page_count"] = source.get("source_pages", result["page_count"])
        return result
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg:
            raise HTTPException(422, "This PDF is password-protected. Ask for an unlocked copy.")
        raise HTTPException(422, f"This PDF couldn't be read ({type(e).__name__}). It may be damaged.")
