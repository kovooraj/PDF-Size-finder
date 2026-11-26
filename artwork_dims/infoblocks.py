"""Find info / legend areas on a page so they don't count towards any size.

Packaging PDFs often carry a title block or legend beside the die: customer, job and die
numbers, ink lists with colour swatches, a "Cutline / Crease / Perforation" key drawn in the
die colours, approval boxes, revision tables. None of it is the product, but it is vector art
and text, so left alone it widens the dieline bounds, the artwork extents and sometimes the
crop-mark box.

How a block is found (all in PDF points, y up):
1. Text spans are tagged when they read like spec / legend wording (KEYWORDS).
2. Spans close to each other are grouped. A group with enough tagged spans that sits clear of
   the product (the die, or the crop-mark / trim box) is an info block.
3. The block grows to take in the boxes, table rules, swatches and logos drawn around and
   inside it, as long as they stay clear of the product.

`apply_info_blocks` then moves every path, mark, image and text box inside a block out of the
measurement inputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .geometry import PageGeometry, PathRec, Rect, rect_union

KEYWORDS = re.compile(
    r"\b(customer|client|company|job\s*(no|number|name|#)?|order\s*(no|number|#)|p\.?o\.?\s*(no|number|#)|"
    r"die\s*(cut\s*)?(no|number|#)|die\s*-?\s*cut|dieline|die\s*line|legend|ink(s|\s*information)?|colou?rs?|"
    r"pantone|pms|cmyk|process\s*colou?r|spot\s*colou?r|approv\w*|signature|signed|date|revision|rev\b|"
    r"version|designer|responsible|prepared\s*by|artist|product\s*code|item\s*(no|number|code)|sku|comments?|"
    r"notes?|post\s*-?press|non\s*-?\s*print\w*|cut\s*-?\s*line|cutter|crease|fold\s*line|perf\w*|"
    r"bleed|safe(ty)?\s*(area|zone|line)?|varnish|coating|substrate|material|board|caliper|"
    r"print(ing)?\s*(process|surface|method|side)|proof|please\s*(check|review)|dimensions?|specs?|"
    r"specification|quantity|qty|finish|emboss\w*|foil|glue\s*(area|flap)?|"
    r"technical\s*drawing|drawing\s*(no|number)|scale|units?|tolerance)\b",
    re.I,
)

GAP = 25.0          # spans this close (pt) join one group (~0.35")
GROW_PAD = 18.0     # how far round the text a block may take in boxes and rules (0.25")
MIN_KEYWORDS = 3


@dataclass
class InfoBlock:
    rect: Rect
    keywords: list[str] = field(default_factory=list)
    spans: int = 0

    def as_dict(self, pt_per_in: float = 72.0) -> dict:
        return {"rect_pt": [round(v, 2) for v in self.rect], "keywords": self.keywords[:8], "text_spans": self.spans,
                "width_in": round((self.rect[2] - self.rect[0]) / pt_per_in, 3),
                "height_in": round((self.rect[3] - self.rect[1]) / pt_per_in, 3)}


def _overlap(a: Rect, b: Rect) -> float:
    """Area of a ∩ b over the area of a."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return max(0.0, w) * max(0.0, h) / area


def _inside(a: Rect, b: Rect, frac: float = 0.8) -> bool:
    if a[2] - a[0] < 0.5 or a[3] - a[1] < 0.5:  # a line: both ends inside
        return b[0] <= a[0] and a[2] <= b[2] and b[1] <= a[1] and a[3] <= b[3]
    return _overlap(a, b) >= frac


def _near(a: Rect, b: Rect, gap: float) -> bool:
    return a[0] <= b[2] + gap and b[0] <= a[2] + gap and a[1] <= b[3] + gap and b[1] <= a[3] + gap


