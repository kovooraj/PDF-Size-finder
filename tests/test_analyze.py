import json
import os
import types

import pytest

from artwork_dims import analyze as analyze_mod
from artwork_dims import ai as ai_mod
from artwork_dims.analyze import analyze, analyze_pdf, strip_private
from artwork_dims.textdims import find_callouts, labelled_dims, matching_spec
from tests import samples


def fields(result, page=0):
    return {k: v["value_in"] for k, v in result["pages"][page]["result"]["fields"].items()}


def run(pdf, ptype=None):
    return strip_private(analyze_pdf(pdf, ptype))


def test_business_card_from_crop_marks():
    r = run(samples.business_card())
    p = r["pages"][0]
    assert p["result"]["product_type"] == "commercial"
    assert fields(r) == {"width": 3.5, "height": 2.0}
    assert p["result"]["size_basis"]["trim_source"] == "crop/trim marks"
    assert any("bleeds about 0.125" in n for n in p["result"]["notes"])


def test_trimbox_wins():
    r = run(samples.trimbox_flyer())
    assert fields(r) == {"width": 8.5, "height": 11.0}
    assert r["pages"][0]["result"]["size_basis"]["trim_source"] == "PDF TrimBox"


def test_whitespace_is_not_product_size():
    r = run(samples.oversized_page())
    assert fields(r) == {"width": 5.0, "height": 7.0}
    assert r["pages"][0]["result"]["warnings"]


def test_roll_label_cut_contour():
    r = run(samples.roll_label())
    p = r["pages"][0]["result"]
    assert p["product_type"] == "roll_label"
    assert fields(r) == {"width": 3.0, "height": 4.0}
    assert any("rounded-corner" in n for n in p["notes"])


@pytest.mark.parametrize("L,W,D", [(3.0, 2.0, 6.0), (4.25, 1.75, 5.5)])
def test_carton_lwd(L, W, D):
    r = run(samples.rte_carton(L=L, W=W, D=D))
    f = fields(r)
    assert r["pages"][0]["result"]["product_type"] == "folding_carton"
    assert f["length"] == pytest.approx(L, abs=0.01)
    assert f["width"] == pytest.approx(W, abs=0.01)
    assert f["depth"] == pytest.approx(D, abs=0.01)
    assert f["flat_width"] == pytest.approx(0.5 + 2 * L + 2 * W, abs=0.01)


@pytest.mark.parametrize("W,H,G", [(6.0, 9.0, 4.0), (4.0, 6.0, 2.5)])
def test_pouch_side_by_side_layout(W, H, G):
    r = run(samples.standup_pouch(W=W, H=H, G=G))
    f = fields(r)
    assert r["pages"][0]["result"]["product_type"] == "pouch"
    assert (f["width"], f["height"], f["gusset"]) == pytest.approx((W, H, G), abs=0.01)


def test_callouts_value_before_label_and_qualifiers():
    text = ('6.00" Width\n6.25" Width + Bleed\n8.00" Height\n2.00" Gusset\n'
            'Zipper Width:\n10.0mm\nSeal Width: 8mm\nPouch Dimensions:\n8.00"x6.00"x2.00"\n')
    cs = find_callouts(text)
    assert labelled_dims(cs) == {"width": 6.0, "height": 8.0, "gusset": 2.0}
    assert matching_spec(cs, [6.0, 8.0, 2.0]) is not None
    assert matching_spec(cs, [6.25, 8.0, 2.0]) is None


@pytest.mark.parametrize("text,vals", [
    ('3.5" x 2"', [3.5, 2.0]),
    ("4 x 2 x 6 in", [4.0, 2.0, 6.0]),
    ("120mm x 80mm", [120 / 25.4, 80 / 25.4]),
    ('3-1/2" x 2"', [3.5, 2.0]),
])
def test_callout_formats(text, vals):
    c = find_callouts(text)[0]
    assert c.values_in == pytest.approx(vals, abs=1e-3)


def test_ignores_quantities():
    assert find_callouts("Qty 500 x 2 versions") == []


# ---------------------------------------------------------------- AI merge (stubbed client)

class _FakeMessages:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(
            stop_reason="end_turn", model=kw["model"],
            content=[types.SimpleNamespace(type="text", text=json.dumps(self.payload))])


class _FakeClient:
    def __init__(self, payload):
        self.beta = types.SimpleNamespace(messages=_FakeMessages(payload))


def _ai_payload(ptype):
    return {"product_type": ptype, "product_type_reason": "x", "dimensions": [],
            "agrees_with_measurement": True, "disagreements": [], "summary_for_sales": "s",
            "questions_for_customer": [], "artwork_observations": []}


