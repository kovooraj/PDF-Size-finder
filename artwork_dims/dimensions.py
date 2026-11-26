"""Product-specific dimension rules, working from measured PDF geometry.

Conventions (inches internally):
  * Pouch            Width x Height (+ Gusset). Gusset = full unfolded bottom
                     gusset depth (industry "W x H x G").
  * Folding carton   Flat (unfolded) size, plus L x W x D: L x W = opening
                     (L the longer side), D = depth (distance between the top and
                     bottom score lines of the body).
  * Roll label       Width x Height of one label (die-cut shape).
  * Commercial print Finished (trim) Width x Height.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .classify import PT_PER_IN, Classified, union_bbox
from .geometry import PathRec, Rect, Segment
from .textdims import Callout, labelled_dims, matching_spec

PRODUCT_TYPES = ("pouch", "folding_carton", "roll_label", "commercial")


@dataclass
class Field:
    value_in: float
    source: str
    confidence: str  # high | medium | low

    def as_dict(self):
        return {"value_in": round(self.value_in, 4), "value_mm": round(self.value_in * 25.4, 2),
                "source": self.source, "confidence": self.confidence}


@dataclass
class PageFacts:
    media: Rect
    trim_box: Rect | None
    bleed_box: Rect | None
    trim_marks_rect: Rect | None
    classified: Classified
    art_bbox: Rect | None
    callouts: list[Callout]
    text: str
    raster_only: bool


@dataclass
class DimResult:
    product_type: str
    product_type_source: str
    fields: dict[str, Field] = field(default_factory=dict)
    size_basis: dict | None = None
    panels: dict | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    # Where to draw each dimension on the preview: PDF points, y up. See ann().
    annotations: list[dict] = field(default_factory=list)
    # A question the page should ask the user before the result is final (e.g. pouch gusset depth).
    prompt: dict | None = None

    def ann(self, key: str, a, b, side: str, lane: int = 0, primary: bool = True, transposed: bool = False,
            value_in: float | None = None, label: str | None = None):
        """a, b: (u, v) points; when `transposed`, u/v are swapped back to x/y.
        side: which side of the measured edge the dimension line sits on ('top', 'bottom', 'left',
        'right' in u/v space). lane: stacking order when several dimensions share a side."""
        if transposed:
            a, b = (a[1], a[0]), (b[1], b[0])
            side = {"top": "right", "bottom": "left", "right": "top", "left": "bottom"}[side]
        self.annotations.append({"key": key, "label": label or ANN_LABEL.get(key, key), "x1": a[0], "y1": a[1],
                                 "x2": b[0], "y2": b[1], "side": side, "lane": lane, "primary": primary,
                                 "value_in": value_in})

    def as_dict(self):
        anns = []
        for an in self.annotations:
            v = an["value_in"] if an["value_in"] is not None else (
                self.fields[an["key"]].value_in if an["key"] in self.fields else None)
            if v is not None:
                anns.append({**an, "value_in": round(v, 4)})
        return {
            "annotations": anns,
            "prompt": self.prompt,
            "product_type": self.product_type,
            "product_type_source": self.product_type_source,
            "fields": {k: v.as_dict() for k, v in self.fields.items()},
            "size_basis": self.size_basis,
            "panels": self.panels,
            "notes": self.notes,
            "warnings": self.warnings,
            "questions": self.questions,
        }


ANN_LABEL = {"width": "W", "height": "H", "gusset": "G", "length": "L", "depth": "D",
             "flat_width": "Flat W", "flat_height": "Flat H"}


def _ann_rect(res: "DimResult", r: Rect, wkey="width", hkey="height", primary=True, lane=0):
    """Width under the rectangle, height to its left."""
    res.ann(wkey, (r[0], r[1]), (r[2], r[1]), "bottom", lane, primary)
    res.ann(hkey, (r[0], r[1]), (r[0], r[3]), "left", lane, primary)


def _w(r: Rect) -> float:
    return (r[2] - r[0]) / PT_PER_IN


def _h(r: Rect) -> float:
    return (r[3] - r[1]) / PT_PER_IN


def _same_rect(a: Rect | None, b: Rect | None, tol: float = 1.0) -> bool:
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def _close(a: float, b: float, rel: float = 0.03, abs_in: float = 0.04) -> bool:
    return abs(a - b) <= max(abs_in, rel * max(a, b))


# --------------------------------------------------------------------------- size basis

def best_trim(f: PageFacts) -> tuple[Rect, str, str]:
    if f.trim_box and not _same_rect(f.trim_box, f.media):
        return f.trim_box, "PDF TrimBox", "high"
    if f.trim_marks_rect:
        return f.trim_marks_rect, "crop/trim marks", "high"
    cut = union_bbox(f.classified.cut)
    if cut and (cut[2] - cut[0]) > 18 and (cut[3] - cut[1]) > 18:
        return cut, "dieline (outer cut line)", "high" if f.classified.dieline_source == "spot" else "medium"
    if f.art_bbox and not f.raster_only:
        mw, mh = _w(f.media), _h(f.media)
        aw, ah = _w(f.art_bbox), _h(f.art_bbox)
        if aw < mw - 0.1 or ah < mh - 0.1:
            return f.art_bbox, "artwork extents (page has white space around the art)", "low"
    return f.media, "page size (no trim box, crop marks or dieline found)", "medium"


def whitespace_report(f: PageFacts, trim: Rect) -> dict | None:
    if not f.art_bbox:
        return None
    a = f.art_bbox

    def margins(outer: Rect):
        return {
            "left_in": round((a[0] - outer[0]) / PT_PER_IN, 3),
            "right_in": round((outer[2] - a[2]) / PT_PER_IN, 3),
            "bottom_in": round((a[1] - outer[1]) / PT_PER_IN, 3),
            "top_in": round((outer[3] - a[3]) / PT_PER_IN, 3),
        }

    return {
        "art_bbox_in": {"width": round(_w(a), 4), "height": round(_h(a), 4)},
        "margins_to_page": margins(f.media),
        "margins_to_trim": margins(trim),   # negative = art bleeds past trim
    }


# --------------------------------------------------------------------------- fold-line helpers

def _fold_segments(f: PageFacts) -> tuple[list[Segment], str]:
    """Straight lines that can be score / fold lines: every crease line plus every interior
    cut-colour line. Many dielines draw the score lines in the same colour (or layer) as the
    cut, and a 'Crease' swatch may only appear in a legend, so trusting the crease colour
    alone misses the panels. Short slits and legend swatches drop out later because panel
    lines must span the body."""
    c = f.classified
    crease = [s for p in c.crease for s in p.segments if s.orientation() in ("h", "v") and s.length > 2]
    outer = union_bbox(c.cut)
    inner = []
    if outer:
        for p in c.cut:
            for s in p.segments:
                o = s.orientation()
                if o == "v" and outer[0] + 3 < s.x0 < outer[2] - 3 and s.length > 18:
                    inner.append(s)
                elif o == "h" and outer[1] + 3 < s.y0 < outer[3] - 3 and s.length > 18:
                    inner.append(s)
    if crease and not inner:
        return crease, "crease/fold lines"
    if inner and not crease:
        return inner, "interior dieline lines (no separate crease colour; verify)"
    return crease + inner, "dieline score and fold lines"


def _clusters(segs: list[Segment], orient: str, tol: float = 1.5):
    """Group parallel fold lines by position. Returns list of
    (pos, total_len, lo, hi) sorted by pos."""
    items = []
    for s in segs:
        if s.orientation() != orient:
            continue
        if orient == "v":
            items.append(((s.x0 + s.x1) / 2, s.length, min(s.y0, s.y1), max(s.y0, s.y1)))
        else:
            items.append(((s.y0 + s.y1) / 2, s.length, min(s.x0, s.x1), max(s.x0, s.x1)))
    items.sort()
    out: list[list] = []
    for pos, ln, lo, hi in items:
        if out and abs(pos - out[-1][0]) <= tol:
            g = out[-1]
            n = g[4] + 1
            g[0] = (g[0] * g[4] + pos) / n
            g[1] += ln
            g[2] = min(g[2], lo)
            g[3] = max(g[3], hi)
            g[4] = n
        else:
            out.append([pos, ln, lo, hi, 1])
    return [(g[0], g[1], g[2], g[3]) for g in out]


def _alternating_panels(bounds: list[float]):
    """Find 4 consecutive panels a,b,a,b (the body of a carton / side-gusset
    pouch). bounds in points. Returns (start_idx, a_in, b_in) or None."""
    gaps = [(bounds[i + 1] - bounds[i]) / PT_PER_IN for i in range(len(bounds) - 1)]
    best = None
    for i in range(len(gaps) - 3):
        a1, b1, a2, b2 = gaps[i:i + 4]
        if min(a1, b1, a2, b2) < 0.2:
            continue
        if _close(a1, a2) and _close(b1, b2):
            total = a1 + b1 + a2 + b2
            if best is None or total > best[3]:
                best = (i, (a1 + a2) / 2, (b1 + b2) / 2, total)
    if best is None:
        return None
    return best[0], best[1], best[2], gaps


def _transpose(seg: Segment) -> Segment:
    return Segment(seg.y0, seg.x0, seg.y1, seg.x1, seg.curve)


def _tr(r: Rect) -> Rect:
    return (r[1], r[0], r[3], r[2])


def _interior_lines(f: PageFacts) -> list[Segment]:
    """Every straight dieline line (cut or crease, solid or dashed). Pouch dielines often draw
    the panel / gusset edges solid and only the gusset centre (or zipper) dashed."""
    c = f.classified
    return [s for p in c.crease + c.cut for s in p.segments if s.orientation() in ("h", "v") and s.length > 2]


def _pouch_layout(f: PageFacts) -> dict | None:
    """Front | bottom gusset | back on one sheet, stacked or side by side.

    Looks for two interior lines b1 < b2 across the sheet with front (edge -> b1) ~ back
    (b2 -> edge) and a smaller gusset between them; a centre fold inside the gusset is a
    strong confirming sign. Without a gusset pair, a single centre line means a lay-flat
    pouch folded at the bottom."""
    flat = union_bbox(f.classified.cut)
    if not flat:
        return None
    segs = _interior_lines(f)
    best = None
    fold = None
    for transposed in (False, True):
        ss = [_transpose(sg) for sg in segs] if transposed else segs
        fl = _tr(flat) if transposed else flat
        across = fl[2] - fl[0]
        lo, hi = fl[1], fl[3]
        pos = [cl[0] for cl in _clusters(ss, "h")
               if (cl[3] - cl[2]) >= 0.5 * across and cl[1] >= 0.5 * across and lo + 3 < cl[0] < hi - 3]
        for i, b1 in enumerate(pos):
            for b2 in pos[i + 1:]:
                front, back, g = (b1 - lo) / PT_PER_IN, (hi - b2) / PT_PER_IN, (b2 - b1) / PT_PER_IN
                if g < 0.25 or g >= 0.9 * min(front, back) or not _close(front, back):
                    continue
                mid = (b1 + b2) / 2
                centre = any(abs(q - mid) <= max(1.5, 0.03 * (b2 - b1)) for q in pos if b1 < q < b2)
                score = (2.0 if centre else 0.0) - abs(front - back) / max(front, back)
                if best is None or score > best["score"]:
                    best = {"kind": "gusset", "transposed": transposed, "bounds": [lo, b1, b2, hi], "fl": fl,
                            "height": (front + back) / 2, "gusset": g, "across": across / PT_PER_IN,
                            "centre": centre, "score": score}
        mid = (lo + hi) / 2
        for q in pos:
            if fold is None and abs(q - mid) <= max(1.5, 0.01 * (hi - lo)):
                fold = {"kind": "fold", "transposed": transposed, "bounds": [lo, q, hi], "fl": fl,
                        "height": (q - lo) / PT_PER_IN, "across": across / PT_PER_IN, "score": -1.0}
    return best or fold


def _outline_is_rectangular(f: PageFacts) -> bool:
    """Pouch, label and print outlines fill their bounding box; a carton's flaps don't."""
    flat = union_bbox(f.classified.cut)
    if not flat:
        return False
    tol = 1.5
    on_edge = 0.0
    for p in f.classified.cut:
        for sg in p.segments:
            o = sg.orientation()
            if o == "h" and (abs(sg.y0 - flat[1]) <= tol or abs(sg.y0 - flat[3]) <= tol):
                on_edge += sg.length
            elif o == "v" and (abs(sg.x0 - flat[0]) <= tol or abs(sg.x0 - flat[2]) <= tol):
                on_edge += sg.length
    perimeter = 2 * ((flat[2] - flat[0]) + (flat[3] - flat[1]))
    return perimeter > 0 and on_edge / perimeter >= 0.85


def _find_carton_body(f: PageFacts) -> dict | None:
    """Four alternating body panels L, W, L, W between score lines, plus the depth."""
    c = f.classified
    flat = union_bbox(c.cut + c.crease)
    if not flat:
        return None
    segs, fold_src = _fold_segments(f)
    found = None
    for transposed in (False, True):
        ss = [_transpose(sg) for sg in segs] if transposed else segs
        fl = _tr(flat) if transposed else flat
        fh = fl[3] - fl[1]
        # a panel line needs real length: aligned tuck-flap slits at the top and bottom of the
        # sheet span the height but add up to almost nothing
        vcl = [cl for cl in _clusters(ss, "v") if (cl[3] - cl[2]) >= 0.2 * fh and cl[1] >= 0.2 * fh]
        bounds = sorted({fl[0], *[cl[0] for cl in vcl], fl[2]})
        alt = _alternating_panels(bounds)
        if not alt:
            continue
        i, a, b, gaps = alt
        body_x0, body_x1 = bounds[i], bounds[i + 4]
        hcl = [cl for cl in _clusters(ss, "h")
               if min(cl[3], body_x1) - max(cl[2], body_x0) >= 0.4 * (body_x1 - body_x0)]
        hcl.sort(key=lambda cl: -cl[1])
        depth = abs(hcl[0][0] - hcl[1][0]) / PT_PER_IN if len(hcl) >= 2 else None
        cand = (a + b) * 2
        if found is None or cand > found["score"]:
            found = {"score": cand, "a": a, "b": b, "depth": depth, "gaps": gaps, "i": i,
                     "transposed": transposed, "bounds": bounds, "fl": fl, "fold_src": fold_src,
                     "depth_at": (hcl[0][0], hcl[1][0]) if len(hcl) >= 2 else None}
    return found


# --------------------------------------------------------------------------- products

def folding_carton(f: PageFacts, res: DimResult):
    c = f.classified
    flat = union_bbox(c.cut + c.crease)
    if not flat:
        trim, src, conf = best_trim(f)
        res.fields["flat_width"] = Field(_w(trim), src, "low")
        res.fields["flat_height"] = Field(_h(trim), src, "low")
        _ann_rect(res, trim, "flat_width", "flat_height")
        res.warnings.append("No dieline found. A folding carton needs its dieline to get L x W x D; "
                            "ask the customer for the dieline or the structural spec.")
        res.questions.append("Can the customer supply the carton dieline (or the L x W x D and style, e.g. reverse tuck end)?")
        return
    src = "dieline outer bounds"
    conf = "high" if c.dieline_source == "spot" else "medium"
    res.fields["flat_width"] = Field(_w(flat), src, conf)
    res.fields["flat_height"] = Field(_h(flat), src, conf)
    _ann_rect(res, flat, "flat_width", "flat_height", primary=False)

    found = _find_carton_body(f)
    fold_src = found["fold_src"] if found else ""
    if not found:
        res.warnings.append("Could not find the 4 alternating body panels (L, W, L, W) in the fold lines. "
                            "This may be a tray, sleeve, mailer or a multi-up layout; flat size is reliable, "
                            "L x W x D needs confirming.")
        res.questions.append("What carton style is this (reverse tuck end, straight tuck end, auto-bottom, tray, sleeve)?")
        return
    L, W = max(found["a"], found["b"]), min(found["a"], found["b"])
    fconf = "high" if fold_src in ("crease/fold lines", "dieline score and fold lines") and c.dieline_source == "spot" else "medium"
    res.fields["length"] = Field(L, f"body panel width ({fold_src})", fconf)
    res.fields["width"] = Field(W, f"body panel width ({fold_src})", fconf)
    if found["depth"]:
        res.fields["depth"] = Field(found["depth"], f"distance between body score lines ({fold_src})", fconf)
    else:
        res.warnings.append("Could not find the top and bottom score lines of the body, so depth is unknown.")
    gaps = found["gaps"]
    i = found["i"]
    bnd, fl, tp = found["bounds"], found["fl"], found["transposed"]
    first_key = "length" if found["a"] >= found["b"] else "width"
    second_key = "width" if first_key == "length" else "length"
    res.ann(first_key, (bnd[i], fl[3]), (bnd[i + 1], fl[3]), "top", transposed=tp)
    res.ann(second_key, (bnd[i + 1], fl[3]), (bnd[i + 2], fl[3]), "top", transposed=tp)
    if found["depth_at"]:
        y1, y2 = sorted(found["depth_at"])
        res.ann("depth", (fl[2], y1), (fl[2], y2), "right", transposed=tp)
    res.panels = {
        "orientation": "panels run vertically" if not found["transposed"] else "panels run horizontally",
        "panel_widths_in": [round(g, 4) for g in gaps],
        "body_panels_index": [i, i + 3],
        "glue_flap_in": round(gaps[i - 1], 4) if i > 0 else (round(gaps[i + 4], 4) if i + 4 < len(gaps) else None),
    }
    res.notes.append("Panel sizes are measured on the score lines. Allow for board caliper: panels that should "
                     "match can differ by about one board thickness.")


def pouch(f: PageFacts, res: DimResult):
    lab = labelled_dims(f.callouts)
    trim, tsrc, tconf = best_trim(f)
    segs, fold_src = _fold_segments(f)
    flat = union_bbox(f.classified.cut) or trim
    fw, fh = _w(flat), _h(flat)

    # Side-gusset pouch: front | gusset | back | gusset
    vcl = [cl for cl in _clusters(segs, "v")
           if (cl[3] - cl[2]) >= 0.5 * (flat[3] - flat[1]) and cl[1] >= 0.5 * (flat[3] - flat[1])]
    sg_bounds = sorted({flat[0], *[cl[0] for cl in vcl], flat[2]})
    alt = _alternating_panels(sg_bounds) if vcl else None
    if alt:
        k, a, b, gaps = alt
        res.ann("width" if a >= b else "gusset", (sg_bounds[k], flat[3]), (sg_bounds[k + 1], flat[3]), "top")
        res.ann("gusset" if a >= b else "width", (sg_bounds[k + 1], flat[3]), (sg_bounds[k + 2], flat[3]), "top")
        res.ann("height", (flat[2], flat[1]), (flat[2], flat[3]), "right")
        res.fields["width"] = Field(max(a, b), f"front panel ({fold_src})", "medium")
        res.fields["height"] = Field(fh, "dieline height", "medium")
        res.fields["gusset"] = Field(min(a, b), f"side gusset panel ({fold_src})", "medium")
        res.notes.append("Layout looks like a side-gusset pouch (front / gusset / back / gusset).")
    else:
        # Front | bottom gusset | back on one sheet, side by side (Print Monkey's setup guide)
        # or stacked. Panel edges are often solid and only the gusset centre dashed.
        layout = _pouch_layout(f)
        src = "dieline panel and gusset lines"
        if layout:
            tp, bnd, fl = layout["transposed"], layout["bounds"], layout["fl"]
            res.ann("height", (fl[2], bnd[0]), (fl[2], bnd[1]), "right", transposed=tp)
            if layout["kind"] == "gusset":
                res.ann("gusset", (fl[2], bnd[1]), (fl[2], bnd[2]), "right", transposed=tp)
            res.ann("width", (fl[0], fl[3]), (fl[2], fl[3]), "top", transposed=tp)
            res.ann("flat", (fl[0], fl[1]), (fl[0], fl[3]), "left", primary=False, transposed=tp,
                    value_in=(fl[3] - fl[1]) / PT_PER_IN, label="Flat")
        if layout and layout["kind"] == "gusset":
            way = "side by side" if layout["transposed"] else "stacked"
            res.fields["width"] = Field(layout["across"], "dieline, across the panels", "medium")
            res.fields["height"] = Field(layout["height"], f"front/back panel ({src})", "medium")
            res.fields["gusset"] = Field(layout["gusset"], f"bottom gusset between the panels ({src})", "medium")
            res.notes.append(f"Layout looks like front panel / bottom gusset / back panel ({way})"
                             + ("; the gusset has a centre fold" if layout.get("centre") else "")
                             + ". The gusset is the full unfolded depth.")
        elif layout and layout["kind"] == "fold":
            res.fields["width"] = Field(layout["across"], "dieline, across the panels", "medium")
            res.fields["height"] = Field(layout["height"], "half of the folded layout", "medium")
            res.notes.append("Single centre fold and no gusset found: front and back on one sheet, folded at the bottom.")
        else:
            res.fields["width"] = Field(_w(trim), tsrc, tconf)
            res.fields["height"] = Field(_h(trim), tsrc, tconf)
            _ann_rect(res, trim)
            if not lab.get("gusset") and not lab.get("side_gusset"):
                # No gusset or fold lines in the file: the page asks for the gusset depth and works out
                # W x H (x G) from the printed sheet. 0 = lay-flat pouch (front and back on one sheet).
                sw, sh = _w(trim), _h(trim)
                ratio = max(sw, sh) / max(min(sw, sh), 1e-6)
                res.prompt = {
                    "type": "pouch_gusset",
                    "sheet_w_in": round(sw, 4), "sheet_h_in": round(sh, 4),
                    "sheet_source": tsrc, "sheet_confidence": tconf,
                    # front + back side by side is the normal print layout; a near-square sheet is
                    # more likely a single panel
                    "suggested_layout": "front_back" if ratio >= 1.5 else "single",
                }

    # A printed spec that matches the measurement confirms it; then labels can't override it.
    measured = [res.fields[k].value_in for k in ("width", "height", "gusset") if k in res.fields]
    if len(measured) >= 2 and matching_spec(f.callouts, measured):
        return
    # Otherwise printed labelled callouts override geometry heuristics
    for key in ("width", "height", "gusset"):
        if key in lab:
            prev = res.fields.get(key)
            if prev and not _close(prev.value_in, lab[key], rel=0.02):
                res.warnings.append(f"Printed {key} callout {lab[key]:.3f}\" differs from measured "
                                    f"{prev.value_in:.3f}\"; using the callout.")
            res.fields[key] = Field(lab[key], "dimension callout printed on the dieline", "high")
    if "side_gusset" in lab:
        res.fields["gusset"] = Field(lab["side_gusset"], "side gusset callout", "high")
    if f.raster_only and tsrc.startswith("page size"):
        res.warnings.append("The file is a flat image (no vector dieline), so sizes come from the page size or text only.")


def roll_label(f: PageFacts, res: DimResult):
    cut_closed = [p for p in f.classified.cut if p.closed or len(p.segments) >= 3]
    if cut_closed:
        sizes = Counter((round(_w(p.bbox), 3), round(_h(p.bbox), 3)) for p in cut_closed
                        if _w(p.bbox) > 0.2 and _h(p.bbox) > 0.2)
        if sizes:
            (w, h), n = sizes.most_common(1)[0]
            conf = "high" if f.classified.dieline_source == "spot" else "medium"
            res.fields["width"] = Field(w, "die-cut line of one label", conf)
            res.fields["height"] = Field(h, "die-cut line of one label", conf)
            shape_paths = [p for p in cut_closed if (round(_w(p.bbox), 3), round(_h(p.bbox), 3)) == (w, h)]
            _ann_rect(res, shape_paths[0].bbox)
            res.notes.append(f"Label shape: {_shape(shape_paths[0], w, h)}.")
            if n > 1:
                res.notes.append(f"{n} identical labels on the sheet (multi-up layout); size is for one label.")
            return
    trim, src, conf = best_trim(f)
    res.fields["width"] = Field(_w(trim), src, conf)
    res.fields["height"] = Field(_h(trim), src, conf)
    _ann_rect(res, trim)
    if f.classified.dieline_source == "none":
        res.questions.append("No die line in the file. Is the label a rectangle with square corners, or does it need a custom die shape?")


def _shape(p: PathRec, w: float, h: float) -> str:
    curves = sum(1 for s in p.segments if s.curve)
    lines = sum(1 for s in p.segments if not s.curve and s.length > 0.5)
    if curves and not lines:
        return "circle" if _close(w, h, 0.02, 0.02) else "oval"
    if curves and lines:
        return "rounded-corner rectangle"
    if lines == 4:
        return "square" if _close(w, h, 0.01, 0.01) else "rectangle"
    return "custom shape"


def commercial(f: PageFacts, res: DimResult):
    trim, src, conf = best_trim(f)
    res.fields["width"] = Field(_w(trim), src, conf)
    res.fields["height"] = Field(_h(trim), src, conf)
    _ann_rect(res, trim)
    if f.bleed_box and not _same_rect(f.bleed_box, trim):
        b = (trim[0] - f.bleed_box[0]) / PT_PER_IN
        res.notes.append(f"BleedBox gives about {b:.3f}\" bleed.")
    elif f.art_bbox and src != "artwork extents (page has white space around the art)":
        a = f.art_bbox
        bleed = min(trim[0] - a[0], trim[1] - a[1], a[2] - trim[2], a[3] - trim[3]) / PT_PER_IN
        if bleed > 0.01:
            res.notes.append(f"Artwork bleeds about {bleed:.3f}\" past the trim on every side.")
        elif bleed < -0.01 and src in ("crop/trim marks", "PDF TrimBox"):
            res.notes.append("Artwork does not reach the trim on every side (white border or no bleed).")


# --------------------------------------------------------------------------- dispatcher

POUCH_WORDS = re.compile(r"\bpouch|gusset|zipper|zip\s*lock|tear\s*notch|hang\s*hole|stand.?up|seal\s+area", re.I)
CARTON_WORDS = re.compile(r"\btuck|glue\s*flap|dust\s*flap|carton|crash\s*lock|auto.?bottom|caliper", re.I)
LABEL_WORDS = re.compile(r"\blabel|cut\s*contour|kiss\s*cut|unwind|core\b|liner", re.I)


def guess_product(f: PageFacts) -> str:
    c = f.classified
    if POUCH_WORDS.search(f.text):
        return "pouch"
    if CARTON_WORDS.search(f.text):
        return "folding_carton"
    if c.cut:
        # Shape first: a symmetric front / gusset / back layout is a pouch; four alternating
        # body panels inside a flap-shaped (non-rectangular) outline is a carton.
        layout = _pouch_layout(f)
        rectangular = _outline_is_rectangular(f)
        if layout and layout["kind"] == "gusset":
            return "pouch"
        if not rectangular and _find_carton_body(f):
            return "folding_carton"
        if layout and rectangular:
            return "pouch"
        body = None if layout else _find_carton_body(f)
        if body and abs(body["a"] - body["b"]) > 0.05 * max(body["a"], body["b"]):
            # A full-rectangle die (flaps as wide as the panels) is still a carton when the
            # body shows two different alternating panel widths and nothing looks like a pouch.
            return "folding_carton"
        if c.crease and not rectangular:
            return "folding_carton"
    names = " ".join(c.dieline_names)
    if LABEL_WORDS.search(f.text) or re.search(r"contour|kiss", names, re.I):
        return "roll_label"
    if c.cut:
        cut = union_bbox(c.cut)
        if cut and _w(cut) <= 8 and _h(cut) <= 8:
            return "roll_label"
    return "commercial"


MAIN_FIELDS = {"pouch": ("width", "height", "gusset"), "folding_carton": ("length", "width", "depth"),
               "roll_label": ("width", "height"), "commercial": ("width", "height")}


def _confirm_with_printed_spec(f: PageFacts, res: DimResult):
    keys = [k for k in MAIN_FIELDS[res.product_type] if k in res.fields]
    if len(keys) < 2:
        return
    spec = matching_spec(f.callouts, [res.fields[k].value_in for k in keys])
    if spec:
        for k in keys:
            fld = res.fields[k]
            fld.confidence = "high"
            fld.source += f"; matches printed spec '{spec.text}'"
        res.notes.append(f"Measured size matches the size printed in the file ('{spec.text}').")
    flat_keys = [k for k in ("flat_width", "flat_height") if k in res.fields]
    if flat_keys:
        flat = matching_spec(f.callouts, [res.fields[k].value_in for k in flat_keys])
        if flat:
            for k in flat_keys:
                res.fields[k].confidence = "high"


def compute(f: PageFacts, product_type: str | None) -> DimResult:
    if product_type in PRODUCT_TYPES:
        res = DimResult(product_type, "selected by user")
    else:
        res = DimResult(guess_product(f), "auto-detected from file (rules)")
    {"pouch": pouch, "folding_carton": folding_carton, "roll_label": roll_label,
     "commercial": commercial}[res.product_type](f, res)

    _confirm_with_printed_spec(f, res)
    trim, src, conf = best_trim(f)
    res.size_basis = {
        "trim_source": src,
        "trim_in": {"width": round(_w(trim), 4), "height": round(_h(trim), 4)},
        "page_in": {"width": round(_w(f.media), 4), "height": round(_h(f.media), 4)},
        "whitespace": whitespace_report(f, trim),
    }
    ws = res.size_basis["whitespace"]
    if ws and src.startswith("page size"):
        m = ws["margins_to_page"]
        if min(m.values()) > 0.25:
            res.warnings.append("The art sits inside a larger page with white space all round. The page size is "
                                "probably not the product size; check the artwork extents.")
    if src.startswith("artwork extents"):
        res.warnings.append("Size is taken from the artwork extents because the file has no trim box, crop marks "
                            "or dieline. Confirm with the customer.")
    return res