def _expand(r: Rect, d: float) -> Rect:
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def find_info_blocks(spans: list[dict], geo: PageGeometry, media: Rect, product: Rect | None) -> list[InfoBlock]:
    """spans: [{"text", "rect"}] in PDF points. product: the die / trim area to protect, if known."""
    if not spans:
        return []
    # 1-2. group spans by proximity
    n = len(spans)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(n), key=lambda i: spans[i]["rect"][0])
    for ai, a in enumerate(order):
        ra = spans[a]["rect"]
        for b in order[ai + 1:]:
            rb = spans[b]["rect"]
            if rb[0] > ra[2] + GAP:
                break
            if _near(ra, rb, GAP):
                parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    mw, mh = media[2] - media[0], media[3] - media[1]
    blocks: list[InfoBlock] = []
    for ids in groups.values():
        kws = []
        for i in ids:
            for m in KEYWORDS.finditer(spans[i]["text"]):
                kws.append(m.group(0).strip().lower())
        hits = sum(1 for i in ids if KEYWORDS.search(spans[i]["text"]))
        if hits < MIN_KEYWORDS and not (hits >= 2 and len(ids) >= 8):
            continue
        rect = None
        for i in ids:
            rect = rect_union(rect, spans[i]["rect"])
        if product is not None:
            if _overlap(rect, product) > 0.15:
                continue  # wording on the product itself (nutrition panel, ingredients...)
        else:
            # No die or trim to compare against: only panels hugging a page edge count.
            edge = min(rect[0] - media[0], media[2] - rect[2], rect[1] - media[1], media[3] - rect[3])
            if edge > 0.12 * max(mw, mh) or (rect[2] - rect[0]) * (rect[3] - rect[1]) > 0.35 * mw * mh:
                continue
        blocks.append(InfoBlock(rect, sorted(set(kws)), len(ids)))

    # 3. grow each block over the boxes, rules, swatches, logos and labels around it.
    # With a product to protect, a block keeps taking in anything that touches it (within GAP)
    # and stays clear of the product, so a whole side panel of stacked sections comes out as
    # one. Without one, it only takes in what sits inside or tightly frames the text.
    guard = _expand(product, GROW_PAD) if product is not None else None
    page_area = mw * mh
    items: list[Rect] = [p.bbox for p in geo.paths] + [im.bbox for im in geo.images] + [sp["rect"] for sp in spans]

    def clear(bb: Rect) -> bool:
        if (bb[2] - bb[0]) * (bb[3] - bb[1]) > 0.4 * page_area:
            return False
        return guard is None or _overlap(bb, guard) <= 0.0 and not _crosses(bb, guard)

    for blk in blocks:
        for _ in range(40):
            grown = blk.rect
            zone = _expand(blk.rect, GROW_PAD)
            for bb in items:
                if guard is not None:
                    take = _near(bb, blk.rect, GAP) and clear(bb)
                else:
                    take = (_inside(bb, zone, 0.9) or _encloses_tightly(bb, blk.rect)) and clear(bb)
                if take:
                    grown = rect_union(grown, bb)
            if grown == blk.rect:
                break
            blk.rect = grown
        if guard is None:
            # No product to measure against: a box drawn round the text (title-block frame)
            # marks the panel's extent. Take the smallest one that isn't half the page.
            ba = (blk.rect[2] - blk.rect[0]) * (blk.rect[3] - blk.rect[1])
            frames = [p.bbox for p in geo.paths if p.stroke
                      and p.bbox[0] <= blk.rect[0] + 1 and p.bbox[1] <= blk.rect[1] + 1
                      and p.bbox[2] >= blk.rect[2] - 1 and p.bbox[3] >= blk.rect[3] - 1
                      and (p.bbox[2] - p.bbox[0]) * (p.bbox[3] - p.bbox[1]) <= min(6 * ba, 0.25 * page_area)]
            if frames:
                blk.rect = min(frames, key=lambda f: (f[2] - f[0]) * (f[3] - f[1]))
    return _merge(blocks)


def _crosses(a: Rect, b: Rect) -> bool:
    """Touching or overlapping at all (zero-area lines included)."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _encloses_tightly(frame: Rect, inner: Rect) -> bool:
    """A frame drawn round the block (title-block border): contains it, not much bigger."""
    if not (frame[0] <= inner[0] + 1 and frame[1] <= inner[1] + 1 and frame[2] >= inner[2] - 1 and frame[3] >= inner[3] - 1):
        return False
    fa = (frame[2] - frame[0]) * (frame[3] - frame[1])
    ia = max(1e-6, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return fa <= 2.5 * ia


def _merge(blocks: list[InfoBlock]) -> list[InfoBlock]:
    out: list[InfoBlock] = []
    for b in sorted(blocks, key=lambda b: b.rect[0]):
        for o in out:
            if _near(o.rect, b.rect, 4):
                o.rect = rect_union(o.rect, b.rect)
                o.keywords = sorted(set(o.keywords + b.keywords))
                o.spans += b.spans
                break
        else:
            out.append(b)
    return out


def in_blocks(r: Rect | None, blocks: list[InfoBlock], frac: float = 0.8) -> bool:
    return r is not None and any(_inside(r, b.rect, frac) for b in blocks)


def apply_info_blocks(cls, blocks: list[InfoBlock]) -> int:
    """Move everything inside an info block out of the measurement inputs. Returns how many items."""
    if not blocks:
        return 0
    moved = 0

    def keep(paths: list[PathRec]) -> list[PathRec]:
        nonlocal moved
        out = []
        for p in paths:
            if in_blocks(p.bbox, blocks):
                cls.annotations.append(p)
                moved += 1
            else:
                out.append(p)
        return out

    cls.cut = keep(cls.cut)
    cls.crease = keep(cls.crease)
    cls.art = keep(cls.art)
    before = len(cls.trim_marks)
    cls.trim_marks = [s for s in cls.trim_marks
                      if not in_blocks((min(s.x0, s.x1), min(s.y0, s.y1), max(s.x0, s.x1), max(s.y0, s.y1)), blocks)]
    moved += before - len(cls.trim_marks)
    return moved
