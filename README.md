# Artwork Dimension Finder

A sales person uploads a customer's artwork PDF and gets the product dimensions:

| Product | Output |
|---|---|
| Pouch | Width × Height × Gusset (gusset = full unfolded bottom gusset) |
| Folding carton | Flat size, plus Length × Width × Depth (L × W = opening, D = body depth) |
| Roll label | Width × Height of one label's die shape (+ shape: rectangle / rounded / circle / oval) |
| Commercial print | Finished trim Width × Height (bleed reported separately) |

## Using it

Drop a PDF anywhere on the page, pick the product (or leave it on Auto-detect), and press **Find dimensions**.
The result shows each dimension as a tile, plus a preview with the dimension arrows drawn on the artwork.
Hover a tile to highlight its arrow. Toggle layers (dimensions, dieline, folds, size box, artwork extents,
crop marks), switch inches/mm, and click the preview (or **Expand**) for a full-screen viewer: scroll or pinch
to zoom, drag to move, `+`/`-`/`0` keys, Esc to close.

## How it works

1. **Measure (exact, no AI).** `artwork_dims/geometry.py` walks the PDF content stream with pikepdf and records
   every path with its **spot colour name**, **layer (OCG)**, dash pattern and clip. That is what separates a
   dieline from artwork (`CutContour`, `Dieline`, `Die Cut`, `Crease`, `Fold`…) and crop marks from art
   (`All`/Registration colour).
   `classify.py` sorts paths into cut / crease / guide / crop marks / annotation / art. If a file has no named
   dieline, a thin single-colour stroke group spanning the sheet is used and flagged "verify".
2. **Size basis, in order:** PDF TrimBox → crop marks → dieline bounds → artwork extents (when the page has
   white space round the art) → page size. White fills, clipped-away art, registration items and dieline notes
   don't count as artwork, so empty margins never inflate the size.
3. **Product rules** (`dimensions.py`): carton L/W/D from the 4 alternating body panels (L, W, L, W) between
   score lines; pouch from the front | gusset | back layout (side by side, as in the Print Monkey setup guide, or
   stacked), or side-gusset front | gusset | back | gusset; label from the most common closed cut shape.
4. **Printed callouts** (`textdims.py`): `8.00"x6.00"x2.00"`, `6.00" Width`, `Gusset: 2"`, `120mm x 80mm`…
   A printed spec that matches the measurement raises confidence to high. Labels such as *Zipper Width*,
   *Seal Width* and *Width + Bleed* are ignored.
5. **AI review** (`ai.py`, optional): Claude (`claude-opus-5-5`) gets a clean render, an annotated render and
   the measured JSON. It returns structured JSON: product type, its own reading of the dimensions with the basis,
   any disagreements, a one-line summary for the quote, and questions for the customer. If the type was
   auto-detected and Claude names a different one, the measurement rules re-run with that type, so the numbers
   still come from geometry. If the AI call fails, the measured result is still returned.

6. **Imposition** (`public/nesting.js`, runs in the browser, so settings update instantly):
   - *Folding cartons*: the die outline is filled into a bitmap (32 px/in), grown by half the gutter, and copies
     are dropped row by row as close as the outline allows: straight, staggered, or head-to-tail (every other row
     turned 180°), die as drawn or turned 90°. A free-form pass also places dies one by one, in any of four
     turns, at the first free spot from the gripper edge, so flaps tuck into each other's gaps ("lego-ing").
     The layout with the most ups wins; each sheet is also tried fed the other way round.
   - *Commercial print*: exact rectangle fill, mixing turned pieces into the leftover strip.
   - *Sheets* (editable, saved per browser): 28 × 40 and 24 × 36 offset (gripper 0.5″, tail 0.375″, sides
     0.25″), 20.8125 × 29.5 digital (gripper 0.375″, tail and sides 0.25″). Gutter 0.25″.
   - *Pouches*: 30″ web, 29″ usable, lanes 0.25″ apart, 0.125″ along the web.
   - *Roll labels*: 13″ web, 12″ usable, 0.125″ gaps.

## Run locally

```bash
pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=...        # optional; without it only the measurement pass runs
uvicorn app:app --port 8000         # open http://localhost:8000
python -m pytest -q
node --test tests/js/nesting.test.js
```

## Deploy on Vercel

