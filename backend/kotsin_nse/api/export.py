"""Any rendered page's tables as an .xlsx workbook — standard library only.

Built for the ``/temporary`` pages: the operator's rule (2026-09-25) is that anything shared there
must also download in a format that fits it, and for a page of tables that is a spreadsheet.

The workbook is read off the page's own HTML rather than rebuilt from the data, so the download
cannot drift from what the page shows: one sheet per ``<table>``, named by the ``<h2>`` above it,
headers bold and frozen, numbers as numbers, percentages as percentages, and every paragraph of
prose on a closing "Notes" sheet. No openpyxl: the format is a zip of a handful of XML parts, and a
dependency that needs the network to install is a poor trade for ~150 lines.
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from xml.sax.saxutils import escape

#: numbers as the pages print them: thousands commas, a typographic minus, an optional sign
_NUM = re.compile(r"^[+\-−]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_PCT = re.compile(r"^[+\-−]?\d+(\.\d+)?%$")
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_EMPTY = {"", "—", "-", "–"}


@dataclass
class Sheet:
    name: str
    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


class _Collector(HTMLParser):
    """Tables (with the heading before each), paragraphs, and the page title."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.sheets: list[Sheet] = []
        self.notes: list[str] = []
        self._heading = ""
        self._buf: list[str] | None = None
        self._capture: str = ""  # "title" | "h2" | "cell" | "p"
        self._table: Sheet | None = None
        self._row: list[str] | None = None
        self._in_head = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._capture, self._buf = "title", []
        elif tag in ("h1", "h2", "h3") and self._table is None:
            self._capture, self._buf = "h2", []
        elif tag == "table":
            self._table = Sheet(name=self._heading or f"Table {len(self.sheets) + 1}")
        elif tag == "thead":
            self._in_head = True
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._capture, self._buf = "cell", []
        elif tag == "p" and self._table is None:
            self._capture, self._buf = "p", []
        elif tag == "br" and self._buf is not None:
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        text = " ".join("".join(self._buf or []).split())
        if tag == "title" and self._capture == "title":
            self.title, self._capture, self._buf = text, "", None
        elif tag in ("h1", "h2", "h3") and self._capture == "h2":
            self._heading, self._capture, self._buf = text, "", None
        elif tag in ("td", "th") and self._capture == "cell" and self._row is not None:
            self._row.append(text)
            self._capture, self._buf = "", None
        elif tag == "tr" and self._table is not None and self._row is not None:
            if self._in_head and not self._table.header:
                self._table.header = self._row
            elif self._row:
                self._table.rows.append(self._row)
            self._row = None
        elif tag == "thead":
            self._in_head = False
        elif tag == "table" and self._table is not None:
            self.sheets.append(self._table)
            self._table = None
        elif tag == "p" and self._capture == "p":
            if text:
                self.notes.append(text)
            self._capture, self._buf = "", None

    def handle_data(self, data: str) -> None:
        if self._buf is not None:
            self._buf.append(data)


def tables_from_html(page: str) -> tuple[str, list[Sheet], list[str]]:
    c = _Collector()
    c.feed(page)
    c.close()
    return c.title, c.sheets, c.notes


# -- the workbook ----------------------------------------------------------------------------------


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _xml(text: str) -> str:
    return escape(_BAD_XML.sub("", text))


def _cell(ref: str, raw: str, *, header: bool = False) -> str:
    """One cell. Numbers become numbers and percentages percentages, so the sheet sorts and sums;
    anything else stays the text the page printed."""
    v = raw.strip()
    if header:
        return f'<c r="{ref}" t="inlineStr" s="1"><is><t xml:space="preserve">{_xml(v)}</t></is></c>'
    if v in _EMPTY:
        return ""
    plain = v.replace(",", "").replace("−", "-").lstrip("+")
    if _NUM.match(v):
        return f'<c r="{ref}"><v>{float(plain)!r}</v></c>' if "." in plain else f'<c r="{ref}"><v>{int(plain)}</v></c>'
    if _PCT.match(v):
        return f'<c r="{ref}" s="2"><v>{float(plain[:-1]) / 100!r}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xml(v)}</t></is></c>'


