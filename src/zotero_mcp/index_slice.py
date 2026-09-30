"""Page slice of a source index, for ``zotero-cli index show --pages``.

An auditor grades one page range of an index, not the whole of it.
``filter_index_pages`` returns a new index that holds only what that range
touches:

* ``facts``, ``tables_figures``, ``equations`` and ``gaps`` keep an entry whose
  ``page`` is in the range. The section an entry is filed under does not
  matter, so a fact filed under a neighbour section stays when its page is in
  the range, and a gap with a null ``section_id`` is judged by its page alone.
* ``sections`` keeps a section whose ``[start_page, end_page]`` meets the range,
  so a page that two sections share keeps both.
* ``schema``, ``header``, ``vocabulary`` and any unknown top-level key pass
  through unchanged.

The slice is a view, not a valid index: a ``fact_ids`` list may name a fact
that the slice dropped, so the result need not pass ``validate_index``. The
input is never changed; the result shares no structure with it.

An error is ``cli_json.CliError`` with the code ``bad_pages``.
"""

from __future__ import annotations

import copy

from zotero_mcp.cli_json import CliError

#: The arrays whose entries carry one ``page``.
_PAGE_ARRAYS = ("facts", "tables_figures", "equations", "gaps")


def _check_pages(pages: list[int], page_count: object) -> list[int]:
    """Return the sorted distinct pages, or raise ``bad_pages``."""
    if not pages:
        raise CliError("no pages given: the page list is empty", code="bad_pages")
    checked = set()
    for page in pages:
        if isinstance(page, bool) or not isinstance(page, int):
            raise CliError(f"page {page!r} is not an integer", code="bad_pages")
        if page < 1:
            raise CliError(f"page {page} is below 1", code="bad_pages")
        if isinstance(page_count, int) and not isinstance(page_count, bool) and page > page_count:
            raise CliError(
                f"page {page} is above the page count of the index ({page_count})",
                code="bad_pages",
            )
        checked.add(page)
    return sorted(checked)


def _is_page(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def filter_index_pages(index: dict, pages: list[int]) -> dict:
    """Return a copy of ``index`` cut to ``pages``; the input stays as it was."""
    header = index.get("header")
    page_count = header.get("page_count") if isinstance(header, dict) else None
    wanted = set(_check_pages(pages, page_count))

    out = copy.deepcopy(index)
    for key in _PAGE_ARRAYS:
        if key in out:
            out[key] = [
                e for e in out[key]
                if isinstance(e, dict) and _is_page(e.get("page")) and e["page"] in wanted
            ]
    if "sections" in out:
        out["sections"] = [
            s for s in out["sections"]
            if isinstance(s, dict)
            and _is_page(s.get("start_page")) and _is_page(s.get("end_page"))
            and any(s["start_page"] <= p <= s["end_page"] for p in wanted)
        ]
    return out