1. Push this folder to a new GitHub repo, then in Vercel: **Add New → Project → import the repo**. Vercel detects
   FastAPI from `app.py` and serves the page from `public/`. No build settings are needed.
2. **Settings → Environment Variables:**

   | Variable | Needed | What it does |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | for AI review | Without it the page greys out "AI review" and still measures |
   | `ARTWORK_DIMS_TOKEN` | recommended | Access key. Share the link as `https://<your-app>.vercel.app/?key=<token>`; the page remembers it |
   | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | for PDFs over 4 MB | See below |
   | `ARTWORK_DIMS_BUCKET` | optional | Storage bucket name, default `dimension-finder` |
   | `ARTWORK_DIMS_ALLOWED_ORIGINS` | optional | Lets your Next.js site call `/api/analyze` from the browser |
   | `ARTWORK_DIMS_MODEL`, `ARTWORK_DIMS_EFFORT` | optional | Default `claude-opus-5-5`, `medium` |

3. Redeploy after adding variables.

**Large files (up to 1 GB).** A Vercel Function accepts only about 4.5 MB per request, so bigger files go
straight from the browser to Supabase Storage (one-time signed upload URL, with a progress bar). The server
never downloads the whole file. It opens it with HTTP range reads and builds a small *measurement copy*: big
images become grey placeholders in the same position, and Illustrator's private editing data (`/PieceInfo`,
often half the file) is dropped. Vector paths, spot colours, layers, text and fonts are kept, so the result is
the same. A 400 MB test file needed 13 MB of reads and under a second. The upload is deleted after analysis,
and unanalysed uploads older than 24 hours are cleaned up.

Setup (Supabase project `cjubwgebqlvdufqjbhnm`):
1. The private bucket `dimension-finder` (PDF only, 1 GB per file) already exists.
2. **Storage → Settings → Global file size limit:** raise it to 1 GB (on Pro it defaults to 50 MB, and the lower
   of the global and bucket limits wins).
3. In Vercel, add `SUPABASE_URL=https://cjubwgebqlvdufqjbhnm.supabase.co` and `SUPABASE_SERVICE_ROLE_KEY`
   (Project Settings → API Keys → a secret key `sb_secret_…`, or the legacy `service_role` key). Redeploy.

Limits: `ARTWORK_DIMS_MAX_MB` (default 1024), `ARTWORK_DIMS_DIRECT_LIMIT_MB` (default 4; on a host without
Vercel's body limit you can raise it and skip storage), `ARTWORK_DIMS_LITE_MB` (default 25: files above this are
measured from the reduced copy).

`vercel.json` sets a 120 s time limit (AI review usually takes 15–60 s) and 2 GB memory for rendering.
PyMuPDF is pinned to 1.x because 2.x pulls in onnxruntime, which would push the function toward Vercel's size limit.

API: `POST /api/analyze` (multipart: `file` **or** `storage_path`, `product_type` =
auto|pouch|folding_carton|roll_label|commercial, `use_ai` = true|false; header `Authorization: Bearer <token>` if
`ARTWORK_DIMS_TOKEN` is set). `GET /api/config` reports what is switched on.

## Fitting it into Print Monkey Design

The site's preflight (`src/lib/preflight.ts`) runs in the browser and **assumes** the trim size from the order
(it centres the ordered size on the page). It never reads a dieline or crop marks, so it can't tell a sales
person what size a file *is*. This service does that. The API key must stay server-side, so it runs as a
backend: a separate Python service (Railway, Render, Fly) or a Vercel Python function. A sales/admin page in the
Next.js app posts the PDF to `/api/analyze`. Possible follow-ups:
- add a `"Detected size"` key next to `"Preflight"` in `artwork_submissions.configuration`;
- feed the detected trim into the existing `sizeCheck()` instead of the ordered size, and warn when they differ.

## Known limits
- Only the first 4 pages are measured, and only the first 2 get an AI review.
- Very large files are measured from the first 4 pages; their big images appear as grey boxes in the preview and to the AI.
- Raster-only PDFs (a flat scan) have no vector dieline. The size then comes from the page or the visible pixels.
- Multi-up sheets: labels report one label; cartons and pouches on a gang sheet need the AI review or a manual check.
- Carton panels are measured on the score lines; allow for board caliper.
