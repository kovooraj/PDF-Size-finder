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
