"""Label the calibration charts with Claude Opus 5.5 — one fixed prompt, one JSON form, every response kept."""
import base64, json, sys, time
from pathlib import Path
import anthropic
from kotsin_nse.config import Settings

S = Path(sys.argv[1]) / "chart_labels"; PNG = S / "png"; OUT = S / (sys.argv[2] if len(sys.argv) > 2 else "calib_opus.jsonl")
MODEL = "claude-opus-5-5"
METHOD = """You are grading intraday setups for a discretionary Indian equity trader. Each image is a 5-minute candlestick
chart of one NSE stock from the 09:15 open up to and including the last bar shown; nothing after it is known.
Prices are shown as percent of the previous day's close. Drawn on the chart: the day's Central Pivot Range
(the shaded band: TC on top, P in the middle, BC at the bottom, from the previous day's high, low and close),
R1 and S1, and the previous day's high and low (PDH, PDL, dashed). The side of the setup is written on the chart
and one bar is marked "retest". The last bar is the bar after the retest, the one on which the entry would be
taken at its close.

The trader's method, in his words:
- The sequence: price tests the open side of the CPR, breaks structure in the trade's direction, closes
  through the CPR edge (TC for a long, BC for a short), then comes back to retest that edge and the retest
  holds; the first close beyond the retest bar is the entry.
- A proper retest is a pause after an impulse, not a pause inside it: price should have travelled well beyond
  the edge before coming back, and the pullback should hold the edge (wicks through it are fine if the body
  does not close through).
- A proper double bottom (for a long; double top for a short): two lows at the level, the second holding at
  or above the first, with a rejection in between — stability on the level.
- The candle where a pattern first shows is an alert, not the entry; the entry is the confirmation after it.
- A rejection at the CPR edge: price pushes through TC (or BC) and fails within a few bars, closing back
  inside; the trader fades that.
- He wants room: the next level in the way (R1/S1, PDH/PDL) should be far enough from the entry to pay.

Grade only what the chart shows. Do not guess the stock, the date, or what happened next."""
SCHEMA = {
    "type": "object",
    "properties": {
        "double_bottom": {"type": "string", "enum": ["none", "weak", "proper"]},
        "retest": {"type": "string", "enum": ["none", "weak", "clean"]},
        "rejection_at_edge": {"type": "string", "enum": ["no", "yes"]},
        "impulse": {"type": "string", "enum": ["weak", "moderate", "strong"]},
        "take": {"type": "string", "enum": ["no", "yes"]},
        "reason": {"type": "string"},
    },
    "required": ["double_bottom", "retest", "rejection_at_edge", "impulse", "take", "reason"],
    "additionalProperties": False,
}
client = anthropic.Anthropic(api_key=Settings().anthropic_api_key.get_secret_value())
done = {json.loads(l)["blind_id"] for l in OUT.read_text().splitlines()} if OUT.exists() else set()
ids = sorted(p.stem for p in PNG.glob("c*.png"))
tot_in = tot_out = tot_cache = 0
with OUT.open("a") as f:
    for bid in ids:
        if bid in done:
            continue
        img = base64.standard_b64encode((PNG / f"{bid}.png").read_bytes()).decode()
        t0 = time.time()
        resp = client.messages.create(
            model=MODEL, max_tokens=1500,
            system=[{"type": "text", "text": METHOD, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
                {"type": "text", "text": "Grade this setup in the JSON form."},
            ]}],
        )
        if resp.stop_reason == "refusal":
            rec = {"blind_id": bid, "error": "refusal", "category": getattr(resp.stop_details, "category", None)}
        else:
            text = next(b.text for b in resp.content if b.type == "text")
            rec = {"blind_id": bid, "model": resp.model, "request_id": resp._request_id, "label": json.loads(text),
                   "usage": resp.usage.to_dict(), "seconds": round(time.time() - t0, 1)}
            u = resp.usage; tot_in += u.input_tokens; tot_out += u.output_tokens; tot_cache += (u.cache_read_input_tokens or 0)
        f.write(json.dumps(rec) + "\n"); f.flush()
        print(bid, rec.get("label", rec)["take"] if "label" in rec else rec, flush=True)
print("tokens in", tot_in, "cache_read", tot_cache, "out", tot_out)
print("cost est $", round(tot_in * 4 / 1e6 + tot_cache * 0.2 / 1e6 + tot_out * 20 / 1e6, 3))
