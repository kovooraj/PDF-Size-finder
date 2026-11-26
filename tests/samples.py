"""Build synthetic artwork PDFs with known dimensions for tests.

Each builder returns PDF bytes. Spot colours are real /Separation colour
spaces, as Illustrator / ArtPro / Esko exports produce them.
"""
from __future__ import annotations

import io

import pikepdf

IN = 72.0


def _sep(pdf: pikepdf.Pdf, name: str, cmyk=(0, 1, 0, 0)):
    fn = pdf.make_indirect(pikepdf.Dictionary(
        FunctionType=2, Domain=[0, 1], C0=[0, 0, 0, 0], C1=list(cmyk), N=1))
    return pikepdf.Array([pikepdf.Name.Separation, pikepdf.Name("/" + name), pikepdf.Name.DeviceCMYK, fn])


def _make(page_w_in, page_h_in, content: str, colorspaces: dict | None = None, boxes: dict | None = None,
          extra_pages: list[str] | None = None) -> bytes:
    pdf = pikepdf.new()
    cs = pikepdf.Dictionary({"/" + k: _sep(pdf, v[0], v[1]) for k, v in (colorspaces or {}).items()})
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica))
    for body in [content] + (extra_pages or []):
        res = pikepdf.Dictionary(ColorSpace=cs, Font=pikepdf.Dictionary(F1=font))
        page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, page_w_in * IN, page_h_in * IN],
                                  Resources=res, Contents=pdf.make_stream(body.encode()))
        for k, v in (boxes or {}).items():
            page[k] = [x * IN for x in v]
        pdf.pages.append(pikepdf.Page(page))
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _crop_marks(x0, y0, x1, y1, off=0.125, ln=0.25) -> str:
    """Registration-colour crop marks at trim corners, offset outward."""
    s = ["q /Reg CS 1 SCN 0.25 w"]
    for x in (x0, x1):
        for y, d in ((y0, -1), (y1, 1)):
            s.append(f"{x*IN:.3f} {(y + d*off)*IN:.3f} m {x*IN:.3f} {(y + d*(off+ln))*IN:.3f} l S")
    for y in (y0, y1):
        for x, d in ((x0, -1), (x1, 1)):
            s.append(f"{(x + d*off)*IN:.3f} {y*IN:.3f} m {(x + d*(off+ln))*IN:.3f} {y*IN:.3f} l S")
    s.append("Q")
    return "\n".join(s)


def business_card() -> bytes:
    """3.5 x 2 card with 1/8 bleed, crop marks, on a 4.5 x 3 page (no TrimBox)."""
    x0, y0 = 0.5, 0.5
    art = (f"q 0 0.6 1 0 k {(x0-0.125)*IN} {(y0-0.125)*IN} {3.75*IN} {2.25*IN} re f Q\n"
           f"BT /F1 12 Tf 0 0 0 1 k {(x0+0.3)*IN} {(y0+1)*IN} Td (Jane Doe) Tj ET\n")
    return _make(4.5, 3, art + _crop_marks(x0, y0, x0 + 3.5, y0 + 2), {"Reg": ("All", (1, 1, 1, 1))})


def roll_label() -> bytes:
    """3 x 4 rounded-corner label, CutContour spot, art bleeding 1/16."""
    x0, y0, w, h, r = 0.5, 0.5, 3.0, 4.0, 0.25
    k = 0.5523 * r
    X, Y = lambda v: v * IN, lambda v: v * IN
    p = [f"{X(x0+r)} {Y(y0)} m", f"{X(x0+w-r)} {Y(y0)} l",
         f"{X(x0+w-r+k)} {Y(y0)} {X(x0+w)} {Y(y0+r-k)} {X(x0+w)} {Y(y0+r)} c",
         f"{X(x0+w)} {Y(y0+h-r)} l",
         f"{X(x0+w)} {Y(y0+h-r+k)} {X(x0+w-r+k)} {Y(y0+h)} {X(x0+w-r)} {Y(y0+h)} c",
         f"{X(x0+r)} {Y(y0+h)} l",
         f"{X(x0+r-k)} {Y(y0+h)} {X(x0)} {Y(y0+h-r+k)} {X(x0)} {Y(y0+h-r)} c",
         f"{X(x0)} {Y(y0+r)} l",
         f"{X(x0)} {Y(y0+r-k)} {X(x0+r-k)} {Y(y0)} {X(x0+r)} {Y(y0)} c h"]
    art = f"q 1 0 0 0 k {X(x0-0.0625)} {Y(y0-0.0625)} {X(w+0.125)} {Y(h+0.125)} re f Q\n"
    die = "q /CS0 CS 1 SCN 0.5 w\n" + "\n".join(p) + " S Q\n"
    text = "BT /F1 9 Tf 0 0 0 1 k 36 10 Td (Label 3 x 4 in, CutContour) Tj ET\n"
    return _make(4, 5, art + die + text, {"CS0": ("CutContour", (0, 1, 0, 0))})