def test_ai_type_override_reruns_rules(monkeypatch):
    # The rules call the card 'commercial'; the AI says roll label -> rules re-run as roll label.
    fake = _FakeClient(_ai_payload("roll_label"))
    monkeypatch.setattr(ai_mod.anthropic, "Anthropic", lambda: fake)
    r = analyze(samples.business_card(), None, use_ai=True)
    p = r["pages"][0]
    assert p["result"]["product_type"] == "roll_label"
    assert p["result"]["product_type_source"] == "identified by AI from the artwork"
    assert p["result_rules_guess"]["product_type"] == "commercial"
    call = fake.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert sum(1 for b in call["messages"][0]["content"] if b["type"] == "image") == 2
    assert "_clean_png" not in p and "_facts" not in p


def test_ai_failure_keeps_measurement(monkeypatch):
    def boom():
        raise RuntimeError("no network")
    monkeypatch.setattr(ai_mod.anthropic, "Anthropic", boom)
    r = analyze(samples.business_card(), None, use_ai=True)
    p = r["pages"][0]
    assert "error" in p["ai_review"]
    assert fields(r) == {"width": 3.5, "height": 2.0}


# ---------------------------------------------------------------- real files (optional)

REAL_POUCH = os.environ.get("ARTWORK_DIMS_REAL_POUCH")


@pytest.mark.skipif(not REAL_POUCH, reason="set ARTWORK_DIMS_REAL_POUCH to a real 6x8x2 pouch dieline PDF")
def test_real_pouch_template():
    r = run(open(REAL_POUCH, "rb").read())
    for i in range(len(r["pages"])):
        f = fields(r, i)
        assert (f["width"], f["height"], f["gusset"]) == pytest.approx((6.0, 8.0, 2.0), abs=0.01)


# ---------------------------------------------------------------- large files

def test_lite_copy_gives_same_dimensions():
    from artwork_dims.lite import make_lite
    for name, fn in samples.ALL.items():
        full = run(fn())
        lite, info = make_lite(__import__("io").BytesIO(fn()))
        assert fields(run(lite)) == fields(full), name


def test_big_images_are_replaced(tmp_path):
    import io as _io
    import pikepdf
    from artwork_dims.intake import from_fileobj
    from artwork_dims.lite import KEEP_IMAGE_BYTES
    pdf = pikepdf.open(_io.BytesIO(samples.business_card()))
    pg = pdf.pages[0]
    # random bytes: incompressible, so the stored stream really is over the threshold
    big = pdf.make_stream(__import__("os").urandom(KEEP_IMAGE_BYTES + 1000), Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image,
                          Width=1000, Height=(KEEP_IMAGE_BYTES + 1000) // 1000, ColorSpace=pikepdf.Name.DeviceGray,
                          BitsPerComponent=8)
    pg.obj.Resources.XObject = pikepdf.Dictionary(Im0=big)
    pg.contents_add(pdf.make_stream(b"q 100 0 0 50 40 40 cm /Im0 Do Q"))
    pg.obj.PieceInfo = pikepdf.Dictionary(Illustrator=pikepdf.Dictionary(Private=pdf.make_stream(b"x" * 100000)))
    path = tmp_path / "big.pdf"
    pdf.save(path)
    with open(path, "rb") as f:
        data, info = from_fileobj(f, KEEP_IMAGE_BYTES * 100)  # pretend it's huge to force the reduced copy
    assert info["reduced"] and info["images_replaced"] == 1
    assert len(data) < 50_000
    assert fields(run(data)) == {"width": 3.5, "height": 2.0}


def test_range_file_reads_little(tmp_path):
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from artwork_dims.remote import HTTPRangeFile

    class RangeHandler(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            import re as _re
            p = tmp_path / self.path.lstrip("/")
            data = p.read_bytes()
            m = _re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
            a, b = int(m.group(1)), min(int(m.group(2) or len(data) - 1), len(data) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
            self.send_header("Content-Length", str(b - a + 1))
            self.end_headers()
            self.wfile.write(data[a:b + 1])

    blob = bytes(range(256)) * 120000  # ~30 MB
    (tmp_path / "f.bin").write_bytes(blob)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(RangeHandler, directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        f = HTTPRangeFile(f"http://127.0.0.1:{srv.server_port}/f.bin")
        assert f.size == len(blob)
        f.seek(len(blob) - 100)
        assert f.read(100) == blob[-100:]
        f.seek(12345)
        assert f.read(10) == blob[12345:12355]
        assert f.bytes_fetched < len(blob)
    finally:
        srv.shutdown()


def test_pouch_without_gusset_asks_for_it():
    r = run(samples.layflat_sheet(), "pouch")
    res = r["pages"][0]["result"]
    pr = res["prompt"]
    assert pr and pr["type"] == "pouch_gusset"
    assert (pr["sheet_w_in"], pr["sheet_h_in"]) == (10.0, 4.0)
    assert pr["suggested_layout"] == "front_back" and pr["sheet_source"] == "crop/trim marks"


def test_pouch_with_folds_does_not_ask():
    assert run(samples.standup_pouch(), "pouch")["pages"][0]["result"]["prompt"] is None
    assert run(samples.layflat_sheet(), "commercial")["pages"][0]["result"]["prompt"] is None
