"""A self-expiring page of one session's signals, served from the engine itself.

``/temporary`` is a link to hand to somebody for a day: every FUDKII trigger of the session it was
created for, with the levels, the volume surge on both legs, where the nearest raw pivot sat
against the trigger bar, what each book did with it, and the reason anything that did not trade did
not trade. It is read-only and renders from the ledger on each request, so it stays current as the
session runs, and it deletes itself the moment it is asked for after its expiry — no background
task to forget, no file left behind.
"""

from __future__ import annotations

import html
import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..market.session import IST
from ..strategy.gapscore import f14_score, gap_open_class

TTL_HOURS = 24


@dataclass(frozen=True, slots=True)
class Ticket:
    """What was created, for which session, and when it stops being served."""

    day: str
    created_ts: float
    expires_ts: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_ts

    @property
    def hours_left(self) -> float:
        return max(0.0, (self.expires_ts - time.time()) / 3600)

    def to_json(self) -> dict[str, Any]:
        return {"day": self.day, "created_ts": self.created_ts, "expires_ts": self.expires_ts,
                "hours_left": round(self.hours_left, 2)}


class TemporaryPage:
    """The ticket on disk. Absent file, absent page — which is also how it is deleted."""

    def __init__(self, root: Path) -> None:
        self.path = root / "temporary.json"

    def create(self, day: date, hours: float = TTL_HOURS) -> Ticket:
        now = time.time()
        t = Ticket(day=day.isoformat(), created_ts=now, expires_ts=now + hours * 3600)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(t.to_json()))
        return t

    def read(self) -> Ticket | None:
        """The live ticket, or None. An expired one deletes itself here rather than lingering."""
        try:
            d = json.loads(self.path.read_text())
            t = Ticket(day=d["day"], created_ts=float(d["created_ts"]), expires_ts=float(d["expires_ts"]))
        except (OSError, ValueError, KeyError):
            return None
        if t.expired:
            self.revoke()
            return None
        return t

    def revoke(self) -> None:
        self.path.unlink(missing_ok=True)


# -- assembling one session ---------------------------------------------------------------------


def _pivot_read(bar: dict[str, Any], levels: dict[str, float] | None, atr: float) -> dict[str, Any] | None:
    """Where the nearest raw classic pivot sat against the trigger bar, in ATR."""
    if not levels or not atr:
        return None
    o, hi, lo, cl = bar["open"], bar["high"], bar["low"], bar["close"]
    label, price = min(levels.items(), key=lambda kv: abs(kv[1] - cl))
    near_name, near_px = min(
        (("close", cl), ("open", o), ("high", hi), ("low", lo)), key=lambda p: abs(price - p[1])
    )
    return {
        "label": label, "price": round(price, 2), "inside_bar": lo <= price <= hi,
        "nearest_to": near_name, "atr_from_close": round((price - cl) / atr, 2),
        "atr_from_nearest": round(abs(price - near_px) / atr, 2),
    }


def _levels_ahead(close: float, levels: dict[str, float] | None, bullish: bool, n: int = 4
                  ) -> list[tuple[str, float]]:
    """The future's own classic levels ahead of its close, nearest first.

    Not a second confluence ladder — the trade's stop and targets are computed on the equity and
    mapped onto the option through delta, and nothing here changes that. These are the levels the
    future would have to pass through, reported so the read has both legs.
    """
    if not levels:
        return []
    ahead = [(k, v) for k, v in levels.items() if (v > close) == bullish and v != close]
    return sorted(ahead, key=lambda kv: abs(kv[1] - close))[:n]


def _level_behind(close: float, levels: dict[str, float] | None, bullish: bool) -> tuple[str, float] | None:
    if not levels:
        return None
    behind = [(k, v) for k, v in levels.items() if (v < close) == bullish and v != close]
    return min(behind, key=lambda kv: abs(kv[1] - close)) if behind else None


