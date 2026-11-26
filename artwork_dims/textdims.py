"""Find dimension callouts written in the artwork / dieline text, e.g.
  '3.5" x 2"', '4 x 2 x 6 in', '120mm x 80mm', 'W: 6"  H: 9"  G: 3.5"',
  'Gusset 3-1/2"', 'L 3.000 x W 2.000 x D 6.000'.
Values are returned in inches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

UNICODE_FRAC = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
                "⅓": 1 / 3, "⅔": 2 / 3, "⅙": 1 / 6, "⅚": 5 / 6, "⅕": 0.2}

NUM = r"(?:\d+(?:\.\d+)?(?:\s*[-\s]\s*\d+/\d+)?|\d+/\d+|\.\d+)\s*[½¼¾⅛⅜⅝⅞⅓⅔]?"
UNIT = r"(?:\"|”|″|''|in\b|inch(?:es)?\b|mm\b|cm\b|pt\b)"
SEP = r"\s*(?:x|X|×|\*|by)\s*"

TRIPLE = re.compile(rf"({NUM})\s*({UNIT})?{SEP}({NUM})\s*({UNIT})?{SEP}({NUM})\s*({UNIT})?")
DOUBLE = re.compile(rf"({NUM})\s*({UNIT})?{SEP}({NUM})\s*({UNIT})?")
LABEL = r"width|height|length|depth|gusset|bottom[ \t]+gusset|side[ \t]+gusset|W|H|L|D|G"
# Label then value, on the same line:  'Width: 6"'  'W 6.00 in'
LABEL_FIRST = re.compile(rf"\b({LABEL})\b[ \t]*[:=]?[ \t]*({NUM})[ \t]*({UNIT})?", re.I)
# Value then label, on the same line:  '6.00" Width'  '2.00" Gusset'
VALUE_FIRST = re.compile(rf"({NUM})[ \t]*({UNIT})[ \t]*({LABEL})\b", re.I)
# A label qualified like this is about something else (zipper width, seal width, width + bleed).
QUALIFIER_BEFORE = re.compile(r"(zipper|zip|seal|hole|notch|hang|bleed|line|flat|window|label|core|liner)[ \t]*$", re.I)
QUALIFIER_AFTER = re.compile(r"^[ \t]*(\+|with|w/|incl|including|bleed)", re.I)

LABEL_MAP = {"width": "width", "w": "width", "height": "height", "h": "height", "length": "length",
             "l": "length", "depth": "depth", "d": "depth", "gusset": "gusset", "g": "gusset",
             "bottom gusset": "gusset", "side gusset": "side_gusset"}


@dataclass
class Callout:
    text: str
    values_in: list[float]
    labels: list[str] | None
    unit: str            # 'in', 'mm', 'cm', 'pt' or 'assumed-in' / 'assumed-mm'

    def as_dict(self):
        return {"text": self.text, "values_in": [round(v, 4) for v in self.values_in],
                "labels": self.labels, "unit": self.unit}


def parse_number(s: str) -> float | None:
    s = s.strip()
    frac = 0.0
    if s and s[-1] in UNICODE_FRAC:
        frac = UNICODE_FRAC[s[-1]]
        s = s[:-1].strip()
    if not s:
        return frac or None
    m = re.fullmatch(r"(\d+)\s*[-\s]\s*(\d+)/(\d+)", s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3)) + frac
    m = re.fullmatch(r"(\d+)/(\d+)", s)
    if m:
        d = int(m.group(2))
        return (int(m.group(1)) / d if d else 0) + frac
    try:
        return float(s) + frac
    except ValueError:
        return None


def _norm_unit(u: str | None) -> str | None:
    if not u:
        return None
    u = u.lower().strip()
    if u in ('"', "”", "″", "''", "in", "inch", "inches"):
        return "in"
    return u


def _to_in(v: float, unit: str) -> float:
    return {"in": v, "mm": v / 25.4, "cm": v / 2.54, "pt": v / 72.0}.get(unit.replace("assumed-", ""), v)


def _resolve_unit(units: list[str | None], values: list[float]) -> str:
    known = [u for u in units if u]
    if known:
        return known[-1]
    # No unit printed: guess. Anything over 60 is almost certainly mm.
    return "assumed-mm" if max(values) > 60 else "assumed-in"


def find_callouts(text: str) -> list[Callout]:
    out: list[Callout] = []
    used: list[tuple[int, int]] = []
    flat = re.sub(r"[ \t]+", " ", text)

    def overlaps(a, b):
        return any(not (b <= s or a >= e) for s, e in used)

    for rx, n in ((TRIPLE, 3), (DOUBLE, 2)):
        for m in rx.finditer(flat):
            if overlaps(m.start(), m.end()):
                continue
            nums = [parse_number(m.group(1 + 2 * i)) for i in range(n)]
            if any(v is None or v <= 0 for v in nums):
                continue
            units = [_norm_unit(m.group(2 + 2 * i)) for i in range(n)]
            unit = _resolve_unit(units, nums)  # type: ignore[arg-type]
            if unit.startswith("assumed") and n == 2 and not re.search(r"\d\s*[xX×]\s*\d", m.group(0)):
                continue
            # Ignore things like quantities "500 x 2" with no unit and integers only
            if unit.startswith("assumed") and all(float(v).is_integer() for v in nums) and max(nums) > 30:  # type: ignore[arg-type]
                continue
            out.append(Callout(m.group(0).strip(), [_to_in(v, unit) for v in nums], None, unit))  # type: ignore[arg-type]
            used.append((m.start(), m.end()))

    labelled: list[Callout] = []
    for line in flat.splitlines():
        taken: list[tuple[int, int]] = []
        for rx, order in ((VALUE_FIRST, "vf"), (LABEL_FIRST, "lf")):
            for m in rx.finditer(line):
                if any(not (m.end() <= a or m.start() >= b) for a, b in taken):
                    continue
                if order == "vf":
                    num, unit, lab = m.group(1), m.group(2), m.group(3)
                    lab_start, lab_end = m.start(3), m.end(3)
                else:
                    lab, num, unit = m.group(1), m.group(2), m.group(3)
                    lab_start, lab_end = m.start(1), m.end(1)
                if QUALIFIER_BEFORE.search(line[:lab_start]) or QUALIFIER_AFTER.search(line[lab_end:]):
                    continue
                label = LABEL_MAP.get(re.sub(r"\s+", " ", lab.lower()))
                v = parse_number(num)
                u = _norm_unit(unit)
                if not label or v is None or v <= 0:
                    continue
                if len(lab) == 1 and not u:  # bare 'H 2' in body copy is not a dimension
                    continue
                u = u or ("assumed-mm" if v > 60 else "assumed-in")
                labelled.append(Callout(m.group(0).strip(), [_to_in(v, u)], [label], u))
                taken.append((m.start(), m.end()))
    out.extend(labelled)
    return out


def matching_spec(callouts: list[Callout], measured: list[float], tol: float = 0.04) -> Callout | None:
    """A printed 'A x B x C' callout whose values equal the measured values in
    any order (designers write W x H x G, H x W x G, L x W x D...)."""
    want = sorted(measured)
    for c in callouts:
        if c.labels or len(c.values_in) != len(measured):
            continue
        got = sorted(c.values_in)
        if all(abs(a - b) <= max(tol, 0.01 * b) for a, b in zip(got, want)):
            return c
    return None


def labelled_dims(callouts: list[Callout]) -> dict[str, float]:
    """Collapse labelled callouts into {'width': in, 'height': in, ...} (first wins)."""
    d: dict[str, float] = {}
    for c in callouts:
        if c.labels:
            d.setdefault(c.labels[0], c.values_in[0])
    return d
