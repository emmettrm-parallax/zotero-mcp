"""`index show` and `index search`, shared by the CLI and the MCP tools.

This is the logic `cli_standalone._index_show` and `_index_search` used to carry directly.
It moved here so an MCP tool (`tools/index_tools.py`) can call the same code the CLI calls,
instead of re-implementing it against a shell subprocess. The CLI keeps thin wrappers of the
same two names that call `setup_zotero_environment()` and then :func:`show` or :func:`search`.

`index_grep`, `pdf_source`, `source_index` and `index_slice` are imported INSIDE
:func:`show` and :func:`search`, never at module load time. `tests/test_cli_source_commands.py`
replaces those four modules with fakes through `sys.modules` before a command runs. A
module-level import here would bind the real modules first, and the fakes would never apply.
That also keeps this module, and anything that imports it, free of a `pymupdf` dependency
until a call actually reads a PDF-backed note.
"""

from __future__ import annotations

from zotero_mcp import cli_json as _cli_json


def _parse_pages(value):
    """`all` or `None` means every page. `3`, `3-6`, `1,4,6-9` becomes a sorted page list.

    Mirrors `cli_standalone._parse_pages`, kept as its own copy so this module never
    imports the CLI — the MCP tools import this module too, and the CLI already owns a
    `--pages` parser with the same rule for its own flag.
    """
    if value is None or str(value).strip().lower() == "all":
        return None
    pages = set()
    try:
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, hi = (int(x) for x in part.split("-", 1))
                pages.update(range(lo, hi + 1))
            else:
                pages.add(int(part))
    except ValueError:
        pages = set()
    if not pages or min(pages) < 1:
        raise _cli_json.CliError(
            f"pages must be all, N, N-M, or a comma list of those, got {value!r}",
            code="bad_pages",
        )
    return sorted(pages)


def _terms(values, *, regex: bool) -> list:
    """The terms of `index show --grep` or `index search`, checked before any read.

    `parse_terms` refuses an empty list (`bad_grep`) and `check_terms` a pattern
    that does not compile (`bad_regex`), so a bad call never reaches the notes.
    """
    from zotero_mcp import index_grep

    terms = index_grep.parse_terms(values, regex=regex)
    index_grep.check_terms(terms, regex=regex)
    return terms


def _filter_with_section(report: dict, section) -> dict:
    """The filter report with `section` after `pages`, where the contract lists it."""
    out = {}
    for key, value in report.items():
        out[key] = value
        if key == "pages":
            out["section"] = section
    out.setdefault("section", section)
    return out


#: What `index show` applies when a filter flag is on and `limit` is not. This module
#: cannot import `index_grep`, which loads only when a call runs, so this mirrors
#: `index_grep.SHOW_LIMIT`. A test compares the two.
SHOW_LIMIT = 40

#: The entry lists of an index that `index search` returns for each item.
SEARCH_LISTS = ("sections", "facts", "vocabulary", "tables_figures", "equations", "gaps")

#: The parts of a filter report that stay on each item of `index search`.
SEARCH_REPORT = ("expanded_terms", "expanded_total", "expanded_symbols", "broad_terms",
                 "term_counts", "total", "matched", "returned", "truncated")


def show(key, *, section=None, pages=None, grep=None, regex: bool = False, expand: bool = False,
        fields=None, limit=None, ctx) -> dict:
    """`index show`: the index as stored, or the filtered view when a filter flag is on."""
    from zotero_mcp import pdf_source, source_index

    parsed_pages = _parse_pages(pages)
    filtered = grep is not None or fields is not None or limit is not None
    if grep is None and (expand or regex):
        raise _cli_json.CliError("--expand and --regex need --grep", code="bad_grep")
    terms = None if grep is None else _terms(grep, regex=regex)
    data = source_index.show_index(pdf_source.parent_key(key), section=section, ctx=ctx)
    if not filtered:
        if parsed_pages is not None:
            # After --section, so the two filters compose (AND). The slice
            # checks the pages against the index header, not against a PDF.
            from zotero_mcp import index_slice

            data = {**data, "index": index_slice.filter_index_pages(data["index"], parsed_pages)}
        return data

    from zotero_mcp import index_grep

    index, report = index_grep.apply_filters(
        data["index"], pages=parsed_pages, terms=terms, regex=regex, expand=expand,
        fields=fields or "full",
        limit=SHOW_LIMIT if limit is None else limit,
    )
    return {**data, "index": index, "filter": _filter_with_section(report, section)}


def search(terms, *, items=None, tag="status/indexed", fields="lead", expand: bool = False,
          limit=10, max_items=10, regex: bool = False, ctx) -> dict:
    """`index search`: the terms in the index of each item of the set, most facts first."""
    from zotero_mcp import index_grep, pdf_source, source_index

    parsed_terms = _terms(terms, regex=regex)
    if items is not None:
        requested = [key.strip() for key in items.split(",") if key.strip()]
        keys = list(dict.fromkeys(pdf_source.parent_key(key) for key in requested))
        tag = None
    else:
        requested = None
        keys = list(dict.fromkeys(source_index.list_items_with_tag(tag)))
    indexes = source_index.show_indexes(keys, ctx=ctx) if keys else {}

    hits, no_hits, skipped = [], [], []
    for key in keys:
        result = indexes.get(key) or {"error": {"code": "error", "message": "no index was read"}}
        if result.get("error"):
            error = result["error"]
            skipped.append({"item_key": key, "code": error.get("code") or "error",
                            "message": error.get("message") or ""})
            continue
        index, report = index_grep.apply_filters(
            result["index"], terms=parsed_terms, regex=regex, expand=expand,
            fields=fields, limit=limit,
        )
        matched = report.get("matched") or {}
        if not any(matched.values()):
            no_hits.append(key)
            continue
        header = result["index"].get("header")
        header = header if isinstance(header, dict) else {}
        item = {
            "item_key": key,
            "title": header.get("title"),
            "year": header.get("year"),
            "attachment_key": header.get("attachment_key"),
            "page_count": header.get("page_count"),
            "filter": {name: report.get(name) for name in SEARCH_REPORT},
            **{name: index.get(name, []) for name in SEARCH_LISTS},
        }
        hits.append(((-matched.get("facts", 0), -sum(matched.values()), key), item))

    hits.sort(key=lambda pair: pair[0])
    result_items = [item for _order, item in hits]
    if max_items:
        result_items = result_items[:max_items]
    return {
        "terms": parsed_terms, "regex": regex, "expand": expand, "fields": fields,
        "limit": limit, "max_items": max_items, "tag": tag,
        "items_requested": requested, "searched": len(keys),
        "items_truncated": len(hits) - len(result_items), "items": result_items,
        "no_hits": sorted(no_hits), "skipped": skipped,
    }
