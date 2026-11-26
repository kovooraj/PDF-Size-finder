"""Entry point: analyze_pdf(bytes, product_type=None) -> dict."""
from __future__ import annotations

import base64
import io
import re

import numpy as np
import pikepdf
import pymupdf

from .classify import PT_PER_IN, art_extents, classify, trim_from_marks, union_bbox
from .dimensions import PRODUCT_TYPES, PageFacts, _clusters, _fold_segments, compute
from .geometry import Rect, extract_page_geometry
from .textdims import find_callouts

MAX_PAGES = 4
PREVIEW_LONG_SIDE = 1500   # PNG for the AI review
VIEW_LONG_SIDE = 3200      # JPEG for the zoomable viewer
MAX_OVERLAY_SEGMENTS = 6000

# Text that belongs to the dieline / tech notes, not to the printed artwork.
NON_ART_TEXT = re.compile(
    r"dieline|die\s*line|do\s*not\s*print|non.?print|bleed|safe\s*(area|zone)|trim\b|fold|crease|glue|"
    r"\bcut\b|score|perf|caliper|\d+(\.\d+)?\s*(\"|”|″|in\b|mm\b)",
    re.I,
)


def _box(page_obj, key) -> Rect | None:
    try:
        v = page_obj.get(key)
        if v is None:
            return None
        a = [float(x) for x in v]
        return (min(a[0], a[2]), min(a[1], a[3]), max(a[0], a[2]), max(a[1], a[3]))
    except Exception:
        return None


def _mu_to_pdf(page: pymupdf.Page, r) -> Rect:
    inv = ~page.transformation_matrix
    p0 = pymupdf.Point(r[0], r[1]) * inv
    p1 = pymupdf.Point(r[2], r[3]) * inv
    return (min(p0.x, p1.x), min(p0.y, p1.y), max(p0.x, p1.x), max(p0.y, p1.y))


def _text_boxes(page: pymupdf.Page) -> tuple[str, list[Rect], list[dict]]:
    full = page.get_text("text") or ""
    art_boxes: list[Rect] = []
    spans_out: list[dict] = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = span.get("text", "").strip()
                if not t:
                    continue
                r = _mu_to_pdf(page, span["bbox"])
                is_note = bool(NON_ART_TEXT.search(t))
                spans_out.append({"text": t, "note": is_note})
                if not is_note:
                    art_boxes.append(r)
    return full, art_boxes, spans_out


def _render(doc_bytes: bytes, index: int, overlays: dict | None, long_side: int,
            fmt: str = "png") -> tuple[bytes, float]:
    doc = pymupdf.open(stream=doc_bytes, filetype="pdf")
    page = doc[index]
    page.set_rotation(0)
    try:
        page.set_cropbox(page.mediabox)
    except Exception:
        pass
    if overlays:
        tm = page.transformation_matrix

        def P(x, y):
            return pymupdf.Point(x, y) * tm

        def R(r):
            a, b = P(r[0], r[1]), P(r[2], r[3])
            return pymupdf.Rect(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))

        shape = page.new_shape()
        drawn = 0
        for seg in overlays.get("cut", []):
            if drawn > 6000:
                break
            shape.draw_line(P(seg.x0, seg.y0), P(seg.x1, seg.y1))
            drawn += 1
        shape.finish(color=(0.9, 0.05, 0.1), width=1.6)
        for seg in overlays.get("crease", []):
            if drawn > 12000:
                break
            shape.draw_line(P(seg.x0, seg.y0), P(seg.x1, seg.y1))
            drawn += 1
        shape.finish(color=(1.0, 0.55, 0.0), width=1.6, dashes="[4 3] 0")
        if overlays.get("trim"):
            shape.draw_rect(R(overlays["trim"]))
            shape.finish(color=(0.0, 0.65, 0.25), width=1.4, dashes="[6 3] 0")
        if overlays.get("art"):
            shape.draw_rect(R(overlays["art"]))
            shape.finish(color=(0.45, 0.2, 0.85), width=1.2, dashes="[2 2] 0")
        for seg in overlays.get("marks", []):
            shape.draw_line(P(seg.x0, seg.y0), P(seg.x1, seg.y1))
        shape.finish(color=(0.0, 0.45, 1.0), width=2.2)
        shape.commit()
    w, h = page.rect.width, page.rect.height
    zoom = long_side / max(w, h)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    png = pix.tobytes("jpeg", jpg_quality=82) if fmt == "jpeg" else pix.tobytes("png")
    doc.close()
    return png, zoom


