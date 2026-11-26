"""Artwork Dimension Finder: upload a PDF, get product dimensions.

Run:  uvicorn app:app --reload --port 8000
Env:  ANTHROPIC_API_KEY (for the AI review; without it the measurement pass still works)
      ARTWORK_DIMS_ALLOWED_ORIGINS  comma-separated origins allowed to call /api (e.g. your Next.js site)
      ARTWORK_DIMS_TOKEN            optional shared secret; if set, callers must send  Authorization: Bearer <token>
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from artwork_dims.analyze import analyze
from artwork_dims.dimensions import PRODUCT_TYPES

MAX_BYTES = 50 * 1024 * 1024  # same limit as the website upload page
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Artwork Dimension Finder")
origins = [o.strip() for o in os.environ.get("ARTWORK_DIMS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["POST"], allow_headers=["*"])


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "ai": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))}


@app.post("/api/analyze")
async def analyze_endpoint(
    file: UploadFile = File(...),
    product_type: str = Form("auto"),
    use_ai: bool = Form(True),
    authorization: str | None = Header(default=None),
):
    token = os.environ.get("ARTWORK_DIMS_TOKEN")
    if token and authorization != f"Bearer {token}":
        raise HTTPException(401, "Not authorised")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File is larger than 50 MB.")
    # Check the real header, not just the extension (the website only checks the extension).
    if not data[:1024].lstrip().startswith(b"%PDF-") and b"%PDF-" not in data[:1024]:
        raise HTTPException(415, "That file isn't a PDF. Export the artwork as a PDF and try again.")
    pt = product_type if product_type in PRODUCT_TYPES else None
    try:
        return await run_in_threadpool(analyze, data, pt, use_ai)
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg:
            raise HTTPException(422, "This PDF is password-protected. Ask for an unlocked copy.")
        raise HTTPException(422, f"This PDF couldn't be read ({type(e).__name__}). It may be damaged.")
