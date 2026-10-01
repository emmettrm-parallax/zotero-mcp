"""Source cards: one short note per Zotero item that names what it is about.

A card (schema ``source-card/v1``, see ``validate_card``) is a small substitute for the
larger source index. It gives a finder agent a handful of topics, synonyms and named
quantities to search before it ever opens a PDF. One item holds at most one card, stored
as ONE child note with an ``<h1>Source card</h1>`` heading, a ``<p>`` summary and one
``<pre>`` block: line 1 the literal ``source-card/v1``, line 2 the compact JSON.

``validate_card``, ``render_card_note`` and ``parse_card_note`` are pure. ``push_card``
and ``list_cards`` reach the library through ``zotero_mcp.library`` and
``zotero_mcp.tools.annotations``, imported inside the function body, so importing this
module touches neither the network nor a PDF. A card is text, never a page image, so this
module never imports pymupdf.

Errors are ``CliError`` (``zotero_mcp.cli_json``) with the code ``invalid_card``, except a
bad ``--grep`` pattern, which carries ``bad_grep`` or ``bad_regex`` the way ``index_grep``
reports them.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from zotero_mcp.cli_json import CliError
from zotero_mcp.text_match import BadRegexError, compile_term, normalize_text

__all__ = [
    "SCHEMA", "TOP_LEVEL_KEYS", "validate_card", "render_card_note", "parse_card_note",
    "push_card", "list_cards",
]

SCHEMA = "source-card/v1"
KINDS = ("paper", "book", "report", "web")
TOP_LEVEL_KEYS = (
    "schema", "item_key", "title_short", "kind", "topics", "synonyms", "quantities",
    "scope", "not_about", "pages", "tags",
)
#: Each key here holds a list of non-empty strings, bounded ``low..high`` items
#: (``high`` of ``None`` means no upper bound). ``tags`` carries no stated floor or
#: ceiling in the schema, only the per-item length rule every list shares.
_LIST_LIMITS: dict[str, tuple[int, int | None]] = {
    "topics": (5, 8),
    "synonyms": (10, 20),
    "quantities": (0, 10),
    "tags": (0, None),
}
_MAX_ITEM_CHARS = 60
_MAX_TITLE_CHARS = 60
_MAX_SENTENCE_CHARS = 200
_ITEM_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")

# The string fields a search term can match, then the list fields (one field per item).
_STRING_FIELDS = ("item_key", "title_short", "kind", "scope", "not_about")
_LIST_FIELDS = ("topics", "synonyms", "quantities", "tags")

#: A literal term shorter than this is a whole-word, case-sensitive match -- the same
#: rule ``index_grep`` uses, so "Cd" does not hit every "cd" in a synonym list.
_SHORT_TERM = 3

_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_CARD_H1 = "Source card"
_BLOCK_LINE_RE = re.compile(r"^\s*" + re.escape(SCHEMA) + r"\s*$")
_NOTE_KEY_RE = re.compile(r"Note key:\s*([A-Z0-9]{8})")

#: A generous cap on the keys ``list_cards`` asks the backend for in one call. The
#: library this round holds two dozen items. This leaves room to grow without a second
#: call, which is the point of asking for every top-level item at once.
_LIBRARY_LIMIT = 10_000


def _invalid(problems: list[str]) -> CliError:
    head = "; ".join(problems[:5])
    more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
    return CliError(f"invalid card: {len(problems)} problem(s): {head}{more}", code="invalid_card")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _check_text(problems: list[str], key: str, value: Any, *, max_chars: int) -> None:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{key} must be a non-empty string")
    elif len(value) > max_chars:
        problems.append(f"{key} is {len(value)} characters, more than {max_chars}")


def _check_list(problems: list[str], key: str, value: Any, *, low: int, high: int | None) -> None:
    if not isinstance(value, list):
        problems.append(f"{key} must be an array")
        return
    if len(value) < low or (high is not None and len(value) > high):
        bound = f"{low}-{high}" if high is not None else f"at least {low}"
        problems.append(f"{key} must hold {bound} item(s), got {len(value)}")
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            problems.append(f"{key}[{i}] must be a non-empty string")
        elif len(item) > _MAX_ITEM_CHARS:
            problems.append(f"{key}[{i}] is {len(item)} characters, more than {_MAX_ITEM_CHARS}")


def validate_card(obj: Any) -> list[str]:
    """Human-readable problems with a card. An empty list means valid.

    Rules: every top-level key of ``TOP_LEVEL_KEYS`` is present and no other key is.
    ``schema`` is ``source-card/v1``. ``item_key`` matches ``[A-Z0-9]{8}``. ``kind`` is
    one of ``KINDS``. ``title_short`` is at most 60 characters. ``scope`` and
    ``not_about`` are at most 200 characters. ``topics`` holds 5 to 8 items,
    ``synonyms`` 10 to 20, ``quantities`` 0 to 10, each a non-empty string of at most 60
    characters. ``pages`` is a positive integer or null.
    """
    if not isinstance(obj, dict):
        return ["card must be a JSON object"]
    problems: list[str] = []

    unknown = sorted(key for key in obj if key not in TOP_LEVEL_KEYS)
    if unknown:
        problems.append("unknown key(s): " + ", ".join(unknown))
    missing = [key for key in TOP_LEVEL_KEYS if key not in obj]
    if missing:
        problems.append("missing top-level key(s): " + ", ".join(missing))

    if "schema" in obj and obj["schema"] != SCHEMA:
        problems.append(f"schema is {obj['schema']!r}, expected {SCHEMA!r}")
    if "item_key" in obj:
        value = obj["item_key"]
        if not isinstance(value, str) or not _ITEM_KEY_RE.match(value):
            problems.append(f"item_key must match [A-Z0-9]{{8}}, got {value!r}")
    if "kind" in obj and obj["kind"] not in KINDS:
        problems.append(f"kind must be one of {KINDS}, got {obj['kind']!r}")
    if "title_short" in obj:
        _check_text(problems, "title_short", obj["title_short"], max_chars=_MAX_TITLE_CHARS)
    if "scope" in obj:
        _check_text(problems, "scope", obj["scope"], max_chars=_MAX_SENTENCE_CHARS)
    if "not_about" in obj:
        _check_text(problems, "not_about", obj["not_about"], max_chars=_MAX_SENTENCE_CHARS)
    for key, (low, high) in _LIST_LIMITS.items():
        if key in obj:
            _check_list(problems, key, obj[key], low=low, high=high)
    if "pages" in obj:
        pages = obj["pages"]
        if pages is not None and (isinstance(pages, bool) or not isinstance(pages, int) or pages < 1):
            problems.append(f"pages must be a positive integer or null, got {pages!r}")
    return problems


# ---------------------------------------------------------------------------
# Rendering and parsing
# ---------------------------------------------------------------------------

def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def render_card_note(card: dict) -> str:
    """The HTML of one card note: an ``<h1>``, a ``<p>`` summary, then the data block.

    ``annotations.create_note`` keeps the text it is given only when that text already
    holds a ``<p>`` or a ``<div>``, so the summary line is not decoration. Without it
    Zotero would rewrite the note on save and the ``<pre>`` block would be lost.
    """
    summary = (
        f"Machine-readable source card ({_esc(SCHEMA)}) for {_esc(card.get('item_key'))}: "
        f"{_esc(card.get('title_short'))}. The JSON in the block below is the source of "
        "truth. Do not edit this note by hand."
    )
    pre = _esc(f"{SCHEMA}\n{_dumps(card)}")
    return f"<h1>{_esc(_CARD_H1)}</h1><p>{summary}</p><pre>{pre}</pre>"


class _PreCollector(HTMLParser):
    """Text of the first top-level ``<pre>`` block. A tag inside it is dropped, an entity
    decoded. A card note holds at most one such block, so the first is the only one."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: str | None = None
        self._buf: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "pre" and self.text is None:
            if self._depth == 0:
                self._buf = []
            self._depth += 1

    def handle_endtag(self, tag):
        if tag == "pre" and self._depth:
            self._depth -= 1
            if self._depth == 0 and self._buf is not None:
                self.text = "".join(self._buf)
                self._buf = None

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)


