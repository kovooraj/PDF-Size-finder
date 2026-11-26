# Artwork Dimension Finder

A sales person uploads a customer's artwork PDF and gets the product dimensions:

| Product | Output |
|---|---|
| Pouch | Width × Height × Gusset (gusset = full unfolded bottom gusset) |
| Folding carton | Flat size, plus Length × Width × Depth (L × W = opening, D = body depth) |
| Roll label | Width × Height of one label's die shape (+ shape: rectangle / rounded / circle / oval) |
| Commercial print | Finished trim Width × Height (bleed reported separately) |

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

## Run

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # optional; without it only the measurement pass runs
uvicorn app:app --port 8000         # open http://localhost:8000
python -m pytest -q
```

API: `POST /api/analyze` (multipart: `file`, `product_type` = auto|pouch|folding_carton|roll_label|commercial,
`use_ai` = true|false). Optional env: `ARTWORK_DIMS_TOKEN` (bearer secret), `ARTWORK_DIMS_ALLOWED_ORIGINS`
(CORS), `ARTWORK_DIMS_MODEL`, `ARTWORK_DIMS_EFFORT` (default `medium`).

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
- Raster-only PDFs (a flat scan) have no vector dieline. The size then comes from the page or the visible pixels.
- Multi-up sheets: labels report one label; cartons and pouches on a gang sheet need the AI review or a manual check.
- Carton panels are measured on the score lines; allow for board caliper.
