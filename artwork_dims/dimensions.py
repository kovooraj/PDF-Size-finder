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

    def as_dict(self):
        return {
            "product_type": self.product_type,
            "product_type_source": self.product_type_source,
            "fields": {k: v.as_dict() for k, v in self.fields.items()},
            "size_basis": self.size_basis,
            "panels": self.panels,
            "notes": self.notes,
            "warnings": self.warnings,
            "questions": self.questions,
        }


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
    c = f.classified
    segs = [s for p in c.crease for s in p.segments if s.orientation() in ("h", "v") and s.length > 2]
    if segs:
        return segs, "crease/fold lines"
    # No separate crease colour: interior straight cut-colour lines are usually folds.
    outer = union_bbox(c.cut)
    if not outer:
        return [], "none"
    inner = []
    for p in c.cut:
        for s in p.segments:
            o = s.orientation()
            if o == "v" and outer[0] + 3 < s.x0 < outer[2] - 3 and s.length > 18:
                inner.append(s)
            elif o == "h" and outer[1] + 3 < s.y0 < outer[3] - 3 and s.length > 18:
                inner.append(s)
    return inner, "interior dieline lines (no separate crease colour - verify)"


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


# --------------------------------------------------------------------------- products

def folding_carton(f: PageFacts, res: DimResult):
    c = f.classified
    flat = union_bbox(c.cut + c.crease)
    if not flat:
        trim, src, conf = best_trim(f)
        res.fields["flat_width"] = Field(_w(trim), src, "low")
        res.fields["flat_height"] = Field(_h(trim), src, "low")
        res.warnings.append("No dieline found. A folding carton needs its dieline to get L x W x D; "
                            "ask the customer for the dieline or the structural spec.")
        res.questions.append("Can the customer supply the carton dieline (or the L x W x D and style, e.g. reverse tuck end)?")
        return
    src = "dieline outer bounds"
    conf = "high" if c.dieline_source == "spot" else "medium"
    res.fields["flat_width"] = Field(_w(flat), src, conf)
    res.fields["flat_height"] = Field(_h(flat), src, conf)

    segs, fold_src = _fold_segments(f)
    found = None
    for transposed in (False, True):
        ss = [_transpose(s) for s in segs] if transposed else segs
        fl = _tr(flat) if transposed else flat
        fh = fl[3] - fl[1]
        fw = fl[2] - fl[0]
        vcl = [cl for cl in _clusters(ss, "v") if (cl[3] - cl[2]) >= 0.2 * fh]
        bounds = sorted({fl[0], *[cl[0] for cl in vcl], fl[2]})
        alt = _alternating_panels(bounds)
        if not alt:
            continue
        i, a, b, gaps = alt
        body_x0, body_x1 = bounds[i], bounds[i + 4]
        hcl = [cl for cl in _clusters(ss, "h")
               if min(cl[3], body_x1) - max(cl[2], body_x0) >= 0.4 * (body_x1 - body_x0)]
        hcl.sort(key=lambda cl: -cl[1])
        depth = None
        if len(hcl) >= 2:
            depth = abs(hcl[0][0] - hcl[1][0]) / PT_PER_IN
        cand = (a + b) * 2
        if found is None or cand > found["score"]:
            found = {"score": cand, "a": a, "b": b, "depth": depth, "gaps": gaps, "i": i,
                     "transposed": transposed}
    if not found:
        res.warnings.append("Could not find the 4 alternating body panels (L, W, L, W) in the fold lines. "
                            "This may be a tray, sleeve, mailer or a multi-up layout; flat size is reliable, "
                            "L x W x D needs confirming.")
        res.questions.append("What carton style is this (reverse tuck end, straight tuck end, auto-bottom, tray, sleeve)?")
        return
    L, W = max(found["a"], found["b"]), min(found["a"], found["b"])
    fconf = "high" if fold_src == "crease/fold lines" and c.dieline_source == "spot" else "medium"
    res.fields["length"] = Field(L, f"body panel width ({fold_src})", fconf)
    res.fields["width"] = Field(W, f"body panel width ({fold_src})", fconf)
    if found["depth"]:
        res.fields["depth"] = Field(found["depth"], f"distance between body score lines ({fold_src})", fconf)
    else:
        res.warnings.append("Could not find the top and bottom score lines of the body, so depth is unknown.")
    gaps = found["gaps"]
    i = found["i"]
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
    vcl = [cl for cl in _clusters(segs, "v") if (cl[3] - cl[2]) >= 0.5 * (flat[3] - flat[1])]
    alt = _alternating_panels(sorted({flat[0], *[cl[0] for cl in vcl], flat[2]})) if vcl else None
    if alt:
        _, a, b, gaps = alt
        res.fields["width"] = Field(max(a, b), f"front panel ({fold_src})", "medium")
        res.fields["height"] = Field(fh, "dieline height", "medium")
        res.fields["gusset"] = Field(min(a, b), f"side gusset panel ({fold_src})", "medium")
        res.notes.append("Layout looks like a side-gusset pouch (front / gusset / back / gusset).")
    else:
        # Front | bottom gusset | back on one sheet. Print Monkey's setup guide lays it out
        # sideways (2 x H + G wide, W tall); other suppliers stack it. Try both.
        layout = None
        for transposed in (False, True):
            ss = [_transpose(s) for s in segs] if transposed else segs
            fl = _tr(flat) if transposed else flat
            hcl = [cl for cl in _clusters(ss, "h") if (cl[3] - cl[2]) >= 0.5 * (fl[2] - fl[0])]
            bounds = sorted({fl[1], *[cl[0] for cl in hcl], fl[3]})
            gaps = [(bounds[k + 1] - bounds[k]) / PT_PER_IN for k in range(len(bounds) - 1)]
            across = (fl[2] - fl[0]) / PT_PER_IN
            if len(gaps) >= 3 and _close(gaps[0], gaps[-1]) and sum(gaps[1:-1]) < gaps[0]:
                layout = ("gusset", gaps, across, transposed)
                break
            if len(gaps) == 2 and _close(gaps[0], gaps[1]) and layout is None:
                layout = ("fold", gaps, across, transposed)
        if layout and layout[0] == "gusset":
            _, gaps, across, transposed = layout
            way = "side by side" if transposed else "stacked"
            res.fields["width"] = Field(across, "dieline, across the panels", "medium")
            res.fields["height"] = Field(gaps[0], f"front/back panel ({fold_src})", "medium")
            res.fields["gusset"] = Field(sum(gaps[1:-1]), f"bottom gusset between panels ({fold_src})", "medium")
            res.notes.append(f"Layout looks like front panel / bottom gusset / back panel ({way}). "
                             "Gusset is the full unfolded depth.")
        elif layout and layout[0] == "fold":
            _, gaps, across, _t = layout
            res.fields["width"] = Field(across, "dieline, across the panels", "medium")
            res.fields["height"] = Field(gaps[0], "half of folded layout", "medium")
            res.notes.append("Single centre fold and no gusset found: front and back on one sheet, folded at the bottom.")
        else:
            res.fields["width"] = Field(_w(trim), tsrc, tconf)
            res.fields["height"] = Field(_h(trim), tsrc, tconf)
            if not lab.get("gusset"):
                res.questions.append("Is this a stand-up pouch with a bottom gusset? If yes, what gusset depth?")

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
    if f.raster_only:
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
            res.notes.append(f"Label shape: {_shape(shape_paths[0], w, h)}.")
            if n > 1:
                res.notes.append(f"{n} identical labels on the sheet (multi-up layout); size is for one label.")
            return
    trim, src, conf = best_trim(f)
    res.fields["width"] = Field(_w(trim), src, conf)
    res.fields["height"] = Field(_h(trim), src, conf)
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
    if c.crease and c.cut:
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