def annotations_to_view(result_dict: dict, media: Rect) -> dict:
    """Convert annotation coordinates from PDF points (y up) to page points from the top-left."""
    for an in result_dict.get("annotations", []):
        an["x1"], an["y1"] = round(an["x1"] - media[0], 2), round(media[3] - an["y1"], 2)
        an["x2"], an["y2"] = round(an["x2"] - media[0], 2), round(media[3] - an["y2"], 2)
    return result_dict


def _pixel_content_bbox(png: bytes, zoom: float, media: Rect) -> Rect | None:
    pix = pymupdf.Pixmap(png)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    nonwhite = (arr[:, :, :3].min(axis=2) < 245)
    ys, xs = np.nonzero(nonwhite)
    if len(xs) == 0:
        return None
    x0, x1 = xs.min() / zoom, (xs.max() + 1) / zoom
    y0, y1 = ys.min() / zoom, (ys.max() + 1) / zoom
    # pixel space is y-down from the media top-left
    return (media[0] + x0, media[3] - y1, media[0] + x1, media[3] - y0)


def analyze_pdf(data: bytes, product_type: str | None = None, max_pages: int = MAX_PAGES) -> dict:
    if product_type not in PRODUCT_TYPES:
        product_type = None
    pdf = pikepdf.open(io.BytesIO(data))
    mdoc = pymupdf.open(stream=data, filetype="pdf")
    n_pages = len(pdf.pages)
    pages_out = []
    for i in range(min(n_pages, max_pages)):
        pobj = pdf.pages[i].obj
        media = _box(pobj, "/MediaBox") or (0, 0, 612, 792)
        crop = _box(pobj, "/CropBox")
        trim_box = _box(pobj, "/TrimBox")
        bleed_box = _box(pobj, "/BleedBox")
        art_box = _box(pobj, "/ArtBox")
        rotation = int(pobj.get("/Rotate", 0)) % 360

        geo = extract_page_geometry(pdf, i)
        cls = classify(geo, media)
        marks_rect = trim_from_marks(cls.trim_marks, media)

        mpage = mdoc[i]
        text, text_art_boxes, spans = _text_boxes(mpage)
        callouts = find_callouts(text)

        vector_art = [p for p in cls.art if p.fill or p.stroke]
        page_area = (media[2] - media[0]) * (media[3] - media[1])
        img_area = sum(max(0, (im.bbox[2] - im.bbox[0]) * (im.bbox[3] - im.bbox[1])) for im in geo.images)
        raster_only = (not cls.cut and not cls.crease and len(vector_art) <= 2
                       and img_area >= 0.6 * page_area and not text.strip())

        art = art_extents(cls, geo, text_art_boxes, media)
        clean_png, zoom = _render(data, i, None, PREVIEW_LONG_SIDE)
        if raster_only:
            art = _pixel_content_bbox(clean_png, zoom, media) or art

        facts = PageFacts(media=media, trim_box=trim_box, bleed_box=bleed_box, trim_marks_rect=marks_rect,
                          classified=cls, art_bbox=art, callouts=callouts, text=text, raster_only=raster_only)
        res = compute(facts, product_type)

        from .dimensions import best_trim  # local to avoid cycle in type checkers
        trim_rect, _, _ = best_trim(facts)
        overlays = {
            "cut": [s for p in cls.cut for s in p.segments],
            "crease": [s for p in cls.crease for s in p.segments],
            "marks": cls.trim_marks,
            "trim": trim_rect,
            "art": art,
        }
        overlay_png, _ = _render(data, i, overlays, PREVIEW_LONG_SIDE)
        view_jpg, _ = _render(data, i, None, VIEW_LONG_SIDE, "jpeg")
        mw_pt, mh_pt = media[2] - media[0], media[3] - media[1]

        def TL(x, y):  # PDF points (y up) -> page points from the top-left (y down)
            return round(x - media[0], 2), round(media[3] - y, 2)

        def seg_out(s):
            a, b = TL(s.x0, s.y0), TL(s.x1, s.y1)
            if s.curve and s.ctrl:
                c1, c2 = TL(s.ctrl[0], s.ctrl[1]), TL(s.ctrl[2], s.ctrl[3])
                return [*a, *b, *c1, *c2]
            return [*a, *b]

        def rect_out(r):
            if r is None:
                return None
            a, b = TL(r[0], r[3]), TL(r[2], r[1])
            return [a[0], a[1], round(b[0] - a[0], 2), round(b[1] - a[1], 2)]

        result_dict = annotations_to_view(res.as_dict(), media)
        cut_segs = overlays["cut"][:MAX_OVERLAY_SEGMENTS]
        crease_segs = overlays["crease"][:MAX_OVERLAY_SEGMENTS]
        view = {
            "image_b64": base64.b64encode(view_jpg).decode(),
            "image_type": "image/jpeg",
            "width_pt": round(mw_pt, 2),
            "height_pt": round(mh_pt, 2),
            "overlay": {
                "cut": [seg_out(s) for s in cut_segs],
                "crease": [seg_out(s) for s in crease_segs],
                "marks": [seg_out(s) for s in cls.trim_marks],
                "trim": rect_out(trim_rect),
                "art": rect_out(art),
                "truncated": len(overlays["cut"]) > MAX_OVERLAY_SEGMENTS or len(overlays["crease"]) > MAX_OVERLAY_SEGMENTS,
            },
        }

        def ins(r):
            return None if r is None else {"width": round((r[2] - r[0]) / PT_PER_IN, 4),
                                           "height": round((r[3] - r[1]) / PT_PER_IN, 4)}

        cut_bb = union_bbox(cls.cut)
        fold_segs, fold_src = _fold_segments(facts)
        origin = cut_bb or media
        fold_lines = {
            "source": fold_src,
            "origin": "bottom-left of dieline bounds" if cut_bb else "bottom-left of page",
            "vertical_x": [round((c[0] - origin[0]) / PT_PER_IN, 4) for c in _clusters(fold_segs, "v")],
            "horizontal_y": [round((c[0] - origin[1]) / PT_PER_IN, 4) for c in _clusters(fold_segs, "h")],
        }
        pages_out.append({
            "page": i + 1,
            "rotation": rotation,
            "boxes_in": {"media": ins(media), "crop": ins(crop), "trim": ins(trim_box),
                         "bleed": ins(bleed_box), "art": ins(art_box)},
            "detected": {
                "dieline_source": cls.dieline_source,
                "dieline_names": cls.dieline_names,
                "cut_paths": len(cls.cut),
                "crease_paths": len(cls.crease),
                "dieline_bbox_in": ins(cut_bb),
                "trim_marks": len(cls.trim_marks),
                "trim_marks_rect_in": ins(marks_rect),
                "art_bbox_in": ins(art),
                "spot_colors": sorted(geo.spot_colors),
                "layers": sorted(geo.layers),
                "hidden_layers": sorted(geo.layers & geo.hidden_layers),
                "images": len(geo.images),
                "raster_only": raster_only,
                "content_truncated": geo.truncated,
            },
            "fold_lines_in": fold_lines,
            "callouts": [c.as_dict() for c in callouts],
            "text_sample": text[:1500],
            "notes_from_classifier": cls.notes,
            "result": result_dict,
            "view": view,
            "_overlay_png_b64": base64.b64encode(overlay_png).decode(),
            "_clean_png": clean_png,
            "_facts": facts,
        })
    mdoc.close()
    pdf.close()
    return {"page_count": n_pages, "pages_analyzed": len(pages_out), "pages": pages_out}


