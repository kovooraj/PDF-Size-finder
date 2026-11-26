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

from artwork_dims import storage
from artwork_dims.analyze import analyze
from artwork_dims.dimensions import PRODUCT_TYPES

MAX_BYTES = 50 * 1024 * 1024        # same limit as the website upload page
DIRECT_LIMIT = 4 * 1024 * 1024      # above this the page uses the storage hand-off (Vercel body limit)
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
        raise HTTPException(413, "Files over 4 MB need large uploads switched on (Supabase storage). "
                                 "Ask your admin, or upload a smaller PDF (the dieline page only).")
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
    if file is not None:
        data = await file.read(MAX_BYTES + 1)
    elif storage_path and storage.enabled():
        try:
            data = await run_in_threadpool(storage.download, storage_path, MAX_BYTES)
        except ValueError as e:
            raise HTTPException(413 if "large" in str(e) else 400, str(e))
        except Exception as e:
            raise HTTPException(502, f"Couldn't fetch the uploaded file ({type(e).__name__}).")
        finally:
            await run_in_threadpool(storage.delete, storage_path)
    else:
        raise HTTPException(400, "No file received.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File is larger than 50 MB.")
    # Check the real header, not just the extension (the website only checks the extension).
    if b"%PDF-" not in data[:1024]:
        raise HTTPException(415, "That file isn't a PDF. Export the artwork as a PDF and try again.")
    pt = product_type if product_type in PRODUCT_TYPES else None
    try:
        return await run_in_threadpool(analyze, data, pt, use_ai)
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg:
            raise HTTPException(422, "This PDF is password-protected. Ask for an unlocked copy.")
        raise HTTPException(422, f"This PDF couldn't be read ({type(e).__name__}). It may be damaged.")
