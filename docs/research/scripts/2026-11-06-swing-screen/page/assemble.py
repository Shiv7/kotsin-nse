"""Assemble the swing-screen page: the shell + the data tables + the research cards (markdown from the four agents)."""
from __future__ import annotations

import html
import re
from pathlib import Path

S = Path(__file__).parent
GROUPS = [
    ("Long side, new money", ["LODHA", "GODREJPROP", "DLF", "HYUNDAI", "ASIANPAINT", "AMBUJACEM", "JINDALSTEL", "HINDZINC", "RELIANCE", "JIOFIN", "BAJAJFINSV"]),
    ("Where the bull turn just started", ["INDIANB", "TCS", "BANKINDIA", "TATASTEEL", "LT", "EICHERMOT", "INFY", "MPHASIS", "HAVELLS", "CIPLA", "ICICIGI", "VMM", "GRASIM"]),
    ("Short side", ["PIIND", "CUMMINSIND", "SOLARINDS", "CGPOWER", "OIL", "POLICYBZR", "BHARATFORG"]),
]


def inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return s


def parse(md: str) -> tuple[dict[str, dict], str, dict[str, str]]:
    """Cards by symbol, the file's caveat line, and the status flags."""
    cards: dict[str, dict] = {}
    caveat = ""
    flags: dict[str, str] = {}
    cur = None
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("# "):
            i += 1
            continue
        if ln.startswith("## "):
            title = ln[3:].strip()
            if title.lower().startswith("status"):
                cur = "__status__"
                i += 1
                continue
            m = re.match(r"(?:\d+\.\s*)?([A-Z0-9&-]+)\s*[—(-]?\s*(.*)", title)
            sym = m.group(1) if m else title
            name = (m.group(2) or "").strip(" )—-") if m else ""
            cur = sym
            cards[sym] = {"name": name, "table": [], "story": [], "bullets": []}
            i += 1
            continue
        if cur == "__status__" or (cur is None and re.match(r"^-?\s*[A-Z0-9&-]+:\s*(results|due|unknown)", ln)):
            m = re.match(r"^-?\s*([A-Z0-9&-]+):\s*(.+)$", ln)
            if m:
                flags[m.group(1)] = m.group(2).strip()
            i += 1
            continue
        if cur is None:
            if ln.strip() and not ln.startswith("---"):
                caveat = (caveat + " " + ln.strip()).strip()
            i += 1
            continue
        c = cards[cur]
        if ln.startswith("|"):
            cells = [x.strip() for x in ln.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", x) for x in cells):
                i += 1
                continue
            c["table"].append(cells)
        elif ln.startswith("- "):
            c["bullets"].append(ln[2:].strip())
        elif ln.startswith("---"):
            cur = "__status__"
        elif ln.strip():
            c["story"].append(ln.strip())
        i += 1
    return cards, caveat, flags


def card(sym: str, c: dict, flag: str | None) -> str:
    rows = c["table"]
    head = rows[0] if rows and rows[0][0].lower() == "item" else None
    body = rows[1:] if head else rows
    th = "".join(f"<th>{inline(x)}</th>" for x in (head or ["Item", "Value", "Source, date"]))
    tr = "".join("<tr>" + "".join(f"<td>{inline(x)}</td>" for x in r) + "</tr>" for r in body)
    story = "".join(f'<p class="story">{inline(s)}</p>' for s in c["story"])
    bullets = ("<ul>" + "".join(f"<li>{inline(b)}</li>" for b in c["bullets"]) + "</ul>") if c["bullets"] else ""
    fl = f'<span class="flag">{inline(flag)}</span>' if flag else ""
    name = f' <span class="caveat">{inline(c["name"])}</span>' if c["name"] else ""
    return f'<div class="co" id="{sym}"><h4>{sym}{name}{fl}</h4><div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>{story}{bullets}</div>'


def main() -> None:
    shell = (S / "shell.html").read_text()
    for key, fn in (("SECTOR", "t_sector.html"), ("NEW_MONEY", "t_new_money.html"), ("FRESH", "t_fresh.html"), ("ALL3", "t_all3.html"), ("SHORTS", "t_shorts.html")):
        shell = shell.replace(f"<!--{key}-->", (S / fn).read_text())
    cards: dict[str, dict] = {}
    flags: dict[str, str] = {}
    caveats = []
    for fn in sorted(S.glob("research_*.md")):
        c, cav, fl = parse(fn.read_text())
        cards.update(c)
        flags.update(fl)
        if cav:
            caveats.append(cav)
    out = []
    if caveats:
        out.append('<div class="note caveat">' + " ".join(inline(x) for x in caveats) + "</div>")
    missing = []
    for title, syms in GROUPS:
        out.append(f"<h3>{html.escape(title)}</h3>")
        for s in syms:
            if s in cards:
                out.append(card(s, cards[s], flags.get(s)))
            else:
                missing.append(s)
    if missing:
        out.append('<p class="caveat">Not yet researched: ' + ", ".join(missing) + ".</p>")
    shell = shell.replace("<!--RESEARCH-->", "\n".join(out))
    (S / "swing-screen.html").write_text(shell)
    print("cards", len(cards), "missing", missing, "bytes", len(shell))


if __name__ == "__main__":
    main()
