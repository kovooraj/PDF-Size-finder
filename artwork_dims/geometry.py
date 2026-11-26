"""Walk a PDF page's content stream and record every painted path with the
information prepress cares about: separation (spot) colour name, layer (OCG),
dash pattern, stroke width, clip region.

PyMuPDF's get_drawings() does not expose separation names, and the spot colour
name ("CutContour", "Dieline", "Crease", "All"/Registration) is the single most
reliable signal for telling a dieline or crop mark apart from artwork. So we
parse the content stream ourselves with pikepdf.

All coordinates are PDF user space in points (1/72 in), y axis pointing up.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pikepdf

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# Hard cap so a pathological file cannot hang the request.
MAX_OPERATORS = 2_000_000
MAX_FORM_DEPTH = 12


def mat_mul(m1: Matrix, m2: Matrix) -> Matrix:
    """m1 then m2 (PDF row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


Rect = tuple[float, float, float, float]  # x0, y0, x1, y1


def rect_union(r1: Rect | None, r2: Rect | None) -> Rect | None:
    if r1 is None:
        return r2
    if r2 is None:
        return r1
    return (min(r1[0], r2[0]), min(r1[1], r2[1]), max(r1[2], r2[2]), max(r1[3], r2[3]))


def rect_intersect(r1: Rect | None, r2: Rect | None) -> Rect | None:
    if r1 is None:
        return r2
    if r2 is None:
        return r1
    r = (max(r1[0], r2[0]), max(r1[1], r2[1]), min(r1[2], r2[2]), min(r1[3], r2[3]))
    if r[0] >= r[2] or r[1] >= r[3]:
        return (r[0], r[1], r[0], r[1])  # empty
    return r


def points_bbox(pts) -> Rect | None:
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class Segment:
    x0: float
    y0: float
    x1: float
    y1: float
    curve: bool = False

    @property
    def length(self) -> float:
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)

    def orientation(self, tol: float = 0.5) -> str:
        """'h', 'v' or 'd' (diagonal / curve)."""
        if self.curve:
            return "d"
        if abs(self.y1 - self.y0) <= tol:
            return "h"
        if abs(self.x1 - self.x0) <= tol:
            return "v"
        return "d"


@dataclass
class PathRec:
    segments: list[Segment]
    bbox: Rect
    stroke: bool
    fill: bool
    stroke_cs: str          # e.g. "Separation:CutContour", "DeviceCMYK"
    fill_cs: str
    stroke_color: tuple[float, ...]
    fill_color: tuple[float, ...]
    width: float            # stroke width in points (approx, after CTM)
    dashed: bool
    layer: str | None
    clip: Rect | None
    closed: bool

    @property
    def spot_names(self) -> list[str]:
        out = []
        for cs in (self.stroke_cs if self.stroke else None, self.fill_cs if self.fill else None):
            if cs and (cs.startswith("Separation:") or cs.startswith("DeviceN:")):
                out.extend(cs.split(":", 1)[1].split("|"))
        return out


@dataclass
class ImageRec:
    bbox: Rect
    clip: Rect | None
    layer: str | None
    width_px: int
    height_px: int


@dataclass
class PageGeometry:
    paths: list[PathRec] = field(default_factory=list)
    images: list[ImageRec] = field(default_factory=list)
    spot_colors: set[str] = field(default_factory=set)
    layers: set[str] = field(default_factory=set)
    hidden_layers: set[str] = field(default_factory=set)
    truncated: bool = False
    operator_count: int = 0


@dataclass
class _GState:
    ctm: Matrix = IDENTITY
    stroke_cs: str = "DeviceGray"
    fill_cs: str = "DeviceGray"
    stroke_color: tuple[float, ...] = (0.0,)
    fill_color: tuple[float, ...] = (0.0,)
    width: float = 1.0
    dashed: bool = False
    clip: Rect | None = None

    def copy(self) -> "_GState":
        return _GState(**self.__dict__)


def _num(o) -> float:
    try:
        return float(o)
    except Exception:
        return 0.0


def _name(o) -> str:
    s = str(o)
    return s[1:] if s.startswith("/") else s


def _resolve_cs(name: str, resources) -> str:
    if name in ("DeviceGray", "DeviceRGB", "DeviceCMYK", "Pattern"):
        return name
    try:
        cs = resources.ColorSpace[pikepdf.Name("/" + name)]
    except Exception:
        return name
    return _describe_cs(cs)