def _pre_text(note_html: str) -> str | None:
    collector = _PreCollector()
    collector.feed(note_html or "")
    collector.close()
    return collector.text


def parse_card_note(note_html: str) -> dict:
    """The card JSON out of one card note's ``<pre>`` block.

    Raises ``CliError`` (``invalid_card``) when the note holds no ``<pre>`` block, the
    first line is not the literal ``source-card/v1``, or the rest does not parse as a
    JSON object.
    """
    text = _pre_text(note_html)
    if text is None:
        raise CliError("no source-card block found in the note", code="invalid_card")
    first, _, rest = text.strip().partition("\n")
    if not _BLOCK_LINE_RE.match(first.rstrip("\r")):
        raise CliError(f"the first line of the block must be {SCHEMA!r}, got {first!r}", code="invalid_card")
    try:
        card = json.loads(rest.strip())
    except ValueError as exc:
        raise CliError(f"the card JSON does not parse: {exc}", code="invalid_card") from exc
    if not isinstance(card, dict):
        raise CliError("the card JSON is not an object", code="invalid_card")
    return card


def _first_h1_text(note_html: str) -> str:
    match = _H1_RE.search(note_html or "")
    if not match:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).split())


def _is_card_note(note_html: str) -> bool:
    return _first_h1_text(note_html) == _CARD_H1


