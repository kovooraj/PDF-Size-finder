"""Make a small 'measurement copy' of a huge PDF without reading all of it.

A 1 GB print PDF is almost always a few big images (and, for Illustrator
exports, the native .ai data in /PieceInfo). Dimensions don't need either.
We open the source lazily (local temp file or HTTP range reads), copy the
first pages into a new PDF with:
  * every image larger than KEEP_IMAGE_BYTES replaced by a 1x1 grey
    placeholder drawn in the same place (so geometry and the artwork extents
    stay identical),
  * /PieceInfo, /Thumb, /Annots, /Metadata dropped.
Vector paths, spot colours, layers, text and fonts are kept, so the normal
analysis runs unchanged on the copy.
"""
from __future__ import annotations

import io

import pikepdf

KEEP_IMAGE_BYTES = 1_500_000
DROP_PAGE_KEYS = ("/PieceInfo", "/Thumb", "/Annots", "/Metadata", "/AA", "/B")
MAX_DEPTH = 8


def _stream_len(obj) -> int:
    try:
        return int(obj.stream_dict.get("/Length", 0))
    except Exception:
        return 0


class _Stripper:
    def __init__(self, pdf: pikepdf.Pdf):
        self.pdf = pdf
        self.placeholder = None
        self.replaced = 0
        self.replaced_bytes = 0
        self.seen: set = set()

    def _ph(self):
        if self.placeholder is None:
            self.placeholder = self.pdf.make_stream(
                b"\xc8", Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image, Width=1, Height=1,
                ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8)
        return self.placeholder

    def resources(self, res, depth=0):
        if res is None or depth > MAX_DEPTH:
            return
        try:
            key = res.objgen
            if key != (0, 0):
                if key in self.seen:
                    return
                self.seen.add(key)
        except Exception:
            pass
        xobjs = res.get("/XObject")
        if xobjs is not None:
            for name in list(xobjs.keys()):
                try:
                    xo = xobjs[name]
                    sub = str(xo.get("/Subtype", ""))
                except Exception:
                    continue
                if sub == "/Image":
                    n = _stream_len(xo)
                    if n > KEEP_IMAGE_BYTES or "/SMask" in xo and _stream_len(xo.SMask) > KEEP_IMAGE_BYTES:
                        xobjs[name] = self._ph()
                        self.replaced += 1
                        self.replaced_bytes += n
                elif sub == "/Form":
                    for k in ("/PieceInfo", "/Metadata"):
                        if k in xo:
                            del xo[k]
                    self.resources(xo.get("/Resources"), depth + 1)
        pats = res.get("/Pattern")
        if pats is not None:
            for name in list(pats.keys()):
                try:
                    pat = pats[name]
                    if isinstance(pat, pikepdf.Stream):
                        self.resources(pat.get("/Resources"), depth + 1)
                except Exception:
                    continue


def make_lite(src, max_pages: int = 4) -> tuple[bytes, dict]:
    """src: path, binary file object (seekable), or an open pikepdf.Pdf."""
    pdf = src if isinstance(src, pikepdf.Pdf) else pikepdf.open(src)
    n_pages = len(pdf.pages)
    stripper = _Stripper(pdf)
    out = pikepdf.new()
    for i in range(min(n_pages, max_pages)):
        page = pdf.pages[i]
        for k in DROP_PAGE_KEYS:
            if k in page.obj:
                del page.obj[k]
        try:
            res = page.resources
        except Exception:
            res = page.obj.get("/Resources")
        stripper.resources(res)
        out.pages.append(page)
    buf = io.BytesIO()
    out.save(buf, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    info = {"source_pages": n_pages, "images_replaced": stripper.replaced,
            "image_bytes_skipped": stripper.replaced_bytes}
    return buf.getvalue(), info