def _describe_cs(cs) -> str:
    if isinstance(cs, pikepdf.Name):
        return _name(cs)
    if isinstance(cs, pikepdf.Array) and len(cs) > 0:
        family = _name(cs[0])
        if family == "Separation" and len(cs) > 1:
            return "Separation:" + _name(cs[1])
        if family == "DeviceN" and len(cs) > 1:
            try:
                return "DeviceN:" + "|".join(_name(n) for n in cs[1])
            except Exception:
                return "DeviceN"
        if family == "ICCBased":
            try:
                n = int(cs[1].get("/N", 3))
                return {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(n, "ICCBased")
            except Exception:
                return "ICCBased"
        if family == "Indexed":
            return "Indexed"
        return family
    return "Unknown"


def _ocg_name(props) -> str | None:
    try:
        if "/Name" in props:
            return str(props.Name)
        if "/OCGs" in props:  # OCMD
            ocgs = props.OCGs
            if isinstance(ocgs, pikepdf.Array):
                return "|".join(str(o.Name) for o in ocgs if "/Name" in o)
            return str(ocgs.Name)
    except Exception:
        return None
    return None


def _hidden_layers(pdf: pikepdf.Pdf) -> set[str]:
    out: set[str] = set()
    try:
        ocp = pdf.Root.OCProperties
        for ocg in ocp.D.get("/OFF", []):
            out.add(str(ocg.Name))
    except Exception:
        pass
    return out


class _Walker:
    def __init__(self, geo: PageGeometry):
        self.geo = geo
        self.ops = 0

    def walk(self, stream_owner, resources, base: _GState, depth: int = 0, layer_stack=None):
        if depth > MAX_FORM_DEPTH or self.geo.truncated:
            return
        gs = base.copy()
        stack: list[_GState] = []
        layer_stack = list(layer_stack or [])
        subpaths: list[list[tuple[float, float]]] = []
        segs: list[Segment] = []
        closed = False
        cur: tuple[float, float] | None = None   # untransformed current point
        start: tuple[float, float] | None = None
        pending_clip = False

        def cur_layer():
            for name in reversed(layer_stack):
                if name:
                    return name
            return None

        def T(x, y):
            return apply(gs.ctm, x, y)

        def finish(stroke: bool, fill: bool):
            nonlocal subpaths, segs, closed, pending_clip
            pts = [p for sp in subpaths for p in sp]
            bbox = points_bbox(pts)
            if pending_clip and bbox is not None:
                gs.clip = rect_intersect(gs.clip, bbox)
            if (stroke or fill) and bbox is not None:
                scale = math.sqrt(abs(gs.ctm[0] * gs.ctm[3] - gs.ctm[1] * gs.ctm[2])) or 1.0
                rec = PathRec(
                    segments=segs,
                    bbox=bbox,
                    stroke=stroke,
                    fill=fill,
                    stroke_cs=gs.stroke_cs,
                    fill_cs=gs.fill_cs,
                    stroke_color=gs.stroke_color,
                    fill_color=gs.fill_color,
                    width=gs.width * scale,
                    dashed=gs.dashed,
                    layer=cur_layer(),
                    clip=gs.clip,
                    closed=closed,
                )
                self.geo.paths.append(rec)
                for n in rec.spot_names:
                    self.geo.spot_colors.add(n)
            subpaths, segs, closed, pending_clip = [], [], False, False

        try:
            instructions = pikepdf.parse_content_stream(stream_owner)
        except Exception:
            return

        for operands, operator in instructions:
            self.ops += 1
            if self.ops > MAX_OPERATORS:
                self.geo.truncated = True
                break
            op = str(operator)
            try:
                if op == "q":
                    stack.append(gs.copy())
                elif op == "Q":
                    if stack:
                        gs = stack.pop()
                elif op == "cm":
                    m = tuple(_num(v) for v in operands)
                    gs.ctm = mat_mul(m, gs.ctm)  # type: ignore[arg-type]
                elif op == "w":
                    gs.width = _num(operands[0])
                elif op == "d":
                    arr = operands[0]
                    gs.dashed = len(arr) > 0 and any(_num(v) > 0 for v in arr)
                elif op == "gs":
                    try:
                        eg = resources.ExtGState[operands[0]]
                        if "/LW" in eg:
                            gs.width = _num(eg.LW)
                        if "/D" in eg:
                            arr = eg.D[0]
                            gs.dashed = len(arr) > 0 and any(_num(v) > 0 for v in arr)
                    except Exception:
                        pass
                elif op == "CS":
                    gs.stroke_cs = _resolve_cs(_name(operands[0]), resources)
                elif op == "cs":
                    gs.fill_cs = _resolve_cs(_name(operands[0]), resources)
                elif op in ("SC", "SCN"):
                    gs.stroke_color = tuple(_num(v) for v in operands if not isinstance(v, pikepdf.Name))
                elif op in ("sc", "scn"):
                    gs.fill_color = tuple(_num(v) for v in operands if not isinstance(v, pikepdf.Name))
                elif op == "G":
                    gs.stroke_cs, gs.stroke_color = "DeviceGray", (_num(operands[0]),)
                elif op == "g":
                    gs.fill_cs, gs.fill_color = "DeviceGray", (_num(operands[0]),)
                elif op == "RG":
                    gs.stroke_cs, gs.stroke_color = "DeviceRGB", tuple(_num(v) for v in operands)
                elif op == "rg":
                    gs.fill_cs, gs.fill_color = "DeviceRGB", tuple(_num(v) for v in operands)
                elif op == "K":
                    gs.stroke_cs, gs.stroke_color = "DeviceCMYK", tuple(_num(v) for v in operands)
                elif op == "k":
                    gs.fill_cs, gs.fill_color = "DeviceCMYK", tuple(_num(v) for v in operands)
                # ---- path construction
                elif op == "m":
                    x, y = _num(operands[0]), _num(operands[1])
                    subpaths.append([T(x, y)])
                    cur = start = (x, y)
                elif op == "l":
                    x, y = _num(operands[0]), _num(operands[1])
                    if cur is None:
                        cur = start = (x, y)
                        subpaths.append([T(x, y)])
                    p0, p1 = T(*cur), T(x, y)
                    segs.append(Segment(p0[0], p0[1], p1[0], p1[1]))
                    subpaths[-1].append(p1)
                    cur = (x, y)
                elif op in ("c", "v", "y"):
                    vals = [_num(v) for v in operands]
                    if op == "c":
                        ctrl = [(vals[0], vals[1]), (vals[2], vals[3])]
                        end = (vals[4], vals[5])
                    elif op == "v":
                        ctrl = [(vals[0], vals[1])]
                        end = (vals[2], vals[3])
                    else:
                        ctrl = [(vals[0], vals[1])]
                        end = (vals[2], vals[3])
                    if cur is None:
                        cur = start = end
                        subpaths.append([T(*end)])
                    p0, p1 = T(*cur), T(*end)
                    segs.append(Segment(p0[0], p0[1], p1[0], p1[1], curve=True))
                    subpaths[-1].extend([T(*c) for c in ctrl] + [p1])
                    cur = end
                elif op == "re":
                    x, y, w, h = (_num(v) for v in operands)
                    corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
                    tc = [T(*c) for c in corners]
                    subpaths.append(tc)
                    for i in range(4):
                        a, b = tc[i], tc[(i + 1) % 4]
                        segs.append(Segment(a[0], a[1], b[0], b[1]))
                    cur = start = (x, y)
                    closed = True
                elif op == "h":
                    if cur is not None and start is not None and cur != start:
                        p0, p1 = T(*cur), T(*start)
                        segs.append(Segment(p0[0], p0[1], p1[0], p1[1]))
                    cur = start
                    closed = True
                # ---- painting
                elif op in ("S",):
                    finish(True, False)
                elif op == "s":
                    closed = True
                    finish(True, False)
                elif op in ("f", "F", "f*"):
                    finish(False, True)
                elif op in ("B", "B*", "b", "b*"):
                    finish(True, True)
                elif op == "n":
                    finish(False, False)
                elif op in ("W", "W*"):
                    pending_clip = True
                # ---- marked content / layers
                elif op == "BDC":
                    tag = _name(operands[0])
                    lname = None
                    if tag == "OC" and len(operands) > 1:
                        props = operands[1]
                        if isinstance(props, pikepdf.Name):
                            try:
                                props = resources.Properties[props]
                            except Exception:
                                props = None
                        if props is not None:
                            lname = _ocg_name(props)
                            if lname:
                                self.geo.layers.add(lname)
                    layer_stack.append(lname)
                elif op == "BMC":
                    layer_stack.append(None)
                elif op == "EMC":
                    if layer_stack:
                        layer_stack.pop()
                # ---- xobjects
                elif op == "Do":
                    try:
                        xo = resources.XObject[operands[0]]
                    except Exception:
                        continue
                    sub = _name(xo.get("/Subtype", ""))
                    if sub == "Image":
                        corners = [T(0, 0), T(1, 0), T(1, 1), T(0, 1)]
                        self.geo.images.append(ImageRec(
                            bbox=points_bbox(corners),  # type: ignore[arg-type]
                            clip=gs.clip,
                            layer=cur_layer(),
                            width_px=int(xo.get("/Width", 0)),
                            height_px=int(xo.get("/Height", 0)),
                        ))
                    elif sub == "Form":
                        fm = tuple(_num(v) for v in xo.get("/Matrix", [1, 0, 0, 1, 0, 0]))
                        child = gs.copy()
                        child.ctm = mat_mul(fm, gs.ctm)  # type: ignore[arg-type]
                        if "/BBox" in xo:
                            bb = [_num(v) for v in xo.BBox]
                            corners = [apply(child.ctm, bb[0], bb[1]), apply(child.ctm, bb[2], bb[1]),
                                       apply(child.ctm, bb[2], bb[3]), apply(child.ctm, bb[0], bb[3])]
                            child.clip = rect_intersect(child.clip, points_bbox(corners))
                        child_layers = list(layer_stack)
                        if "/OC" in xo:
                            lname = _ocg_name(xo.OC)
                            if lname:
                                self.geo.layers.add(lname)
                            child_layers.append(lname)
                        child_res = xo.get("/Resources", resources)
                        self.walk(xo, child_res, child, depth + 1, child_layers)
            except Exception:
                # One malformed operator must not kill the analysis.
                continue


def extract_page_geometry(pdf: pikepdf.Pdf, page_index: int) -> PageGeometry:
    geo = PageGeometry()
    geo.hidden_layers = _hidden_layers(pdf)
    page = pdf.pages[page_index]
    resources = page.obj.get("/Resources", pikepdf.Dictionary())
    try:
        resources = page.resources
    except Exception:
        pass
    walker = _Walker(geo)
    walker.walk(page, resources, _GState())
    geo.operator_count = walker.ops
    return geo
