"""Table cells of a PDF, for a reader who needs the numbers in a table.

``zotero-cli tables`` gives a reader agent the header, rows and caption of each
table on the pages it asks for. The cells come from PyMuPDF's ``find_tables``.
A reader still checks them against the page image: the image wins.

Two strategies, one per kind of table:

``lines``
    ``page.find_tables(strategy="lines")`` on the whole page. It finds ruled
    grids. Plot gridlines and axis frames also look like grids, so a table with
    no caption is dropped when fewer than ``LINES_MIN_FILLED`` of its cells
    hold text (measured: plot grids fill 1-45% of their cells). A table with a
    caption is always kept.

``text``
    Booktabs tables have horizontal rules and no vertical ones, so ``lines``
    finds nothing. Under each ``Table N`` caption the engine takes the first
    rule that lies close below it, chains the rules that follow (same columns,
    no other caption between), and runs ``find_tables(strategy="text")`` on the
    box around the chain. It keeps the largest table found. No chain means no
    table. A full-page ``text`` run is not used: on a page of body text it
    returns the text as one table (measured).

A caption is a text line that starts with ``Table`` and a label. It belongs to
a table when it lies within ``LAYOUT_CAPTION_MAX_DISTANCE`` of a page height
above the top or below the bottom of the table and overlaps
``LAYOUT_CAPTION_MIN_OVERLAP`` of the narrower width (the ``layout`` rules).
Each caption goes to one table, the nearest first.

The ``text`` strategy splits a word at a column border ("Gr" | "aphite"), and
numbers stay whole. Cell text goes through ``pdf_grep.normalize_text``.

PyMuPDF prints an advert to stdout on the first ``find_tables`` call, which
corrupts ``zotero-cli --json``. Every call runs inside ``redirect_stdout``.
"""

from __future__ import annotations

import contextlib
import io
import re

import pymupdf

from zotero_mcp.pdf_grep import normalize_text
from zotero_mcp.pdf_layout import (
    LAYOUT_CAPTION_MAX_DISTANCE,
    LAYOUT_CAPTION_MIN_OVERLAP,
    LAYOUT_RULE_MIN_WIDTH,
)
from zotero_mcp.pdf_utils import page_range_error

STRATEGIES = ("lines", "text")

LINES_MIN_FILLED = 0.5            # an uncaptioned grid keeps at least this share of cells with text
RULE_MAX_HEIGHT = 2.0             # a drawing this thin (points) is a rule
RULE_PIECE_MIN_WIDTH = 20.0       # narrower pieces are not part of a rule (points)
RULE_JOIN_DY = 1.0                # pieces of one rule differ in height by at most this (points)
RULE_JOIN_GAP = 3.0               # ... and leave a gap of at most this (points)
TEXT_START_MAX_DISTANCE = 0.10    # the first rule lies this close below the caption (page height)
TEXT_CHAIN_MAX_GAP = 0.20         # the next rule of a chain lies this close below the last (page height)
TEXT_CHAIN_MIN_OVERLAP = 0.5      # share of the shorter rule that the next rule shares in x
TEXT_CLIP_PAD = 2.0               # margin around the chain box (points)

_CAPTION_RE = re.compile(r"(?i)^table\s+([A-Z]?\d+(?:[.-]\d+)*[a-z]?|[IVXLC]+)\b")