def _sheet_xml(sheet: Sheet) -> str:
    width = max([len(sheet.header), *(len(r) for r in sheet.rows)] or [1])
    grid = ([sheet.header] if sheet.header else []) + sheet.rows
    widths = [8.0] * width
    for row in grid:
        for i, v in enumerate(row[:width]):
            widths[i] = min(60.0, max(widths[i], len(v) * 1.1 + 2))
    cols = "".join(f'<col min="{i}" max="{i}" width="{w:.1f}" customWidth="1"/>' for i, w in enumerate(widths, 1))
    out = []
    for r, row in enumerate(grid, 1):
        cells = "".join(
            _cell(f"{_col(c)}{r}", v, header=bool(sheet.header) and r == 1) for c, v in enumerate(row[:width], 1)
        )
        out.append(f'<row r="{r}">{cells}</row>')
    freeze = (
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
        'state="frozen"/></sheetView></sheetViews>'
        if sheet.header else '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
    )
    filt = f'<autoFilter ref="A1:{_col(width)}{len(grid)}"/>' if sheet.header and sheet.rows else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{freeze}<cols>{cols}</cols><sheetData>{''.join(out)}</sheetData>{filt}</worksheet>"
    )


def _notes_xml(notes: list[str]) -> str:
    rows = "".join(
        f'<row r="{i}"><c r="A{i}" t="inlineStr" s="3"><is><t xml:space="preserve">{_xml(n)}</t></is></c></row>'
        for i, n in enumerate(notes, 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<cols><col min="1" max="1" width="120" customWidth="1"/></cols>'
        f"<sheetData>{rows}</sheetData></worksheet>"
    )


def _names(sheets: list[Sheet]) -> list[str]:
    """Excel's rules: at most 31 characters, none of []:*?/\\, unique ignoring case."""
    seen: set[str] = set()
    out = []
    for s in sheets:
        # "Signals · 24" -> "Signals": the count belongs to the page heading, not the tab
        base = re.sub(r"\s*·\s*\d+$", "", s.name)
        base = re.sub(r"[\[\]:*?/\\]", " ", base).strip()[:31] or "Sheet"
        name, n = base, 2
        while name.lower() in seen:
            suffix = f" ({n})"
            name, n = base[: 31 - len(suffix)] + suffix, n + 1
        seen.add(name.lower())
        out.append(name)
    return out


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE8EDF3"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
    '<alignment wrapText="1" vertical="top"/></xf>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def workbook(sheets: list[Sheet], notes: list[str] | None = None, title: str = "") -> bytes:
    sheets = [s for s in sheets if s.header or s.rows]
    if notes:
        sheets = [*sheets, Sheet(name="Notes")]
    if not sheets:
        sheets = [Sheet(name="Empty", header=["Nothing to export"])]
    names = _names(sheets)
    n = len(sheets)
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, n + 1)
        )
        + "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        "</Relationships>"
    )
    wb = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        + "".join(f'<sheet name="{_xml(nm)}" sheetId="{i}" r:id="rId{i}"/>' for i, nm in enumerate(names, 1))
        + "</sheets></workbook>"
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, n + 1)
        )
        + f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_xml(title)}</dc:title><dc:creator>kotsin-nse</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created></cp:coreProperties>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("docProps/core.xml", core)
        for i, s in enumerate(sheets, 1):
            body = _notes_xml(notes or []) if (notes and i == n) else _sheet_xml(s)
            z.writestr(f"xl/worksheets/sheet{i}.xml", body)
    return buf.getvalue()


def html_to_xlsx(page: str) -> bytes:
    """The page's tables, one sheet each, and its prose on a Notes sheet."""
    title, sheets, notes = tables_from_html(page)
    return workbook(sheets, notes, title)
