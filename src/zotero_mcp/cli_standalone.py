"""
Standalone CLI for Zotero — direct tool access without an MCP server.

Usage:
    zotero-cli search "Einstein 1905"
    zotero-cli get metadata ITEM_KEY
    zotero-cli get collections
    zotero-cli add doi 10.1234/example -c "Reading List"
    zotero-cli add url https://arxiv.org/abs/2301.00001
    zotero-cli add isbn 9780262046305 -c "_project/books"
    zotero-cli add bibtex --file refs.bib -c topic
    zotero-cli add csl-json --json - -c topic   # reads stdin
    zotero-cli edit ITEM_KEY --title "New Title"
    zotero-cli notes list
    zotero-cli annotations list --item-key ITEM_KEY
    zotero-cli db status
"""

import argparse
import json
import sys

from zotero_mcp import cli_json as _cli_json

# Reuse environment setup from the original CLI module
from zotero_mcp.cli import (
    _format_chunking_status,
    _print_batch_import,
    _print_batch_status,
    _print_update_stats,
    obfuscate_config_for_display,
    setup_zotero_environment,
)

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

class CLIContext:
    """Drop-in replacement for fastmcp.Context that writes to stderr."""

    def __init__(self, verbose: bool = False):
        self._verbose = verbose

    def info(self, message: str) -> None:
        if self._verbose:
            print(f"[INFO] {message}", file=sys.stderr)

    def warning(self, message: str) -> None:
        print(f"[WARN] {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        print(f"[ERROR] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Lazy tool imports
# ---------------------------------------------------------------------------

def _import_tools():
    """Import tool modules lazily to avoid heavy startup cost."""
    from zotero_mcp import client as _client
    from zotero_mcp.tools import annotations, retrieval, search, write
    return search, retrieval, annotations, write, _client


def _ctx(args) -> CLIContext:
    return CLIContext(verbose=getattr(args, "verbose", False))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _json_mode(args) -> bool:
    return bool(getattr(args, "json_out", False))


def _out(args, command: str, *, data=None, text: str | None = None) -> None:
    """Emit one command's result in whichever shape the caller asked for.

    In markdown mode this is `print(text)` and nothing more. In JSON mode the
    command supplies `data` when it has real structure to offer, and only
    `text` when its answer genuinely is a status line -- a write that
    succeeded, a config dump. Wrapping prose as {"text": ...} rather than
    parsing it back into fields keeps the envelope honest: a caller can always
    branch on `ok`, and never has to guess whether a field was extracted
    reliably.
    """
    if text is not None:
        text = _cli_vocabulary(text)
    if data is None and text is not None and _reports_failure(text):
        # Most tools return their failures as prose rather than raising. Left
        # alone, that prose became an `ok: true` envelope with exit code 0, so
        # a caller that checked `ok` believed a failed write had worked.
        if _json_mode(args):
            _cli_json.emit_error(command, text.strip(), code="tool_error")
        else:
            print(text, file=sys.stderr)
        sys.exit(1)
    if _json_mode(args):
        _cli_json.emit(command, data if data is not None else {"text": text or ""})
    else:
        print(text if text is not None else "")


_FAILURE_RE = None


def _reports_failure(text: str) -> bool:
    """Whether a tool's prose result is a failure report.

    Tools that fail without raising lead with one of a few fixed phrasings
    ("Error: ...", "Error creating ...", "Failed to ...", "Could not ...",
    "Cannot ..."). Only the opening words count: a success message may quote
    an error further down, and must stay a success.
    """
    global _FAILURE_RE
    if _FAILURE_RE is None:
        import re
        _FAILURE_RE = re.compile(r"^[\s#*>]*(Error\b|Failed to\b|Could not\b|Cannot\b)")
    return bool(_FAILURE_RE.match(text))


# MCP tool names that tool output mentions in its advice, and the command that
# does the same thing here. Advice naming a tool the CLI user cannot call is a
# dead end.
_CLI_EQUIVALENTS = {
    "zotero_semantic_search": "`zotero-cli search --mode semantic`",
    "zotero_search_items": "`zotero-cli search`",
    "zotero_read_pdf_pages": "`zotero-cli read`",
    "zotero_get_pdf_outline": "`zotero-cli outline`",
    "zotero_get_page_layout": "`zotero-cli layout`",
    "zotero_get_item_children": "`zotero-cli get children`",
}


def _cli_vocabulary(text: str) -> str:
    for tool_name, command in _CLI_EQUIVALENTS.items():
        if tool_name in text:
            text = text.replace(tool_name, command)
    return text


def _items_for_json(items, detail: str = "summary") -> dict:
    return {
        "count": len(items),
        "items": _cli_json.project_items(items, detail),
    }


_ITEM_KEY_RE = None


def _keys_from_markdown(markdown: str) -> list[str]:
    """Item keys, in order, from a tool's markdown result.

    Every item the codebase renders goes through `utils.format_item_result`
    or `client.format_item_metadata`, and both always write the key on its own
    `**Item Key:** KEY` line; `keys_only` listings write `` - `KEY` | ... ``.
    Reading the keys back out of that is what lets the JSON path reuse the
    tool's own selection logic verbatim -- the search cascade, the semantic
    ranking, the collection scoping -- instead of reimplementing it and
    drifting from what markdown mode returns. The two modes therefore always
    agree on *which* items matched; JSON only changes how they are rendered.
    A detailed children listing writes a third shape, `   - Key: KEY`, and a
    listing for several parents writes a fourth, `  - [KEY] Attachment: ...` (#505).
    """
    global _ITEM_KEY_RE
    if _ITEM_KEY_RE is None:
        import re
        _ITEM_KEY_RE = re.compile(
            r"^\*\*Item Key:\*\*\s*`?([A-Z0-9]{8})`?\s*$"
            r"|^- `([A-Z0-9]{8})`"
            r"|^\s*- Key:\s*([A-Z0-9]{8})\s*$"
            r"|^\s*- \[([A-Z0-9]{8})\] ",
            re.MULTILINE,
        )
    keys: list[str] = []
    seen: set[str] = set()
    for match in _ITEM_KEY_RE.finditer(markdown or ""):
        key = match.group(1) or match.group(2) or match.group(3) or match.group(4)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _fetch_projected(backend, keys: list[str], detail: str = "summary") -> list[dict]:
    """Fetch *keys* and project them, preserving the order they were given in.

    Order carries meaning -- relevance for a search, recency for `get recent`
    -- so it is restored explicitly rather than left to whatever the backend
    returns. Keys the fetch does not return (deleted between the two calls,
    or not visible here) are dropped rather than faked. Batching is the
    backend's business now, so there is no chunk loop left here.
    """
    if not keys:
        return []
    try:
        found = backend.get_items(keys)
    except Exception:
        found = {}
    return [_cli_json.project_item(found[k], detail) for k in keys if k in found]


def _read_backend():
    """The read backend, for the JSON paths that project raw records.

    Structured output reads the same records the markdown formatters read;
    it just skips the formatting. Nothing about *which* records to fetch is
    duplicated -- where a command's selection logic is non-trivial (search
    variants, the semantic cascade), the JSON path calls that same logic and
    projects its result.
    """
    from zotero_mcp import library as _library

    return _library.get_library_backend()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_config(args):
    import os
    setup_zotero_environment()
    config = {
        k: v for k, v in os.environ.items()
        if k.startswith("ZOTERO_") or k in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY")
    }
    if not getattr(args, "show_secrets", False):
        config = obfuscate_config_for_display(config)
    if getattr(args, "json_out", False):
        _cli_json.emit("config", {"settings": dict(sorted(config.items()))})
        return
    print("=== Zotero Configuration ===")
    for k, v in sorted(config.items()):
        print(f"  {k}={v}")


def cmd_search(args):
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    ctx = _ctx(args)

    if args.mode == "tag":
        result = search_mod.search_by_tag(
            tag=args.query.split(","), limit=args.limit,
            collection_key=getattr(args, "collection", None), ctx=ctx,
        )
    elif args.mode == "citekey":
        result = search_mod.search_by_citation_key(citekey=args.query, ctx=ctx)
    elif args.mode == "advanced":
        try:
            conditions = json.loads(args.conditions)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON in --conditions: {e}", file=sys.stderr)
            sys.exit(1)
        result = search_mod.advanced_search(
            conditions=conditions, join_mode=args.join_mode,
            sort_by=args.sort_by, sort_direction=args.sort_direction,
            limit=args.limit,
            search_all_libraries=getattr(args, "all_libraries", False), ctx=ctx,
        )
    elif args.mode == "semantic":
        filters = None
        if getattr(args, "filters", None):
            try:
                filters = json.loads(args.filters)
            except json.JSONDecodeError as e:
                print(f"Error: invalid JSON in --filters: {e}", file=sys.stderr)
                sys.exit(1)
        result = search_mod.semantic_search(
            query=args.query, limit=args.limit, filters=filters,
            search_all_libraries=getattr(args, "all_libraries", False), ctx=ctx,
        )
    elif args.mode == "notes":
        result = annotations.search_notes(query=args.query, limit=args.limit, ctx=ctx)
    else:
        result = search_mod.search_items(
            query=args.query, qmode=args.qmode, limit=args.limit,
            collection_key=getattr(args, "collection", None),
            search_all_libraries=getattr(args, "all_libraries", False), ctx=ctx,
        )

    if _json_mode(args):
        keys = _keys_from_markdown(result)
        items = _fetch_projected(_read_backend(), keys,
                                 getattr(args, "detail", "summary"))
        _out(args, "search", data={
            "query": args.query, "mode": args.mode,
            "count": len(items), "items": items,
        })
        return
    print(result)


def cmd_get(args):
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    ctx = _ctx(args)
    sub = args.subcommand

    json_mode = _json_mode(args)

    if sub == "metadata":
        # In JSON mode the raw Zotero record is strictly better than a
        # projection: the caller asked for one specific item, so there is
        # nothing to trim for size and no reason to hide a field.
        fmt = "json" if (json_mode and args.output_format == "markdown") else args.output_format
        result = retrieval.get_item_metadata(
            item_key=args.item_key, include_abstract=not args.no_abstract,
            format=fmt, ctx=ctx,
        )
        if json_mode:
            try:
                _cli_json.emit("get metadata", json.loads(result))
            except ValueError:
                _out(args, "get metadata", text=result)
            return
        print(result)
    elif sub == "fulltext":
        result = retrieval.get_item_fulltext(item_key=args.item_key, ctx=ctx)
        _out(args, "get fulltext",
             data={"item_key": args.item_key, "text": result, "chars": len(result)}
             if json_mode else None,
             text=result)
    elif sub == "bibtex":
        result = retrieval.get_item_metadata(item_key=args.item_key, format="bibtex", ctx=ctx)
        _out(args, "get bibtex",
             data={"item_key": args.item_key, "bibtex": result} if json_mode else None,
             text=result)
    elif sub == "collections":
        if json_mode:
            cols = _read_backend().list_collections()[:args.limit]
            _cli_json.emit("get collections", {
                "count": len(cols),
                "collections": [_cli_json.project_collection(c) for c in cols],
            })
            return
        print(retrieval.get_collections(limit=args.limit, ctx=ctx))
    elif sub == "collection-items":
        result = retrieval.get_collection_items(
            collection_key=args.collection_key, detail=args.detail, limit=args.limit,
            offset=getattr(args, "offset", 0), ctx=ctx,
        )
        if json_mode:
            keys = _keys_from_markdown(result)
            _cli_json.emit("get collection-items", {
                "collection_key": args.collection_key,
                "offset": getattr(args, "offset", 0),
                "count": len(keys),
                "items": _fetch_projected(_read_backend(), keys, args.detail),
            })
            return
        print(result)
    elif sub == "children":
        # One tool now handles both arities; --item-keys and --item-key are
        # kept as distinct CLI flags for backwards compatibility.
        keys = getattr(args, "item_keys", None) or args.item_key
        result = retrieval.get_item_children(item_key=keys, ctx=ctx)
        if json_mode:
            child_keys = _keys_from_markdown(result)
            _cli_json.emit("get children", {
                "item_key": keys,
                "count": len(child_keys),
                "items": _fetch_projected(_read_backend(), child_keys, "summary"),
            })
            return
        print(result)
    elif sub == "tags":
        if json_mode:
            tags = _read_backend().list_tags(limit=args.limit)
            _cli_json.emit("get tags", {
                "count": len(tags),
                "tags": [_cli_json.project_tag(t) for t in tags],
            })
            return
        print(retrieval.get_tags(limit=args.limit, ctx=ctx))
    elif sub == "recent":
        result = retrieval.get_recent(
            limit=args.limit, collection_key=getattr(args, "collection", None), ctx=ctx,
        )
        if json_mode:
            keys = _keys_from_markdown(result)
            _cli_json.emit("get recent", {
                "count": len(keys),
                "items": _fetch_projected(_read_backend(), keys, "summary"),
            })
            return
        print(result)
    elif sub == "libraries":
        _out(args, "get libraries", text=retrieval.list_libraries(ctx=ctx))
    elif sub == "feeds":
        _out(args, "get feeds", text=retrieval.list_feeds(ctx=ctx))
    elif sub == "feed-items":
        _out(args, "get feed-items",
             text=retrieval.get_feed_items(
                 library_id=args.library_id, limit=args.limit, ctx=ctx))
    else:
        if json_mode:
            _cli_json.emit_error("get", f"Unknown 'get' subcommand: {sub}",
                                 code="unknown_subcommand")
        else:
            print(f"Unknown 'get' subcommand: {sub}", file=sys.stderr)
        sys.exit(1)


def cmd_annotations(args):
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    ctx = _ctx(args)

    json_mode = _json_mode(args)

    if args.subcommand == "list":
        # The tool already speaks JSON here, so --json just selects it and
        # wraps the result in the envelope every other command uses.
        fmt = "json" if (json_mode and args.format == "markdown") else args.format
        result = annotations.get_annotations(
            item_key=getattr(args, "item_key", None),
            use_pdf_extraction=args.pdf_extraction,
            limit=args.limit, format=fmt, ctx=ctx,
        )
        if json_mode:
            try:
                _cli_json.emit("annotations list", json.loads(result))
            except ValueError:
                _out(args, "annotations list", text=result)
            return
        print(result)
    elif args.subcommand == "create":
        _out(args, "annotations create", text=annotations.create_annotation(
            attachment_key=args.attachment_key, page=args.page,
            text=getattr(args, "text", None),
            rect=_parse_rect(getattr(args, "rect", None)),
            note=_parse_rect(getattr(args, "note", None), size=2, flag="--note"),
            comment=getattr(args, "comment", None),
            color=_resolve_color(args.color),
            tags=_split_csv(getattr(args, "tags", None)), ctx=ctx,
        ))
    elif args.subcommand == "batch":
        _annotations_batch(args, annotations, ctx)
    elif args.subcommand == "update":
        _out(args, "annotations update", text=annotations.update_annotation(
            annotation_key=args.annotation_key, text=getattr(args, "text", None),
            comment=getattr(args, "comment", None), color=getattr(args, "color", None),
            add_tags=_split_csv(getattr(args, "add_tags", None)),
            remove_tags=_split_csv(getattr(args, "remove_tags", None)), ctx=ctx,
        ))
    elif args.subcommand == "delete":
        _out(args, "annotations delete", text=annotations.delete_annotation(
            annotation_key=args.annotation_key, ctx=ctx,
        ))
    else:
        if json_mode:
            _cli_json.emit_error("annotations",
                                 f"Unknown 'annotations' subcommand: {args.subcommand}",
                                 code="unknown_subcommand")
        else:
            print(f"Unknown 'annotations' subcommand: {args.subcommand}", file=sys.stderr)
        sys.exit(1)


def _read_annotation_specs(source: str) -> list[dict]:
    """Annotation specs from a file or stdin: JSON Lines, or one JSON array."""
    if source == "-":
        raw = sys.stdin.read()
    else:
        try:
            with open(source, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            raise _cli_json.CliError(f"Cannot read {source}: {exc}", code="bad_file") from exc

    stripped = raw.strip()
    if not stripped:
        raise _cli_json.CliError("No annotations given", code="empty_batch")

    if stripped.startswith("["):
        try:
            specs = json.loads(stripped)
        except ValueError as exc:
            raise _cli_json.CliError(f"Invalid JSON array: {exc}", code="bad_json") from exc
    else:
        specs = []
        for line_no, line in enumerate(stripped.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                specs.append(json.loads(line))
            except ValueError as exc:
                raise _cli_json.CliError(f"Line {line_no} is not valid JSON: {exc}",
                                         code="bad_json") from exc

    if not isinstance(specs, list) or not all(isinstance(s, dict) for s in specs):
        raise _cli_json.CliError("Each annotation must be a JSON object", code="bad_json")
    return specs


def _annotations_batch(args, annotations, ctx) -> None:
    """`annotations batch`: create (or with --dry-run, locate) many annotations.

    Specs are grouped by attachment, and each group is one call: the PDF is
    fetched once and the annotations are written together. Every spec is
    attempted even when others fail, each outcome is listed so a caller can
    retry just the failures, and the exit code is 1 when any spec failed.
    """
    import time

    specs = _read_annotation_specs(args.file)
    started = time.monotonic()

    groups: dict[str, list[int]] = {}
    for position, spec in enumerate(specs):
        groups.setdefault(spec.get("attachment_key") or args.attachment_key, []).append(position)
        for key, size in (("rect", 4), ("note", 2)):
            if spec.get(key) is not None:
                try:
                    spec[key] = _parse_rect(spec[key], size=size, flag=key)
                except _cli_json.CliError:
                    pass  # left as given; the tool reports the malformed value for this spec
        if spec.get("color"):
            spec["color"] = _resolve_color(spec["color"])

    results: list[dict] = [{}] * len(specs)
    for attachment_key, positions in groups.items():
        outcomes = annotations.create_annotations(
            attachment_key, [specs[p] for p in positions], ctx=ctx,
            dry_run=args.dry_run, allow_epub=True,
        )
        for position, outcome in zip(positions, outcomes):
            results[position] = {**outcome, "index": position + 1}
            if not outcome["ok"]:
                results[position]["error"] = _cli_vocabulary(outcome["error"].strip())

    failed = [r for r in results if not r["ok"]]
    seconds = round(time.monotonic() - started, 2)

    if _json_mode(args):
        _cli_json.emit("annotations batch", {
            "dry_run": bool(args.dry_run),
            "succeeded": len(results) - len(failed),
            "failed": len(failed),
            "seconds": seconds,
            "results": results,
        })
    else:
        for r in results:
            where = f"#{r['index']} p{r['page']}"
            if not r["ok"]:
                print(f"FAIL  {where}: {r['error']}")
            elif args.dry_run and r["type"] == "highlight":
                found = r.get("page_found") or r.get("chapter_found")
                moved = f" (found on p{found})" if found not in (None, r["page"]) else ""
                print(f"ok    {where}{moved}: {r.get('matched_text', '')[:120]}")
            elif args.dry_run:
                print(f"ok    {where} area")
            else:
                print(f"ok    {where} {r['type']} {r['annotation_key']}")
        verb = "Located" if args.dry_run else "Created"
        print(f"\n{verb} {len(results) - len(failed)}/{len(results)} in {seconds}s"
              + (f"; {len(failed)} failed" if failed else ""))

    if failed:
        sys.exit(1)


def cmd_layout(args):
    """Figure and table boxes on one or more pages of a PDF attachment."""
    setup_zotero_environment()
    _s, _r, annotations, _w, _c = _import_tools()
    pages = _parse_pages(args.pages)
    layouts, filename, error = annotations.detect_layouts(args.attachment_key, pages, ctx=_ctx(args))
    if error:
        _out(args, "layout", text=error)
        return

    if _json_mode(args):
        regions = []
        for layout in layouts:
            for region in layout["regions"]:
                x, y, w, h = region["bbox"]
                regions.append({
                    "page": layout["page"],
                    "bbox": region["bbox"],
                    "source": region["source"],
                    "caption_label": region["caption_label"],
                    "caption_text": region["caption_text"],
                    "confidence": region["confidence"],
                    "rect_arg": f"{x:.4f},{y:.4f},{w:.4f},{h:.4f}",
                })
        warnings = [f"p{layout['page']}: {w}" for layout in layouts for w in layout.get("warnings", [])]
        _cli_json.emit("layout", {"attachment_key": args.attachment_key, "filename": filename,
                                  "pages_scanned": [layout["page"] for layout in layouts],
                                  "regions": regions, "warnings": warnings})
        return

    with_regions = [layout for layout in layouts if layout["regions"]]
    if not with_regions:
        print(f"No figure/table regions detected on the {len(layouts)} page(s) scanned.")
        return
    for layout in with_regions:
        # One paste-ready example is enough; repeating it per page buries the tables.
        style = "cli" if layout is with_regions[-1] else "none"
        print(annotations._format_page_layout(layout, args.attachment_key, layout["page"],
                                              filename, hint_style=style))
        print()


def _with_source(pdf, data: dict) -> dict:
    """An engine result under the keys of the PDF it read.

    The engines take a path and know nothing of Zotero, so the identity
    fields are added here. They lead the result and win over any field of
    the same name the engine returned.
    """
    head = {"key": pdf.parent_key, "attachment_key": pdf.attachment_key, "title": pdf.title}
    return {**head, **{k: v for k, v in data.items() if k not in head}}


def _emit_result(args, command: str, data: dict, render) -> None:
    """Emit *data* as the JSON envelope, or `render(data)` as markdown."""
    _out(args, command, data=data, text=None if _json_mode(args) else render(data))


def _pdf_page_count(path) -> int:
    """How many pages the PDF at *path* has."""
    import pymupdf

    with pymupdf.open(path) as doc:
        return len(doc)


def _check_pages(pages, path, flag: str = "--pages") -> None:
    """Refuse a page list that is empty, starts below 1, or runs past the PDF.

    `all` reaches here as None and needs no check. The count is read last, so
    a list that is wrong on its face never opens the PDF.
    """
    if pages is None:
        return
    if not pages or min(pages) < 1:
        raise _cli_json.CliError(
            f"{flag} must name pages from 1 up, got {pages!r}", code="bad_pages",
        )
    count = _pdf_page_count(path)
    if max(pages) > count:
        raise _cli_json.CliError(
            f"{flag} names page {max(pages)}, but the PDF has {count} pages",
            code="bad_pages",
        )


def cmd_grep(args):
    """Count and locate terms in a PDF attachment, page by page."""
    import re

    as_plain_text = getattr(args, "text", False)
    if as_plain_text and _json_mode(args):
        raise _cli_json.CliError("--text does not combine with --json", code="bad_flags")
    pages = _parse_pages(args.pages)
    if args.regex:
        # Reported before the PDF is fetched, like a bad --rect.
        for term in args.terms:
            try:
                re.compile(term)
            except re.error as exc:
                raise _cli_json.CliError(f"Bad regex {term!r}: {exc}", code="bad_regex") from exc
    setup_zotero_environment()
    from zotero_mcp import pdf_grep, pdf_source

    with pdf_source.resolved_pdf(args.key, _ctx(args)) as pdf:
        _check_pages(pages, pdf.path)
        try:
            data = pdf_grep.grep_pdf(
                pdf.path, args.terms, regex=args.regex, word=args.word, pages=pages,
                context=args.context, max_hits=args.max_hits, order=args.order,
                use_cache=not args.no_cache, jobs=args.jobs,
            )
        except re.error as exc:
            raise _cli_json.CliError(f"Bad regex: {exc}", code="bad_regex") from exc
    if as_plain_text:
        if not data.get("total_hits"):
            print("no hits", file=sys.stderr)
            return
        print(pdf_grep.format_grep_text(data))
        if data.get("truncated"):
            print("snippets were cut by max-hits", file=sys.stderr)
        return
    _emit_result(args, "grep", _with_source(pdf, data), pdf_grep.format_grep_markdown)


def cmd_sections(args):
    """Split a PDF attachment into sections, from its outline or in page chunks."""
    pages = _parse_pages(args.pages)
    setup_zotero_environment()
    from zotero_mcp import pdf_sections, pdf_source

    with pdf_source.resolved_pdf(args.key, _ctx(args)) as pdf:
        _check_pages(pages, pdf.path)
        data = pdf_sections.sections_for_pdf(
            pdf.path, pages=pages, max_level=args.max_level,
            chunk_pages=args.chunk_pages, inventory=args.inventory,
        )
    _emit_result(args, "sections", _with_source(pdf, data), pdf_sections.format_sections_markdown)


def cmd_tables(args):
    """The cells of each table on the named pages of a PDF attachment."""
    pages = _parse_pages(args.pages)
    setup_zotero_environment()
    from zotero_mcp import pdf_source, pdf_tables

    with pdf_source.resolved_pdf(args.key, _ctx(args)) as pdf:
        _check_pages(pages, pdf.path)
        data = pdf_tables.tables_for_pdf(pdf.path, pages=pages, strategy=args.strategy)
    _emit_result(args, "tables", {"attachment_key": pdf.attachment_key, **data},
                 pdf_tables.format_tables_markdown)


def _read_index_json(source: str):
    """The index object from FILE, or from stdin when *source* is `-`."""
    try:
        if source == "-":
            raw = sys.stdin.read()
        else:
            with open(source, encoding="utf-8") as handle:
                raw = handle.read()
        return json.loads(raw)
    except (OSError, ValueError) as exc:
        where = "stdin" if source == "-" else source
        raise _cli_json.CliError(
            f"Cannot read index JSON from {where}: {exc}", code="invalid_index",
        ) from exc


def _count(value) -> int:
    """A count that an engine may report as a number or as a list of keys."""
    return len(value) if isinstance(value, (list, tuple, set)) else int(value or 0)


def _format_index_push(data: dict) -> str:
    verb = "Dry run for" if data.get("dry_run") else "Indexed"
    lines = [
        f"{verb} {data.get('item_key')}: {data.get('parts')} note part(s), "
        f"{data.get('chars')} characters",
        f"created {_count(data.get('created'))}, updated {_count(data.get('updated'))}, "
        f"trashed {_count(data.get('trashed'))}",
    ]
    counts = data.get("counts") or {}
    if counts:
        lines.append(", ".join(f"{name} {n}" for name, n in counts.items()))
    if data.get("note_keys"):
        lines.append("notes: " + ", ".join(data["note_keys"]))
    return "\n".join(lines)


def _format_filter_line(report: dict) -> str:
    """The one-line summary of a filtered `index show`, from `data.filter`."""
    total, matched = report.get("total") or {}, report.get("matched") or {}
    returned, truncated = report.get("returned") or {}, report.get("truncated") or {}
    broad = ", ".join(report.get("broad_terms") or [])
    return (f"Filter: {matched.get('facts', 0)} of {total.get('facts', 0)} facts matched, "
            f"{returned.get('facts', 0)} returned, {truncated.get('facts', 0)} cut by --limit; "
            f"expanded {report.get('expanded_total', 0)}; broad [{broad}]")


def _format_index_show(data: dict) -> str:
    head = (f"Source index for {data.get('item_key')}: {data.get('parts')} note part(s)"
            + (f" ({', '.join(data['note_keys'])})" if data.get("note_keys") else ""))
    if data.get("filter"):
        head += "\n" + _format_filter_line(data["filter"])
    return head + "\n\n" + json.dumps(data.get("index"), indent=2, ensure_ascii=False)


def _fact_lead_line(fact: dict) -> str:
    """`p5 F0012 quantity = value unit (condition) [ref]`. A null part is left out.

    A fact with no quantity and no value carries a `lead` key instead. The line then reads
    `p5 F0012 "<lead>"`.
    """
    def shown(key):
        value = fact.get(key)
        return "-" if value is None else value

    if fact.get("lead") is not None:
        return f"p{shown('page')} {shown('id')} \"{fact['lead']}\""

    line = f"p{shown('page')} {shown('id')} {shown('quantity')} = {shown('value')}"
    for key, wrap in (("unit", "{}"), ("condition", "({})"), ("ref", "[{}]")):
        if fact.get(key) is not None:
            line += " " + wrap.format(fact[key])
    return line


def _format_index_search(data: dict) -> str:
    blocks = []
    for item in data.get("items") or []:
        matched = (item.get("filter") or {}).get("matched") or {}
        year = f" ({item['year']})" if item.get("year") else ""
        title = f" {item['title']}" if item.get("title") else ""
        lines = [f"## {item.get('item_key')}{title}{year}: "
                 f"{matched.get('facts', 0)} facts, {matched.get('vocabulary', 0)} vocabulary"]
        lines += [_fact_lead_line(fact) for fact in item.get("facts") or []]
        blocks.append("\n".join(lines))
    notes = []
    if data.get("no_hits"):
        notes.append("No hits: " + ", ".join(data["no_hits"]))
    for skip in data.get("skipped") or []:
        notes.append(f"Skipped {skip.get('item_key')}: {skip.get('code')}: {skip.get('message')}")
    if data.get("items_truncated"):
        notes.append(f"{data['items_truncated']} more item(s) with hits cut by --max-items")
    if notes:
        blocks.append("\n".join(notes))
    return "\n\n".join(blocks) or "No items to search."


from zotero_mcp.index_query import SHOW_LIMIT as _INDEX_SHOW_LIMIT  # noqa: E402,F401

#: `_INDEX_SHOW_LIMIT` above is kept only because a test compares it with
#: `index_grep.SHOW_LIMIT`. The logic that used it now lives on `index_query`, which
#: both this CLI and the MCP tools of `tools/index_tools.py` call.


def _index_show(args, ctx) -> dict:
    """`index show`: thin CLI wrapper over `index_query.show`."""
    from zotero_mcp import index_query

    setup_zotero_environment()
    return index_query.show(
        args.key, section=args.section, pages=args.pages, grep=args.grep, regex=args.regex,
        expand=args.expand, fields=args.fields, limit=args.limit, ctx=ctx,
    )


def _index_search(args, ctx) -> dict:
    """`index search`: thin CLI wrapper over `index_query.search`."""
    from zotero_mcp import index_query

    setup_zotero_environment()
    return index_query.search(
        args.terms, items=args.items, tag=args.tag, fields=args.fields, expand=args.expand,
        limit=args.limit, max_items=args.max_items, regex=args.regex, ctx=ctx,
    )


#: `KEY kind title_short: scope`, at most this many characters, `[hit: ...]` inside the cap.
_CARD_LINE_CAP = 200

#: The fields a compact `index cards` record keeps, without `--expand`.
_CARD_COMPACT_FIELDS = ("item_key", "kind", "title_short", "scope")


def _card_line(card: dict, hit: list) -> str:
    """`KEY kind title_short: scope`, at most `_CARD_LINE_CAP` characters.

    `[hit: ...]` stays inside the cap: the title and scope are cut first, with the hit
    note kept whole. Only a pathologically long hit list falls back to a hard cut of the
    whole line.
    """
    tail = f" [hit: {', '.join(hit)}]" if hit else ""
    head = f"{card.get('item_key')} {card.get('kind')} {card.get('title_short')}: {card.get('scope')}"
    line = head + tail
    if len(line) <= _CARD_LINE_CAP:
        return line
    room = _CARD_LINE_CAP - len(tail) - 3  # "..." marks the cut
    if room < 1:
        return line[:_CARD_LINE_CAP]
    return head[:room] + "..." + tail


def _card_block(card: dict) -> str:
    """The full card, pretty-printed, for `index cards --expand`."""
    head = f"## {card.get('item_key')} ({card.get('note_key', '-')})"
    return head + "\n" + json.dumps(card, indent=2, ensure_ascii=False)


def _format_index_cards(data: dict) -> str:
    cards = data.get("cards") or []
    if not cards:
        return f"No cards match {', '.join(data['terms'])}." if data.get("terms") else "No cards."
    if data.get("expand"):
        return "\n\n".join(_card_block(card) for card in cards)
    return "\n".join(_card_line(card, card.get("hit") or []) for card in cards)


def _format_index_cards_push(data: dict) -> str:
    verb = "Updated" if data.get("updated") else "Created"
    line = f"{verb} the card for {data.get('item_key')} (note {data.get('note_key')})"
    if data.get("trashed"):
        line += f", trashed {data['trashed']} surplus card note(s)"
    return line


def _index_cards_list(args, ctx) -> dict:
    """`index cards`: every item's card, filtered by `--grep` and ranked by terms hit."""
    from zotero_mcp import index_query, source_card

    terms = None if args.grep is None else index_query._terms(args.grep, regex=args.regex)
    setup_zotero_environment()
    result = source_card.list_cards(terms=terms, regex=args.regex, ctx=ctx)
    cards = result["cards"] if args.expand else [
        {key: card.get(key) for key in (*_CARD_COMPACT_FIELDS, "hit")} for card in result["cards"]
    ]
    return {
        "cards": cards, "count": result["count"], "terms": result["terms"],
        "regex": args.regex, "expand": args.expand,
    }


def _read_card_json(source: str) -> dict:
    """The card object from FILE, or from stdin when *source* is `-`.

    A twin of `_read_index_json` with its own error code, kept separate so `index push`
    keeps its own `invalid_index` wording and this stays inside the cards code added for
    `index cards push`.
    """
    try:
        if source == "-":
            raw = sys.stdin.read()
        else:
            with open(source, encoding="utf-8") as handle:
                raw = handle.read()
        return json.loads(raw)
    except (OSError, ValueError) as exc:
        where = "stdin" if source == "-" else source
        raise _cli_json.CliError(
            f"Cannot read card JSON from {where}: {exc}", code="invalid_card",
        ) from exc


def _index_cards_push(args, ctx) -> dict:
    """`index cards push`: write or replace the one card note of an item."""
    from zotero_mcp import pdf_source, source_card

    card = _read_card_json(args.from_file)
    problems = source_card.validate_card(card)
    if problems:
        shown = "; ".join(problems[:10])
        more = f"; and {len(problems) - 10} more" if len(problems) > 10 else ""
        raise _cli_json.CliError(
            f"Card is not valid ({len(problems)} problem(s)): {shown}{more}",
            code="invalid_card",
        )
    setup_zotero_environment()
    return source_card.push_card(pdf_source.parent_key(args.key), card, ctx=ctx)


def cmd_index(args):
    """Push a source index into an item's notes, or read it back."""
    from zotero_mcp import pdf_source, source_index

    ctx = _ctx(args)
    if args.subcommand == "push":
        index = _read_index_json(args.from_file)
        problems = source_index.validate_index(index)
        if problems:
            shown = "; ".join(problems[:10])
            more = f"; and {len(problems) - 10} more" if len(problems) > 10 else ""
            raise _cli_json.CliError(
                f"Index is not valid ({len(problems)} problem(s)): {shown}{more}",
                code="invalid_index",
            )
        setup_zotero_environment()
        data = source_index.push_index(
            pdf_source.parent_key(args.key), index, replace=args.replace,
            tags=_split_csv(args.tags), dry_run=args.dry_run, ctx=ctx,
        )
        _emit_result(args, "index push", data, _format_index_push)
    elif args.subcommand == "show":
        _emit_result(args, "index show", _index_show(args, ctx), _format_index_show)
    elif args.subcommand == "search":
        _emit_result(args, "index search", _index_search(args, ctx), _format_index_search)
    elif args.subcommand == "cards":
        if getattr(args, "cards_subcommand", None) == "push":
            _emit_result(args, "index cards push", _index_cards_push(args, ctx), _format_index_cards_push)
        else:
            _emit_result(args, "index cards", _index_cards_list(args, ctx), _format_index_cards)


def cmd_notes(args):
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    ctx = _ctx(args)

    json_mode = _json_mode(args)

    if args.subcommand == "list":
        result = annotations.get_notes(
            item_key=getattr(args, "item_key", None), limit=args.limit,
            truncate=not args.full, raw_html=args.raw_html, ctx=ctx,
        )
        if json_mode:
            # get_notes writes each note's key as `**Key:** KEY`, not the
            # `**Item Key:**` line _keys_from_markdown reads, so that found
            # nothing and every `--json notes list` came back empty.
            import re
            keys = list(dict.fromkeys(
                re.findall(r"^\*\*Key:\*\*\s*`?([A-Z0-9]{8})`?\s*$", result, re.MULTILINE)
            ))
            fetched = _read_backend().get_items(keys)
            notes = []
            for key in keys:
                try:
                    notes.append(_cli_json.project_note(fetched[key]))
                except Exception:
                    continue
            _cli_json.emit("notes list", {"count": len(notes), "notes": notes})
            return
        print(result)
    elif args.subcommand == "create":
        note_text = sys.stdin.read() if args.text == "-" else (args.text or "")
        if not note_text:
            print("Error: provide note text via --text TEXT or --text - (reads stdin)",
                  file=sys.stderr)
            sys.exit(1)
        tags = args.tags.split(",") if args.tags else []
        _out(args, "notes create", text=annotations.create_note(
            item_key=args.item_key, note_title=args.title or "CLI Note",
            note_text=note_text, tags=tags, ctx=ctx,
        ))
    elif args.subcommand == "update":
        note_text = sys.stdin.read() if args.text == "-" else args.text
        _out(args, "notes update", text=annotations.update_note(item_key=args.item_key, note_text=note_text, ctx=ctx))
    elif args.subcommand == "delete":
        _out(args, "notes delete", text=annotations.delete_note(item_key=args.item_key, ctx=ctx))
    else:
        print(f"Unknown 'notes' subcommand: {args.subcommand}", file=sys.stderr)
        sys.exit(1)


def _collect_collection_specs(args) -> list | None:
    """Merge --collections (comma-split) with repeatable -c/--collection.

    Each -c value is ONE spec, never comma-split — so names containing
    commas work. Returns None when neither flag was given.
    """
    specs = []
    if getattr(args, "collections", None):
        specs.extend(s.strip() for s in args.collections.split(",") if s.strip())
    for spec in getattr(args, "collection", None) or []:
        if spec.strip():
            specs.append(spec.strip())
    return specs or None


def cmd_add(args):
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    ctx = _ctx(args)
    tags = args.tags.split(",") if args.tags else None
    collections = _collect_collection_specs(args)
    if_exists = getattr(args, "if_exists", "file")
    create_missing = getattr(args, "create_collections", False)

    if args.subcommand == "doi":
        _out(args, "add doi", text=write_mod.add_by_doi(
            doi=args.doi, collections=collections, tags=tags,
            attach_mode=args.attach_mode, if_exists=if_exists,
            create_missing_collections=create_missing, ctx=ctx,
        ))
    elif args.subcommand == "url":
        _out(args, "add url", text=write_mod.add_by_url(
            url=args.url, collections=collections, tags=tags,
            attach_mode=args.attach_mode, if_exists=if_exists,
            create_missing_collections=create_missing, ctx=ctx,
        ))
    elif args.subcommand == "file":
        _out(args, "add file", text=write_mod.add_from_file(
            file_path=args.filepath, title=getattr(args, "title", None),
            item_type=getattr(args, "item_type", "document"),
            collections=collections, tags=tags, if_exists=if_exists,
            create_missing_collections=create_missing, ctx=ctx,
        ))
    elif args.subcommand == "isbn":
        _out(args, "add isbn", text=write_mod.add_by_isbn(
            isbn=args.isbn, collections=collections, tags=tags,
            if_exists=if_exists, create_missing_collections=create_missing,
            ctx=ctx,
        ))
    elif args.subcommand == "bibtex":
        bibtex = sys.stdin.read() if args.bibtex == "-" else args.bibtex
        _out(args, "add bibtex", text=write_mod.add_by_bibtex(
            bibtex=bibtex, file_path=getattr(args, "file", None),
            collections=collections, tags=tags,
            attach_mode=args.attach_mode, if_exists=if_exists,
            create_missing_collections=create_missing, ctx=ctx,
        ))
    elif args.subcommand == "csl-json":
        csl_json = sys.stdin.read() if args.json == "-" else args.json
        _out(args, "add csl-json", text=write_mod.add_by_csl_json(
            csl_json=csl_json, file_path=getattr(args, "file", None),
            collections=collections, tags=tags,
            attach_mode=args.attach_mode, if_exists=if_exists,
            create_missing_collections=create_missing, ctx=ctx,
        ))
    else:
        print(f"Unknown 'add' subcommand: {args.subcommand}", file=sys.stderr)
        sys.exit(1)


def cmd_collections(args):
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    ctx = _ctx(args)

    if args.subcommand == "create":
        _out(args, "collections create", text=write_mod.create_collection(
            name=args.name, parent_collection=getattr(args, "parent", None), ctx=ctx,
        ))
    elif args.subcommand == "update":
        _out(args, "collections update", text=write_mod.update_collection(
            collection_key=args.collection_key, name=args.name,
            parent_collection=args.parent, to_top_level=args.top_level, ctx=ctx,
        ))
    elif args.subcommand == "search":
        _out(args, "collections search", text=write_mod.search_collections(query=args.query, ctx=ctx))
    elif args.subcommand == "manage":
        _out(args, "collections manage", text=write_mod.manage_collections(
            item_keys=args.item_keys.split(","),
            add_to=args.add_to.split(",") if args.add_to else None,
            remove_from=args.remove_from.split(",") if args.remove_from else None,
            ctx=ctx,
        ))
    else:
        print(f"Unknown 'collections' subcommand: {args.subcommand}", file=sys.stderr)
        sys.exit(1)


def cmd_tags(args):
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    ctx = _ctx(args)
    _out(args, "tags", text=write_mod.batch_update_tags(
        query=args.query or "",
        add_tags=args.add.split(",") if args.add else None,
        remove_tags=args.remove.split(",") if args.remove else None,
        tag=args.tag.split(",") if args.tag else None,
        limit=args.limit,
        ctx=ctx,
    ))


def cmd_edit(args):
    """Edit metadata fields of an existing Zotero item."""
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    ctx = _ctx(args)

    creators = None
    if args.creators:
        try:
            creators = json.loads(args.creators)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON in --creators: {e}", file=sys.stderr)
            sys.exit(1)

    # Flat metadata flags all travel in one `fields` mapping now; only the
    # params with delta semantics stay top-level.
    flat_fields = {
        "title": args.title,
        "date": args.date,
        "publication_title": args.publication_title,
        "abstract": args.abstract,
        "doi": args.doi,
        "url": args.url,
        "extra": args.extra,
        "volume": args.volume,
        "issue": args.issue,
        "pages": args.pages,
        "publisher": args.publisher,
        "issn": args.issn,
        "language": args.language,
        "short_title": args.short_title,
        "edition": args.edition,
        "isbn": args.isbn,
        "book_title": args.book_title,
    }
    fields = {k: v for k, v in flat_fields.items() if v is not None}

    _out(args, "edit", text=write_mod.update_item(
        item_key=args.item_key,
        fields=fields or None,
        creators=creators,
        tags=args.tags.split(",") if args.tags else None,
        add_tags=args.add_tags.split(",") if args.add_tags else None,
        remove_tags=args.remove_tags.split(",") if args.remove_tags else None,
        collections=args.collections.split(",") if args.collections else None,
        collection_names=args.collection_names.split(",") if args.collection_names else None,
        ctx=ctx,
    ))


def cmd_duplicates(args):
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    ctx = _ctx(args)

    if args.subcommand == "find":
        _out(args, "duplicates find", text=write_mod.find_duplicates(
            method=args.method, collection_key=getattr(args, "collection", None),
            limit=args.limit, ctx=ctx,
        ))
    elif args.subcommand == "merge":
        _out(args, "duplicates merge", text=write_mod.merge_duplicates(
            keeper_key=args.keeper_key,
            duplicate_keys=args.duplicate_keys.split(","),
            confirm=not args.dry_run, ctx=ctx,
        ))
    else:
        print(f"Unknown 'duplicates' subcommand: {args.subcommand}", file=sys.stderr)
        sys.exit(1)


def cmd_db(args):
    """Manage the semantic search database."""
    from pathlib import Path
    setup_zotero_environment()
    from zotero_mcp.cli import _save_zotero_db_path_to_config
    from zotero_mcp.semantic_search import create_semantic_search

    config_path_arg = getattr(args, "config_path", None)
    config_path = (
        Path(config_path_arg) if config_path_arg
        else Path.home() / ".config" / "zotero-mcp" / "config.json"
    )

    if args.subcommand == "update":
        db_path = getattr(args, "db_path", None)
        if db_path:
            _save_zotero_db_path_to_config(config_path, db_path)
        search = create_semantic_search(str(config_path), db_path=db_path)
        if getattr(args, "openai_batch", None) is True and search.chroma_client.embedding_model != "openai":
            print("Error: --openai-batch requires ZOTERO_EMBEDDING_MODEL=openai", file=sys.stderr)
            sys.exit(1)
        fulltext = getattr(args, "fulltext", False)
        if fulltext:
            from zotero_mcp.utils import is_local_mode
            if not is_local_mode():
                print("Error: --fulltext requires local mode (ZOTERO_LOCAL=true).", file=sys.stderr)
                sys.exit(1)
        stats = search.update_database(
            force_full_rebuild=args.force_rebuild,
            limit=args.limit,
            extract_fulltext=fulltext,
            use_openai_batch=getattr(args, "openai_batch", None),
            allow_mass_deletion=getattr(args, "allow_mass_deletion", False),
        )
        _print_update_stats(stats)
        if stats.get("error"):
            print(f"Error: {stats['error']}", file=sys.stderr)
            sys.exit(1)

    elif args.subcommand == "batch-status":
        search = create_semantic_search(str(config_path))
        status = search.get_openai_batch_status(batch_ids=getattr(args, "batch_id", None))
        _print_batch_status(status)

    elif args.subcommand == "batch-import":
        search = create_semantic_search(str(config_path))
        stats = search.import_openai_batch(batch_ids=getattr(args, "batch_id", None))
        _print_batch_import(stats)

    elif args.subcommand == "status":
        search = create_semantic_search(str(config_path))
        status = search.get_database_status()
        ci = status.get("collection_info", {})
        uc = status.get("update_config", {})
        bc = status.get("openai_batch", {})
        print("=== Semantic Search Database Status ===")
        print(f"Collection: {ci.get('name', 'Unknown')}")
        print(f"Document count: {ci.get('count', 0)}")
        print(f"Embedding model: {ci.get('embedding_model', 'Unknown')}")
        print(f"Database path: {ci.get('persist_directory', 'Unknown')}")
        print("\nUpdate configuration:")
        print(f"- Auto update: {uc.get('auto_update', False)}")
        print(f"- Frequency: {uc.get('update_frequency', 'manual')}")
        print(f"- Last update: {uc.get('last_update', 'Never')}")
        print(f"- Should update: {status.get('should_update', False)}")
        print(f"- OpenAI Batch API: {'active' if bc.get('active') else 'inactive'}")
        print(f"- Passage chunking: {_format_chunking_status(status)}")
        if ci.get("error"):
            print(f"\nError: {ci['error']}")

    elif args.subcommand == "inspect":
        from collections import Counter
        search = create_semantic_search(str(config_path))
        client = search.chroma_client
        col = client.collection

        if args.stats:
            meta = col.get(include=["metadatas"])
            metas = meta.get("metadatas", [])
            info = client.get_collection_info()
            print("=== Semantic DB Stats ===")
            print(f"Collection: {info.get('name')} @ {info.get('persist_directory')}")
            print(f"Count: {info.get('count')}")
            ct = Counter((m or {}).get("item_type", "") for m in metas)
            print("Item types:")
            for t, c in ct.most_common(20):
                print(f"  {t or '(missing)'}: {c}")
            coverage: dict = {}
            for m in metas:
                m = m or {}
                t = m.get("item_type", "") or "(missing)"
                cov = coverage.setdefault(t, {"total": 0, "with_fulltext": 0, "pdf": 0, "html": 0})
                cov["total"] += 1
                if m.get("has_fulltext"):
                    cov["with_fulltext"] += 1
                    src = (m.get("fulltext_source") or "").lower()
                    if src == "pdf":
                        cov["pdf"] += 1
                    elif src == "html":
                        cov["html"] += 1
            print("Fulltext coverage (by type):")
            for t, cov in coverage.items():
                print(f"  {t}: {cov['with_fulltext']}/{cov['total']} (pdf:{cov['pdf']}, html:{cov['html']})")
            titles = [(m or {}).get("title", "") for m in metas]
            ct_titles = Counter(t for t in titles if t)
            common = ct_titles.most_common(10)
            if common:
                print("Common titles:")
                for t, c in common:
                    print(f"  {t[:80]}{'...' if len(t) > 80 else ''}: {c}")
        else:
            include = ["metadatas"]
            if args.show_documents:
                include.append("documents")
            data = col.get(limit=args.limit, include=include)
            print("=== Semantic DB Inspection ===")
            print(f"Total documents: {client.get_collection_info().get('count', 0)}")
            print(f"Showing up to: {args.limit}")
            shown = 0
            for i, meta in enumerate(data.get("metadatas", [])):
                meta = meta or {}
                title = meta.get("title", "")
                creators = meta.get("creators", "")
                if args.filter_text:
                    needle = args.filter_text.lower()
                    if needle not in (title or "").lower() and needle not in (creators or "").lower():
                        continue
                print(f"- {title} | {creators}")
                if args.show_documents:
                    doc = (data.get("documents", [""])[i] or "").strip()
                    snippet = doc[:200].replace("\n", " ") + ("..." if len(doc) > 200 else "")
                    if snippet:
                        print(f"  doc: {snippet}")
                shown += 1
                if shown >= args.limit:
                    break
            if shown == 0:
                print("No records matched your filter.")
    else:
        print(f"Unknown 'db' subcommand: {args.subcommand}", file=sys.stderr)
        sys.exit(1)


def cmd_library(args):
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    ctx = _ctx(args)

    if args.action == "switch":
        _out(args, "library switch", text=retrieval.switch_library(
            library_id=args.library_id, library_type=args.library_type, ctx=ctx,
        ))
    elif args.action == "list":
        _out(args, "library list", text=retrieval.list_libraries(ctx=ctx))
    elif args.action == "reset":
        _client.clear_active_library()
        print("Switched back to default library configuration.")
    else:
        print(f"Unknown library action: {args.action}", file=sys.stderr)
        sys.exit(1)


def cmd_outline(args):
    setup_zotero_environment()
    search_mod, retrieval, annotations, write_mod, _client = _import_tools()
    _out(args, "outline", text=write_mod.get_pdf_outline(item_key=args.item_key, ctx=_ctx(args)))


def _split_csv(value):
    """Comma-separated flag value -> list, or None when the flag was absent."""
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


#: The eight colors of Zotero's own annotation palette, by the names its
#: reader uses. Accepting names spares a caller from memorising hex codes and
#: keeps annotations on colors Zotero can filter by.
ZOTERO_COLORS = {
    "yellow": "#ffd400",
    "red": "#ff6666",
    "green": "#5fb236",
    "blue": "#2ea8e5",
    "purple": "#a28ae5",
    "magenta": "#e56eee",
    "orange": "#f19837",
    "gray": "#aaaaaa",
}


def _resolve_color(value):
    """A Zotero color name -> its hex; anything else passes through."""
    if value is None:
        return None
    return ZOTERO_COLORS.get(str(value).strip().lower(), value)


def _parse_rect(value, size=4, flag="--rect"):
    """`x,y,w,h` or `[x, y, w, h]` -> list of four floats (`x,y` -> two for --note).

    Raises CliError on anything else, so a typo is reported as a usage error
    before a PDF is fetched.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = [p for p in str(value).strip().strip("[]").replace(" ", "").split(",") if p]
    try:
        rect = [float(p) for p in parts]
    except (TypeError, ValueError):
        rect = []
    if len(rect) != size:
        shape = "four numbers x,y,width,height" if size == 4 else "two numbers x,y"
        raise _cli_json.CliError(
            f"{flag} must be {shape} (normalized 0-1), got {value!r}",
            code="bad_rect",
        )
    return rect


def _nonneg_int(value: str) -> int:
    """argparse type for --limit and --max-items: a whole number, 0 or more."""
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be a whole number from 0 up, got {value!r}")
    return number


def _parse_pages(value):
    """`all` -> None (every page); `3`, `3-6`, `1,4,6-9` -> sorted page list."""
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
            f"--pages must be all, N, N-M, or a comma list of those, got {value!r}",
            code="bad_pages",
        )
    return sorted(pages)


def cmd_read(args):
    """Read a page range out of an item's PDF, as text or as page images."""
    rect = _parse_rect(getattr(args, "rect", None))
    as_image = getattr(args, "format", "text") == "image"
    as_plain_text = getattr(args, "text", False)
    if as_plain_text and (_json_mode(args) or as_image):
        raise _cli_json.CliError("--text does not combine with --json or --format image",
                                 code="bad_flags")
    if rect is not None and not as_image:
        raise _cli_json.CliError("--rect needs --format image", code="bad_rect")
    setup_zotero_environment()
    from zotero_mcp.tools import read_pdf as read_pdf_mod

    if as_plain_text:
        print(read_pdf_mod.read_pdf_page_texts(
            args.item_key, args.start_page, args.end_page, ctx=_ctx(args),
        ))
        return

    if as_image:
        import os
        import tempfile

        header, pages = read_pdf_mod.render_pdf_pages(
            args.item_key, args.start_page, args.end_page, rect=rect, ctx=_ctx(args),
        )
        out_dir = getattr(args, "out", None) or tempfile.mkdtemp(prefix="zotero_pages_")
        os.makedirs(out_dir, exist_ok=True)
        images = []
        for page in pages:
            suffix = "-region" if rect is not None else ""
            path = os.path.join(out_dir, f"{args.item_key}-p{page['page']}{suffix}.png")
            with open(path, "wb") as handle:
                handle.write(page["png"])
            images.append({"page": page["page"], "path": path,
                           "width": page["width"], "height": page["height"]})
        if _json_mode(args):
            _cli_json.emit("read", {"item_key": args.item_key, "format": "image", "images": images})
        else:
            print(header + "\n")
            for image in images:
                print(f"p{image['page']}: {image['path']} ({image['width']}x{image['height']})")
        return

    text = read_pdf_mod.read_pdf_text(
        args.item_key, args.start_page, args.end_page, ctx=_ctx(args), surface="cli",
    )
    _out(args, "read",
         data={"item_key": args.item_key, "start_page": args.start_page,
               "end_page": args.end_page, "text": text, "chars": len(text)}
         if _json_mode(args) else None,
         text=text)


def cmd_attach(args):
    setup_zotero_environment()
    _s, _r, _a, write_mod, _c = _import_tools()
    _out(args, "attach", text=write_mod.attach_file(
        item_key=args.item_key, file_path=args.file, url=args.url,
        filename=args.filename, ctx=_ctx(args),
    ))


def cmd_delete(args):
    setup_zotero_environment()
    _s, _r, _a, write_mod, _c = _import_tools()
    if args.subcommand == "item":
        _out(args, "delete item", text=write_mod.delete_item(
            item_key=args.item_key, allow_note=args.allow_note, ctx=_ctx(args),
        ))
    elif args.subcommand == "collection":
        _out(args, "delete collection", text=write_mod.delete_collection(
            collection_key=args.collection_key, ctx=_ctx(args),
        ))
    elif args.subcommand == "annotation":
        _s2, _r2, annotations, _w2, _c2 = _import_tools()
        _out(args, "delete annotation", text=annotations.delete_annotation(
            annotation_key=args.annotation_key, ctx=_ctx(args),
        ))
    else:
        _fail(args, "delete", f"Unknown 'delete' subcommand: {args.subcommand}",
              "unknown_subcommand")


def cmd_export(args):
    setup_zotero_environment()
    from zotero_mcp.tools import synthesis as synthesis_mod
    text = synthesis_mod.export_bibliography(
        item_keys=_split_csv(args.item_keys), collection_key=args.collection,
        style=args.style, export_format=args.format, ctx=_ctx(args),
    )
    _out(args, "export",
         data={"style": args.style, "format": args.format, "bibliography": text}
         if _json_mode(args) else None,
         text=text)


def cmd_related(args):
    setup_zotero_environment()
    from zotero_mcp.tools import discovery as discovery_mod
    _out(args, "related", text=discovery_mod.find_related_papers(
        identifier=args.identifier, direction=args.direction,
        limit=args.limit, ctx=_ctx(args),
    ))


def cmd_coverage(args):
    setup_zotero_environment()
    from zotero_mcp.tools import discovery as discovery_mod
    _out(args, "coverage", text=discovery_mod.library_coverage(
        collection_key=args.collection, limit=args.limit, ctx=_ctx(args),
    ))


def cmd_synthesize(args):
    setup_zotero_environment()
    from zotero_mcp.tools import synthesis as synthesis_mod
    json_mode = _json_mode(args)
    fmt = "json" if (json_mode and args.format == "markdown") else args.format
    result = synthesis_mod.synthesize_annotations(
        collection_key=args.collection, tag=_split_csv(args.tag),
        limit=args.limit, format=fmt, ctx=_ctx(args),
    )
    if json_mode:
        try:
            _cli_json.emit("synthesize", json.loads(result))
        except ValueError:
            _out(args, "synthesize", text=result)
        return
    print(result)


def cmd_path(args):
    """Where an item's attachment actually lives on disk."""
    setup_zotero_environment()
    _s, retrieval, _a, _w, _c = _import_tools()
    text = retrieval.get_attachment_path(item_key=args.item_key, ctx=_ctx(args))
    _out(args, "path",
         data={"item_key": args.item_key, "text": text} if _json_mode(args) else None,
         text=text)


def cmd_batch(args):
    setup_zotero_environment()
    _s, _r, _a, write_mod, _c = _import_tools()
    set_keys = None
    if args.set:
        try:
            set_keys = json.loads(args.set)
        except json.JSONDecodeError as e:
            _fail(args, "batch", f"invalid JSON in --set: {e}", "bad_json")
    _out(args, "batch", text=write_mod.batch_update(
        item_keys=_split_csv(args.item_keys), query=args.query or "",
        tag=_split_csv(args.tag), add_tags=_split_csv(args.add_tags),
        remove_tags=_split_csv(args.remove_tags), set_keys=set_keys,
        remove_keys=_split_csv(args.remove_keys), limit=args.limit, ctx=_ctx(args),
    ))


def _fail(args, command: str, message: str, code: str = "error"):
    """Report a usage error in whichever shape the caller asked for, then exit."""
    if _json_mode(args):
        _cli_json.emit_error(command, message, code=code)
    else:
        print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Zotero CLI — standalone library access without an MCP server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  zotero-cli search "Smith 2020"\n'
            '  zotero-cli search --mode tag "important,research"\n'
            '  zotero-cli search --mode semantic "machine learning"\n'
            "  zotero-cli get metadata ITEM_KEY\n"
            "  zotero-cli get collections\n"
            "  zotero-cli get recent --limit 20\n"
            "  zotero-cli annotations list --item-key ITEM_KEY\n"
            "  zotero-cli notes create --item-key ITEM_KEY --text -\n"
            "  zotero-cli add doi 10.1234/example\n"
            "  zotero-cli edit ITEM_KEY --title \"New Title\" --add-tags reviewed\n"
            "  zotero-cli db status\n"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show verbose output")
    parser.add_argument(
        "--json", action="store_true", dest="json_out",
        help="Emit a JSON envelope on stdout instead of markdown "
             "(see `zotero-cli --json-schema` for the shape)",
    )
    parser.add_argument(
        "--json-schema", action="store_true", dest="json_schema",
        help="Print the --json envelope contract and exit",
    )

    sub = parser.add_subparsers(dest="command", help="Command to run")
    # Every subcommand accepts the global flags after its own name too, so
    # both `zotero-cli --json search x` and `zotero-cli search --json x` work.
    # SUPPRESS is load-bearing: without it each subparser would write its own
    # default into the namespace and silently undo a flag given before the
    # subcommand name.
    _sub_add_parser = sub.add_parser

    def add_parser(*args, **kwargs):
        p = _sub_add_parser(*args, **kwargs)
        p.add_argument("-v", "--verbose", action="store_true",
                       default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        p.add_argument("--json", action="store_true", dest="json_out",
                       default=argparse.SUPPRESS,
                       help="Emit a JSON envelope instead of markdown")
        return p

    sub.add_parser = add_parser

    # config
    cfg_p = sub.add_parser("config", help="Show current Zotero configuration")
    cfg_p.add_argument("--show-secrets", action="store_true", help="Show full API keys")

    # search
    s_p = sub.add_parser("search", help="Search your Zotero library", aliases=["s"])
    s_p.add_argument("query", nargs="?", default="", help="Search query")
    s_p.add_argument("--mode", choices=["items", "tag", "citekey", "advanced", "semantic", "notes"],
                     default="items", help="Search mode (default: items)")
    s_p.add_argument("--qmode", choices=["titleCreatorYear", "everything"],
                     default="titleCreatorYear")
    s_p.add_argument("--collection", help="Scope to a collection key")
    s_p.add_argument("--limit", type=int, default=10)
    s_p.add_argument("--conditions", help='JSON conditions for advanced mode')
    s_p.add_argument("--join-mode", choices=["all", "any"], default="all")
    s_p.add_argument("--sort-by")
    s_p.add_argument("--sort-direction", choices=["asc", "desc"], default="asc")
    s_p.add_argument("--filters", help='JSON filters for semantic mode')
    s_p.add_argument("--all-libraries", action="store_true",
                     help="Search every accessible library at once instead of "
                          "the active one, labelling each result with its "
                          "library (items, advanced and semantic modes). "
                          "Requires the SQLite backend (the default in local mode).")
    s_p.add_argument("--detail", choices=["keys_only", "summary", "full"],
                     default="summary",
                     help="How much of each item --json returns (no effect on "
                          "markdown output)")

    # get
    g_p = sub.add_parser("get", help="Get items, collections, tags, etc.", aliases=["g"])
    g_sub = g_p.add_subparsers(dest="subcommand")
    gm = g_sub.add_parser("metadata", help="Get item metadata")
    gm.add_argument("item_key")
    gm.add_argument("--no-abstract", action="store_true")
    gm.add_argument("--output-format", choices=["markdown", "bibtex"], default="markdown")
    gf = g_sub.add_parser("fulltext", help="Get full text of an item")
    gf.add_argument("item_key")
    gb = g_sub.add_parser("bibtex", help="Get BibTeX for an item")
    gb.add_argument("item_key")
    gc = g_sub.add_parser("collections", help="List all collections")
    gc.add_argument("--limit", type=int, default=500)
    gci = g_sub.add_parser("collection-items", help="Get items in a collection")
    gci.add_argument("collection_key")
    gci.add_argument("--detail", choices=["keys_only", "summary", "full"], default="summary")
    gci.add_argument("--limit", type=int, default=50)
    gci.add_argument("--offset", type=int, default=0,
                     help="Index of the first item to return, for paging a "
                          "collection larger than --limit")
    gch = g_sub.add_parser("children", help="Get child items (attachments, notes)")
    gch.add_argument("item_key", nargs="?")
    gch.add_argument("--item-keys", help="Comma-separated keys for batch mode")
    gt = g_sub.add_parser("tags", help="List all tags")
    gt.add_argument("--limit", type=int, default=500)
    gr = g_sub.add_parser("recent", help="Get recently added items")
    gr.add_argument("--limit", type=int, default=10)
    gr.add_argument("--collection")
    g_sub.add_parser("libraries", help="List accessible libraries")
    g_sub.add_parser("feeds", help="List RSS feeds")
    gfi = g_sub.add_parser("feed-items", help="Get items from an RSS feed")
    gfi.add_argument("library_id", type=int)
    gfi.add_argument("--limit", type=int, default=20)

    # annotations
    a_p = sub.add_parser("annotations", help="Manage annotations", aliases=["ann"])
    a_sub = a_p.add_subparsers(dest="subcommand")
    al = a_sub.add_parser("list", help="Get annotations")
    al.add_argument("--item-key")
    al.add_argument("--pdf-extraction", action="store_true")
    al.add_argument("--limit", type=int, default=100)
    al.add_argument("--format", choices=["markdown", "json"], default="markdown")
    au = a_sub.add_parser("update", help="Update an existing annotation")
    au.add_argument("annotation_key")
    au.add_argument("--text")
    au.add_argument("--comment")
    au.add_argument("--color")
    au.add_argument("--add-tags", help="Comma-separated tags to add")
    au.add_argument("--remove-tags", help="Comma-separated tags to remove")
    ad = a_sub.add_parser("delete", help="Delete an annotation")
    ad.add_argument("annotation_key")
    ac = a_sub.add_parser("create", help="Create a highlight (--text), an area box (--rect) "
                                         "or a sticky note (--note)")
    ac.add_argument("--attachment-key", required=True)
    ac.add_argument("--page", required=True, type=int)
    ac.add_argument("--text", help="Exact text to highlight")
    ac.add_argument("--rect", help="Area box x,y,width,height, normalized 0-1; "
                                   "`zotero-cli layout` prints boxes for figures and tables")
    ac.add_argument("--note", help="Sticky note centered at x,y, normalized 0-1; "
                                   "its text is --comment")
    ac.add_argument("--comment")
    ac.add_argument("--color", default="#ffd400",
                    help=f"Hex, or a Zotero color name: {', '.join(ZOTERO_COLORS)}")
    ac.add_argument("--tags", help="Comma-separated tags")
    ab = a_sub.add_parser("batch", help="Create many annotations from JSON Lines in one run")
    ab.add_argument("--attachment-key", required=True,
                    help="Attachment for lines that do not name their own")
    ab.add_argument("--file", default="-",
                    help="JSON Lines (or a JSON array) of {page, text|rect|note, comment, color, tags}; "
                         "- reads stdin")
    ab.add_argument("--dry-run", action="store_true",
                    help="Locate every highlight and report what it would cover, without writing")

    # layout -- figure/table boxes to aim area annotations at
    ly_p = sub.add_parser("layout", help="Find figure and table boxes on PDF pages")
    ly_p.add_argument("attachment_key")
    ly_p.add_argument("--pages", default="all",
                      help="Pages to scan: all (default), 3, 3-6, or 1,4,6-9")

    # grep / sections / index -- page-anchored search and the source index note
    gr_p = sub.add_parser("grep", help="Count and locate terms in a PDF, page by page")
    gr_p.add_argument("key", help="Item key or PDF attachment key")
    gr_p.add_argument("terms", nargs="+", metavar="TERM",
                      help="Terms to find; each is counted on its own")
    gr_p.add_argument("--regex", action="store_true", help="Treat each TERM as a regex")
    gr_p.add_argument("--word", action="store_true", help="Match whole words only")
    gr_p.add_argument("--pages", default="all",
                      help="Pages to search: all (default), 3, 3-6, or 1,4,6-9")
    gr_p.add_argument("--context", type=int, default=300,
                      help="Characters of context on each side of a match")
    gr_p.add_argument("--max-hits", type=int, default=200,
                      help="Cap on snippets returned; counts always cover every hit")
    gr_p.add_argument("--order", choices=["page", "score"], default="page",
                      help="Order pages by number, or by hit density")
    gr_p.add_argument("--no-cache", action="store_true", help="Do not use the page-text cache")
    gr_p.add_argument("--jobs", type=int, help="Worker processes for text extraction")
    gr_p.add_argument("--text", action="store_true",
                      help="Print one 'pN: snippet' line per hit instead of markdown. "
                           "Does not combine with --json")

    sc_p = sub.add_parser("sections", help="Split a PDF into sections for reading in parts")
    sc_p.add_argument("key", help="Item key or PDF attachment key")
    sc_p.add_argument("--pages", default="all",
                      help="Pages to cover: all (default), 3, 3-6, or 1,4,6-9")
    sc_p.add_argument("--max-level", type=int, default=2,
                      help="Deepest outline level that starts a section")
    sc_p.add_argument("--chunk-pages", type=int, default=8,
                      help="Longest section in pages; also the chunk size with no outline")
    sc_p.add_argument("--inventory", action="store_true",
                      help="List the tables, figures and equations in each section")

    tb_p = sub.add_parser("tables", help="Read the cells of the tables on PDF pages")
    tb_p.add_argument("key", help="Item key or PDF attachment key")
    tb_p.add_argument("--pages", required=True,
                      help="Pages to read: all, 3, 3-6, or 1,4,6-9")
    tb_p.add_argument("--strategy", choices=["lines", "text"], default="lines",
                      help="lines finds ruled tables; text finds tables under a "
                           "'Table N' caption that have no ruled cells")

    ix_p = sub.add_parser("index", help="Store, read or search source index notes")
    ix_sub = ix_p.add_subparsers(dest="subcommand", required=True)
    ixp = ix_sub.add_parser("push", help="Write an index JSON file into the item's notes")
    ixp.add_argument("key", help="Item key or PDF attachment key")
    ixp.add_argument("--from", dest="from_file", required=True, metavar="FILE",
                     help="Index JSON file; - reads stdin")
    ixp.add_argument("--replace", action="store_true",
                     help="Replace the item's existing index instead of failing")
    ixp.add_argument("--tags", help="Comma-separated tags to add to the parent item")
    ixp.add_argument("--dry-run", action="store_true",
                     help="Report what would be written, without writing")
    ixs = ix_sub.add_parser("show", help="Read an item's index back from its notes")
    ixs.add_argument("key", help="Item key or PDF attachment key")
    ixs.add_argument("--section", help="Only this section (S03) and its entries")
    ixs.add_argument("--pages",
                     help="Only entries on these pages: 3, 3-6, or 1,4,6-9 (with --section, both apply)")
    # The filter flags default to None, so that the command sees what was given:
    # any of --grep, --fields and --limit turns the filtered view on.
    ixs.add_argument("--grep", action="append", metavar="TERM",
                     help="Only entries that match TERM[,TERM]. Repeat the flag to add terms. Any term matches.")
    ixs.add_argument("--regex", action="store_true", help="Treat each --grep value as one regex")
    ixs.add_argument("--fields", choices=["lead", "full"],
                     help="lead keeps a few short fields per entry, full the whole record (default full)")
    ixs.add_argument("--expand", action="store_true",
                     help="Also match the variants and symbols of the vocabulary entries that match a --grep term")
    ixs.add_argument("--limit", type=_nonneg_int,
                     help="Most entries of each kind to return, best matches first. "
                          "Default 40 with --grep or --fields. 0 means no cap.")
    ixq = ix_sub.add_parser("search", help="Find terms in the indexes of many items at once")
    ixq.add_argument("terms", nargs="+", metavar="TERM",
                     help="Terms to find. Each value splits on commas, so quote a term that has spaces.")
    ixq.add_argument("--items", metavar="K,K",
                     help="Item or PDF attachment keys to search, instead of every item with --tag")
    ixq.add_argument("--tag", default="status/indexed,status/index-failed-gate",
                     help="Search every item that has this tag, or a comma-separated list of "
                          "tags (ignored with --items). An index that failed the audit gate "
                          "still holds facts, so recall searches both tags by default.")
    ixq.add_argument("--fields", choices=["lead", "full"], default="lead",
                     help="lead keeps a few short fields per entry, full the whole record")
    ixq.add_argument("--expand", action="store_true",
                     help="Also match the variants and symbols of the vocabulary entries that match a TERM")
    ixq.add_argument("--limit", type=_nonneg_int, default=10,
                     help="Most entries of each kind to return for each item. 0 means no cap.")
    ixq.add_argument("--max-items", type=_nonneg_int, default=10,
                     help="Most items with hits to return, most facts first. 0 means no cap.")
    ixq.add_argument("--regex", action="store_true", help="Treat each TERM as one regex")
    ixc = ix_sub.add_parser("cards", help="List every item's source card, or push one")
    ixc.add_argument("--grep", action="append", metavar="TERM",
                     help="Only cards that match TERM[,TERM] in any field. Repeat the flag to add terms.")
    ixc.add_argument("--regex", action="store_true", help="Treat each --grep value as one regex")
    ixc.add_argument("--expand", action="store_true",
                     help="Print the full card instead of the one-line compact form")
    ixc_sub = ixc.add_subparsers(dest="cards_subcommand")
    ixcp = ixc_sub.add_parser("push", help="Write or replace an item's source card")
    ixcp.add_argument("key", help="Item key or PDF attachment key")
    ixcp.add_argument("--from", dest="from_file", default="-", metavar="FILE",
                      help="Card JSON file. Use - to read stdin, the default.")
    # A leaf parser is not wrapped by add_parser above, so it gets --json here.
    # SUPPRESS keeps a `--json` given before the command from being undone.
    for leaf in (ixp, ixs, ixq, ixc, ixcp):
        leaf.add_argument("--json", action="store_true", dest="json_out",
                          default=argparse.SUPPRESS,
                          help="Emit a JSON envelope instead of markdown")

    # notes
    n_p = sub.add_parser("notes", help="Manage notes", aliases=["n"])
    n_sub = n_p.add_subparsers(dest="subcommand")
    nl = n_sub.add_parser("list", help="List notes")
    nl.add_argument("--item-key")
    nl.add_argument("--limit", type=int, default=20)
    nl.add_argument("--full", action="store_true")
    nl.add_argument("--raw-html", action="store_true")
    nc = n_sub.add_parser("create", help="Create a note")
    nc.add_argument("--item-key", required=True)
    nc.add_argument("--title")
    nc.add_argument("--text", help="Note text (use - to read from stdin)")
    nc.add_argument("--tags")
    nu = n_sub.add_parser("update", help="Update a note")
    nu.add_argument("--item-key", required=True)
    nu.add_argument("--text", help="New text (use - for stdin)")
    nd = n_sub.add_parser("delete", help="Delete a note")
    nd.add_argument("--item-key", required=True)

    # add
    add_p = sub.add_parser("add", help="Add items to your library")
    add_sub = add_p.add_subparsers(dest="subcommand")

    def _add_common_flags(p):
        """Collection/idempotency flags shared by every `add` subcommand."""
        p.add_argument("--collections",
                       help="Comma-separated collection keys, names, or paths")
        p.add_argument("-c", "--collection", action="append", metavar="SPEC",
                       help="Collection key, name, or parent/child path "
                            "(repeatable; not comma-split, so names with "
                            "commas work)")
        p.add_argument("--tags", help="Comma-separated tags")
        p.add_argument("--if-exists", dest="if_exists",
                       choices=["file", "skip", "duplicate"], default="file",
                       help="When the item already exists: 'file' (default) "
                            "reuses it and adds missing collections/tags; "
                            "'skip' leaves it untouched; 'duplicate' creates "
                            "a new item anyway")
        p.add_argument("--create-collections", dest="create_collections",
                       action="store_true",
                       help="Create collections that don't exist yet "
                            "(including parent/child paths)")

    adoi = add_sub.add_parser("doi", help="Add item by DOI")
    adoi.add_argument("doi")
    _add_common_flags(adoi)
    adoi.add_argument("--attach-mode", choices=["auto", "linked_url", "import_file", "none", "required"], default="auto")
    aurl = add_sub.add_parser("url", help="Add item by URL")
    aurl.add_argument("url")
    _add_common_flags(aurl)
    aurl.add_argument("--attach-mode", choices=["auto", "linked_url", "import_file", "none", "required"], default="auto")
    afil = add_sub.add_parser("file", help="Add item from local file (.pdf/.epub)")
    afil.add_argument("--filepath", required=True)
    afil.add_argument("--title", help="Override title if metadata extraction misses")
    afil.add_argument("--item-type", default="document",
                      help="Zotero item type for the new item (default: document)")
    _add_common_flags(afil)
    aisbn = add_sub.add_parser("isbn", help="Add book by ISBN")
    aisbn.add_argument("isbn")
    _add_common_flags(aisbn)
    abib = add_sub.add_parser("bibtex", help="Add items from BibTeX")
    abib.add_argument("--bibtex",
                      help="Inline BibTeX (use - to read from stdin)")
    abib.add_argument("--file", help="Path to a .bib/.bibtex file")
    _add_common_flags(abib)
    abib.add_argument("--attach-mode", choices=["auto", "linked_url", "import_file", "none", "required"],
                      default="auto")
    acsl = add_sub.add_parser("csl-json", help="Add items from CSL JSON")
    acsl.add_argument("--json", dest="json",
                      help="Inline CSL JSON (use - to read from stdin)")
    acsl.add_argument("--file", help="Path to a .json/.csljson file")
    _add_common_flags(acsl)
    acsl.add_argument("--attach-mode", choices=["auto", "linked_url", "import_file", "none", "required"],
                      default="auto")

    # collections
    col_p = sub.add_parser("collections", help="Manage collections", aliases=["coll"])
    col_sub = col_p.add_subparsers(dest="subcommand")
    ccs = col_sub.add_parser("create", help="Create a collection")
    ccs.add_argument("name")
    ccs.add_argument("--parent")
    cus = col_sub.add_parser("update", help="Rename a collection or move it under another parent")
    cus.add_argument("collection_key")
    cus.add_argument("--name", help="New name")
    cus.add_argument("--parent", help="Key or name of the new parent collection")
    cus.add_argument("--top-level", action="store_true", help="Move out of any parent collection")
    css = col_sub.add_parser("search", help="Search collections by name")
    css.add_argument("query")
    cmg = col_sub.add_parser("manage", help="Add/remove items from collections")
    cmg.add_argument("--item-keys", required=True)
    cmg.add_argument("--add-to")
    cmg.add_argument("--remove-from")

    # tags
    t_p = sub.add_parser("tags", help="Batch update tags on matched items")
    t_p.add_argument("--query")
    t_p.add_argument("--tag")
    t_p.add_argument("--add")
    t_p.add_argument("--remove")
    t_p.add_argument("--limit", type=int, default=50)

    # edit (item metadata)
    e_p = sub.add_parser("edit", help="Edit metadata fields of an existing item")
    e_p.add_argument("item_key")
    e_p.add_argument("--title")
    e_p.add_argument("--creators", help="JSON array of creators")
    e_p.add_argument("--date")
    e_p.add_argument("--publication-title")
    e_p.add_argument("--abstract")
    e_p.add_argument("--tags", help="Replace all tags (comma-separated)")
    e_p.add_argument("--add-tags")
    e_p.add_argument("--remove-tags")
    e_p.add_argument("--collections", help="Add to collections (comma-separated keys)")
    e_p.add_argument("--collection-names", help="Add to collections (comma-separated names)")
    e_p.add_argument("--doi")
    e_p.add_argument("--url")
    e_p.add_argument("--extra")
    e_p.add_argument("--volume")
    e_p.add_argument("--issue")
    e_p.add_argument("--pages")
    e_p.add_argument("--publisher")
    e_p.add_argument("--issn")
    e_p.add_argument("--language")
    e_p.add_argument("--short-title")
    e_p.add_argument("--edition")
    e_p.add_argument("--isbn")
    e_p.add_argument("--book-title")

    # duplicates
    d_p = sub.add_parser("duplicates", help="Find or merge duplicate items")
    d_sub = d_p.add_subparsers(dest="subcommand")
    df = d_sub.add_parser("find")
    df.add_argument("--method", choices=["title", "doi", "both"], default="both")
    df.add_argument("--collection")
    df.add_argument("--limit", type=int, default=50)
    dm = d_sub.add_parser("merge")
    dm.add_argument("--keeper-key", required=True)
    dm.add_argument("--duplicate-keys", required=True)
    dm.add_argument("--dry-run", action="store_true")

    # db
    db_p = sub.add_parser("db", help="Manage the semantic search database")
    db_sub = db_p.add_subparsers(dest="subcommand")
    dbu = db_sub.add_parser("update")
    dbu.add_argument("--force-rebuild", action="store_true")
    dbu.add_argument("--limit", type=int)
    dbu.add_argument("--fulltext", action="store_true")
    dbu.add_argument("--allow-mass-deletion", action="store_true")
    dbu.add_argument("--config-path")
    dbu.add_argument("--db-path")
    dbu_batch = dbu.add_mutually_exclusive_group()
    dbu_batch.add_argument("--openai-batch", dest="openai_batch", action="store_true")
    dbu_batch.add_argument("--no-openai-batch", dest="openai_batch", action="store_false")
    dbu.set_defaults(openai_batch=None)
    dbbs = db_sub.add_parser("batch-status")
    dbbs.add_argument("--batch-id", action="append")
    dbbs.add_argument("--config-path")
    dbbi = db_sub.add_parser("batch-import")
    dbbi.add_argument("--batch-id", action="append")
    dbbi.add_argument("--config-path")
    dbs = db_sub.add_parser("status")
    dbs.add_argument("--config-path")
    dbi = db_sub.add_parser("inspect")
    dbi.add_argument("--limit", type=int, default=20)
    dbi.add_argument("--filter-text")
    dbi.add_argument("--show-documents", action="store_true")
    dbi.add_argument("--stats", action="store_true")
    dbi.add_argument("--config-path")

    # library
    lib_p = sub.add_parser("library", help="Switch or list libraries")
    lib_p.add_argument("action", choices=["switch", "list", "reset"], nargs="?", default="list")
    lib_p.add_argument("--library-id")
    lib_p.add_argument("--library-type", choices=["user", "group"], default="group")

    # outline
    out_p = sub.add_parser("outline", help="Get PDF outline/table of contents")
    out_p.add_argument("item_key")

    # read -- page ranges out of an item's PDF
    rd_p = sub.add_parser("read", help="Read a page range from an item's PDF")
    rd_p.add_argument("item_key")
    rd_p.add_argument("--start-page", type=int, required=True)
    rd_p.add_argument("--end-page", type=int, default=None,
                      help="Defaults to --start-page (a single page)")
    rd_p.add_argument("--format", choices=["text", "image"], default="text",
                      help="image writes PNG page images (up to 10 pages) for math, figures and tables")
    rd_p.add_argument("--rect", help="With --format image: crop the start page to x,y,width,height "
                                     "(normalized 0-1), e.g. from `zotero-cli layout`")
    rd_p.add_argument("--out", help="With --format image: directory for the PNG files "
                                    "(default: a new temporary directory)")
    rd_p.add_argument("--text", action="store_true",
                      help="Print plain page text with '--- page N ---' separators, "
                           "no title header. Needs --format text. Does not combine with --json")

    # attach
    at_p = sub.add_parser("attach", help="Attach a file or link a URL to an item")
    at_p.add_argument("item_key")
    at_p.add_argument("--file", help="Path to a local file to upload")
    at_p.add_argument("--url", help="URL to attach as a link")
    at_p.add_argument("--filename", help="Override the stored filename")

    # delete
    del_p = sub.add_parser("delete", help="Delete an item, collection or annotation")
    del_sub = del_p.add_subparsers(dest="subcommand")
    di = del_sub.add_parser("item")
    di.add_argument("item_key")
    di.add_argument("--allow-note", action="store_true",
                    help="Permit deleting a note (refused otherwise, since a "
                         "note is usually deleted by mistake)")
    dc = del_sub.add_parser("collection")
    dc.add_argument("collection_key")
    da = del_sub.add_parser("annotation")
    da.add_argument("annotation_key")

    # export
    ex_p = sub.add_parser("export", help="Export a bibliography")
    ex_p.add_argument("--item-keys", help="Comma-separated item keys")
    ex_p.add_argument("--collection", help="Export a whole collection instead")
    ex_p.add_argument("--style", default="apa", help="CSL style (default: apa)")
    ex_p.add_argument("--format", choices=["bib", "citation", "bibtex"], default="bib")

    # related
    rel_p = sub.add_parser("related", help="Find references/citations for a paper")
    rel_p.add_argument("identifier", help="DOI, arXiv ID, or Zotero item key")
    rel_p.add_argument("--direction", choices=["references", "citations", "both"],
                       default="both")
    rel_p.add_argument("--limit", type=int, default=20)

    # coverage
    cov_p = sub.add_parser("coverage", help="Summarise a library or collection")
    cov_p.add_argument("--collection", help="Scope to one collection")
    cov_p.add_argument("--limit", type=int, default=200)

    # synthesize
    syn_p = sub.add_parser("synthesize", help="Synthesise annotations across items")
    syn_p.add_argument("--collection")
    syn_p.add_argument("--tag", help="Comma-separated tags to scope by")
    syn_p.add_argument("--limit", type=int, default=200)
    syn_p.add_argument("--format", choices=["markdown", "json"], default="markdown")

    # path
    pth_p = sub.add_parser("path", help="Show an attachment's path on disk")
    pth_p.add_argument("item_key")

    # batch
    b_p = sub.add_parser("batch", help="Update tags/Extra fields across many items")
    b_p.add_argument("--item-keys", help="Comma-separated item keys")
    b_p.add_argument("--query", help="Select items by search query instead")
    b_p.add_argument("--tag", help="Comma-separated tags to select by")
    b_p.add_argument("--add-tags", help="Comma-separated tags to add")
    b_p.add_argument("--remove-tags", help="Comma-separated tags to remove")
    b_p.add_argument("--set", help="JSON object of Extra keys to set")
    b_p.add_argument("--remove-keys", help="Comma-separated Extra keys to remove")
    b_p.add_argument("--limit", type=int, default=50)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_CMD_MAP = {
    "config": cmd_config,
    "search": cmd_search, "s": cmd_search,
    "get": cmd_get, "g": cmd_get,
    "annotations": cmd_annotations, "ann": cmd_annotations,
    "notes": cmd_notes, "n": cmd_notes,
    "add": cmd_add,
    "collections": cmd_collections, "coll": cmd_collections,
    "tags": cmd_tags,
    "edit": cmd_edit,
    "duplicates": cmd_duplicates,
    "db": cmd_db,
    "library": cmd_library,
    "outline": cmd_outline,
    "layout": cmd_layout,
    "grep": cmd_grep,
    "sections": cmd_sections,
    "tables": cmd_tables,
    "index": cmd_index,
    "read": cmd_read,
    "attach": cmd_attach,
    "delete": cmd_delete,
    "export": cmd_export,
    "related": cmd_related,
    "coverage": cmd_coverage,
    "synthesize": cmd_synthesize,
    "path": cmd_path,
    "batch": cmd_batch,
}


JSON_SCHEMA_DOC = """zotero-cli --json output contract
=====================================

Every --json invocation prints exactly one JSON object on stdout:

  success:  {"ok": true,  "command": "<name>", "schema": 1, "data": {...}}
  failure:  {"ok": false, "command": "<name>", "schema": 1,
             "error": {"message": "...", "code": "<code>"}}

Failures go to stdout too, so a caller reading one stream sees both
outcomes. Diagnostics ([INFO]/[WARN]/[ERROR]) stay on stderr. The exit
code is 0 on success and non-zero on failure, so `ok` and the exit code
never disagree.

Commands returning structured data
----------------------------------
  search                data.items[]  -- item projections, in rank order
  get metadata          the raw Zotero item record
  get collection-items  data.items[], data.offset, data.count
  get children          data.items[]
  get recent            data.items[]
  get collections       data.collections[]
  get tags              data.tags[]
  get fulltext          data.text, data.chars
  get bibtex            data.bibtex
  annotations list      the annotations payload
  notes list            data.notes[] -- with both .text and .html
  config                data.settings
  grep                  data.counts{}, data.total_hits, data.pages[] -- per-page
                        hits and snippets, each match marked [[ ]]
  sections              data.sections[], data.source, data.scope
  tables                data.pages[].tables[] -- header, rows, caption, rect_arg
  index push            data.note_keys, data.parts, data.counts
  index show            data.index -- the parsed index, or one section of it
                        with --grep, --fields or --limit also data.filter{} -- terms, matched{}, truncated{}
  index search          data.items[] -- hits per indexed item, most facts first; data.no_hits[], data.skipped[]
  index cards           data.cards[] -- compact records (full card plus note_key with
                        --expand), data.count, data.terms
  index cards push      data.item_key, data.note_key, data.created, data.updated, data.trashed

grep, sections, tables and index also fail with a code a caller can branch on:
no_pdf_attachment, bad_regex, bad_pages, no_index, index_exists, invalid_index,
invalid_card, bad_grep. Index search lists an item that it cannot read in data.skipped[],
with the code no_index, invalid_index or error.

Every other command returns {"text": "<the markdown it would have
printed>"}. That is deliberate: those commands' answers really are status
lines, and inventing fields by parsing prose would be less reliable than
handing the prose over intact.

Item projection (--detail keys_only | summary | full)
-----------------------------------------------------
  keys_only  key, itemType, title, date  (+ deleted when trashed)
  summary    the above + creators[], doi, publication, url, tags[],
             collections[]
  full       the above + abstract, raw (the complete Zotero data dict)

`title` resolves type-specific base fields (a statute's nameOfAct, a
note's first line), so it is never the literal "Untitled" for an item
that has a name somewhere.

Compatibility
-------------
`schema` is 1. New fields may be added to `data` at any time; existing
fields are not removed or retyped without bumping it. Parse defensively.
"""


def _keep_pymupdf_off_stdout() -> None:
    """Send PyMuPDF's own messages to stderr.

    PyMuPDF reports through print(). The `import fitz` deprecation warning,
    for one, lands on stdout the first time a tool imports `fitz`, so the
    output of `--json read` and `--json layout` began with a line that is not
    JSON. `set_messages` reroutes those messages to stderr, where the warning
    is noise but no longer breaks a pipe. A PyMuPDF too old to have
    `set_messages` is left as it is.
    """
    try:
        import pymupdf

        pymupdf.set_messages(fd=2)
    except Exception:
        pass


def main():
    parser = build_parser()
    args = parser.parse_args()

    if getattr(args, "json_schema", False):
        print(JSON_SCHEMA_DOC)
        sys.exit(0)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    handler = _CMD_MAP.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    if args.command != "index":
        # The index commands never import pymupdf. The skip saves 0.16 s a call.
        _keep_pymupdf_off_stdout()
    try:
        handler(args)
    except KeyboardInterrupt:
        if _json_mode(args):
            _cli_json.emit_error(args.command, "Interrupted", code="interrupted")
        else:
            print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        # In JSON mode the failure has to arrive in the same shape as a
        # success, or a caller has to parse stderr to find out what happened.
        if _json_mode(args):
            _cli_json.emit_error(
                args.command, str(e),
                code=getattr(e, "code", None) or type(e).__name__,
            )
        else:
            print(f"Error: {e}", file=sys.stderr)
        import os
        if os.environ.get("ZOTERO_CLI_DEBUG"):
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