def tables_for_pdf(pdf_path, *, pages: list[int] | None, strategy: str = "lines") -> dict:
    """Tables on the given 1-based pages of a PDF.

    Args:
        pdf_path: Path of the PDF.
        pages: 1-based page numbers, or ``None`` for every page.
        strategy: ``"lines"`` (ruled grids) or ``"text"`` (rule-bounded tables under a caption).

    Returns:
        ``{"strategy": str, "pages": [{"page": int, "tables": [...]}]}``. Every requested
        page is listed in order, with ``tables: []`` when it has none. A table is
        ``{"id": "p17-t1", "bbox": [x, y, w, h], "rect_arg": "x,y,w,h", "header": [str],
        "rows": [[str | None]], "caption": str | None}``. ``id`` counts from 1, top to
        bottom. ``bbox`` is normalised 0-1 with 4 decimals; ``rect_arg`` feeds
        ``read --format image --rect``. ``rows`` exclude the header row.

    Raises:
        ValueError: ``strategy`` is unknown, or a page is outside the document.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}, got {strategy!r}")
    doc = pymupdf.open(pdf_path)
    try:
        wanted = list(range(1, len(doc) + 1)) if pages is None else list(pages)
        for page_no in wanted:
            error = page_range_error(doc, page_no)
            if error:
                raise ValueError(error)
        return {
            "strategy": strategy,
            "pages": [
                {"page": page_no, "tables": _page_tables(doc[page_no - 1], page_no, strategy)}
                for page_no in wanted
            ],
        }
    finally:
        doc.close()


def format_tables_markdown(data: dict) -> str:
    """The ``tables_for_pdf`` result as text: a page heading, then a caption line and a pipe table per table."""
    blocks = []
    for page in data["pages"]:
        lines = [f"## Page {page['page']}", ""]
        if not page["tables"]:
            lines += ["(no tables)", ""]
        for table in page["tables"]:
            lines += [f"{table['caption'] or '(no caption)'}  [{table['id']}, rect {table['rect_arg']}]", ""]
            width = max([len(table["header"])] + [len(row) for row in table["rows"]])
            for i, row in enumerate([table["header"], *table["rows"]]):
                cells = [_pipe_cell(cell) for cell in row] + [""] * (width - len(row))
                lines.append("| " + " | ".join(cells) + " |")
                if i == 0:
                    lines.append("|" + " --- |" * width)
            lines.append("")
        blocks.append("\n".join(lines))
    return "\n".join(blocks).rstrip() + "\n"


def _pipe_cell(cell: str | None) -> str:
    return (cell or "").replace("|", "\\|")


# ---------------------------------------------------------------------------
# One page
# ---------------------------------------------------------------------------

def _page_tables(page, page_no: int, strategy: str) -> list[dict]:
    """The tables of one page, top to bottom, as output dicts."""
    captions = _captions(page)
    if strategy == "text":
        found = _text_tables(page, captions)
    else:
        found = _lines_tables(page, captions)
    found.sort(key=lambda item: (item[0].bbox[1], item[0].bbox[0]))
    width, height = page.rect.width, page.rect.height
    return [
        _table_dict(table, caption, f"p{page_no}-t{index}", width, height)
        for index, (table, caption) in enumerate(found, start=1)
    ]


def _find_tables(page, **kwargs) -> list:
    with contextlib.redirect_stdout(io.StringIO()):
        return list(page.find_tables(**kwargs).tables)


def _captions(page) -> list[dict]:
    """Text lines that start with ``Table`` and a label: ``{"text", "bbox": (x0, y0, x1, y1)}``, top to bottom."""
    found = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = normalize_text("".join(span["text"] for span in line["spans"]))
            if _CAPTION_RE.match(text):
                found.append({"text": text, "bbox": tuple(line["bbox"])})
    found.sort(key=lambda caption: (caption["bbox"][1], caption["bbox"][0]))
    return found


def _x_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Overlap of two x-ranges, relative to the narrower one."""
    return max(0.0, min(a1, b1) - max(a0, b0)) / max(1e-6, min(a1 - a0, b1 - b0))


def _table_dict(table, caption: str | None, table_id: str, width: float, height: float) -> dict:
    x0, y0, x1, y1 = table.bbox
    x = min(max(x0 / width, 0.0), 1.0)
    y = min(max(y0 / height, 0.0), 1.0)
    w = min(max((x1 - x0) / width, 0.0), 1.0 - x)
    h = min(max((y1 - y0) / height, 0.0), 1.0 - y)
    rows = table.extract()
    if not table.header.external:
        rows = rows[1:]  # the first extracted row is the header row
    return {
        "id": table_id,
        "bbox": [round(x, 4), round(y, 4), round(w, 4), round(h, 4)],
        "rect_arg": f"{x:.4f},{y:.4f},{w:.4f},{h:.4f}",
        "header": [normalize_text(name) if name is not None else "" for name in table.header.names],
        "rows": [[None if cell is None else normalize_text(cell) for cell in row] for row in rows],
        "caption": caption,
    }


# ---------------------------------------------------------------------------
# lines: ruled grids
# ---------------------------------------------------------------------------

def _lines_tables(page, captions: list[dict]) -> list[tuple]:
    """``(table, caption text | None)`` for each ruled grid; a sparse grid with no caption is dropped."""
    tables = _find_tables(page, strategy="lines")
    attached = _attach_captions(tables, captions, page.rect.height)
    return [
        (table, caption)
        for table, caption in zip(tables, attached)
        if caption is not None or _filled_share(table) >= LINES_MIN_FILLED
    ]


