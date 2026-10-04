"""Engine A (phase34, :8500) vs engine B (phase35, :8501) for one session — operator, 2026-10-03: "2 fully
functional engines with all same except the 2 engines' core differences ... it will help to decide which
one to keep and which to discard next week".

usage: compare_ab.py [YYYY-MM-DD]   (default: today IST)

Per book: closed trades and net (realised gross − charges) in each engine; every position that one engine
took and the other did not, or closed differently; every FUDKII trigger graded or published differently;
both engines' feed health. The stop-rule mirrors (books ending _SE / _SA, 4 Oct: every book's fill copied
under stop E and the adaptive stop) are left out of that comparison — they re-trade their book's fills, so
counting them in would count every trade three times — and shown apart: per engine and book, the trades
closed under all three stops, with the current stop's net beside stop E's and the adaptive stop's.
Read-only: the databases are opened read-only, the health pages only read."""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
ENGINES = {
    "A phase34": ("/Users/devinakothari/Downloads/kotsincode/kotsin-nse/backend/data/kotsin_nse.db", 8500),
    "B phase35": ("/Users/devinakothari/Downloads/kotsincode/kotsin-nse-p35/backend/data/kotsin_nse.db", 8501),
}
LABEL = {"FUDKII": "FUDKII", "FUDKII_RT_X": "RT-X", "FUDKII_RT_N": "RT-N", "FUDKII_RT_Y": "RT-Y", "FUDKII_RT_Y_W1": "RT-Y-W1",
         "FUDKII_RT_Y_F": "RT-Y-F", "FUDKII_CT_X": "CT-X", "FUDKII_CT_Y": "CT-Y", "FUDKII_CT_M": "CT-M",
         "FUDKII_RT_MCX": "RT-MCX", "FUKAA": "FUKAA"}
#: shadow books: compared like the others, but not a strategy's money in the stop-rule total
SHADOWS = {"FUDKII_RT_Y_W1", "FUDKII_RT_Y_F", "FUDKII_CT_M"}


def _mirror(book: str) -> bool:
    return book.endswith(("_SE", "_SA"))


def stop_rules(pos: dict) -> dict:
    """Per book: the triggers closed under all three stops (n, and each rule's net), and those still open
    under one of them (waiting). A trigger without either mirror predates them, or their purse refused it."""
    out: dict = {}
    for (book, sid), v in pos.items():
        if _mirror(book):
            continue
        e, a = pos.get((book + "_SE", sid)), pos.get((book + "_SA", sid))
        if e is None and a is None:
            continue
        row = out.setdefault(book, {"n": 0, "current": 0.0, "E": 0.0, "A": 0.0, "waiting": 0})
        if all(x is not None and x["open"] == 0 for x in (v, e, a)):
            row["n"] += 1
            row["current"] += v["net"]
            row["E"] += e["net"]
            row["A"] += a["net"]
        else:
            row["waiting"] += 1
    return out


def _obj(v):
    if isinstance(v, (dict, list)) or v is None:
        return v
    try:
        return ast.literal_eval(v)
    except (ValueError, SyntaxError):
        return v


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load(db: str, lo: float, hi: float):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    pos = {}
    for (j,) in c.execute("select json from positions where opened_ts >= ? and opened_ts < ?", (lo, hi)):
        p = json.loads(j)
        key = (p["strategy"], p.get("signal_id") or p["id"])
        closed = p.get("status") == "CLOSED"
        net = _f(p.get("realised_gross")) - _f(p.get("charges")) if closed else None
        o = pos.setdefault(key, {"sym": p["symbol"], "opened": _f(p.get("opened_ts")), "entry": p.get("entry"), "qty": 0,
                                 "net": 0.0, "open": 0, "why": set(), "exit": set()})
        o["qty"] += int(_f(p.get("qty")))
        if closed:
            o["net"] += net
            o["why"].add(str(p.get("exit_reason")))
            o["exit"].add(str(p.get("exit_price")))
        else:
            o["open"] += 1
    sig = {}
    for (j,) in c.execute("select json from signals where strategy='FUDKII' and ts >= ? and ts < ?", (lo - 1800, hi)):
        s = json.loads(j)
        ctx = _obj(s.get("context")) or {}
        pub = (ctx.get("parent") or {}).get("published")
        sig[(s["symbol"], int(s["ts"]), s["direction"])] = {
            "grade": s.get("grade"), "rr": s.get("rr"), "pub": bool(pub if pub is not None else s.get("decision") != "NOT_PUBLISHED"),
            "why": (s.get("decision_reason") or "")[:110]}
    return pos, sig


def health(port: int) -> str:
    try:
        h = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5))
    except Exception as exc:  # noqa: BLE001 - a report line, not a failure
        return f"not answering ({exc.__class__.__name__})"
    f = h.get("feed") or {}
    hub = f.get("hub") or {}
    hub_text = ("" if not hub else
                f" | hub {hub.get('role')}: twins {hub.get('twins')} pinned {hub.get('pinned')} dropped {hub.get('twins_dropped')}"
                if hub.get("role") == "serve" else
                f" | hub {hub.get('role')}: {hub.get('messages')} msgs, delay mean {hub.get('lag_ms')} ms max {hub.get('lag_max_ms')} ms")
    return (f"{h.get('status')} {h.get('degraded') or ''} | feed connected {f.get('connected')} reconnects {f.get('reconnects')} "
            f"ticks {f.get('ticks')} silence {round(f.get('silence_s') or 0)} s | rest {(h.get('rest') or {}).get('calls')} calls "
            f"{(h.get('rest') or {}).get('failures')} failed | open {h.get('positions_open')}" + hub_text)


