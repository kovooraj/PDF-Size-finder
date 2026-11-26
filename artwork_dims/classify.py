"""Classify painted paths into: dieline cut, dieline crease/fold, trim marks,
annotation (dimension callouts etc.), and artwork. Then derive the trim
rectangle and the artwork extents (to find white space that is not artwork).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .geometry import PageGeometry, PathRec, Rect, Segment, rect_intersect, rect_union

PT_PER_IN = 72.0

# Spot / layer names. Order matters: annotation is checked before cut.
ANNOT_RE = re.compile(r"dimension|dims?\b|measure|info|note|annotat|legend|text|slug|spec|callout", re.I)
CREASE_RE = re.compile(r"crease|fold|score|perf|bend|rule\b", re.I)
CUT_RE = re.compile(r"die|cut|kiss|thru|through|contour|structur|knife|keyline|template|outline|trim\s*line|bleed\s*line|safe|non.?print|tech", re.I)
SAFETY_RE = re.compile(r"safe|bleed\s*line|bleed\b|glue|varnish.?free|no.?print|window|seal|zip|tear|notch|hang", re.I)
REGISTRATION_NAMES = {"all", "registration", "[registration]"}


@dataclass
class Classified:
    cut: list[PathRec] = field(default_factory=list)
    crease: list[PathRec] = field(default_factory=list)
    guides: list[PathRec] = field(default_factory=list)      # safety / bleed guide lines on the dieline layer
    trim_marks: list[Segment] = field(default_factory=list)
    annotations: list[PathRec] = field(default_factory=list)
    art: list[PathRec] = field(default_factory=list)
    dieline_source: str = "none"   # "spot", "layer", "heuristic", "none"
    dieline_names: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _is_white(cs: str, color: tuple[float, ...]) -> bool:
    if not color:
        return False
    if cs == "DeviceCMYK" and len(color) == 4:
        return all(c <= 0.001 for c in color)
    if cs in ("DeviceRGB",) and len(color) == 3:
        return all(c >= 0.999 for c in color)
    if cs == "DeviceGray" and len(color) == 1:
        return color[0] >= 0.999
    if cs.startswith("Separation:") and len(color) == 1:
        return color[0] <= 0.001  # 0% tint of a spot = nothing printed
    return False


def _is_registration(p: PathRec) -> bool:
    names = {n.lower() for n in p.spot_names}
    if names & REGISTRATION_NAMES:
        return True
    # Registration simulated as 100/100/100/100 CMYK
    if p.stroke and p.stroke_cs == "DeviceCMYK" and len(p.stroke_color) == 4 and all(c >= 0.99 for c in p.stroke_color):
        return True
    return False


def _name_role(name: str) -> str | None:
    if not name:
        return None
    if name.lower() in REGISTRATION_NAMES:
        return None
    if ANNOT_RE.search(name) and not re.search(r"die|cut", name, re.I):
        return "annotation"
    if CREASE_RE.search(name):
        return "crease"
    if SAFETY_RE.search(name) and not re.search(r"die|cut|contour", name, re.I):
        return "guide"
    if CUT_RE.search(name):
        return "cut"
    return None


def _role_for_path(p: PathRec) -> tuple[str | None, str | None, str | None]:
    """Return (role, source, name) from spot colour first, then layer."""
    for n in p.spot_names:
        r = _name_role(n)
        if r:
            return r, "spot", n
    if p.layer:
        for part in p.layer.split("|"):
            r = _name_role(part)
            if r:
                return r, "layer", part
    return None, None, None


def _short_hv_segments(p: PathRec, min_len: float, max_len: float) -> list[Segment]:
    out = []
    for s in p.segments:
        o = s.orientation()
        if o in ("h", "v") and min_len <= s.length <= max_len:
            out.append(s)
    return out


def classify(geo: PageGeometry, media: Rect) -> Classified:
    c = Classified()
    names_seen: set[str] = set()
    unassigned: list[PathRec] = []

    for p in geo.paths:
        role, source, name = _role_for_path(p)
        if role == "cut":
            # A dieline spot used with dashes is a fold line.
            (c.crease if p.dashed else c.cut).append(p)
        elif role == "crease":
            c.crease.append(p)
        elif role == "guide":
            c.guides.append(p)
        elif role == "annotation":
            c.annotations.append(p)
        else:
            unassigned.append(p)
            continue
        if source and c.dieline_source in ("none", "layer"):
            c.dieline_source = source if c.dieline_source == "none" or source == "spot" else c.dieline_source
        if name:
            names_seen.add(name)
    c.dieline_names = sorted(names_seen)

    # ---- Trim / crop marks: short horizontal/vertical strokes near page edges.
    mw, mh = media[2] - media[0], media[3] - media[1]
    reg_marks: list[Segment] = []
    other_marks: list[Segment] = []
    rest: list[PathRec] = []
    for p in unassigned:
        if not p.stroke or p.fill:
            rest.append(p)
            continue
        short = _short_hv_segments(p, 4.0, min(72.0, 0.2 * max(mw, mh)))
        if short and len(short) == len([s for s in p.segments if s.length > 0.01]):
            (reg_marks if _is_registration(p) else other_marks).extend(short)
            # keep the path in `rest` only if not registration; decided below
            p._mark_candidate = True  # type: ignore[attr-defined]
        rest.append(p)

    marks = reg_marks if len(reg_marks) >= 4 else reg_marks + other_marks
    c.trim_marks = [s for s in marks if _near_edge(s, media)]

    mark_ids = {id(s) for s in c.trim_marks}
    for p in rest:
        if getattr(p, "_mark_candidate", False) and any(id(s) in mark_ids for s in p.segments):
            continue
        if _is_registration(p):
            continue  # registration-colour items (colour bars, targets) are never product art
        c.art.append(p)

    # ---- Heuristic dieline when nothing is named: a thin, unfilled, single-colour
    # stroke group spanning a large part of the page.
    if not c.cut:
        groups: dict[tuple, list[PathRec]] = defaultdict(list)
        for p in c.art:
            if p.stroke and not p.fill and p.width <= 2.0:
                groups[(p.stroke_cs, tuple(round(v, 2) for v in p.stroke_color))].append(p)
        best = None
        for key, ps in groups.items():
            bb = None
            for p in ps:
                bb = rect_union(bb, p.bbox)
            if bb is None:
                continue
            area = (bb[2] - bb[0]) * (bb[3] - bb[1])
            total_len = sum(s.length for p in ps for s in p.segments)
            if area < 0.15 * mw * mh or total_len < 2 * ((bb[2] - bb[0]) + (bb[3] - bb[1])) * 0.9:
                continue
            if not _looks_like_die_colour(key[0], key[1]):
                continue
            # dielines are mostly straight lines; decorative dashed doodles are curves
            curve_len = sum(s.length for p in ps for s in p.segments if s.curve)
            if total_len and curve_len / total_len > 0.35:
                continue
            # A dieline has structure inside its outline (panel, gusset, fold lines); a bleed or
            # safety line is just a rectangle. Prefer structure, then a pure colour, then area.
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            interior = sum(
                1 for p in ps for s in p.segments
                if (s.orientation() == "h" and bb[1] + 3 < s.y0 < bb[3] - 3 and s.length >= 0.3 * w)
                or (s.orientation() == "v" and bb[0] + 3 < s.x0 < bb[2] - 3 and s.length >= 0.3 * h)
            )
            rank = (interior > 0, _looks_like_pure_colour(key[0], key[1]), area)
            if best is None or rank > best[0]:
                best = (rank, key, ps)
        if best is not None:
            _, key, ps = best
            for p in ps:
                (c.crease if p.dashed else c.cut).append(p)
                c.art.remove(p)
            c.dieline_source = "heuristic"
            c.notes.append(
                f"No named dieline spot colour/layer found; treated thin {key[0]} strokes "
                f"{key[1]} as the dieline (verify)."
            )

    _drop_stray_die_marks(c)
    return c


def _drop_stray_die_marks(c: Classified, gap: float = 9.0) -> None:
    """Legends and swatches ('Cutline', 'Fold') are often drawn in the die spot colours away
    from the die. Group cut + fold paths that touch (within `gap` points) and drop small groups
    that sit clear of the main die, so they don't widen the die bounds."""
    paths = [("cut", p) for p in c.cut] + [("crease", p) for p in c.crease]
    if len(paths) < 3:
        return
    parent = list(range(len(paths)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    boxes = [p.bbox for _, p in paths]
    order = sorted(range(len(paths)), key=lambda i: boxes[i][0])
    for a_i, a in enumerate(order):  # sweep on x so this stays fast with thousands of paths
        ax0, ay0, ax1, ay1 = boxes[a]
        for b in order[a_i + 1:]:
            bx0, by0, bx1, by1 = boxes[b]
            if bx0 > ax1 + gap:
                break
            if by0 <= ay1 + gap and ay0 <= by1 + gap:
                parent[find(a)] = find(b)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(paths)):
        groups[find(i)].append(i)
    length = {g: sum(s.length for i in ids for s in paths[i][1].segments) for g, ids in groups.items()}
    main = max(groups, key=lambda g: length[g])
    mb = None
    for i in groups[main]:
        mb = rect_union(mb, boxes[i])
    drop: set[int] = set()
    for g, ids in groups.items():
        if g == main or length[g] >= 0.15 * length[main]:
            continue
        gb = None
        for i in ids:
            gb = rect_union(gb, boxes[i])
        clear = gb[2] < mb[0] - 2 * gap or gb[0] > mb[2] + 2 * gap or gb[3] < mb[1] - 2 * gap or gb[1] > mb[3] + 2 * gap
        if clear:
            drop.update(ids)
    if not drop:
        return
    dropped = [paths[i][1] for i in sorted(drop)]
    c.cut = [p for i, (k, p) in enumerate(paths) if k == "cut" and i not in drop]
    c.crease = [p for i, (k, p) in enumerate(paths) if k == "crease" and i not in drop]
    c.annotations.extend(dropped)
    c.notes.append(f"Ignored {len(dropped)} dieline-coloured mark{'s' if len(dropped) != 1 else ''} away from the die "
                   "(legend or swatch).")


def _looks_like_pure_colour(cs: str, color: tuple[float, ...]) -> bool:
    """Every channel at an extreme, e.g. 100% magenta or RGB red: the classic dieline swatch."""
    if cs == "DeviceCMYK" and len(color) == 4:
        strong = [v for v in color if v >= 0.8]
        weak = [v for v in color if v <= 0.2]
        return len(strong) >= 1 and len(strong) + len(weak) == 4 and not (len(strong) == 1 and color[3] >= 0.8)
    if cs == "DeviceRGB" and len(color) == 3:
        hi = [v for v in color if v >= 0.8]
        lo = [v for v in color if v <= 0.2]
        return len(hi) in (1, 2) and len(hi) + len(lo) == 3
    return False


def _looks_like_die_colour(cs: str, color: tuple[float, ...]) -> bool:
    """Dielines drawn without a spot colour (or converted to process, e.g. PANTONE 306 C
    → RGB cyan) are a clearly saturated colour: not black, white or grey."""
    if cs == "DeviceCMYK" and len(color) == 4:
        c, m, y, k = color
        if k >= 0.6:
            return False
        rgb = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    elif cs == "DeviceRGB" and len(color) == 3:
        rgb = tuple(color)
    else:
        return False
    hi, lo = max(rgb), min(rgb)
    return hi >= 0.45 and (hi - lo) / hi >= 0.6


def _near_edge(s: Segment, media: Rect, band: float = 0.22) -> bool:
    mw, mh = media[2] - media[0], media[3] - media[1]
    mx, my = (s.x0 + s.x1) / 2, (s.y0 + s.y1) / 2
    fx, fy = (mx - media[0]) / mw, (my - media[1]) / mh
    return fx < band or fx > 1 - band or fy < band or fy > 1 - band


def _cluster(values: list[float], tol: float) -> list[tuple[float, int]]:
    """Cluster 1-D values; returns (mean, count)."""
    out: list[list[float]] = []
    for v in sorted(values):
        if out and abs(v - out[-1][-1]) <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [(sum(g) / len(g), len(g)) for g in out]


def trim_from_marks(marks: list[Segment], media: Rect) -> Rect | None:
    """Crop marks: vertical ticks give trim x, horizontal ticks give trim y.
    Bleed marks sit outside trim marks, so take the innermost pair either side
    of the page centre."""
    if len(marks) < 4:
        return None
    vx = [(s.x0 + s.x1) / 2 for s in marks if s.orientation() == "v"]
    hy = [(s.y0 + s.y1) / 2 for s in marks if s.orientation() == "h"]
    xs = [v for v, n in _cluster(vx, 0.75) if n >= 2]
    ys = [v for v, n in _cluster(hy, 0.75) if n >= 2]
    cx = (media[0] + media[2]) / 2
    cy = (media[1] + media[3]) / 2
    left = [x for x in xs if x < cx]
    right = [x for x in xs if x > cx]
    bottom = [y for y in ys if y < cy]
    top = [y for y in ys if y > cy]
    if not (left and right and bottom and top):
        return None
    r = (max(left), max(bottom), min(right), min(top))
    if r[2] - r[0] < 18 or r[3] - r[1] < 18:
        return None
    return r


def union_bbox(paths: list[PathRec]) -> Rect | None:
    bb = None
    for p in paths:
        bb = rect_union(bb, p.bbox)
    return bb


def art_extents(c: Classified, geo: PageGeometry, text_boxes: list[Rect], media: Rect) -> Rect | None:
    """Visible artwork extents, ignoring dielines, marks, registration items,
    white fills and anything clipped away."""
    bb = None
    page_area = (media[2] - media[0]) * (media[3] - media[1])
    for p in c.art:
        if p.fill and not p.stroke and _is_white(p.fill_cs, p.fill_color):
            continue
        if p.stroke and not p.fill and _is_white(p.stroke_cs, p.stroke_color):
            continue
        vis = rect_intersect(p.bbox, p.clip)
        if vis is None or vis[2] - vis[0] <= 0.01 and vis[3] - vis[1] <= 0.01:
            continue
        # a hairline frame around the whole page is usually an artboard border, not art
        if p.stroke and not p.fill and (vis[2] - vis[0]) * (vis[3] - vis[1]) > 0.97 * page_area:
            continue
        bb = rect_union(bb, vis)
    for im in geo.images:
        vis = rect_intersect(im.bbox, im.clip)
        if vis and vis[2] > vis[0] and vis[3] > vis[1]:
            bb = rect_union(bb, vis)
    for t in text_boxes:
        bb = rect_union(bb, t)
    if bb is not None:
        bb = rect_intersect(bb, media)
    return bb
