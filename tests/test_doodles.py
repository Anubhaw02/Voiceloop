import io
import json

from PIL import Image

from brand_agent.ai_research import build_doodle
from brand_agent.cards import alt_text, card_from_json, render_image
from tests.test_ai_research import ABSTRACT, make_item

FLOW = {
    "layout": "flow",
    "title": "Where KVShare saves GPU memory",
    "nodes": [
        {"label": "Tenants send requests", "status": "neutral"},
        {"label": "Shared KV cache", "note": "reused across tenants", "status": "good"},
        {"label": "Idle GPU memory", "note": "the old way", "status": "bad"},
    ],
    "caption": "37% lower p99 latency on production traces.",
}
COMPARE = {
    "layout": "compare",
    "title": "Per-tenant caches vs shared caches",
    "left": {"title": "Per-tenant", "points": ["Memory sits idle", "Higher tail latency"]},
    "right": {"title": "KVShare", "points": ["Caches shared", "2.1x GPU utilisation"]},
    "caption": "Tested across 3 cloud regions.",
}
BARS = {
    "layout": "bars",
    "title": "KVShare vs vLLM",
    "bars": [{"label": "Tenants tested", "value": 1200}, {"label": "Latency cut (%)", "value": 37, "highlight": True}],
    "caption": "Production traces.",
}


def test_each_layout_renders_a_portrait_png():
    item = make_item()
    for raw in (FLOW, COMPARE, BARS):
        doodle = build_doodle(raw, item, {"identity": {"display_name": "Anubhaw"}}, ABSTRACT)
        assert doodle is not None, raw["layout"]
        image = Image.open(io.BytesIO(render_image(doodle)))
        assert image.format == "PNG" and image.size == (1080, 1350)


def test_doodle_numbers_must_come_from_the_source():
    item = make_item()
    invented = json.loads(json.dumps(BARS))
    invented["bars"][1]["value"] = 80  # not in the paper
    assert build_doodle(invented, item, {}, ABSTRACT) is None
    bad_caption = dict(FLOW, caption="Cuts cloud bills by 90% overnight.")
    assert build_doodle(bad_caption, item, {}, ABSTRACT) is None


def test_invalid_doodles_are_rejected():
    item = make_item()
    assert build_doodle(dict(FLOW, layout="pie"), item, {}, ABSTRACT) is None
    assert build_doodle(dict(FLOW, nodes=FLOW["nodes"][:1]), item, {}, ABSTRACT) is None
    assert build_doodle(dict(COMPARE, right={"title": "", "points": []}), item, {}, ABSTRACT) is None
    assert build_doodle(dict(FLOW, title="x" * 200), item, {}, ABSTRACT) is None
    assert build_doodle("nope", item, {}, ABSTRACT) is None
    assert build_doodle(dict(FLOW, caption="At my company we built this"), item, {}, ABSTRACT) is None


def test_doodle_storage_toggle_and_alt_text():
    doodle = build_doodle(FLOW, make_item(), {}, ABSTRACT)
    assert card_from_json(json.dumps(doodle))["kind"] == "doodle"
    assert card_from_json(json.dumps(dict(doodle, enabled=False))) is None
    text = alt_text(doodle)
    assert text.startswith("Hand-drawn diagram: Where KVShare") and "Shared KV cache" in text