def main() -> None:
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(IST).date()
    lo = datetime.combine(day, datetime.min.time(), IST).timestamp()
    hi = lo + 86400
    (na, (dba, pa)), (nb, (dbb, pb)) = ENGINES.items()
    A_all, SA = load(dba, lo, hi)
    B_all, SB = load(dbb, lo, hi)
    A = {k: v for k, v in A_all.items() if not _mirror(k[0])}
    B = {k: v for k, v in B_all.items() if not _mirror(k[0])}
    print(f"== {day:%a %d %b %Y}: {na} vs {nb}")
    for n, (_, port) in ENGINES.items():
        print(f"  {n} health: {health(port)}")
    books = sorted({k[0] for k in A} | {k[0] for k in B}, key=lambda b: list(LABEL).index(b) if b in LABEL else 99)
    print(f"\n  {'book':<8} {'A trades':>8} {'A net':>10} | {'B trades':>8} {'B net':>10} | {'B − A':>9}")
    ta = tb = 0.0
    for b in books:
        ga = [v for k, v in A.items() if k[0] == b]
        gb = [v for k, v in B.items() if k[0] == b]
        sa, sb = sum(v["net"] for v in ga), sum(v["net"] for v in gb)
        ta, tb = ta + sa, tb + sb
        oa, ob = sum(v["open"] for v in ga), sum(v["open"] for v in gb)
        print(f"  {LABEL.get(b, b):<8} {len(ga):>8} {sa:>10,.0f} | {len(gb):>8} {sb:>10,.0f} | {sb - sa:>+9,.0f}"
              + (f"   (open: A {oa}, B {ob})" if oa or ob else ""))
    print(f"  {'all':<8} {len(A):>8} {ta:>10,.0f} | {len(B):>8} {tb:>10,.0f} | {tb - ta:>+9,.0f}")
    diff = []
    for k in sorted(set(A) | set(B), key=lambda k: ((A.get(k) or B.get(k))["opened"], k)):
        a, b = A.get(k), B.get(k)
        if a and b and abs(a["net"] - b["net"]) < 1 and a["open"] == b["open"] and a["why"] == b["why"]:
            continue
        x = a or b
        t = datetime.fromtimestamp(x["opened"], IST).strftime("%H:%M")
        fa = f"{a['net']:>+8,.0f} {'/'.join(sorted(a['why'])) or 'open'} @ {'/'.join(sorted(a['exit'])) or '-'}" if a else "not taken"
        fb = f"{b['net']:>+8,.0f} {'/'.join(sorted(b['why'])) or 'open'} @ {'/'.join(sorted(b['exit'])) or '-'}" if b else "not taken"
        diff.append(f"  {t} {LABEL.get(k[0], k[0]):<7} {x['sym']:<11} | A {fa:<34} | B {fb}")
    print(f"\n  positions that differ: {len(diff)}")
    print("\n".join(diff) if diff else "  — none")
    sd = []
    for k in sorted(set(SA) | set(SB), key=lambda k: (k[1], k[0])):
        a, b = SA.get(k), SB.get(k)
        if a and b and a["grade"] == b["grade"] and a["pub"] == b["pub"]:
            continue
        t = datetime.fromtimestamp(k[1] + 1800, IST).strftime("%H:%M")
        fa = f"{a['grade']} rr {a['rr']} {'published' if a['pub'] else 'not published'}" if a else "no trigger"
        fb = f"{b['grade']} rr {b['rr']} {'published' if b['pub'] else 'not published'}" if b else "no trigger"
        sd.append(f"  {t} {k[0]:<11} {k[2][:4]} | A {fa:<30} | B {fb}")
    print(f"\n  FUDKII triggers: A {len(SA)}, B {len(SB)}; graded or published differently: {len(sd)}")
    print("\n".join(sd) if sd else "  — none")
    for name, allpos in ((na, A_all), (nb, B_all)):
        rules = stop_rules(allpos)
        print(f"\n  stop rules · {name}: the same trades, closed under all three stops (net after charges)")
        if not rules:
            print("  — no mirrored trade")
            continue
        print(f"  {'book':<8} {'trades':>6} {'current':>10} {'stop E':>10} {'adaptive':>10} | {'E − cur':>9} {'A − cur':>9}")
        tot = {"n": 0, "current": 0.0, "E": 0.0, "A": 0.0, "waiting": 0}
        for b in sorted(rules, key=lambda b: list(LABEL).index(b) if b in LABEL else 99):
            r = rules[b]
            if b not in SHADOWS:
                for k in tot:
                    tot[k] += r[k]
            print(f"  {LABEL.get(b, b):<8} {r['n']:>6} {r['current']:>10,.0f} {r['E']:>10,.0f} {r['A']:>10,.0f} | "
                  f"{r['E'] - r['current']:>+9,.0f} {r['A'] - r['current']:>+9,.0f}"
                  + (f"   ({r['waiting']} still open under a rule)" if r["waiting"] else "") + ("   shadow" if b in SHADOWS else ""))
        print(f"  {'trading':<8} {tot['n']:>6} {tot['current']:>10,.0f} {tot['E']:>10,.0f} {tot['A']:>10,.0f} | "
              f"{tot['E'] - tot['current']:>+9,.0f} {tot['A'] - tot['current']:>+9,.0f}")


if __name__ == "__main__":
    main()