def rte_carton(L=3.0, W=2.0, D=6.0, glue=0.5, tuck=1.5) -> bytes:
    """Reverse tuck end carton. Panels left->right: glue | L | W | L | W.
    Cut lines on 'Dieline' spot, folds on 'Crease' spot (dashed)."""
    ox, oy = 0.5, 0.5
    xs = [ox, ox + glue, ox + glue + L, ox + glue + L + W, ox + glue + 2 * L + W, ox + glue + 2 * L + 2 * W]
    yb, yt = oy + tuck, oy + tuck + D
    cut, crease = [], []
    seg = lambda a, b, c, d: f"{a*IN:.3f} {b*IN:.3f} m {c*IN:.3f} {d*IN:.3f} l"
    # outer outline: body + top tuck over panel 1 (L) and bottom tuck over panel 3 (L)
    cut += [seg(xs[0], yb + 0.25, xs[1], yb), seg(xs[0], yt - 0.25, xs[1], yt), seg(xs[0], yb + 0.25, xs[0], yt - 0.25)]
    cut += [seg(xs[1], yt, xs[1], yt + W), seg(xs[1], yt + W, xs[2], yt + W), seg(xs[2], yt + W, xs[2], yt)]  # top tuck lid
    cut += [seg(xs[2], yt, xs[5], yt), seg(xs[1], yb, xs[3], yb)]
    cut += [seg(xs[3], yb, xs[3], yb - W), seg(xs[3], yb - W, xs[4], yb - W), seg(xs[4], yb - W, xs[4], yb)]  # bottom tuck
    cut += [seg(xs[4], yb, xs[5], yb), seg(xs[5], yb, xs[5], yt)]
    cut += [seg(xs[1], yt + W, xs[1] + 0.2, yt + W + tuck * 0.6), seg(xs[1] + 0.2, yt + W + tuck * 0.6, xs[2] - 0.2, yt + W + tuck * 0.6),
            seg(xs[2] - 0.2, yt + W + tuck * 0.6, xs[2], yt + W)]
    for x in xs[1:5]:
        crease.append(seg(x, yb, x, yt))
    crease += [seg(xs[1], yt, xs[2], yt), seg(xs[1], yb, xs[5], yb), seg(xs[2], yt, xs[5], yt)]
    crease += [seg(xs[1], yt + W, xs[2], yt + W), seg(xs[3], yb - W, xs[4], yb - W)]
    art = f"q 0 0.2 0.9 0 k {xs[1]*IN} {yb*IN} {(2*L+2*W)*IN} {D*IN} re f Q\n"
    die = "q /Die CS 1 SCN 0.5 w\n" + "\n".join(c + " S" for c in cut) + "\nQ\n"
    cr = "q /Crs CS 1 SCN 0.5 w [4 2] 0 d\n" + "\n".join(c + " S" for c in crease) + "\nQ\n"
    total_w = glue + 2 * L + 2 * W + 1
    return _make(total_w, D + 2 * W + 2 * tuck + 1, art + die + cr,
                 {"Die": ("Dieline", (0, 1, 0, 0)), "Crs": ("Crease", (1, 0, 0, 0))})


def standup_pouch(W=6.0, H=9.0, G=4.0) -> bytes:
    """Print Monkey layout: flat sheet front | gusset | back side by side,
    2H + G wide by W tall; fold lines dashed on the dieline spot."""
    ox, oy = 0.5, 0.5
    fw, fh = 2 * H + G, W
    seg = lambda a, b, c, d: f"{a*IN:.3f} {b*IN:.3f} m {c*IN:.3f} {d*IN:.3f} l"
    die = f"q /Die CS 1 SCN 0.5 w {ox*IN} {oy*IN} {fw*IN} {fh*IN} re S Q\n"
    folds = [seg(ox + H, oy, ox + H, oy + fh), seg(ox + H + G / 2, oy, ox + H + G / 2, oy + fh),
             seg(ox + H + G, oy, ox + H + G, oy + fh)]
    fold = "q /Die CS 1 SCN 0.5 w [3 3] 0 d\n" + "\n".join(f + " S" for f in folds) + "\nQ\n"
    art = f"q 0.8 0 0.6 0 k {(ox-0.125)*IN} {(oy-0.125)*IN} {(fw+0.25)*IN} {(fh+0.25)*IN} re f Q\n"
    note = "BT /F1 10 Tf 0 0 0 1 k 40 14 Td (Stand-up pouch with zipper and tear notch) Tj ET\n"
    return _make(fw + 1, fh + 1, art + die + fold + note, {"Die": ("Die Cut", (0, 1, 0, 0))})


def oversized_page() -> bytes:
    """5 x 7 flyer art dropped on a letter page, no marks, no boxes."""
    x0, y0 = 1.75, 2.0
    art = f"q 0.1 0.9 0.2 0 k {x0*IN} {y0*IN} {5*IN} {7*IN} re f Q\n"
    return _make(8.5, 11, art)


def trimbox_flyer() -> bytes:
    """8.5 x 11 with bleed page 8.75 x 11.25 and TrimBox/BleedBox set."""
    art = f"q 0 0 0 0.2 k 0 0 {8.75*IN} {11.25*IN} re f Q\n"
    return _make(8.75, 11.25, art, boxes={"/TrimBox": [0.125, 0.125, 8.625, 11.125],
                                           "/BleedBox": [0, 0, 8.75, 11.25]})


def layflat_sheet() -> bytes:
    """Lay-flat pouch art: front + back side by side, 10 x 4 sheet with crop marks, no dieline."""
    x0, y0 = 0.5, 0.5
    art = f"q 0.85 0.1 0.6 0 k {(x0-0.125)*IN} {(y0-0.125)*IN} {10.25*IN} {4.25*IN} re f Q\n"
    return _make(11, 5, art + _crop_marks(x0, y0, x0 + 10, y0 + 4), {"Reg": ("All", (1, 1, 1, 1))})


ALL = {
    "business_card": business_card,
    "roll_label": roll_label,
    "rte_carton": rte_carton,
    "standup_pouch": standup_pouch,
    "oversized_page": oversized_page,
    "trimbox_flyer": trimbox_flyer,
    "layflat_sheet": layflat_sheet,
}

if __name__ == "__main__":
    import pathlib
    import sys
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in ALL.items():
        (out / f"{name}.pdf").write_bytes(fn())
        print("wrote", out / f"{name}.pdf")