def assemble(
    *, signals: list[dict], positions: list[dict], trades: list[dict], events: list[dict],
    market: dict[str, dict[str, float]] | None = None, vix: float | None = None,
) -> list[dict[str, Any]]:
    """One row per trigger, with everything the day book shows. Pure: rows in, rows out.

    ``market`` carries what the two gap readings need and a signal does not: each symbol's previous
    close, its daily ATR and today's session open. Absent, those columns are simply blank — the
    reading is advisory and never worth failing a page for.
    """
    market = market or {}
    routes = {e["signal_id"]: e for e in events if e.get("kind") == "counter.route" and e.get("signal_id")}
    skips: dict[str, list[dict]] = {}
    for e in events:
        if e.get("kind") == "rt_twin.skipped" and e.get("signal_id"):
            skips.setdefault(e["signal_id"], []).append(e)
    trade_by_pos = {t.get("position_id"): t for t in trades}

    out: list[dict[str, Any]] = []
    for sig in sorted(signals, key=lambda s: s["ts"]):
        if sig.get("strategy") != "FUDKII":
            continue
        conf = (sig.get("context") or {}).get("confluence") or {}
        zones = (sig.get("context") or {}).get("zones") or []
        ev = sig.get("evidence") or {}
        atr = float(ev.get("atr") or 0.0)
        entry = float(sig.get("entry") or 0.0)
        bull = sig.get("direction") == "BULLISH"
        ahead = [z for z in zones if (z["price"] > entry) == bull and z["price"] != entry]
        walls = [z for z in ahead if z.get("wall")]
        wall = min(walls, key=lambda z: abs(z["price"] - entry)) if walls else None
        legs = {leg["name"]: leg for leg in ((routes.get(sig["signal_id"]) or {}).get("legs") or [])}
        eq, fut = legs.get("equity"), legs.get("future")

        fills = []
        for p in positions:
            if p.get("symbol") != sig.get("symbol") or abs(float(p.get("opened_ts") or 0) - sig["ts"]) > 3600:
                continue
            t = trade_by_pos.get(p.get("id")) or {}
            inst = p.get("instrument") or {}
            lot = int(inst.get("lot_size") or 1)
            fills.append({
                "book": p.get("strategy", ""), "contract": inst.get("name", ""),
                "opened_ts": p.get("opened_ts"), "closed_ts": p.get("closed_ts"),
                "qty": p.get("qty"), "lots": int(p.get("qty") or 0) // max(1, lot),
                "premium": p.get("entry"), "opt_stop": p.get("initial_option_sl"),
                "opt_stop_now": p.get("option_sl"), "opt_targets": p.get("option_targets") or [],
                "status": p.get("status"), "exit_price": p.get("exit_price"),
                "exit_reason": p.get("exit_reason"), "net": t.get("net"), "r": t.get("r_multiple"),
            })
        fills.sort(key=lambda f: (f["book"] != "FUDKII", f["book"]))

        fut_close = fut["close"] if fut else 0.0
        fut_levels = fut.get("levels") if fut else None
        fut_stop = _level_behind(fut_close, fut_levels, bull) if fut else None
        out.append({
            "ts": sig["ts"], "fired_ts": sig.get("created_ts") or sig["ts"],
            "symbol": sig.get("symbol"), "dir": sig.get("direction"),
            "grade": sig.get("grade"), "decision": sig.get("decision"),
            "reason": sig.get("decision_reason") or sig.get("reason") or "",
            "entry": entry, "atr": round(atr, 2), "atr_pct": round(float(ev.get("atr_pct") or 0), 2),
            "rr": conf.get("rr"), "fortress": conf.get("fortress"), "room_atr": conf.get("room_ratio"),
            "eq_stop": conf.get("stop"), "eq_stop_zone": conf.get("stop_zone"),
            "eq_targets": conf.get("targets") or [], "eq_target_zones": conf.get("target_zones") or [],
            "wall_price": wall["price"] if wall else None,
            "wall_strength": wall["strength"] if wall else None,
            "wall_atr": round(abs(wall["price"] - entry) / atr, 2) if wall and atr else None,
            "surge_t": round(eq["surgeT"], 2) if eq else None,
            "surge_t1": round(eq["surgeT1"], 2) if eq else None,
            "vol_label": eq["volume"] if eq else None,
            "fut_surge_t": round(fut["surgeT"], 2) if fut else None,
            "fut_surge_t1": round(fut["surgeT1"], 2) if fut else None,
            "fut_vol_label": fut["volume"] if fut else None,
            "bar_eq": {k: eq[k] for k in ("open", "high", "low", "close")} if eq else None,
            "bar_fut": {k: fut[k] for k in ("open", "high", "low", "close")} if fut else None,
            "pivot_eq": _pivot_read(eq, eq.get("levels"), eq.get("atr")) if eq else None,
            "pivot_fut": _pivot_read(fut, fut.get("levels"), fut.get("atr")) if fut else None,
            "oi": ev.get("oi"), "oi_change_pct": ev.get("oi_change_pct"),
            "fut_stop": {"label": fut_stop[0], "price": round(fut_stop[1], 2)} if fut_stop else None,
            "fut_targets": [{"label": k, "price": round(v, 2)}
                            for k, v in _levels_ahead(fut_close, fut_levels, bull)],
            # the contract actually bought, or the strikes the selector tried and could not
            "contract": fills[0]["contract"] if fills else None,
            **_gap_reads(sig, eq, atr, market.get(sig.get("symbol") or "", {}), vix, bull, conf),
            "skips": [{"book": s.get("book", ""), "why": s.get("reason", "")} for s in skips.get(sig["signal_id"], [])],
            "fills": fills,
        })
    return out


