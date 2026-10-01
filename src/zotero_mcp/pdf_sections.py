"""Section map of a PDF, for splitting a long source into readable units.

``zotero-cli sections`` gives a reader agent a list of page ranges to work
through one at a time. The ranges come from the PDF's outline when it has one,
and from fixed-size page chunks when it does not.

Rules (all pages are physical, 1-based):

- Outline entries of level <= ``max_level`` are the boundaries, in page order.
  Entry *i* spans its own start page to the start page of entry *i+1*,
  inclusive: a section that ends part-way down a page shares that page with the
  next one, so no text is lost. The last entry runs to the end of the scope.
- Pages before the first entry form one ``front`` unit.
- ``pages`` clips every unit to a range; units wholly outside it are dropped.
- A unit longer than ``chunk_pages`` pages is split into near-equal ``split``
  parts. The larger parts come last.
- With no outline entry of level <= ``max_level``, the scope is cut into fixed
  ``chunk_pages``-page ``chunk`` units and ``source`` is ``chunks``. A scope
  that lies inside one section is that section, clipped.

``inventory`` lists the labels found on each unit's pages, as a hint list for a
reader's "missing" pass. Two sources feed it: the layout detector's captions
(``Figure N:`` / ``Table N.``, which it accepts only with a trailing ``:`` or
``.``) and any text line that starts with ``Figure``, ``Fig.``, ``Table``,
``Eq.`` or ``Equation`` and a number (``Figure 13-19 Title``, ``Fig. 3.1``).
The line pattern also fires on in-text sentence starts such as ``Figure 13-20
shows ...``. That over-inclusion is deliberate: the list is a hint, and an
extra label costs a reader one glance where a missing one costs a fact.

Each inventory row also carries ``plots_on_page``, one entry per page of the
row. An entry is ``{page, plots, source, scanned, panel_rects}``. The count sits
on the page that holds the graphic. It comes from the page layout boxes, and
from ``pdf_layout.split_panels`` for a raster box or a scanned page. A reader
uses it to give a page of many plots its own unit. ``source`` names where the
count came from:

- ``layout``: one count for each drawing box, each table box on a page with no
  table label, and each image box that the grid split cannot cut.
- ``grid``: a grid split gave 2 or more cells, or the scan path ran.
- ``captions``: no box and no cell, so the figure caption blocks are the count.
  It is a floor.
- ``labels``: the layout pass failed, so the figure labels are the count.

``panel_rects`` holds one ``x,y,w,h`` rect for each counted plot, in the format
of ``--rect``. ``scanned`` is true when one image covers 95 percent of the page.

The outline is read through ``tools.write._extract_pdf_toc``, which runs
``get_toc()`` in a throwaway child process: on some PDFs it segfaults (#372),
and a segfault in this process would take the CLI or the MCP server with it.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_UNTITLED = "(untitled)"
_FRONT_TITLE = "Front matter"
_TABLE_LABEL_RE = re.compile(r"\btab", re.IGNORECASE)
# A label at the start of a text line: "Figure 13-19 Title", "Fig. 3.1", "Table 2".
_LINE_LABEL_RE = re.compile(r"^(Figure|Fig\.|Table|Eq\.|Equation)\s+\d+([-.]\d+)?")
_EQ_NAME_RE = re.compile(r"^(?:Eq\.|Equation)\s+(\d+(?:[-.]\d+)?)$")
# Layout boxes that can hold a plot. An equation box never does.
_PLOT_SOURCES = ("drawing", "image", "merged", "table")


def sections_for_pdf(
    pdf_path,
    *,
    pages=None,
    max_level: int = 2,
    chunk_pages: int = 8,
    inventory: bool = False,
) -> dict:
    """Split a PDF into units a reader can take one at a time.

    Args:
        pdf_path: Path to the PDF file.
        pages: ``(first, last)`` physical pages to keep, or None for every page.
            Any non-empty sequence of page numbers is read as its first-to-last
            hull, so the CLI's parsed page list works too. ``last`` is clamped
            to the page count.
        max_level: Deepest outline level that starts a unit.
        chunk_pages: Longest unit, in pages. Longer units are split; a PDF with
            no usable outline is cut into chunks of this size.
        inventory: Also list, per unit, the table and figure labels, the
            display equations and the plot count of each page. The labels are
            a hint list. They can include in-text mentions that start a line.

    Returns:
        ``{key, attachment_key, title, page_count, source, scope, sections}``
        plus ``inventory`` when asked for. ``key``, ``attachment_key`` and
        ``title`` are None here; the caller fills them in. Each section is
        ``{id, title, path, level, start_page, end_page, kind}``. Each
        inventory row is ``{section_id, tables, figures, equations,
        unnumbered_equations, plots_on_page}``.

    Raises:
        ValueError: ``max_level`` or ``chunk_pages`` below 1, a page range
            that is empty or outside the document, or a PDF with no pages.
    """
    import pymupdf

    if max_level < 1:
        raise ValueError(f"max_level must be at least 1, got {max_level}")
    if chunk_pages < 1:
        raise ValueError(f"chunk_pages must be at least 1, got {chunk_pages}")

    with pymupdf.open(str(pdf_path)) as doc:
        page_count = doc.page_count
        if page_count < 1:
            raise ValueError("PDF has no pages")
        first, last = _scope(pages, page_count)

        entries = _outline_entries(_read_outline(str(pdf_path)), max_level, page_count)
        source = "outline"
        units: list[dict] = []
        if entries:  # the units tile every page, so some unit always reaches the scope
            for unit in _outline_units(entries, page_count):
                clipped = _clip(unit, first, last)
                if clipped is not None:
                    units.extend(_split(clipped, chunk_pages))
        else:
            source = "chunks"
            units = _chunks(first, last, chunk_pages)

        sections = [
            {
                "id": f"S{number:02d}",
                "title": unit["title"],
                "path": unit["path"],
                "level": unit["level"],
                "start_page": unit["start"],
                "end_page": unit["end"],
                "kind": unit["kind"],
            }
            for number, unit in enumerate(units, start=1)
        ]
        result = {
            "key": None,
            "attachment_key": None,
            "title": None,
            "page_count": page_count,
            "source": source,
            "scope": [first, last],
            "sections": sections,
        }
        if inventory:
            result["inventory"] = _inventory(doc, sections)
    return result


def format_sections_markdown(data: dict) -> str:
    """Render a ``sections_for_pdf`` result as Markdown tables."""
    heading = data.get("title") or "Sections"
    facts = []
    if data.get("key"):
        facts.append(f"Item `{data['key']}`")
    if data.get("attachment_key"):
        facts.append(f"attachment `{data['attachment_key']}`")
    facts.append(f"{data['page_count']} pages")
    facts.append(f"source: {data['source']}")
    first, last = data["scope"]
    facts.append(f"scope: {_page_span(first, last)}")

    lines = [f"# {heading}", "", " · ".join(facts), ""]
    if data["sections"]:
        lines += ["| ID | Pages | Level | Kind | Section |", "| --- | --- | --- | --- | --- |"]
        for section in data["sections"]:
            lines.append(
                f"| {section['id']} | {_page_span(section['start_page'], section['end_page'])} "
                f"| {section['level']} | {section['kind']} | {_cell(section['path'])} |"
            )
    else:
        lines.append("No sections.")

    rows = data.get("inventory")
    if rows is not None:
        lines += [
            "",
            "## Inventory",
            "",
            "| Section | Tables | Figures | Equations | Unnumbered equations | Plots |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in rows:
            plots = sum(entry["plots"] for entry in row.get("plots_on_page", []))
            lines.append(
                f"| {row['section_id']} | {_cell(', '.join(row['tables']) or '-')} "
                f"| {_cell(', '.join(row['figures']) or '-')} "
                f"| {_cell(', '.join(row['equations']) or '-')} | {row['unnumbered_equations']} "
                f"| {plots} |"
            )
    return "\n".join(lines) + "\n"


def _page_span(first: int, last: int) -> str:
    return f"page {first}" if first == last else f"pages {first}-{last}"


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _scope(pages, page_count: int) -> tuple[int, int]:
    """The first and last page to cover, checked against the document."""
    if pages is None:
        return 1, page_count
    numbers = [int(page) for page in pages]
    if not numbers:
        raise ValueError("pages is empty")
    first, last = numbers[0], numbers[-1]
    if len(numbers) > 2:
        first, last = min(numbers), max(numbers)
    if first < 1 or first > last:
        raise ValueError(f"pages must be a 1-based range with first <= last, got {first}-{last}")
    if first > page_count:
        raise ValueError(f"pages {first}-{last} lie outside the {page_count}-page document")
    return first, min(last, page_count)


def _read_outline(pdf_path: str) -> list:
    """Raw outline rows ``[level, title, page]``, or [] when there is none.

    A child process reads it (see the module docstring). A crash or timeout
    reads as "no outline", so the caller falls back to page chunks.
    """
    from zotero_mcp.tools.write import _extract_pdf_toc

    outcome = _extract_pdf_toc(pdf_path)
    if outcome.status != "ok":
        logger.warning("Could not read the PDF outline (%s: %s); using page chunks",
                       outcome.status, outcome.detail)
        return []
    return outcome.toc or []


def _outline_entries(toc: list, max_level: int, page_count: int) -> list[dict]:
    """Kept entries in page order, each with its ``Parent > Child`` path.

    The path comes from the whole outline, so a kept entry names its parents
    even when the parent row itself is not kept. Rows with no resolvable page
    (a destination the PDF never defines reads as 0 or -1) are dropped.
    """
    stack: list[str] = []
    entries: list[dict] = []
    for row in toc:
        try:
            level, title, page = int(row[0]), str(row[1]), int(row[2])
        except (TypeError, ValueError, IndexError):
            continue
        if level < 1:
            continue
        title = " ".join(title.split()) or _UNTITLED
        del stack[level - 1:]
        stack.append(title)
        if level <= max_level and 1 <= page <= page_count:
            entries.append({"title": title, "path": " > ".join(stack), "level": level, "page": page})
    entries.sort(key=lambda entry: entry["page"])  # stable: outline order breaks ties
    return entries


def _outline_units(entries: list[dict], page_count: int) -> list[dict]:
    """One unit per entry, plus a front unit for the pages before the first.

    Entry *i* ends on the page entry *i+1* starts on, so the two share that page
    (a section can end part-way down a page). The last entry ends on the last
    page; ``_clip`` cuts it to the scope.
    """
    units: list[dict] = []
    if entries[0]["page"] > 1:
        units.append({
            "title": _FRONT_TITLE, "path": _FRONT_TITLE, "level": 0,
            "start": 1, "end": entries[0]["page"] - 1, "kind": "front",
        })
    for index, entry in enumerate(entries):
        end = entries[index + 1]["page"] if index + 1 < len(entries) else page_count
        units.append({
            "title": entry["title"], "path": entry["path"], "level": entry["level"],
            "start": entry["page"], "end": end, "kind": "outline",
        })
    return units


def _clip(unit: dict, first: int, last: int) -> dict | None:
    start, end = max(unit["start"], first), min(unit["end"], last)
    if start > end:
        return None
    return {**unit, "start": start, "end": end}


def _split(unit: dict, chunk_pages: int) -> list[dict]:
    """Cut a unit longer than ``chunk_pages`` into near-equal parts.

    Sizes differ by at most one page and the larger parts come last, so nine
    pages at a limit of eight give 4 + 5.
    """
    length = unit["end"] - unit["start"] + 1
    if length <= chunk_pages:
        return [unit]
    parts = -(-length // chunk_pages)
    base, extra = divmod(length, parts)
    result = []
    start = unit["start"]
    for number in range(1, parts + 1):
        size = base + (1 if number > parts - extra else 0)
        suffix = f" (part {number}/{parts})"
        result.append({
            **unit,
            "title": unit["title"] + suffix,
            "path": unit["path"] + suffix,
            "start": start,
            "end": start + size - 1,
            "kind": "split",
        })
        start += size
    return result


def _chunks(first: int, last: int, chunk_pages: int) -> list[dict]:
    units = []
    for start in range(first, last + 1, chunk_pages):
        end = min(start + chunk_pages - 1, last)
        label = _page_span(start, end).capitalize()
        units.append({"title": label, "path": label, "level": 0, "start": start, "end": end, "kind": "chunk"})
    return units


def _inventory(doc, sections: list[dict]) -> list[dict]:
    """Per section, the tables, figures and equations its pages carry.

    Each row also lists the plot count of each of its pages. Pages shared by
    two sections are listed under both. A page that fails to scan counts as
    empty rather than failing the whole map.
    """
    scanned: dict[int, tuple[list[str], list[str], list[str], int, dict]] = {}
    rows = []
    for section in sections:
        tables: list[str] = []
        figures: list[str] = []
        equations: list[str] = []
        unnumbered = 0
        plots_on_page: list[dict] = []
        for page in range(section["start_page"], section["end_page"] + 1):
            if page not in scanned:
                scanned[page] = _scan_page(doc, page)
            page_tables, page_figures, page_equations, page_unnumbered, page_plots = scanned[page]
            _extend_unique(tables, page_tables, "tables")
            _extend_unique(figures, page_figures, "figures")
            _extend_unique(equations, page_equations, "equations")
            unnumbered += page_unnumbered
            # A copy, so the two rows of a shared page never share one list.
            plots_on_page.append({**page_plots, "panel_rects": list(page_plots["panel_rects"])})
        rows.append({
            "section_id": section["id"],
            "tables": tables,
            "figures": figures,
            "equations": equations,
            "unnumbered_equations": unnumbered,
            "plots_on_page": plots_on_page,
        })
    return rows


def _label_key(label: str, kind: str) -> str:
    """Comparison key so ``Fig. 3``, ``figure 3`` and ``Figure 3`` list once.

    A line label ``Eq. 3`` or ``Equation 3`` keys as ``(3)``, the form the math
    scan gives, so the two list once. Other equation labels keep their own text,
    so ``(1a)`` and ``(1b)`` stay apart.
    """
    if kind == "equations":
        named = _EQ_NAME_RE.match(label)
        return f"({named.group(1)})" if named else "".join(label.split())
    text = " ".join(label.lower().split())
    return re.sub(r"^fig\.?(?=\s)", "figure", text)


def _extend_unique(target: list[str], items: list[str], kind: str) -> None:
    """Append the items whose key is not in ``target`` yet, in order."""
    seen = {_label_key(item, kind) for item in target}
    for item in items:
        key = _label_key(item, kind)
        if key not in seen:
            seen.add(key)
            target.append(item)


def _line_labels(page) -> dict[str, list[str]]:
    """Labels that start a text line: ``{tables, figures, equations}``."""
    found: dict[str, list[str]] = {"tables": [], "figures": [], "equations": []}
    for line in page.get_text("text").splitlines():
        match = _LINE_LABEL_RE.match(line.strip())
        if not match:
            continue
        label = " ".join(match.group(0).split())
        if label.startswith("Table"):
            kind = "tables"
        elif label.startswith("Eq"):
            kind = "equations"
        else:
            kind = "figures"
        _extend_unique(found[kind], [label], kind)
    return found


def _caption_labels(page) -> dict[str, list[str]]:
    """Labels of the text blocks that read as captions: ``{tables, figures}``.

    A label keeps the case it has on the page, so ``FIGURE 10.`` gives
    ``FIGURE 10``. Each label appears once.
    """
    from zotero_mcp.pdf_layout import _parse_caption_block

    found: dict[str, list[str]] = {"tables": [], "figures": []}
    for block in page.get_text("blocks"):
        if block[6] != 0:
            continue
        parsed = _parse_caption_block(block[4])
        if parsed:
            kind = "tables" if parsed["kind"] == "table" else "figures"
            _extend_unique(found[kind], [parsed["label"]], kind)
    return found


def _rect_arg(box) -> str:
    """A normalized ``[x, y, w, h]`` box in the format of ``--rect``."""
    x, y, w, h = box
    return f"{x:.4f},{y:.4f},{w:.4f},{h:.4f}"


def _is_scan(page) -> bool:
    """True when one image covers at least ``LAYOUT_MAX_REGION_AREA`` of the page."""
    from zotero_mcp.pdf_layout import LAYOUT_MAX_REGION_AREA

    rect = page.rect
    page_area = rect.width * rect.height
    if page_area <= 0:
        return False
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        width = min(x1, rect.x1) - max(x0, rect.x0)
        height = min(y1, rect.y1) - max(y0, rect.y0)
        if width > 0 and height > 0 and width * height >= LAYOUT_MAX_REGION_AREA * page_area:
            return True
    return False


def _split_cells(page, bbox, **kwargs) -> list[list[float]] | None:
    """The plot cells of ``pdf_layout.split_panels``, or None when it cannot run.

    The function may be absent, and it may raise. In both cases the caller
    counts the box as one plot.
    """
    from zotero_mcp import pdf_layout

    split = getattr(pdf_layout, "split_panels", None)
    if split is None:
        return None
    try:
        cells = split(page, bbox, **kwargs)
    except Exception:
        logger.debug("split_panels failed on %s", bbox, exc_info=True)
        return None
    return [list(cell) for cell in cells or []]


def _page_plots(
    page,
    page_num: int,
    regions: list[dict],
    *,
    layout_failed: bool,
    has_table_label: bool,
    figure_labels: list[str],
    caption_figures: list[str],
) -> dict:
    """The ``plots_on_page`` entry of one page.

    Counting rules, in order:

    - A failed layout pass gives the figure label count.
    - Each drawing box counts 1. A table box counts 1 only when the page has no
      table label, since a plot can read as a table.
    - Each image or merged box counts its grid cells, or 1 when the split gives
      fewer than 2 cells or cannot run.
    - Equation boxes never count.
    - A scanned page with no box is split whole, with its text masked.
    - When nothing was found, the figure caption blocks are the count.
    """
    entry = {"page": page_num, "plots": 0, "source": "layout", "scanned": False, "panel_rects": []}
    try:
        entry["scanned"] = _is_scan(page)
    except Exception:
        pass
    if layout_failed:
        entry.update(plots=len(figure_labels), source="labels")
        return entry

    boxes = [region for region in regions if region.get("source") in _PLOT_SOURCES]
    plots = 0
    rects: list[str] = []
    grid = False
    for region in boxes:
        bbox = region["bbox"]
        if region["source"] == "drawing" or (region["source"] == "table" and not has_table_label):
            plots += 1
            rects.append(_rect_arg(bbox))
        elif region["source"] != "table":
            cells = _split_cells(page, bbox) or []
            if len(cells) >= 2:
                grid = True
                plots += len(cells)
                rects.extend(_rect_arg(cell) for cell in cells)
            else:
                plots += 1
                rects.append(_rect_arg(bbox))

    if entry["scanned"] and not boxes:
        cells = _split_cells(page, [0, 0, 1, 1], mask_text=True)
        if cells is not None:
            grid = True
            plots += len(cells)
            rects.extend(_rect_arg(cell) for cell in cells)

    if plots == 0 and caption_figures:
        entry.update(plots=len(caption_figures), source="captions")
        return entry
    entry.update(plots=plots, source="grid" if grid else "layout", panel_rects=rects)
    return entry


def _scan_page(doc, page_num: int) -> tuple[list[str], list[str], list[str], int, dict]:
    """(table labels, figure labels, equation labels, unnumbered equation count, plot entry).

    Layout captions and equation labels come first. Labels found at the start
    of a text line are added when the layout pass did not already list them.
    Caption blocks come last, so a caption set in capitals (``FIGURE 10.``)
    lists on its own page. The plot entry is described in ``_page_plots``.
    """
    from zotero_mcp.pdf_layout import detect_page_regions, scan_math

    tables: list[str] = []
    figures: list[str] = []
    layout_failed = False
    try:
        outcome = detect_page_regions(doc, page_num)
        regions = outcome.get("regions", [])
        layout_failed = "error" in outcome
    except Exception:
        regions = []
        layout_failed = True
    for region in regions:
        label = region.get("caption_label")
        # Display equations come from scan_math below, with their own labels.
        if not label or region.get("source") == "equation":
            continue
        if _TABLE_LABEL_RE.search(label):
            _extend_unique(tables, [label], "tables")
        else:
            _extend_unique(figures, [label], "figures")

    try:
        equations, _inline = scan_math(doc[page_num - 1])
    except Exception:
        equations = []
    labels = [equation["label"] for equation in equations if equation["label"]]
    unnumbered = len(equations) - len(labels)

    try:
        by_line = _line_labels(doc[page_num - 1])
    except Exception:
        by_line = {"tables": [], "figures": [], "equations": []}
    _extend_unique(tables, by_line["tables"], "tables")
    _extend_unique(figures, by_line["figures"], "figures")
    _extend_unique(labels, by_line["equations"], "equations")

    try:
        by_caption = _caption_labels(doc[page_num - 1])
    except Exception:
        by_caption = {"tables": [], "figures": []}
    _extend_unique(tables, by_caption["tables"], "tables")
    _extend_unique(figures, by_caption["figures"], "figures")

    try:
        page = doc[page_num - 1]
        plots = _page_plots(
            page, page_num, regions,
            layout_failed=layout_failed, has_table_label=bool(tables),
            figure_labels=figures, caption_figures=by_caption["figures"],
        )
    except Exception:
        plots = {"page": page_num, "plots": 0, "source": "labels", "scanned": False, "panel_rects": []}
    return tables, figures, labels, unnumbered, plots
