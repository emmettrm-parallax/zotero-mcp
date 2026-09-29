"""Source index notes: a fact index of one Zotero source, stored as child notes.

An index (schema ``source-index/v1``, see ``validate_index``) is stored on the
parent item as one or more child notes. Each note holds

* ``<h1>Source index</h1>`` (later parts: ``Source index (part k of n)``),
* a ``<p>`` line and escaped HTML tables, a view for a person reading the note,
* ONE ``<pre>`` block: line 1 ``source-index/v1 part k/n build <build_id>``,
  line 2 the compact JSON of that part.

The JSON is the source of truth and the tables are a view, so the parser reads
only the ``<pre>`` blocks. Part 1 carries every key of the index plus as many
facts as fit under ``max_chars``. Later parts carry a ``facts`` slice and
nothing else. A fact is never split across parts.

The JSON is written with ``ensure_ascii`` so the block is plain ASCII: a note
editor that normalises exotic whitespace or Unicode cannot alter it. The
literal ``<p>`` matters too: ``annotations.create_note`` stores the text as
given only when it already contains ``<p>`` or ``<div>``.

Errors are ``SourceIndexError`` (a ``cli_json.CliError``) with the machine
codes ``no_index``, ``index_exists`` and ``invalid_index``.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from zotero_mcp.cli_json import CliError

SCHEMA = "source-index/v1"
TOP_LEVEL_KEYS = (
    "schema", "header", "vocabulary", "sections", "facts", "tables_figures", "equations", "gaps",
)
#: Zotero forum reports put the note sync limit at 200k-250k characters.
DEFAULT_MAX_CHARS = 190_000

_ARRAYS = ("vocabulary", "sections", "facts", "tables_figures", "equations", "gaps")
_ID_ARRAYS = ("sections", "facts", "tables_figures", "equations", "gaps")

# Only used while packing, so the part count is not known yet: a wide stand-in
# keeps the size estimate an upper bound of the real part.
_N_PLACEHOLDER = 99_999

_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_H1_INDEX_RE = re.compile(r"^Source index(?: \(part (\d+) of (\d+)\))?$")
_BLOCK_LINE_RE = re.compile(r"^\s*source-index/v1\s+part\s+(\d+)/(\d+)\s+build\s+(.+?)\s*$")
_NOTE_KEY_RE = re.compile(r"Note key:\s*([A-Z0-9]{8})")


class SourceIndexError(CliError):
    """A failure with a stable code; ``problems`` lists every detail found."""

    def __init__(self, message: str, code: str = "error", problems: list[str] | None = None):
        super().__init__(message, code=code)
        self.problems = list(problems or [])


def _invalid(problems: list[str], what: str = "invalid index") -> SourceIndexError:
    head = "; ".join(problems[:5])
    more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
    return SourceIndexError(f"{what}: {len(problems)} problem(s): {head}{more}", "invalid_index", problems)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_index(obj) -> list[str]:
    """Human-readable problems with an index. An empty list means valid.

    Rules: all eight top-level keys exist; each id is unique in its array;
    every page is in 1..``header.page_count``; every ``section_id`` and every
    ``fact_ids`` entry points to an existing entry.
    """
    if not isinstance(obj, dict):
        return ["index must be a JSON object"]
    problems: list[str] = []

    missing = [key for key in TOP_LEVEL_KEYS if key not in obj]
    if missing:
        problems.append("missing top-level key(s): " + ", ".join(missing))
    if "schema" in obj and obj["schema"] != SCHEMA:
        problems.append(f"schema is {obj['schema']!r}, expected {SCHEMA!r}")

    page_count = None
    if "header" in obj:
        header = obj["header"]
        if not isinstance(header, dict):
            problems.append("header must be an object")
        elif not _is_int(header.get("page_count")) or header["page_count"] < 1:
            problems.append("header.page_count must be a positive integer")
        else:
            page_count = header["page_count"]

    entries: dict[str, list[dict]] = {}
    for name in _ARRAYS:
        if name not in obj:
            continue
        value = obj[name]
        if not isinstance(value, list):
            problems.append(f"{name} must be an array")
            continue
        good = []
        for i, entry in enumerate(value):
            if isinstance(entry, dict):
                good.append(entry)
            else:
                problems.append(f"{name}[#{i}] must be an object")
        entries[name] = good

    def label(name: str, i: int, entry: dict) -> str:
        return f"{name}[{entry['id']}]" if isinstance(entry.get("id"), str) and entry["id"] else f"{name}[#{i}]"

    ids: dict[str, set[str]] = {}
    for name in _ID_ARRAYS:
        seen: set[str] = set()
        for i, entry in enumerate(entries.get(name, [])):
            entry_id = entry.get("id")
            if not isinstance(entry_id, str) or not entry_id:
                problems.append(f"{name}[#{i}] has no id")
            elif entry_id in seen:
                problems.append(f"{name}: duplicate id {entry_id}")
            else:
                seen.add(entry_id)
        ids[name] = seen

    def check_page(where: str, field: str, value: Any) -> None:
        if not _is_int(value):
            problems.append(f"{where}: {field} must be an integer, got {value!r}")
        elif page_count is not None and not 1 <= value <= page_count:
            problems.append(f"{where}: {field} {value} is outside 1..{page_count}")

    for name in ("facts", "tables_figures", "equations", "gaps"):
        for i, entry in enumerate(entries.get(name, [])):
            check_page(label(name, i, entry), "page", entry.get("page"))
    for i, entry in enumerate(entries.get("sections", [])):
        for field in ("start_page", "end_page"):
            check_page(label("sections", i, entry), field, entry.get(field))
    for i, entry in enumerate(entries.get("vocabulary", [])):
        pages = entry.get("pages", [])
        if not isinstance(pages, list):
            problems.append(f"vocabulary[#{i}]: pages must be an array")
            continue
        for page in pages:
            check_page(f"vocabulary[{entry.get('term', f'#{i}')}]", "page", page)

    # A gap may sit outside every section, so its section_id may be null; the
    # other arrays must name a section.
    for name in ("facts", "tables_figures", "equations", "gaps"):
        for i, entry in enumerate(entries.get(name, [])):
            section_id = entry.get("section_id")
            if section_id is None and name == "gaps":
                continue
            if section_id not in ids["sections"]:
                problems.append(f"{label(name, i, entry)}: section_id {section_id!r} is not a section id")
    for name in ("tables_figures", "equations"):
        for i, entry in enumerate(entries.get(name, [])):
            fact_ids = entry.get("fact_ids", [])
            if not isinstance(fact_ids, list):
                problems.append(f"{label(name, i, entry)}: fact_ids must be an array")
                continue
            for fact_id in fact_ids:
                if fact_id not in ids["facts"]:
                    problems.append(f"{label(name, i, entry)}: fact_id {fact_id!r} is not a fact id")
    return problems


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def _cell(value: Any) -> str:
    """Plain text for one table cell (escaped by the caller)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list) and all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in value):
        return ", ".join(str(v) for v in value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _pages_cell(value: Any) -> str:
    """``[[665, 689]]`` as ``665-689``; anything else as a plain cell."""
    if isinstance(value, list) and value and all(
        isinstance(p, list) and len(p) == 2 and all(_is_int(x) for x in p) for p in value
    ):
        return ", ".join(f"{a}-{b}" for a, b in value)
    return _cell(value)


def _row(cells: list[Any], tag: str = "td") -> str:
    return "<tr>" + "".join(f"<{tag}>{_esc(_cell(c))}</{tag}>" for c in cells) + "</tr>"


def _table(headers: list[str], rows: list[str]) -> str:
    return "<table><tbody>" + _row(headers, "th") + "".join(rows) + "</tbody></table>"


def _section_block(title: str, headers: list[str], rows: list[str]) -> str:
    body = _table(headers, rows) if rows else "<p>None.</p>"
    return f"<h2>{_esc(title)}</h2>{body}"


def _page_cell(entry: dict) -> str:
    printed = entry.get("printed_page")
    return f"{entry.get('page')}" if printed is None else f"{entry.get('page')} [{printed}]"


def _fact_row(fact: dict) -> str:
    value = fact.get("value") if fact.get("value") is not None else fact.get("value_num")
    value_cell = " ".join(_cell(x) for x in (value, fact.get("unit")) if x not in (None, ""))
    quantity = " ".join(_cell(x) for x in (fact.get("quantity"), f"({fact['symbol']})" if fact.get("symbol") else None)
                        if x)
    return _row([
        fact.get("id"), fact.get("kind"), _page_cell(fact), fact.get("section_id"), fact.get("ref"),
        fact.get("statement"), quantity, value_cell, fact.get("condition"), fact.get("validity"),
        fact.get("confidence"),
    ])


_FACT_HEADERS = ["id", "kind", "page [printed]", "section", "ref", "statement", "quantity", "value",
                 "condition", "validity", "conf"]


def _variables_cell(variables: Any) -> str:
    if not isinstance(variables, list):
        return _cell(variables)
    parts = []
    for var in variables:
        if not isinstance(var, dict):
            parts.append(_cell(var))
            continue
        text = f"{_cell(var.get('symbol'))} = {_cell(var.get('meaning'))}"
        parts.append(text + (f" [{_cell(var['unit'])}]" if var.get("unit") else ""))
    return "; ".join(parts)


def _part_one_tables_before_facts(index: dict) -> str:
    header = index.get("header") if isinstance(index.get("header"), dict) else {}
    header_rows = [_row([key, _pages_cell(value) if key == "scope_pages" else value]) for key, value in header.items()]
    vocab_rows = [
        _row([v.get("term"), v.get("kind"), v.get("symbol"), v.get("meaning"), v.get("variants"), v.get("pages")])
        for v in index.get("vocabulary") or [] if isinstance(v, dict)
    ]
    section_rows = [
        _row([s.get("id"), s.get("title"), s.get("level"), f"{s.get('start_page')}-{s.get('end_page')}", s.get("path")])
        for s in index.get("sections") or [] if isinstance(s, dict)
    ]
    return (
        _section_block("Header", ["field", "value"], header_rows)
        + _section_block("Vocabulary", ["term", "kind", "symbol", "meaning", "variants", "pages"], vocab_rows)
        + _section_block("Sections", ["id", "title", "level", "pages", "path"], section_rows)
    )


def _part_one_tables_after_facts(index: dict) -> str:
    tf_rows = [
        _row([t.get("id"), t.get("label"), t.get("kind"), t.get("caption"), t.get("page"), t.get("section_id"),
              t.get("extracted"), t.get("fact_ids")])
        for t in index.get("tables_figures") or [] if isinstance(t, dict)
    ]
    eq_rows = [
        _row([e.get("id"), e.get("label"), e.get("page"), e.get("section_id"), e.get("latex"),
              _variables_cell(e.get("variables")), e.get("validity"), e.get("fact_ids")])
        for e in index.get("equations") or [] if isinstance(e, dict)
    ]
    gap_rows = [
        _row([g.get("id"), g.get("page"), g.get("section_id"), g.get("kind"), g.get("note")])
        for g in index.get("gaps") or [] if isinstance(g, dict)
    ]
    return (
        _section_block("Tables and figures",
                       ["id", "label", "kind", "caption", "page", "section", "extracted", "facts"], tf_rows)
        + _section_block("Equations",
                         ["id", "label", "page", "section", "latex", "variables", "validity", "facts"], eq_rows)
        + _section_block("Gaps", ["id", "page", "section", "kind", "note"], gap_rows)
    )


def _render_part(k: int, n: int, build_id: str, index: dict, facts: list[dict], rows: list[str]) -> str:
    """One note. ``facts`` and ``rows`` are the slice for this part."""
    title = "Source index" if k == 1 else f"Source index (part {k} of {n})"
    intro = (
        f"<p>Machine-readable source index ({SCHEMA}), part {k} of {n}, build {_esc(build_id)}. "
        "The JSON in the block at the end of this note is the source of truth and the tables are a view. "
        "Do not edit this note by hand.</p>"
    )
    # The facts table is always rendered (even with no rows) so that the size
    # of a part is its fixed part plus a constant amount per fact.
    facts_table = "<h2>Facts</h2>" + _table(_FACT_HEADERS, rows)
    if k == 1:
        payload = dict(index)
        payload["facts"] = facts
        body = (
            _part_one_tables_before_facts(index) + facts_table + _part_one_tables_after_facts(index)
        )
    else:
        payload = {"facts": facts}
        body = facts_table
    block = f"{SCHEMA} part {k}/{n} build {build_id}\n{_dumps(payload)}"
    return f"<h1>{_esc(title)}</h1>{intro}{body}<pre>{_esc(block)}</pre>"


def render_index_notes(index, max_chars: int = 190_000) -> list[str]:
    """Note HTML for an index, one string per part, each at most ``max_chars``.

    Raises ValueError when the index cannot be laid out: no ``header.build_id``,
    or part 1 without facts (or one fact) alone is over ``max_chars``.
    """
    if not isinstance(index, dict) or not isinstance(index.get("header"), dict):
        raise ValueError("index must be an object with a header")
    build_id = str(index["header"].get("build_id") or "").strip()
    if not build_id or "\n" in build_id or "\r" in build_id:
        raise ValueError("header.build_id must be a non-empty single-line string")
    facts = [f for f in (index.get("facts") or [])]

    rows = [_fact_row(f) if isinstance(f, dict) else _row([f]) for f in facts]
    costs = [len(rows[i]) + len(_esc(_dumps(f))) + 1 for i, f in enumerate(facts)]  # +1: the JSON comma

    def fixed(first: bool) -> int:
        # A wide part number and count make this an upper bound for every part.
        k = 1 if first else _N_PLACEHOLDER
        return len(_render_part(k, _N_PLACEHOLDER, build_id, index, [], []))

    room_first, room_next = max_chars - fixed(True), max_chars - fixed(False)
    if room_first < 0:
        raise ValueError(
            f"header, vocabulary, sections, tables_figures, equations and gaps alone need {fixed(True)} "
            f"characters, more than max_chars={max_chars}"
        )
    slices: list[list[int]] = [[]]
    room = room_first
    for i, cost in enumerate(costs):
        if cost <= room:
            slices[-1].append(i)
            room -= cost
        elif cost > room_next:
            raise ValueError(f"fact {i} ({facts[i].get('id') if isinstance(facts[i], dict) else i}) needs {cost} "
                             f"characters, more than one part of max_chars={max_chars} holds")
        else:
            slices.append([i])
            room = room_next - cost

    n = len(slices)
    notes = [
        _render_part(k, n, build_id, index, [facts[i] for i in ids], [rows[i] for i in ids])
        for k, ids in enumerate(slices, start=1)
    ]
    # The estimate uses a wider part number and count than the real ones, so this
    # cannot trip; it guards the packing arithmetic against a future edit.
    for k, note in enumerate(notes, start=1):
        if len(note) > max_chars:
            raise ValueError(f"part {k} is {len(note)} characters, over max_chars={max_chars}")
    return notes


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class _PreCollector(HTMLParser):
    """Text of every top-level ``<pre>``: tags inside (``<code>``) dropped, entities decoded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buf: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "pre":
            if self._depth == 0:
                self._buf = []
            self._depth += 1
        elif tag == "br" and self._buf is not None:
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag == "pre" and self._depth:
            self._depth -= 1
            if self._depth == 0 and self._buf is not None:
                self.blocks.append("".join(self._buf))
                self._buf = None

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def close(self):
        super().close()
        if self._buf is not None:  # a truncated note: keep what there is, the JSON check will reject it
            self.blocks.append("".join(self._buf))
            self._buf = None


def _pre_blocks(note_html: str) -> list[str]:
    collector = _PreCollector()
    collector.feed(note_html)
    collector.close()
    return collector.blocks


def parse_index_notes(htmls) -> dict:
    """Rebuild the index from the HTML of its notes, in any order.

    Reads only the ``<pre>`` blocks (a ``<pre><code>`` block and entity
    encoding are fine). Raises SourceIndexError(``invalid_index``) for no
    block, a missing or duplicate part, mixed part counts or build ids, bad
    JSON, or a later part that holds anything but facts.
    """
    if isinstance(htmls, str):
        htmls = [htmls]
    problems: list[str] = []
    parts: dict[int, tuple[int, str, str]] = {}  # part -> (declared n, build id, json text)
    for note_html in htmls:
        for block in _pre_blocks(note_html or ""):
            first, _, rest = block.strip().partition("\n")
            match = _BLOCK_LINE_RE.match(first.rstrip("\r"))
            if not match:
                continue  # a <pre> that is not an index block
            k, n, build = int(match.group(1)), int(match.group(2)), match.group(3)
            if k in parts:
                problems.append(f"part {k} appears more than once")
            else:
                parts[k] = (n, build, rest.strip())
    if not parts:
        raise _invalid(["no source-index block found in the notes"], "cannot read index")

    declared = {n for n, _, _ in parts.values()}
    builds = {build for _, build, _ in parts.values()}
    if len(declared) > 1:
        problems.append("parts disagree on the part count: " + ", ".join(str(n) for n in sorted(declared)))
    if len(builds) > 1:
        problems.append("parts come from different builds: " + ", ".join(sorted(builds)))
    total = max(declared)
    missing = [k for k in range(1, total + 1) if k not in parts]
    if missing:
        problems.append(f"missing part(s) {', '.join(str(k) for k in missing)} of {total}")
    extra = sorted(k for k in parts if k < 1 or k > total)
    if extra:
        problems.append(f"part number(s) {', '.join(str(k) for k in extra)} outside 1..{total}")
    if problems:
        raise _invalid(problems, "cannot read index")

    decoded: dict[int, dict] = {}
    for k, (_, _, text) in parts.items():
        try:
            value = json.loads(text)
        except ValueError as exc:
            problems.append(f"part {k}: the JSON does not parse ({exc})")
            continue
        if not isinstance(value, dict):
            problems.append(f"part {k}: the JSON is not an object")
        elif k > 1 and set(value) != {"facts"}:
            problems.append(f"part {k}: a later part may hold only facts, found {sorted(value)}")
        elif not isinstance(value.get("facts", []), list):
            problems.append(f"part {k}: facts is not an array")
        else:
            decoded[k] = value
    if problems:
        raise _invalid(problems, "cannot read index")

    index = decoded[1]
    header = index.get("header")
    if isinstance(header, dict) and header.get("build_id") is not None:
        (build,) = builds
        if str(header["build_id"]).strip() != build:
            raise _invalid([f"header.build_id {header['build_id']!r} does not match the block line build {build!r}"],
                           "cannot read index")
    facts = list(index.get("facts", []))
    for k in range(2, total + 1):
        facts.extend(decoded[k]["facts"])
    index["facts"] = facts
    return index


# ---------------------------------------------------------------------------
# Zotero access: find, push, show
# ---------------------------------------------------------------------------

@dataclass
class _IndexNote:
    key: str
    part: int
    added: str
    html: str


def _first_h1_text(note_html: str) -> str:
    match = _H1_RE.search(note_html or "")
    if not match:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).split())


def _find_index_notes(parent_key: str, ctx) -> list[_IndexNote]:
    """The index notes under a parent, in part order (an h1 of ``Source index`` opens each)."""
    from zotero_mcp import library as _library

    backend = _library.get_library_backend()
    children = backend.get_children([parent_key], item_type="note")
    if parent_key not in children:
        raise SourceIndexError(f"cannot list the notes of item {parent_key}: no such item?", "error")
    found = []
    for note in children[parent_key]:
        data = note.get("data", {})
        if data.get("deleted"):
            continue
        match = _H1_INDEX_RE.match(_first_h1_text(data.get("note", "")))
        if match:
            found.append(_IndexNote(
                key=note.get("key") or data.get("key", ""),
                part=int(match.group(1) or 1),
                added=data.get("dateAdded", ""),
                html=data.get("note", ""),
            ))
    found.sort(key=lambda n: (n.part, n.added, n.key))
    return found


def _counts(index: dict) -> dict[str, int]:
    return {name: len(index.get(name) or []) for name in _ARRAYS}


def _clean_tags(tags) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = tags.split(",")
    return [str(t).strip() for t in tags if str(t).strip()]


def _write_failed(what: str, result: str, done: str) -> SourceIndexError:
    return SourceIndexError(
        f"{what} failed: {result}. {done} Run the push again with --replace to finish.", "error"
    )


def push_index(parent_key, index, *, replace=False, tags=None, dry_run=False, ctx) -> dict:
    """Write an index as child notes of ``parent_key``.

    Existing index notes are an error unless ``replace``: then they are updated
    in place, new parts are created and surplus parts go to the trash. With
    ``dry_run`` everything is computed and nothing is written. ``tags`` are
    added to the parent item.

    Returns ``{item_key, note_keys, parts, chars, created, updated, trashed,
    dry_run, counts}``. ``created``, ``updated`` and ``trashed`` count notes.
    In a dry run they are the plan, and ``note_keys`` lists only the notes that
    exist now and would be kept.
    """
    problems = validate_index(index)
    if problems:
        raise _invalid(problems)
    try:
        htmls = render_index_notes(index, max_chars=DEFAULT_MAX_CHARS)
    except ValueError as exc:
        raise SourceIndexError(f"invalid index: {exc}", "invalid_index", [str(exc)]) from exc

    existing = _find_index_notes(parent_key, ctx)
    if existing and not replace:
        raise SourceIndexError(
            f"item {parent_key} already has a source index in {len(existing)} note(s) "
            f"({', '.join(n.key for n in existing)}); pass --replace to overwrite it",
            "index_exists",
        )

    kept = min(len(htmls), len(existing))
    plan = {"created": len(htmls) - kept, "updated": kept, "trashed": len(existing) - kept}
    note_keys = [n.key for n in existing[:kept]]
    tag_list = _clean_tags(tags)

    if not dry_run:
        from zotero_mcp.tools import annotations, write

        for note, note_html in zip(existing[:kept], htmls):
            result = annotations.update_note(item_key=note.key, note_text=note_html, ctx=ctx)
            if "Successfully updated" not in result:
                raise _write_failed(f"updating note {note.key}", result, "Earlier parts may already be rewritten.")
        for k in range(kept, len(htmls)):
            result = annotations.create_note(
                item_key=parent_key, note_title="", note_text=htmls[k], tags=None, ctx=ctx,
            )
            match = _NOTE_KEY_RE.search(result)
            if not match:
                raise _write_failed(f"creating part {k + 1}", result, "Earlier parts may already be written.")
            note_keys.append(match.group(1))
        for note in existing[kept:]:
            result = annotations.delete_note(item_key=note.key, ctx=ctx)
            if "Successfully trashed" not in result:
                raise _write_failed(f"trashing note {note.key}", result, "The new index is written.")
        if tag_list:
            result = write.batch_update_tags(item_keys=[parent_key], add_tags=tag_list, ctx=ctx)
            if result.startswith(("Error", "No items found")):
                raise SourceIndexError(
                    f"the index is written (notes {', '.join(note_keys)}) but tagging failed: {result}", "error"
                )

    return {
        "item_key": parent_key,
        "note_keys": note_keys,
        "parts": len(htmls),
        "chars": sum(len(h) for h in htmls),
        **plan,
        "dry_run": bool(dry_run),
        "counts": _counts(index),
    }


def _filter_section(index: dict, section: str) -> dict:
    """The header, the vocabulary and one section with everything anchored in it."""
    entries = [s for s in index.get("sections") or [] if isinstance(s, dict) and s.get("id") == section]
    if not entries:
        known = ", ".join(str(s.get("id")) for s in index.get("sections") or [] if isinstance(s, dict))
        raise SourceIndexError(f"no section {section!r} in the index (sections: {known})", "bad_section")
    filtered = dict(index)
    filtered["sections"] = entries
    for name in ("facts", "tables_figures", "equations", "gaps"):
        filtered[name] = [e for e in index.get(name) or [] if isinstance(e, dict) and e.get("section_id") == section]
    return filtered


def show_index(parent_key, *, section=None, ctx) -> dict:
    """Read the index of ``parent_key``: ``{item_key, note_keys, parts, index}``.

    ``section`` (for example ``S03``) filters the index to that section's facts,
    tables_figures, equations and gaps, plus the header, the vocabulary and that
    one section entry. Raises ``no_index`` when the item has none.
    """
    notes = _find_index_notes(parent_key, ctx)
    if not notes:
        raise SourceIndexError(f"item {parent_key} has no source index", "no_index")
    index = parse_index_notes([n.html for n in notes])
    if section is not None:
        index = _filter_section(index, section)
    return {
        "item_key": parent_key,
        "note_keys": [n.key for n in notes],
        "parts": len(notes),
        "index": index,
    }
