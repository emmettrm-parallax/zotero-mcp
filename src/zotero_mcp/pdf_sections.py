"""Section map of a PDF, for splitting a long source into readable units.

``zotero-cli sections`` gives a reader agent a list of page ranges to work
through one at a time. The ranges come from the PDF's outline when it has one,
and from fixed-size page chunks when it does not.

Rules (all pages are physical, 1-based):

- Outline entries of level <= ``max_level`` are the boundaries, in page order.
  Entry *i* spans its own start page to the page before entry *i+1* starts.
  When the next entry starts on the same page, the two share that page.
- Pages before the first entry form one ``front`` unit.
- ``pages`` clips every unit to a range; units wholly outside it are dropped.
- A unit longer than ``chunk_pages`` pages is split into near-equal ``split``
  parts. The larger parts come last.
- With no outline, or no outline entry inside the scope, the scope is cut
  into fixed ``chunk_pages``-page ``chunk`` units and ``source`` is ``chunks``.

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
        inventory: Also list, per unit, the table and figure captions and the
            display equations found on its pages.

    Returns:
        ``{key, attachment_key, title, page_count, source, scope, sections}``
        plus ``inventory`` when asked for. ``key``, ``attachment_key`` and
        ``title`` are None here; the caller fills them in. Each section is
        ``{id, title, path, level, start_page, end_page, kind}``. Each
        inventory row is ``{section_id, tables, figures, equations,
        unnumbered_equations}``.

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
        if any(first <= entry["page"] <= last for entry in entries):
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
            "| Section | Tables | Figures | Equations | Unnumbered equations |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in rows:
            lines.append(
                f"| {row['section_id']} | {_cell(', '.join(row['tables']) or '-')} "
                f"| {_cell(', '.join(row['figures']) or '-')} "
                f"| {_cell(', '.join(row['equations']) or '-')} | {row['unnumbered_equations']} |"
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
    """One unit per entry, plus a front unit for the pages before the first."""
    units: list[dict] = []
    if entries[0]["page"] > 1:
        units.append({
            "title": _FRONT_TITLE, "path": _FRONT_TITLE, "level": 0,
            "start": 1, "end": entries[0]["page"] - 1, "kind": "front",
        })
    for index, entry in enumerate(entries):
        following = entries[index + 1]["page"] if index + 1 < len(entries) else page_count + 1
        # A successor on the same page shares it; otherwise stop the page before.
        end = following - 1 if following > entry["page"] else entry["page"]
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

    Pages shared by two sections are listed under both. A page that fails to
    scan counts as empty rather than failing the whole map.
    """
    scanned: dict[int, tuple[list[str], list[str], list[str], int]] = {}
    rows = []
    for section in sections:
        tables: list[str] = []
        figures: list[str] = []
        equations: list[str] = []
        unnumbered = 0
        for page in range(section["start_page"], section["end_page"] + 1):
            if page not in scanned:
                scanned[page] = _scan_page(doc, page)
            page_tables, page_figures, page_equations, page_unnumbered = scanned[page]
            _extend_unique(tables, page_tables)
            _extend_unique(figures, page_figures)
            _extend_unique(equations, page_equations)
            unnumbered += page_unnumbered
        rows.append({
            "section_id": section["id"],
            "tables": tables,
            "figures": figures,
            "equations": equations,
            "unnumbered_equations": unnumbered,
        })
    return rows


def _extend_unique(target: list[str], items: list[str]) -> None:
    for item in items:
        if item not in target:
            target.append(item)


def _scan_page(doc, page_num: int) -> tuple[list[str], list[str], list[str], int]:
    """(table labels, figure labels, equation labels, unnumbered equation count)."""
    from zotero_mcp.pdf_layout import detect_page_regions, scan_math

    tables: list[str] = []
    figures: list[str] = []
    try:
        regions = detect_page_regions(doc, page_num).get("regions", [])
    except Exception:
        regions = []
    for region in regions:
        label = region.get("caption_label")
        # Display equations come from scan_math below, with their own labels.
        if not label or region.get("source") == "equation":
            continue
        _extend_unique(tables if _TABLE_LABEL_RE.search(label) else figures, [label])

    try:
        equations, _inline = scan_math(doc[page_num - 1])
    except Exception:
        equations = []
    labels = [equation["label"] for equation in equations if equation["label"]]
    return tables, figures, labels, len(equations) - len(labels)
