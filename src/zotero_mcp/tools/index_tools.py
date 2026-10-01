"""MCP tools for the source index: a safe path to `index show` and `index search`.

The CLI gives a shell-level caller direct access to `index_query.show`/`search`. An MCP
client has no shell, and `zotero_index_show` with neither `grep` nor `pages` would pull a
whole index note in one call -- for a long source that can run past what a client wants in
context. Both tools here refuse that bare call before any read, and clamp `limit` to a hard
cap, instead of leaving either choice to the caller.

Both tools return the same `cli_json.envelope` shape the CLI's `--json` output uses, so a
caller written against one works against the other. A `CliError` from `index_query` becomes
a failure envelope with its code. It is never raised, because a raised `ToolError` carries
FastMCP's own shape, not this one.
"""

from __future__ import annotations

from zotero_mcp import cli_json as _cli_json
from zotero_mcp import index_query
from zotero_mcp._app import mcp
from zotero_mcp._context import Context
from zotero_mcp.client import with_zotero_api_lock
from zotero_mcp.tools import _helpers

#: The hard cap on `limit` for either tool. The CLI's own default for a filtered
#: `index show` is 40 entries per kind. Ten items at that limit through `index search`
#: is already 400 leads, more than an MCP caller should pull in one call. A request of
#: 0 or over this cap becomes this cap, and the envelope then records what was asked for.
LIMIT_CAP = 80


def _clamp_limit(value, default: int) -> tuple[int, int | None]:
    """Coerce *value* to a bound int, returning `(limit, requested)`.

    `requested` is `None` when *value* already fit, so a call site can add
    `limit_clamped_from` to its result only when clamping changed the request.
    """
    if value is None:
        return default, None
    try:
        requested = int(value)
    except (TypeError, ValueError):
        return default, None
    if requested <= 0 or requested > LIMIT_CAP:
        return LIMIT_CAP, requested
    return requested, None


def _as_count(value, default: int) -> int:
    """Coerce *value* to a non-negative int, falling back to *default* on anything else."""
    if value is None:
        return default
    try:
        count = int(value)
    except (TypeError, ValueError):
        return default
    return count if count >= 0 else default


@mcp.tool(
    name="zotero_index_show",
    description=(
        "Read a source index back from an item's notes, filtered to a grep term or a "
        "page range so the result stays small. This is the safe path to the index: a "
        "bare call with neither grep nor pages is refused before any read. "
        "item_key: an 8-character Zotero item key or PDF attachment key. "
        "grep: one or more terms to match (any term matches). At least one of grep or "
        "pages is required, else the call returns the bad_grep error and reads nothing. "
        "pages: a page range such as '3-6', '1,4,9', or 'all'. "
        "section: only this section id (for example 'S03'). Combines with pages. "
        "regex: treat each grep term as one regular expression (needs grep). "
        "expand: also match the vocabulary variants and symbols of a term that matches "
        "a vocabulary entry (needs grep). "
        "fields: 'lead' (default, a few short fields per entry) or 'full' (the whole "
        "record). "
        "limit: entries per kind, default 40, hard capped at 80 -- 0 or over 80 becomes "
        "80, and the result then carries limit_clamped_from with what was asked for. "
        "Returns the same JSON envelope as `zotero-cli --json index show`. "
        "Example: zotero_index_show(item_key='3CKPN9EK', grep=['leakage'])."
    ),
)
@with_zotero_api_lock
def index_show(
    item_key: str,
    grep: list[str] | str | None = None,
    pages: str | None = None,
    section: str | None = None,
    regex: bool = False,
    expand: bool = False,
    fields: str = "lead",
    limit: int | str | None = 40,
    *,
    ctx: Context,
) -> dict:
    """The MCP path to `index show`: no bare read, a hard cap on `limit`."""
    grep = _helpers._normalize_str_list_input(grep, "grep") or None
    if not grep and not pages:
        return _cli_json.envelope(
            "index show", ok=False,
            error="give at least one of grep or pages", code="bad_grep",
        )
    clamped, requested = _clamp_limit(limit, 40)
    try:
        data = index_query.show(
            item_key, section=section, pages=pages, grep=grep, regex=regex,
            expand=expand, fields=fields or "lead", limit=clamped, ctx=ctx,
        )
    except _cli_json.CliError as exc:
        return _cli_json.envelope("index show", ok=False, error=str(exc), code=exc.code)
    if requested is not None and isinstance(data.get("filter"), dict):
        data["filter"]["limit_clamped_from"] = requested
    return _cli_json.envelope("index show", data)


@mcp.tool(
    name="zotero_index_search",
    description=(
        "Find terms in the source indexes of many items at once, most facts first. "
        "terms: one or more terms (any term matches). "
        "items: comma-separated item or PDF attachment keys to search, in place of "
        "every item carrying tag. "
        "tag: default 'status/indexed'. Ignored when items is given. "
        "fields: 'lead' (default, a few short fields per entry) or 'full'. "
        "expand: also match vocabulary variants and symbols. "
        "regex: treat each term as one regular expression. "
        "limit: entries per kind per item, default 10, hard capped at 80 -- 0 or over "
        "80 becomes 80, and the result then carries limit_clamped_from with what was "
        "asked for. "
        "max_items: items to return, default 10, most facts first (0 means no cap). "
        "Returns the same JSON envelope as `zotero-cli --json index search`. "
        "Example: zotero_index_search(terms=['leakage'], items='3CKPN9EK')."
    ),
)
@with_zotero_api_lock
def index_search(
    terms: list[str] | str,
    items: str | None = None,
    tag: str = "status/indexed",
    fields: str = "lead",
    expand: bool = False,
    regex: bool = False,
    limit: int | str | None = 10,
    max_items: int | str | None = 10,
    *,
    ctx: Context,
) -> dict:
    """The MCP path to `index search`: the same backend, a hard cap on `limit`."""
    terms = _helpers._normalize_str_list_input(terms, "terms")
    clamped, requested = _clamp_limit(limit, 10)
    try:
        data = index_query.search(
            terms, items=items, tag=tag, fields=fields or "lead", expand=expand,
            limit=clamped, max_items=_as_count(max_items, 10), regex=regex, ctx=ctx,
        )
    except _cli_json.CliError as exc:
        return _cli_json.envelope("index search", ok=False, error=str(exc), code=exc.code)
    if requested is not None:
        data["limit_clamped_from"] = requested
    return _cli_json.envelope("index search", data)