# ---------------------------------------------------------------------------
# Zotero access: push, list
# ---------------------------------------------------------------------------

@dataclass
class _CardNote:
    key: str
    added: str
    html: str


def _pick_card_notes(children: dict[str, list[dict]], parent_key: str) -> list[_CardNote]:
    """The card notes of one parent in a ``get_children`` result, oldest first.

    A trashed note is skipped. A parent absent from ``children`` raises ``error`` (no
    such item).
    """
    if parent_key not in children:
        raise CliError(f"cannot list the notes of item {parent_key}: no such item?", code="error")
    found = []
    for note in children[parent_key]:
        data = note.get("data", {})
        if data.get("deleted"):
            continue
        if _is_card_note(data.get("note", "")):
            found.append(_CardNote(
                key=note.get("key") or data.get("key", ""),
                added=data.get("dateAdded", ""),
                html=data.get("note", ""),
            ))
    found.sort(key=lambda n: (n.added, n.key))
    return found


def push_card(parent_key: str, card: dict, *, ctx) -> dict:
    """Write *card* as the one card note of ``parent_key``.

    Updates the oldest existing card note in place, creates one when there is none, and
    trashes any surplus card note (more than one is a mistake a push should clean up, not
    compound). A trashed note is never picked as the one to update.

    Raises ``CliError`` (``invalid_card``) when ``card`` fails ``validate_card``, or when
    ``card["item_key"]`` is not ``parent_key``.

    Returns ``{item_key, note_key, created, updated, trashed}``. ``created`` and
    ``updated`` are 0 or 1. ``trashed`` counts surplus notes removed.
    """
    problems = validate_card(card)
    if problems:
        raise _invalid(problems)
    if card["item_key"] != parent_key:
        raise CliError(
            f"card item_key {card['item_key']!r} does not match the parent {parent_key!r}",
            code="invalid_card",
        )
    note_html = render_card_note(card)

    from zotero_mcp import library as _library
    from zotero_mcp.tools import annotations

    children = _library.get_library_backend().get_children([parent_key], item_type="note")
    existing = _pick_card_notes(children, parent_key)

    created = updated = trashed = 0
    if existing:
        oldest, *surplus = existing
        result = annotations.update_note(item_key=oldest.key, note_text=note_html, ctx=ctx)
        if "Successfully updated" not in result:
            raise CliError(f"updating note {oldest.key} failed: {result}", code="error")
        note_key = oldest.key
        updated = 1
        for extra in surplus:
            result = annotations.delete_note(item_key=extra.key, ctx=ctx)
            if "Successfully trashed" not in result:
                raise CliError(f"trashing note {extra.key} failed: {result}", code="error")
            trashed += 1
    else:
        result = annotations.create_note(
            item_key=parent_key, note_title="", note_text=note_html, tags=None, ctx=ctx,
        )
        match = _NOTE_KEY_RE.search(result)
        if not match:
            raise CliError(f"creating the card note failed: {result}", code="error")
        note_key = match.group(1)
        created = 1

    return {
        "item_key": parent_key, "note_key": note_key,
        "created": created, "updated": updated, "trashed": trashed,
    }