def _filled_share(table) -> float:
    """Share of the extracted cells that hold text."""
    cells = [cell for row in table.extract() for cell in row]
    if not cells:
        return 0.0
    return sum(1 for cell in cells if cell and cell.strip()) / len(cells)


def _attach_captions(tables: list, captions: list[dict], page_height: float) -> list[str | None]:
    """The caption text of each table, or None. The nearest pair goes first; a caption serves one table."""
    pairs = []
    for t_idx, table in enumerate(tables):
        tx0, ty0, tx1, ty1 = table.bbox
        for c_idx, caption in enumerate(captions):
            cx0, cy0, cx1, cy1 = caption["bbox"]
            overlap = _x_overlap(tx0, tx1, cx0, cx1)
            if overlap < LAYOUT_CAPTION_MIN_OVERLAP:
                continue
            if cy0 >= ty1:
                gap = cy0 - ty1  # caption below the table
            elif cy1 <= ty0:
                gap = ty0 - cy1  # caption above the table
            else:
                gap = 0.0  # caption inside the table's box
            if gap > LAYOUT_CAPTION_MAX_DISTANCE * page_height:
                continue
            pairs.append((gap, -overlap, t_idx, c_idx))
    pairs.sort()
    result: list[str | None] = [None] * len(tables)
    used: set[int] = set()
    for _gap, _overlap, t_idx, c_idx in pairs:
        if result[t_idx] is None and c_idx not in used:
            result[t_idx] = captions[c_idx]["text"]
            used.add(c_idx)
    return result


# ---------------------------------------------------------------------------
# text: rule-bounded tables under a caption
# ---------------------------------------------------------------------------

def _page_rules(page) -> list[tuple[float, float, float]]:
    """Horizontal rules as ``(y, x0, x1)``, top to bottom. Collinear pieces are joined into one rule."""
    pieces = sorted(
        (rect.y0, rect.x0, rect.x1)
        for rect in (drawing["rect"] for drawing in page.get_drawings())
        if rect.height <= RULE_MAX_HEIGHT and rect.width >= RULE_PIECE_MIN_WIDTH
    )
    rules: list[list[float]] = []
    for y, x0, x1 in pieces:
        joined = None
        for rule in reversed(rules):
            if y - rule[0] > RULE_JOIN_DY:
                break  # sorted by y: no earlier rule is close enough either
            if x0 <= rule[2] + RULE_JOIN_GAP and x1 >= rule[1] - RULE_JOIN_GAP:
                joined = rule
                break
        if joined is None:
            rules.append([y, x0, x1])
        else:
            joined[1], joined[2] = min(joined[1], x0), max(joined[2], x1)
    min_width = LAYOUT_RULE_MIN_WIDTH * page.rect.width
    return [(y, x0, x1) for y, x0, x1 in rules if x1 - x0 >= min_width]


def _text_tables(page, captions: list[dict]) -> list[tuple]:
    """``(table, caption text)`` for each caption that has a rule chain under it and a table in the chain box."""
    if not captions:
        return []
    height = page.rect.height
    rules = _page_rules(page)
    found = []
    for caption in captions:
        cx0, _cy0, cx1, cy1 = caption["bbox"]
        starts = [
            rule for rule in rules
            if 0 <= rule[0] - cy1 <= TEXT_START_MAX_DISTANCE * height
            and _x_overlap(rule[1], rule[2], cx0, cx1) > 0
        ]
        if not starts:
            continue  # no chain, no table
        chain = [starts[0]]
        for rule in rules:
            if rule[0] <= chain[-1][0]:
                continue
            if rule[0] - chain[-1][0] > TEXT_CHAIN_MAX_GAP * height:
                break
            if any(chain[-1][0] < other["bbox"][1] < rule[0] for other in captions):
                break  # another table's caption sits between the two rules
            if _x_overlap(rule[1], rule[2], chain[0][1], chain[0][2]) >= TEXT_CHAIN_MIN_OVERLAP:
                chain.append(rule)
        clip = pymupdf.Rect(
            min(rule[1] for rule in chain) - TEXT_CLIP_PAD,
            chain[0][0] - TEXT_CLIP_PAD,
            max(rule[2] for rule in chain) + TEXT_CLIP_PAD,
            chain[-1][0] + TEXT_CLIP_PAD,
        )
        clipped = _find_tables(page, clip=clip, strategy="text")
        if clipped:
            found.append((max(clipped, key=lambda table: table.row_count * table.col_count), caption["text"]))
    return found