# -- rendering ------------------------------------------------------------------------------------

_CSS = """
:root{--paper:#FAF8F5;--sunk:#F2EFE9;--line:#DCD6CB;--soft:#EBE6DD;--ink:#1D2026;--ink2:#4E5461;
--ink3:#7C8494;--indigo:#3F4A73;--long:#1F6F52;--longbg:#E3F0E9;--short:#A8323F;--shortbg:#F8E4E5;
--hold:#8A6413;--holdbg:#F7EBD3}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--paper:#15171C;--sunk:#1C1F26;
--line:#2E333D;--soft:#242832;--ink:#E9E6E0;--ink2:#A8AFBD;--ink3:#767E8D;--indigo:#9AA6D6;
--long:#5FBF93;--longbg:#16291F;--short:#E0737F;--shortbg:#2B191B;--hold:#D6A845;--holdbg:#2A2213}}
:root[data-theme=dark]{--paper:#15171C;--sunk:#1C1F26;--line:#2E333D;--soft:#242832;--ink:#E9E6E0;
--ink2:#A8AFBD;--ink3:#767E8D;--indigo:#9AA6D6;--long:#5FBF93;--longbg:#16291F;--short:#E0737F;
--shortbg:#2B191B;--hold:#D6A845;--holdbg:#2A2213}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:400 14px/1.5 "IBM Plex Sans",system-ui,-apple-system,sans-serif}
.wrap{padding-inline:20px;padding-block:26px 60px;max-width:1560px;margin:0 auto}
h1{font:700 27px/1.15 "IBM Plex Sans Condensed","IBM Plex Sans",sans-serif;margin:0;letter-spacing:-.01em}
.eyebrow{font:600 10.5px/1 "IBM Plex Sans Condensed",sans-serif;letter-spacing:.14em;
text-transform:uppercase;color:var(--ink3);margin:0 0 5px}
.sub{color:var(--ink2);margin:6px 0 0;max-width:68ch}
header{border-bottom:2px solid var(--ink);padding-bottom:15px;margin-bottom:18px;
display:flex;flex-wrap:wrap;gap:18px;justify-content:space-between;align-items:flex-end}
.tally{display:flex;flex-wrap:wrap;gap:8px}
.tal{background:var(--sunk);border:1px solid var(--soft);border-radius:3px;padding:6px 10px;min-width:78px}
.tal b{display:block;font:600 17px/1.15 "IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
.tal span{font:500 9.5px/1.3 "IBM Plex Sans Condensed",sans-serif;letter-spacing:.09em;
text-transform:uppercase;color:var(--ink3)}
.expiry{background:var(--holdbg);color:var(--hold);border:1px solid currentColor;border-radius:3px;
padding:7px 11px;font:500 12px/1.3 "IBM Plex Sans Condensed",sans-serif;letter-spacing:.04em}
h2{font:600 11px/1 "IBM Plex Sans Condensed",sans-serif;letter-spacing:.13em;text-transform:uppercase;
color:var(--ink3);margin:30px 0 9px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.scroll{overflow-x:auto;border:1px solid var(--soft);border-radius:3px;background:var(--sunk)}
table{border-collapse:collapse;width:100%;font:400 12px/1.45 "IBM Plex Mono",monospace;
font-variant-numeric:tabular-nums;white-space:nowrap}
th{font:600 9.5px/1.25 "IBM Plex Sans Condensed",sans-serif;letter-spacing:.08em;text-transform:uppercase;
color:var(--ink3);text-align:right;padding:9px 10px;border-bottom:1px solid var(--line);
background:var(--sunk);position:sticky;top:0;z-index:2}
td{padding:7px 10px;border-bottom:1px solid var(--soft);text-align:right}
tr:last-child td{border-bottom:0}
th:first-child,td:first-child{text-align:left;position:sticky;left:0;background:var(--sunk);z-index:1}
th:first-child{z-index:3}
td.sym{font:600 12.5px/1.3 "IBM Plex Sans Condensed",sans-serif;letter-spacing:.01em}
td.l,th.l{text-align:left}
.chip{display:inline-block;font:600 9.5px/1 "IBM Plex Sans Condensed",sans-serif;letter-spacing:.07em;
text-transform:uppercase;padding:4px 7px;border-radius:2px}
.filled{background:var(--longbg);color:var(--long)}
.refused{background:var(--shortbg);color:var(--short)}
.blocked{background:var(--holdbg);color:var(--hold)}
.neg{color:var(--short)} .pos{color:var(--long)}
.dim{color:var(--ink3)}
.why{white-space:normal;max-width:52ch;color:var(--ink2);font-size:11.5px;line-height:1.45}
.inside{color:var(--indigo);font-weight:600}
footer{margin-top:30px;padding-top:15px;border-top:1px solid var(--line);color:var(--ink3);
font-size:12.5px;max-width:74ch}
footer b{color:var(--ink2)}
footer p{margin:0 0 8px}
"""