def strip_private(result: dict) -> dict:
    """Remove raw bytes / internal objects before returning JSON."""
    for p in result.get("pages", []):
        p.pop("_clean_png", None)
        p.pop("_overlay_png_b64", None)
        p.pop("_facts", None)
    return result


def analyze(data: bytes, product_type: str | None = None, use_ai: bool = True, ai_pages: int = 2) -> dict:
    """Measurement pass, then (optionally) Claude review per page. When the
    product type was auto-detected and Claude identifies a different one, the
    deterministic rules are re-run with Claude's type so the numbers still come
    from geometry."""
    result = analyze_pdf(data, product_type)
    if use_ai:
        from .ai import review_page
        for page in result["pages"][:ai_pages]:
            try:
                review = review_page(page, product_type)
            except Exception as e:  # network, auth, rate limit: keep the measured result
                review = {"error": f"AI review unavailable: {type(e).__name__}: {e}"}
            page["ai_review"] = review
            ai_type = review.get("product_type")
            if (not product_type and ai_type in PRODUCT_TYPES
                    and ai_type != page["result"]["product_type"]):
                redo = compute(page["_facts"], ai_type)
                redo.product_type_source = "identified by AI from the artwork"
                page["result_rules_guess"] = page["result"]
                page["result"] = annotations_to_view(redo.as_dict(), page["_facts"].media)
            elif not product_type and ai_type == page["result"]["product_type"]:
                page["result"]["product_type_source"] += "; confirmed by AI"
    return strip_private(result)