def _compile(term: str, *, regex: bool):
    try:
        if regex:
            return compile_term(term, regex=True)
        if len(normalize_text(term)) < _SHORT_TERM:
            return compile_term(term, word=True, ignore_case=False)
        return compile_term(term)
    except BadRegexError as exc:
        raise CliError(str(exc), code="bad_regex") from exc


def _card_fields(card: dict) -> list[str]:
    """Every field of *card* a search term can match: the plain strings, then each list
    item. ``schema`` and ``pages`` carry no searchable text."""
    fields = [card[key] for key in _STRING_FIELDS if isinstance(card.get(key), str) and card[key]]
    for key in _LIST_FIELDS:
        value = card.get(key)
        if isinstance(value, list):
            fields.extend(item for item in value if isinstance(item, str) and item)
    return fields


def list_cards(*, terms: list[str] | None = None, regex: bool = False, ctx) -> dict:
    """Every item's card, matched against *terms* over every field.

    One ``get_children`` call covers every top-level item, the way
    ``source_index.show_indexes`` covers the ones it is given. Without *terms* every
    card is kept, in the order ``push_card`` never promises, so the ranking below is what
    makes the list stable: a card's rank is (the count of distinct terms it matches, most
    first), then ``title_short``.

    Returns ``{cards, count, terms}``. Each card in ``cards`` is the full card plus
    ``note_key`` and ``hit`` (the matched terms, in term order). Raises ``CliError``
    (``bad_regex``) for a pattern that does not compile, before any backend call.
    """
    terms = list(terms or [])
    patterns = [_compile(term, regex=regex) for term in terms]

    from zotero_mcp import library as _library

    backend = _library.get_library_backend()
    top_level = backend.search_items("", item_type="-attachment", limit=_LIBRARY_LIMIT)
    keys: list[str] = []
    seen: set[str] = set()
    for item in top_level:
        data = item.get("data") or {}
        key = item.get("key") or data.get("key", "")
        # A standalone note or attachment that happens to pass -attachment carries no
        # parentItem. A child of some other item does, and is not a top-level item.
        if key and key not in seen and not data.get("parentItem"):
            seen.add(key)
            keys.append(key)

    children = backend.get_children(keys, item_type="note") if keys else {}

    rows = []
    for key in keys:
        notes = _pick_card_notes(children, key) if key in children else []
        if not notes:
            continue
        try:
            card = parse_card_note(notes[0].html)
        except CliError:
            continue  # a note shaped like a card but not one this version can read
        fields = _card_fields(card)
        hit = [term for term, pattern in zip(terms, patterns)
              if any(pattern.search(field) for field in fields)]
        if terms and not hit:
            continue
        rows.append({**card, "note_key": notes[0].key, "hit": hit})

    rows.sort(key=lambda row: (-len(row["hit"]), str(row.get("title_short") or "")))
    return {"cards": rows, "count": len(rows), "terms": terms}