def _fmt(v: Any, dp: int = 2) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:,.{dp}f}"
    return html.escape(str(v))


def _bucket(decision: str) -> str:
    return {"PAPER_FILLED": "filled", "WALLET_HALTED": "blocked"}.get(decision, "refused")


_LABEL = {"PAPER_FILLED": "filled", "WALLET_HALTED": "book halted",
          "REJECTED_BOOK": "stale depth", "NO_INSTRUMENT": "no strike"}


def render(rows: list[dict[str, Any]], ticket: Ticket) -> str:
    """The whole session as two wide, scrollable tables. Everything visible, nothing to click."""
    ist = lambda ts: datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S") if ts else "—"  # noqa: E731
    counts = {"filled": 0, "refused": 0, "blocked": 0}
    for r in rows:
        counts[_bucket(r["decision"])] += 1
    net = sum(f["net"] or 0 for r in rows for f in r["fills"] if f.get("net") is not None)
    fills = [(r, f) for r in rows for f in r["fills"]]

    head = [
        "Fired", "Symbol", "Dir", "Gr", "Outcome", "OTM contract", "Bar", "Entry", "ATR", "ATR%",
        "RR", "Fortress", "Room ATR",
        "Eq stop", "Stop zone", "Eq T1", "Eq T2", "Eq T3", "Eq T4", "Target zones",
        "Fut stop", "Fut T1", "Fut T2", "Fut T3", "Fut T4",
        "Wall", "Wall str", "Wall ATR",
        "Vol surge T", "Vol surge T−1", "Vol", "Fut surge T", "Fut surge T−1", "Fut vol",
        "OI", "OI chg%",
        "Gap class", "Gap %", "Gap/ATR1d", "F14", "F14 says", "F14 tier", "F14 components",
        "Eq bar O/H/L/C", "Pivot", "Pivot px", "In bar", "Nearest", "ATR away",
        "Fut bar O/H/L/C", "Fut pivot", "Fut in bar", "Fut ATR away", "Why",
    ]
    body = []
    for r in rows:
        b, pe, pf, be, bf = _bucket(r["decision"]), r["pivot_eq"], r["pivot_fut"], r["bar_eq"], r["bar_fut"]
        ohlc = lambda x: "—" if not x else " · ".join(_fmt(x[k]) for k in ("open", "high", "low", "close"))  # noqa: E731
        eqt = list(r["eq_targets"]) + [None] * 4
        ft = list(r["fut_targets"]) + [None] * 4
        fs = r["fut_stop"]
        body.append(
            f'<tr><td class="sym">{ist(r["fired_ts"])}</td><td class="sym l">{html.escape(r["symbol"] or "")}</td>'
            f'<td>{"long" if r["dir"] == "BULLISH" else "short"}</td><td>{html.escape(r["grade"] or "—")}</td>'
            f'<td><span class="chip {b}">{_LABEL.get(r["decision"], r["decision"])}</span></td>'
            f'<td class="l">{html.escape(r["contract"] or "—")}</td>'
            f'<td class="dim">{ist(r["ts"])}</td>'
            f'<td>{_fmt(r["entry"])}</td><td>{_fmt(r["atr"])}</td><td>{_fmt(r["atr_pct"])}%</td>'
            f'<td>{_fmt(r["rr"])}</td><td>{_fmt(r["fortress"], 1)}</td><td>{_fmt(r["room_atr"])}</td>'
            f'<td>{_fmt(r["eq_stop"])}</td><td class="dim l">{html.escape(r["eq_stop_zone"] or "—")}</td>'
            + "".join(f"<td>{_fmt(t)}</td>" for t in eqt[:4])
            + f'<td class="dim l">{html.escape(" / ".join(r["eq_target_zones"]) or "—")}</td>'
            f'<td>{(_fmt(fs["price"]) + " " + fs["label"]) if fs else "—"}</td>'
            + "".join(f'<td>{(_fmt(t["price"]) + " " + t["label"]) if t else "—"}</td>' for t in ft[:4])
            + f'<td>{_fmt(r["wall_price"])}</td><td>{_fmt(r["wall_strength"], 1)}</td>'
            f'<td>{_fmt(r["wall_atr"])}</td><td>{_fmt(r["surge_t"])}</td><td>{_fmt(r["surge_t1"])}</td>'
            f'<td class="dim">{html.escape(r["vol_label"] or "—")}</td>'
            f'<td>{_fmt(r["fut_surge_t"])}</td><td>{_fmt(r["fut_surge_t1"])}</td>'
            f'<td class="dim">{html.escape(r["fut_vol_label"] or "—")}</td>'
            f'<td>{_fmt(r["oi"], 0)}</td><td>{_fmt(r["oi_change_pct"])}</td>'
            + _gap_cells(r)
            + f'<td>{ohlc(be)}</td>'
            f'<td class="dim l">{html.escape(pe["label"]) if pe else "—"}</td><td>{_fmt(pe["price"]) if pe else "—"}</td>'
            f'<td class="{"inside" if pe and pe["inside_bar"] else "dim"}">{("yes" if pe["inside_bar"] else "no") if pe else "—"}</td>'
            f'<td class="dim">{html.escape(pe["nearest_to"]) if pe else "—"}</td>'
            f'<td>{_fmt(pe["atr_from_nearest"]) if pe else "—"}</td>'
            f'<td>{ohlc(bf)}</td>'
            f'<td class="dim l">{html.escape(pf["label"]) if pf else "—"}</td>'
            f'<td class="{"inside" if pf and pf["inside_bar"] else "dim"}">{("yes" if pf["inside_bar"] else "no") if pf else "—"}</td>'
            f'<td>{_fmt(pf["atr_from_nearest"]) if pf else "—"}</td>'
            f'<td class="why l">{html.escape(r["reason"])}</td></tr>'
        )

    fhead = ["Entry time", "Symbol", "Book", "OTM contract", "Lots", "Qty", "Premium", "Option SL",
             "SL now", "Option targets", "Status", "Exit", "Exit time", "Reason", "Net", "R"]
    fbody = []
    for r, f in fills:
        net_cls = "neg" if (f.get("net") or 0) < 0 else "pos"
        fbody.append(
            f'<tr><td class="sym">{ist(f["opened_ts"])}</td><td class="sym l">{html.escape(r["symbol"])}</td>'
            f'<td class="l">{html.escape(f["book"].replace("FUDKII_", "").replace("FUDKII", "PARENT"))}</td>'
            f'<td class="l">{html.escape(f["contract"])}</td><td>{f["lots"]}</td><td>{f["qty"]:,}</td>'
            f'<td>{_fmt(f["premium"])}</td><td>{_fmt(f["opt_stop"])}</td><td>{_fmt(f["opt_stop_now"])}</td>'
            f'<td>{" · ".join(_fmt(t) for t in f["opt_targets"]) or "—"}</td>'
            f'<td class="dim">{html.escape(f["status"] or "")}</td><td>{_fmt(f["exit_price"])}</td>'
            f'<td>{ist(f["closed_ts"])}</td><td class="dim l">{html.escape(f["exit_reason"] or "—")}</td>'
            f'<td class="{net_cls}">{_fmt(f["net"], 0)}</td>'
            f'<td class="{net_cls}">{_fmt(f["r"])}</td></tr>'
        )

    skips = [(r, s) for r in rows for s in r["skips"]]
    skip_html = "".join(
        f'<tr><td class="sym l">{html.escape(r["symbol"])}</td>'
        f'<td class="l">{html.escape(s["book"].replace("FUDKII_", ""))}</td>'
        f'<td class="why l">{html.escape(s["why"])}</td></tr>' for r, s in skips
    )
    pretty_day = datetime.fromisoformat(ticket.day).strftime("%d %B %Y")
    expires = datetime.fromtimestamp(ticket.expires_ts, IST).strftime("%d %b, %H:%M IST")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>FUDKII Day Book — {html.escape(pretty_day)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>{_CSS}</style></head><body><div class="wrap">
<header>
  <div><p class="eyebrow">NSE options · paper · rendered live from the ledger</p>
  <h1>FUDKII Day Book</h1>
  <p class="sub">Every trigger of {html.escape(pretty_day)}, what each book did with it, and the
  reason anything that did not trade did not trade. Scroll each table sideways for the full set of
  columns.</p></div>
  <div style="display:flex;flex-direction:column;gap:9px;align-items:flex-end">
    <div class="expiry">Temporary link · deletes itself {html.escape(expires)}</div>
    <div class="tally">
      <div class="tal"><b>{len(rows)}</b><span>signals</span></div>
      <div class="tal"><b>{counts["filled"]}</b><span>filled</span></div>
      <div class="tal"><b>{counts["refused"]}</b><span>refused</span></div>
      <div class="tal"><b>{counts["blocked"]}</b><span>book halted</span></div>
      <div class="tal"><b>{"−" if net < 0 else "+"}₹{abs(net):,.0f}</b><span>net across books</span></div>
    </div>
  </div>
</header>
<h2>Signals · {len(rows)}</h2>
<div class="scroll"><table><thead><tr>{"".join(f'<th class="l">{h}</th>' if h in ("Symbol", "OTM contract", "Stop zone", "Target zones", "Pivot", "Fut pivot",
                     "Gap class", "F14 says", "F14 components", "Why") else f"<th>{h}</th>" for h in head)}</tr></thead>
<tbody>{"".join(body)}</tbody></table></div>
<h2>Executions · {len(fills)}</h2>
<div class="scroll"><table><thead><tr>{"".join(f'<th class="l">{h}</th>' if h in ("Symbol", "Book", "Contract", "Reason") else f"<th>{h}</th>" for h in fhead)}</tr></thead>
<tbody>{"".join(fbody) or '<tr><td colspan="16" class="dim">nothing filled</td></tr>'}</tbody></table></div>
<h2>Twins that stood aside · {len(skips)}</h2>
<div class="scroll"><table><thead><tr><th class="l">Symbol</th><th class="l">Book</th><th class="l">Reason</th></tr></thead>
<tbody>{skip_html or '<tr><td colspan="3" class="dim">none</td></tr>'}</tbody></table></div>
<footer>
  <p><b>The pivot columns.</b> "Pivot" is the nearest raw classic pivot to the trigger bar's close.
  "In bar" says whether it fell between the bar's high and low. "ATR away" is its distance from
  whichever of open, high, low or close it sat closest to, in units of the 30-minute ATR — so 0.16
  means about a sixth of a typical bar.</p>
  <p><b>On the future.</b> The trade's stop and targets are computed on the equity and mapped onto
  the option through delta; nothing is decided on the future. Its stop and T1–T4 columns are its own
  classic pivots either side of its close, nearest first — the levels it would have to pass through,
  reported so the read has both legs, not a second ladder the trade acts on.</p>
  <p><b>Fired</b> is when the signal was actually emitted; <b>Bar</b> is the 30-minute bucket it
  fired on, which starts half an hour earlier. <b>OI</b> is the front future's open interest on the
  trigger bar and <b>OI chg%</b> its change.</p>
  <p><b>Volume surge</b> is the trigger bar against the T−2 to T−7 baseline; T−1 is the bar before
  it. Both legs are shown because the dried-volume gate reads both.</p>
  <p><b>Gap class and F14 are advisory and nothing routes on them.</b> They are the old stack's two
  gap readings, ported so a session can be read against what it would have said — this engine's own
  router does not look at the overnight gap at all. <b>Gap class</b> labels the 09:15 open against
  yesterday's close and today's daily pivots; a <b>*</b> means the gap was large enough to be
  "fill likely" but was labelled on S1 or R1 instead, because that classifier tests direction before
  magnitude and returns early. <b>F14</b> is a nine-component counter-trend score, flipping at 50
  and only when at least one candle-anchored component fired; its ATR-tier bonus is shown but not
  added, matching the setting that stack ships disabled. Two components are missing here because
  their inputs do not exist in this engine, so a score is a floor, not an exact figure.</p>
  <p>This page is served by the engine and renders from the ledger on every request, so it stays
  current while the session runs. It stops serving at {html.escape(expires)}.</p>
</footer>
</div></body></html>"""


EXPIRED_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link expired</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#FAF8F5;color:#1D2026;
font:400 15px/1.6 ui-sans-serif,system-ui,sans-serif;padding:24px}
@media(prefers-color-scheme:dark){body{background:#15171C;color:#E9E6E0}}
div{max-width:44ch;text-align:center}
h1{font:600 22px/1.25 ui-sans-serif,system-ui,sans-serif;margin:0 0 10px}
p{margin:0;color:#7C8494}
</style></head><body><div>
<h1>This link has expired</h1>
<p>The day book was shared for 24 hours and that window has closed. The page has deleted itself.
Ask for a fresh link if you still need it.</p>
</div></body></html>"""


def _gap_reads(
    sig: dict[str, Any], eq: dict[str, Any] | None, atr30: float,
    mkt: dict[str, float], vix: float | None, bullish: bool, conf: dict[str, Any],
) -> dict[str, Any]:
    """The two reference-stack readings for one trigger. Advisory: nothing routes on them."""
    prev_close, atr1d, open_today = mkt.get("prev_close", 0.0), mkt.get("atr1d", 0.0), mkt.get("open", 0.0)
    levels = (eq or {}).get("levels") or {}
    gap = None
    if open_today and prev_close:
        gap = gap_open_class(open_px=open_today, prev_close=prev_close, atr1d=atr1d,
                             r1=levels.get("1d.R1"), s1=levels.get("1d.S1"))
    f14 = None
    if eq and atr30 > 0 and gap is not None:
        phase = "OPEN" if datetime.fromtimestamp(sig["ts"], IST).strftime("%H:%M") <= "09:15" else "MID"
        f14 = f14_score(
            bullish=bullish, grade=sig.get("grade") or "F", rr=float(conf.get("rr") or 0),
            fortress=float(conf.get("fortress") or 0), atr30=atr30,
            bar=(eq["open"], eq["high"], eq["low"], eq["close"]),
            gap_pct=gap.gap_pct, phase=phase, vix=vix,
        )
    return {"gap": gap.to_json() if gap else None, "f14": f14.to_json() if f14 else None}


_VERDICT_CHIP = {"COUNTER": "refused", "IN_TREND": "filled", "SKIP": "blocked"}


def _gap_cells(r: dict[str, Any]) -> str:
    """The two reference readings as table cells: values, then what each would have concluded."""
    g, f = r.get("gap"), r.get("f14")
    if not g:
        return '<td class="dim l">—</td><td>—</td><td>—</td><td>—</td><td class="dim l">—</td><td>—</td><td class="dim l">—</td>'
    label = g["label"] + (" *" if g.get("fill_hidden") else "")
    title = ("fill-likely by magnitude, but labelled on S1/R1 because the classifier returns "
             "before the ATR test") if g.get("fill_hidden") else ""
    cells = [
        f'<td class="dim l"{f" title={title!r}" if title else ""}>{html.escape(label)}</td>',
        f'<td>{_fmt(g["gap_pct"])}%</td>',
        f'<td>{_fmt(g["gap_atr1d"])}</td>',
    ]
    if not f:
        return "".join(cells) + '<td>—</td><td class="dim l">—</td><td>—</td><td class="dim l">—</td>'
    v = f["verdict"]
    says = f'<span class="chip {_VERDICT_CHIP.get(v, "blocked")}">{v.replace("_", " ").lower()}</span>'
    if f["blocked"]:
        says += f' <span class="dim">{html.escape(f["blocked"].lower())}</span>'
    comps = " · ".join(c.split("(")[0] for c in f["components"]) or "—"
    cells += [
        f'<td>{f["score"]}</td>',
        f'<td class="l">{says}</td>',
        f'<td>T{f["tier"]}{"" if not f["tier_points"] else f" (+{f['tier_points']})"}</td>',
        f'<td class="why l" title="{html.escape(" | ".join(f["components"]))}">{html.escape(comps)}</td>',
    ]
    return "".join(cells)
